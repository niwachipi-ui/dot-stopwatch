"""Explicit, bounded Core Audio backend for bridge.relay.

Construction/import is inert. start() is the only native startup path. The
worker has no physical-input fallback, no files, network, model API or UI calls.
Runtime remains unverified: IOProc registration has blocked on this Mac.
"""
import argparse
import array
import base64
import ctypes as C
from contextlib import ExitStack
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from coreaudio_tap import (Native, Tap, OneBufferList, checked_pids, fourcc,
                           output_clock_uid)
from tap_watchdog import valid_owned_resource, cleanup_owned

MAX_INPUT_SECONDS = 60
MAX_PCM_BYTES = 16000 * MAX_INPUT_SECONDS * 2
REPLY_RATE = 48000
REPLY_FRAMES = 4800
MAX_LINE = ((MAX_PCM_BYTES + 2) // 3) * 4 + 4096
MAX_RESPONSE_LINE = 512000
MAX_TURN_METRICS_MS = 180000
REPLY_QUIET_SECONDS = 1.5
MAX_REPLY_SECONDS = 60
PROTOCOL_VERSION = 2


class BackendUnavailable(RuntimeError):
    pass


class ReplyPreroll:
    """Retain 200 ms before first audible speech without sending idle audio.

    The caller supplies validated mono float32 bytes and its level decision.
    Starting a new instance for each turn prevents preceding-turn audio reuse.
    """
    MAX_BYTES = int(REPLY_RATE * .2) * 4

    def __init__(self):
        self.started = False
        self.pending = bytearray()

    def feed(self, raw, audible):
        if self.started:
            return raw
        if not audible:
            self.pending.extend(raw)
            if len(self.pending) > self.MAX_BYTES:
                del self.pending[:-self.MAX_BYTES]
            return None
        self.started = True
        onset = bytes(self.pending) + raw
        self.pending.clear()
        return onset


class ReplyWindow:
    """Bounded reply segmentation, independent of native audio and wall clocks.

    Allow normal sentence pauses and longer answers. Audio is emitted in 100 ms
    packets; only the current packet and 200 ms onset buffer stay here.
    """
    def __init__(self, now, input_seconds):
        self.armed_at = now
        self.deadline = now + 20 + input_seconds
        self.preroll = ReplyPreroll()
        self.pending = bytearray()
        self.silence = self.total = 0
        self.first_reply_ms = None
        self.ended = False
        self.final_metrics = None
        self.input_not_before = now
        self.input_gate_closed = False
        self.post_input_audible_seen = True
        self.post_input_wait_until = None

    def hold_for_input(self, until):
        self.input_not_before = until
        self.input_gate_closed = True
        self.post_input_audible_seen = False
        self.post_input_wait_until = until + 20
        if self.first_reply_ms is None:
            self.deadline = until + 20
        else:
            self.deadline = max(self.deadline, until + MAX_REPLY_SECONDS + REPLY_QUIET_SECONDS)

    def _finish(self, reason, now):
        self.ended = True
        self.final_metrics = {
            "final_reason": reason,
            "first_reply_ms": self.first_reply_ms,
            "elapsed_ms": max(0, round((now - self.armed_at) * 1000)),
            "reply_duration_ms": round(self.total * 1000 / REPLY_RATE),
        }

    def poll(self, now):
        if (not self.ended and self.first_reply_ms is not None
                and not self.post_input_audible_seen and self.post_input_wait_until is not None
                and now >= self.post_input_wait_until):
            self._finish("silence", now)
            packet = bytes(self.pending)
            self.pending.clear()
            return [(packet, True)]
        if self.ended or now < self.deadline:
            return []
        self._finish("no_reply_timeout" if self.first_reply_ms is None else "reply_timeout", now)
        packet = bytes(self.pending)
        self.pending.clear()
        return [(packet, True)]

    def feed(self, raw, audible, now):
        if self.ended:
            return []
        if len(raw) % 4:
            raise ValueError("Reply frames must be float32 bytes")
        expired = self.poll(now)
        if expired:
            return expired
        if self.input_gate_closed and now >= self.input_not_before:
            self.input_gate_closed = False
            self.silence = 0
        if audible and not self.input_gate_closed and not self.post_input_audible_seen:
            self.post_input_audible_seen = True
            self.silence = 0
            self.deadline = max(self.deadline, now + MAX_REPLY_SECONDS + REPLY_QUIET_SECONDS)
        raw = self.preroll.feed(raw, audible)
        if raw is None:
            return []
        if self.first_reply_ms is None:
            self.first_reply_ms = max(0, round((now - self.armed_at) * 1000))
            # Wall-clock bound survives missing callbacks; the sample limit is
            # authoritative when capture continues normally.
            self.deadline = max(now, self.input_not_before) + MAX_REPLY_SECONDS + REPLY_QUIET_SECONDS
        if not audible and not self.post_input_audible_seen:
            # Do not spend the reply's 60-second budget on a long pause while
            # the recorded question is still playing into Chrome.
            quiet_left = max(0, round(REPLY_QUIET_SECONDS * REPLY_RATE) - self.silence)
            raw = raw[:quiet_left * 4]
        raw = raw[:(MAX_REPLY_SECONDS * REPLY_RATE - self.total) * 4]
        self.silence = 0 if audible else self.silence + len(raw) // 4
        self.total += len(raw) // 4
        self.pending.extend(raw)
        if self.total >= MAX_REPLY_SECONDS * REPLY_RATE:
            self._finish("reply_limit", now)
        elif self.post_input_audible_seen and self.silence >= round(REPLY_QUIET_SECONDS * REPLY_RATE):
            self._finish("silence", now)
        packets = []
        while len(self.pending) >= REPLY_FRAMES * 4:
            packet = bytes(self.pending[:REPLY_FRAMES * 4])
            del self.pending[:REPLY_FRAMES * 4]
            packets.append((packet, self.ended and not self.pending))
        if self.ended and (self.pending or not packets):
            packets.append((bytes(self.pending), True))
            self.pending.clear()
        return packets


class PlaybackReplyWindow:
    """Keep early browser replies until input playback ends, then pace delivery.

    The input-start notification follows the HAL worker's queued ACK. Its
    conservative fallback covers a lost notification without an unbounded wait.
    This controls relay delivery, not Chrome's own conversational VAD.
    """
    MAX_BUFFER_BYTES = MAX_REPLY_SECONDS * REPLY_RATE * 4
    DISPATCH_GRACE_SECONDS = 4.25
    OUTPUT_TAIL_SECONDS = .25
    MAX_CATCHUP_SECONDS = .2

    def __init__(self, now, input_seconds):
        if not math.isfinite(input_seconds) or not 0 < input_seconds <= MAX_INPUT_SECONDS:
            raise ValueError("Invalid input playback duration")
        self.input_seconds = input_seconds
        self.input_started = False
        self.input_until = now + input_seconds + self.DISPATCH_GRACE_SECONDS
        self.capture = ReplyWindow(now, input_seconds)
        self.capture.hold_for_input(self.input_until)
        self.packets = deque()
        self.buffered_bytes = 0
        self.next_emit_at = None

    @property
    def final_metrics(self):
        return self.capture.final_metrics

    def mark_input_started(self, now):
        if self.input_started:
            raise ValueError("Input playback already started")
        self.input_started = True
        self.input_until = now + self.input_seconds + self.OUTPUT_TAIL_SECONDS
        self.capture.hold_for_input(self.input_until)

    def _append(self, packets):
        for packet in packets:
            self.buffered_bytes += len(packet[0])
            if self.buffered_bytes > self.MAX_BUFFER_BYTES:
                raise ValueError("Deferred reply exceeded its bounded buffer")
            self.packets.append(packet)

    def _drain(self, now):
        if now < self.input_until or not self.packets:
            return []
        if self.next_emit_at is not None and now < self.next_emit_at:
            return []
        packet = self.packets.popleft()
        self.buffered_bytes -= len(packet[0])
        # Advance the audio timeline, not the time this poll happened to run.
        # Scheduling 100 ms from every late poll accumulates jitter into a
        # steadily slower stream. Catch up at most 200 ms after a long stall;
        # each call still returns only one bounded packet.
        scheduled = now if self.next_emit_at is None else max(
            self.next_emit_at, now - self.MAX_CATCHUP_SECONDS)
        self.next_emit_at = scheduled + len(packet[0]) / (REPLY_RATE * 4)
        return [packet]

    def poll(self, now):
        self._append(self.capture.poll(now))
        return self._drain(now)

    def feed(self, raw, audible, now):
        self._append(self.capture.feed(raw, audible, now))
        return self._drain(now)


def reply_metrics(entry):
    """Accept fixed numeric metadata only, never source text or audio."""
    value = entry.get("metrics")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
            "final_reason", "first_reply_ms", "elapsed_ms", "reply_duration_ms"}:
        raise ValueError("Invalid reply metrics")
    if value["final_reason"] not in {"silence", "reply_limit", "no_reply_timeout", "reply_timeout"}:
        raise ValueError("Invalid reply completion reason")
    for key in ("elapsed_ms", "reply_duration_ms"):
        if type(value[key]) is not int or not 0 <= value[key] <= MAX_TURN_METRICS_MS:
            raise ValueError("Invalid reply timing")
    if value["first_reply_ms"] is not None and (
            type(value["first_reply_ms"]) is not int or not 0 <= value["first_reply_ms"] <= MAX_TURN_METRICS_MS):
        raise ValueError("Invalid reply onset timing")
    return dict(value)


def validate_recording(recording):
    turn_id = getattr(recording, "turn_id", None)
    pcm = getattr(recording, "pcm", None)
    rate = getattr(recording, "sample_rate", None)
    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 160:
        raise ValueError("A bounded explicit turn ID is required")
    if (rate != 16000 or getattr(recording, "channels", 1) != 1
            or not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2):
        raise ValueError("Expected at most 60 seconds of mono 16 kHz PCM16LE bytes")
    return turn_id, pcm


def decode_reply(entry):
    if entry.get("rate") != REPLY_RATE or entry.get("channels") != 1:
        raise ValueError("Reply stream must be mono 48 kHz")
    encoded = entry.get("pcm_f32")
    if not isinstance(encoded, str) or len(encoded) > REPLY_FRAMES * 4 * 2:
        raise ValueError("Reply packet exceeds limits")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) % 4 or len(raw) > REPLY_FRAMES * 4:
        raise ValueError("Reply packet exceeds limits")
    values = array.array("f", raw)
    if sys.byteorder != "little":
        values.byteswap()
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Reply contains a non-finite sample")
    final = entry.get("final")
    if not isinstance(final, bool) or not values and not final:
        raise ValueError("Invalid reply completion packet")
    return values, final


class _Run:
    def __init__(self, process, submit, on_error):
        self.process, self.submit, self.on_error = process, submit, on_error
        self.ready, self.stop = threading.Event(), threading.Event()
        self.outgoing = queue.Queue(maxsize=1)
        self.threads, self.resources = [], []
        self.active_turn, self.error = None, None
        self.phase = "starting"
        self.armed_ack_supported = False
        self.armed_turn = None
        self.armed = threading.Event()
        self.max_input_seconds = 10
        self.input_started_supported = False
        self.input_started_sent = False


class CoreAudioRelayBackend:
    """Explicit sink with a bounded, independently killable native worker.

    submit_reply must be thread-safe and quick. False leaves a packet untouched;
    it is retried for at most two seconds, then the backend fails closed. A run
    has one active turn. cancel purges both directions and requires fresh start.
    """
    def __init__(self, reply_pids, *, python_executable=None, startup_timeout=15,
                 worker_command=None, cleanup_command=None):
        self.reply_pids = checked_pids(reply_pids)
        if not 1 <= startup_timeout <= 30:
            raise ValueError("Startup timeout must be 1 to 30 seconds")
        self.startup_timeout = float(startup_timeout)
        self.python_executable = str(python_executable or sys.executable)
        if worker_command is not None:
            self.worker_command = list(worker_command)
        elif getattr(sys, "frozen", False):
            self.worker_command = [sys.executable, "--internal-relay", "--worker"]
        else:
            self.worker_command = [self.python_executable, str(Path(__file__).resolve()), "--worker"]
        if cleanup_command is not None:
            if (not isinstance(cleanup_command, (tuple, list)) or not cleanup_command
                    or any(not isinstance(part, str) or not part for part in cleanup_command)):
                raise ValueError("Cleanup command must be a nonempty argument list")
            self.cleanup_command = list(cleanup_command)
        elif getattr(sys, "frozen", False):
            self.cleanup_command = [sys.executable, "--internal-relay-cleanup"]
        else:
            self.cleanup_command = [self.python_executable, str(Path(__file__).resolve()), "--cleanup"]
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._run = None
        self._latest_run = None
        self._last_error = None
        self._phase = "stopped"
        self._last_turn_metrics = None

    @property
    def ready(self):
        with self._lock:
            run = self._run
            return bool(run and run.ready.is_set() and not run.stop.is_set())

    @property
    def armed_ack_supported(self):
        with self._lock:
            return bool(self._run and self._run.armed_ack_supported)

    @property
    def max_input_seconds(self):
        with self._lock:
            return self._run.max_input_seconds if self._run else 10

    @property
    def input_started_supported(self):
        with self._lock:
            return bool(self._run and self._run.input_started_supported)

    def input_started(self, turn_id):
        """Notify the scoped worker only after HAL accepted playback."""
        with self._lock:
            run = self._run
            if (not run or run.stop.is_set() or not run.input_started_supported
                    or run.active_turn != turn_id or run.input_started_sent):
                raise BackendUnavailable("Input playback notification does not match active turn")
            try:
                run.outgoing.put_nowait(("input_started", turn_id, None))
                run.input_started_sent = True
            except queue.Full:
                raise BackendUnavailable("Input playback notification queue is occupied")

    @property
    def last_turn_metrics(self):
        with self._lock:
            return dict(self._last_turn_metrics) if self._last_turn_metrics else None

    def wait_armed(self, turn_id, timeout=2):
        """Wait only for this accepted turn's explicit v2 acknowledgement.

        Old frozen workers advertise no capability and return False immediately;
        callers must retain their old fallback rather than infer readiness.
        """
        if not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("Arming timeout must be positive and at most five seconds")
        with self._lock:
            run = self._run
            if not run or not run.armed_ack_supported or run.active_turn != turn_id:
                return False
        deadline = time.monotonic() + timeout
        while not run.stop.is_set():
            if run.armed.wait(min(.05, max(0, deadline - time.monotonic()))):
                with self._lock:
                    return self._run is run and not run.stop.is_set() and run.armed_turn == turn_id
            if time.monotonic() >= deadline:
                return False
        return False

    @property
    def stopped(self):
        """True after worker/readers/queues have completed teardown.

        Native resource cleanup failures remain available through last_error.
        Merely clearing ready does not establish this state.
        """
        with self._lock:
            return self._run is None

    @property
    def public_input_identity(self):
        """Copy of this worker's published microphone identity; no native reads."""
        with self._lock:
            if self._run is None:
                return None
            matches = [value for value in self._run.resources
                       if value.get("public") is True and value.get("name") == "Dot Watch Mic"]
            return dict(matches[0]) if len(matches) == 1 else None

    @property
    def last_error(self):
        with self._lock:
            return self._run.error if self._run else self._last_error

    @property
    def phase(self):
        with self._lock:
            return self._run.phase if self._run else self._phase

    def start(self, *, submit_reply, on_error=None):
        if not callable(submit_reply):
            raise ValueError("A reply callback is required")
        # Teardown holds this lifecycle lock through EOF/resource cleanup. A new
        # worker can never overlap an old worker or inherit its state/readers.
        with self._lifecycle, self._lock:
            if self._run is not None:
                raise BackendUnavailable("Backend is already started; cancel before restarting")
            command = [*self.worker_command, "--reply-pids", *map(str, self.reply_pids)]
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, bufsize=1)
            run = _Run(process, submit_reply, on_error)
            self._run = run
            self._latest_run = run
            self._last_error = None
            self._last_turn_metrics = None
            run.threads = [threading.Thread(target=self._read, args=(run,), daemon=True),
                           threading.Thread(target=self._read_steps, args=(run,), daemon=True),
                           threading.Thread(target=self._write, args=(run,), daemon=True)]
            for thread in run.threads:
                thread.start()
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if run.ready.wait(.05):
                if not run.stop.is_set():
                    return
                break
            if run.stop.is_set() or process.poll() is not None:
                break
        reason = run.error or f"Core Audio startup stopped at {run.phase}"
        self._cancel(run)
        raise BackendUnavailable(reason)

    def offer(self, recording):
        turn_id, pcm = validate_recording(recording)
        with self._lock:
            run = self._run
            if not run or not run.ready.is_set() or run.stop.is_set() or run.active_turn is not None:
                return False
            if len(pcm) > run.max_input_seconds * 16000 * 2:
                raise BackendUnavailable("Selected audio worker does not support this recording duration")
            # Identity exists before any child/reply callback can run.
            run.active_turn = turn_id
            run.armed_turn = None
            run.input_started_sent = False
            run.armed.clear()
            try:
                run.outgoing.put_nowait(("offer", turn_id, pcm))
            except queue.Full:
                run.active_turn = None
                return False
            return True

    def _write(self, run):
        while not run.stop.is_set():
            try:
                kind, turn_id, pcm = run.outgoing.get(timeout=.1)
            except queue.Empty:
                continue
            if run.stop.is_set():
                break
            try:
                message = ({"input_started": turn_id} if kind == "input_started" else
                           {"offer": turn_id, "pcm16": base64.b64encode(pcm).decode("ascii")})
                run.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                run.process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail(run, f"Dedicated audio worker write failed: {type(exc).__name__}")
                break
            finally:
                pcm = None

    def _remember(self, run, value):
        if valid_owned_resource(value):
            with self._lock:
                for item in run.resources:
                    if item["uid"] != value["uid"]:
                        continue
                    # stderr can report the owned aggregate before stdout
                    # publishes its complete format/name metadata. Enrich that
                    # same resource without changing its cleanup identity.
                    identity = ("uid", "tap_uid", "device_id", "tap_id")
                    if all(item[key] == value[key] for key in identity):
                        item.update(value)
                    return
                run.resources.append(dict(value))

    def _read_steps(self, run):
        # Drain to EOF even after stop: SIGTERM cleanup can emit resource IDs.
        while True:
            line = run.process.stderr.readline(4097)
            if not line:
                return
            if len(line) > 4096 or not line.endswith("\n"):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue  # Never mirror arbitrary diagnostics/audio into logs.
            if isinstance(value, dict):
                self._remember(run, value)
                if "native_step" in value and not run.stop.is_set():
                    with self._lock:
                        run.phase = str(value["native_step"])

    def _read(self, run):
        try:
            while True:
                line = run.process.stdout.readline(MAX_RESPONSE_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_RESPONSE_LINE or not line.endswith("\n"):
                    if run.stop.is_set():
                        continue
                    self._fail(run, "Worker response exceeds bounded protocol")
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("Invalid worker response")
                    if "resource" in entry:
                        self._remember(run, entry["resource"])
                        continue
                    # After stop only identity metadata is accepted. Late ready
                    # or reply messages cannot reactivate or deliver old audio.
                    if run.stop.is_set():
                        continue
                    if entry.get("ready") is True:
                        with self._lock:
                            if not run.stop.is_set():
                                input_limit = entry.get("max_input_seconds", 10)
                                if type(input_limit) is not int or not 1 <= input_limit <= MAX_INPUT_SECONDS:
                                    raise ValueError("Invalid worker input duration capability")
                                run.max_input_seconds = input_limit
                                run.input_started_supported = entry.get("input_started_ack") is True
                                run.armed_ack_supported = (entry.get("protocol") == PROTOCOL_VERSION
                                                           and entry.get("armed_ack") is True)
                                run.phase = "ready"
                                run.ready.set()
                    elif "armed" in entry:
                        with self._lock:
                            if not run.armed_ack_supported or entry["armed"] != run.active_turn:
                                raise ValueError("Worker arming acknowledgement does not match active turn")
                            run.armed_turn = entry["armed"]
                            run.armed.set()
                    elif "input_started" in entry:
                        with self._lock:
                            if not run.input_started_supported or entry["input_started"] != run.active_turn:
                                raise ValueError("Input playback acknowledgement does not match active turn")
                    elif "error" in entry:
                        raise BackendUnavailable(str(entry["error"])[:240])
                    elif "reply" in entry:
                        turn_id = entry["reply"]
                        samples, final = decode_reply(entry)
                        metrics = reply_metrics(entry)
                        if metrics is not None and not final:
                            raise ValueError("Reply metrics require a final packet")
                        with self._lock:
                            if turn_id != run.active_turn:
                                raise ValueError("Worker reply turn does not match active turn")
                        accepted = False
                        deadline = time.monotonic() + 2
                        while not run.stop.is_set() and time.monotonic() < deadline:
                            if run.submit(turn_id, samples, channels=1, final=final):
                                accepted = True
                                break
                            run.stop.wait(.01)
                        if not accepted and not run.stop.is_set():
                            raise BackendUnavailable("Reply was rejected or backpressure exceeded two seconds")
                        if final and accepted:
                            with self._lock:
                                if run.active_turn == turn_id:
                                    run.active_turn = None
                                    self._last_turn_metrics = metrics
                except Exception as exc:
                    self._fail(run, str(exc)[:240])
        except (OSError, ValueError) as exc:
            self._fail(run, f"Dedicated audio worker read failed: {type(exc).__name__}")
        finally:
            if not run.stop.is_set():
                self._fail(run, "Dedicated audio worker exited")

    def _fail(self, run, reason):
        with self._lock:
            if run.stop.is_set():
                return
            run.error, run.phase = reason, "failed"
            run.ready.clear()
            run.stop.set()
        # Reader must remain available to drain late ownership metadata.
        threading.Thread(target=self._finish_failure, args=(run, reason), daemon=True).start()

    def _finish_failure(self, run, reason):
        with self._lifecycle:
            self._cancel(run)
            # Never let an old failure notification stop a replacement session.
            if self._latest_run is run and run.on_error:
                run.on_error(reason)

    def cancel(self):
        with self._lock:
            run = self._run
        if run is not None:
            self._cancel(run)

    def _cancel(self, run):
        with self._lifecycle:
            with self._lock:
                if self._run is not run:
                    return
                run.stop.set()
                run.ready.clear()
                run.active_turn = None
                while True:
                    try:
                        run.outgoing.get_nowait()
                    except queue.Empty:
                        break
            process = run.process
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            # Child is gone, so pipe readers reach EOF. Join BEFORE taking the
            # resource snapshot; stop does not suppress metadata in either pipe.
            deadline = time.monotonic() + 3
            for thread in run.threads:
                if thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
            if any(t.is_alive() for t in run.threads):
                with self._lock:
                    run.error = run.error or "Audio worker reader did not finish; restart refused"
                # Keep this stopped run attached rather than allow a new run
                # while an unbounded caller-supplied callback is still running.
                return
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
            with self._lock:
                resources = list(run.resources)
            for identity in reversed(resources):
                try:
                    self._cleanup_resource(identity)
                except (OSError, subprocess.TimeoutExpired, BackendUnavailable):
                    run.error = run.error or "Audio resource cleanup needs verification"
            with self._lock:
                self._last_error = run.error
                self._phase = "failed" if run.error else "stopped"
                self._run = None
                run.resources.clear()
                run.submit = None

    def _cleanup_resource(self, identity):
        # Killable cleanup rechecks per-run UIDs; never remove a reused native ID.
        command = [*self.cleanup_command, json.dumps(identity)]
        result = subprocess.run(command, capture_output=True, timeout=5)
        if result.returncode:
            raise BackendUnavailable("Audio resource cleanup needs verification")


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def worker(reply_pids, *, external_input=False):
    """The only native path. Explicit caller invocation is required."""
    import numpy as np
    import sounddevice as sd
    pids = checked_pids(reply_pids)
    n = Native()
    clock_id = n.uint(1, "dOut")
    output_clock_uid(n, clock_id)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    playback = queue.Queue(maxsize=1)
    captures = queue.Queue(maxsize=128)
    commands = queue.Queue(maxsize=2)
    problem = [None]
    current = np.zeros((0, 2), dtype=np.float32)
    position = 0

    def render(data, frames, _timing, _status):
        nonlocal current, position
        data.fill(0)
        if stop.is_set():
            return
        written = 0
        while written < frames:
            if position == len(current):
                try:
                    current = playback.get_nowait()
                except queue.Empty:
                    break
                position = 0
            count = min(frames - written, len(current) - position)
            data[written:written + count] = current[position:position + count]
            written += count
            position += count

    def receive(_device, _now, incoming, _at, _out, _out_at, _client):
        try:
            if not incoming or stop.is_set():
                return 0
            buffers = C.cast(incoming, C.POINTER(OneBufferList)).contents
            if buffers.count != 1:
                problem[0] = "Unexpected reply input stream layout"
                return 0
            buf = buffers.buffers[0]
            if not buf.data or not buf.byte_size:
                return 0
            if buf.channels != 1 or buf.byte_size % 4 or buf.byte_size > REPLY_RATE * 4:
                problem[0] = "Unexpected reply block size"
                return 0
            try:
                captures.put_nowait(C.string_at(buf.data, buf.byte_size))
            except queue.Full:
                problem[0] = "Reply capture queue overflow"
        except Exception:
            problem[0] = "Reply capture callback failed"
        return 0

    def read_commands():
        try:
            while not stop.is_set():
                line = sys.stdin.readline(MAX_LINE + 1)
                if not line:
                    stop.set()
                    break
                if len(line) > MAX_LINE or not line.endswith("\n"):
                    raise ValueError("Input packet exceeds limit")
                entry = json.loads(line)
                commands.put(entry, timeout=1)
        except Exception:
            problem[0] = "Invalid or excessive input packet"

    # HAL mode already has an explicit input sink. Avoid creating a second
    # virtual microphone and replaying the same recording into that unused tap.
    # Legacy mode retains its original public-mic path for existing clients.
    with ExitStack() as stack:
        if not external_input:
            stack.enter_context(sd.OutputStream(samplerate=REPLY_RATE, channels=2,
                                                dtype="float32", callback=render))
            mic = stack.enter_context(Tap(n, [os.getpid()], public=True,
                                          clock_output_device=clock_id))
            emit({"resource": mic.metadata()})
        reply = stack.enter_context(Tap(n, pids, public=False, clock_output_device=clock_id))
        emit({"resource": reply.metadata()})
        fmt = reply.format()
        if not (fmt.format_id == fourcc("lpcm") and fmt.format_flags & 1
                and not fmt.format_flags & 2 and fmt.bits_per_channel == 32
                and fmt.channels_per_frame == 1 and fmt.sample_rate == REPLY_RATE):
            raise BackendUnavailable("Reply format must be mono float32 at 48 kHz")
        reply.start_capture(receive)
        threading.Thread(target=read_commands, daemon=True).start()
        emit({"ready": True, "protocol": PROTOCOL_VERSION, "armed_ack": True,
              "max_input_seconds": MAX_INPUT_SECONDS,
              "input_started_ack": True,
              "input_playback_guard": True,
              "external_input": external_input, "reply_pids": pids,
              "microphone": None if external_input else "Dot Watch Mic"})
        active = window = None

        def send_packets(packets):
            nonlocal active
            for raw, final in packets:
                value = {"reply": active, "rate": REPLY_RATE, "channels": 1,
                         "pcm_f32": base64.b64encode(raw).decode("ascii"), "final": final}
                if final:
                    value["metrics"] = window.final_metrics
                emit(value)
                if final:
                    active = None

        while not stop.is_set():
            if problem[0]:
                raise BackendUnavailable(problem[0])
            try:
                message = commands.get_nowait()
            except queue.Empty:
                message = None
            if message is not None:
                if isinstance(message, dict) and set(message) == {"input_started"}:
                    if not external_input or active is None or message["input_started"] != active:
                        raise BackendUnavailable("Input playback notification does not match active turn")
                    window.mark_input_started(time.monotonic())
                    emit({"input_started": active})
                    message = None
            if message is not None:
                if active is not None or not isinstance(message, dict) or set(message) != {"offer", "pcm16"}:
                    raise BackendUnavailable("Another reply is in progress or invalid input")
                turn_id = message["offer"]
                if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 160:
                    raise BackendUnavailable("Invalid turn identity")
                pcm = base64.b64decode(message["pcm16"], validate=True)
                if not 0 < len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2:
                    raise BackendUnavailable("Invalid completed recording size")
                # The explicit acknowledgement follows generation reset and
                # discarding old frames. HAL can dispatch without guessing.
                while True:
                    try:
                        captures.get_nowait()
                    except queue.Empty:
                        break
                active = turn_id
                window = PlaybackReplyWindow(time.monotonic(), len(pcm) / 32000)
                if not external_input:
                    values = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                    scaled = np.interp(np.arange(len(values) * 3) / 3,
                                       np.arange(len(values)), values).astype(np.float32)
                    playback.put_nowait(np.repeat(scaled[:, None], 2, axis=1))
                    window.mark_input_started(time.monotonic())
                pcm = None
                message.clear()
                emit({"armed": active})
            try:
                raw = captures.get(timeout=.01)
            except queue.Empty:
                raw = None
            if active is None:
                continue
            now = time.monotonic()
            expired = window.poll(now)
            if expired:
                send_packets(expired)
                if active is None:
                    continue
            if raw is None:
                continue
            floats = np.frombuffer(raw, dtype="<f4")
            if not np.all(np.isfinite(floats)):
                raise BackendUnavailable("Non-finite reply audio")
            audible = bool(np.sqrt(np.mean(floats * floats)) >= .002)
            send_packets(window.feed(raw, audible, now))
        stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--cleanup")
    parser.add_argument("--reply-pids", type=int, nargs="+")
    parser.add_argument("--external-input", action="store_true",
                        help="Capture replies only; an explicit external sink supplies the microphone")
    args = parser.parse_args(argv)
    if args.cleanup:
        cleanup_owned(json.loads(args.cleanup))
    else:
        pids = checked_pids(args.reply_pids or [])
        try:
            worker(pids, external_input=args.external_input)
        except Exception as exc:
            emit({"error": str(exc)[:240]})
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
