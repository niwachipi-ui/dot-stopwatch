"""Explicit USB/audio session core; no device opening, files, network or mic start.

Call session.start() FIRST for read-only device/guard preflight; only after that
succeeds call backend.start(submit_reply=session.submit_reply,
on_error=session.stop). Tick only when that explicit backend startup is ready.
On startup failure call session.stop(). The injected backend's cancel() must
purge and stop BOTH input/output audio workers.
Drive tick() from one control loop. submit_reply() may run on a reader thread.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import secrets
import struct
import threading
import time

from transport import AudioInfo, GenerationChanged, MAX_CAPTURE_BYTES, MAX_STREAM_CHUNK_BYTES, RATE, TransportError

REPLY_RATE = 48000
PLAY_CHUNK_BYTES = RATE * 2  # One second bounds audio already queued on device.
STREAM_PLAY_CHUNK_BYTES = RATE * 3  # 1.5 s amortizes network round trips and buffers jitter.
MAX_PENDING_REPLY_BYTES = PLAY_CHUNK_BYTES * 20
MAX_REPLY_FRAMES_PER_CALL = REPLY_RATE


class SessionError(Exception):
    pass


@dataclass(frozen=True)
class Recording:
    turn_id: str
    pcm: bytes
    sample_rate: int = RATE
    channels: int = 1


class ReplyResampler:
    """Streaming 48 kHz float mono/stereo to 16 kHz PCM16.

    A 63-tap windowed-sinc low-pass at 7 kHz precedes 3:1 decimation. Its
    31-input-sample delay is retained across chunks; final flush keeps the tail.
    Only the two documented rates/formats are supported. No dependency required.
    """
    TAPS = 63

    def __init__(self):
        cutoff = 7000 / REPLY_RATE
        middle = (self.TAPS - 1) / 2
        taps = []
        for index in range(self.TAPS):
            x = index - middle
            sinc = 2 * cutoff if x == 0 else math.sin(2 * math.pi * cutoff * x) / (math.pi * x)
            taps.append(sinc * (0.54 - 0.46 * math.cos(2 * math.pi * index / (self.TAPS - 1))))
        scale = sum(taps)
        self.taps = tuple(value / scale for value in taps)
        self.history = deque([0.0] * self.TAPS, maxlen=self.TAPS)
        self.phase = 0
        self.finished = False
        self.input_frames = 0

    @staticmethod
    def validate(samples, channels=1, sample_rate=REPLY_RATE):
        if sample_rate != REPLY_RATE or channels not in (1, 2):
            raise ValueError("Reply must be 48000 Hz float mono or interleaved stereo.")
        if len(samples) % channels or len(samples) // channels > MAX_REPLY_FRAMES_PER_CALL:
            raise ValueError("Each reply submission must contain at most one second of complete frames.")
        mono = []
        for offset in range(0, len(samples), channels):
            values = [float(samples[offset + channel]) for channel in range(channels)]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Reply contains non-finite samples.")
            # Avoid overflow while downmixing two very large but finite values.
            value = sum(max(-1.0, min(1.0, value)) / channels for value in values)
            mono.append(value)
        return mono

    def output_size(self, frames, *, final=False):
        padding = (self.TAPS - 1) // 2 if final and (self.input_frames or frames) else 0
        return ((self.phase + frames + padding) // 3) * 2

    def feed_mono(self, mono, *, final=False):
        if self.finished:
            raise ValueError("Reply stream has already ended.")
        output = bytearray()
        self.input_frames += len(mono)
        padding = [0.0] * ((self.TAPS - 1) // 2) if final and self.input_frames else []
        for sample in list(mono) + padding:
            self.history.append(sample)
            self.phase += 1
            if self.phase == 3:
                self.phase = 0
                filtered = sum(value * coefficient for value, coefficient in zip(self.history, self.taps))
                quantized = max(-32768, min(32767, round(filtered * 32768)))
                output.extend(struct.pack("<h", quantized))
        self.finished = final
        return bytes(output)

    def feed(self, samples, *, channels=1, sample_rate=REPLY_RATE, final=False):
        return self.feed_mono(self.validate(samples, channels, sample_rate), final=final)


class RelaySession:
    """One physical recording/response at a time, default stopped.

    client provides info(), capture(info), play(pcm). sink provides fast offer()
    returning acceptance and cancel() clearing both directions. The controller
    NEVER unlocks or starts a microphone, opens a process tap, or chooses an
    audio device. start() skips any recording already present at that moment.

    stop() discards RAM queues and cancels the backend, but cannot unsend audio
    already queued on StopWatch (up to three two-second streaming chunks).
    It does not alter the
    device's guard setting; the owning UI must explicitly lock it when desired.
    """
    def __init__(self, client, sink, *, clock=time.monotonic,
                 max_pending_bytes=MAX_PENDING_REPLY_BYTES, turn_timeout=120.0,
                 should_stop=lambda: False):
        if not PLAY_CHUNK_BYTES <= max_pending_bytes <= MAX_PENDING_REPLY_BYTES:
            raise ValueError("Pending reply limit must be between 1 and 20 seconds.")
        if not math.isfinite(turn_timeout) or not 1 <= turn_timeout <= 120:
            raise ValueError("Turn timeout must be between 1 and 120 seconds.")
        self.client, self.sink, self.clock = client, sink, clock
        self.max_pending_bytes = max_pending_bytes
        self.turn_timeout = turn_timeout
        self._should_stop = should_stop
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._generation = 0
        self._running = False
        self._starting = False
        self._cancelling = 0
        self._reason = "not_started"
        self._boot = None
        self._baseline_id = None
        self._claims = deque(maxlen=128)
        self._session_id = ""
        self._handsfree = False
        self._stream_play = False
        self._last_turn_metrics = None
        self._last_error = None
        self._operation = "idle"
        self._last_info = None
        self._audio_epoch = self._call_request = None
        self._listen_needed = False
        self._reset_turn()

    def _reset_turn(self):
        self._turn_id = None
        self._recording = None
        self._recording_deadline = 0.0
        self._turn_deadline = 0.0
        self._sink_accepted = False
        self._reply_final = False
        self._resampler = ReplyResampler()
        self._chunks = deque()
        self._tail = bytearray()
        self._pending_bytes = 0
        self._playing_id = None
        self._enqueued_ids = deque()
        self._turn_metrics = {}
        self._play_events = deque(maxlen=64)

    def snapshot(self):
        with self._lock:
            return {"running": self._running, "reason": self._reason, "turn_id": self._turn_id,
                    "pending_reply_bytes": self._pending_bytes,
                    "pending_recording_bytes": len(self._recording.pcm) if self._recording else 0,
                    "playing_id": self._playing_id, "queued_play_ids":list(self._enqueued_ids),
                    "timing":dict(self._turn_metrics), "last_turn_timing":self._last_turn_metrics,
                    "playback_events":list(self._play_events), "last_error":self._last_error}

    def start(self):
        with self._lock:
            if self._running or self._starting or self._cancelling:
                raise SessionError("Session is already running, starting, or stopping.")
            if self._should_stop():
                raise SessionError("Session start was cancelled.")
            self._generation += 1
            generation = self._generation
            self._starting = True
        try:
            info = self.client.info()
            if info.fields.get("guard") != "0":
                raise SessionError("Recording remains locked; this session will never unlock it.")
            if info.fields.get("mic") != "0":
                raise SessionError("Release the physical button before starting a session.")
            if info.fields.get("blue") != "0":
                raise SessionError("A physically released blue button must be verified before starting.")
        except Exception:
            self.stop("start_rejected", _expected_generation=generation)
            raise
        with self._lock:
            if self._generation != generation or not self._starting or self._should_stop():
                self._starting = False
                raise SessionError("Session start was cancelled.")
            self._starting = False
            self._reset_turn()
            self._last_error = None
            self._boot, self._baseline_id = info.boot, info.id
            self._handsfree = info.fields.get("controls") == "21"
            self._stream_play = self._handsfree and info.fields.get("play_stream") == "1"
            self._audio_epoch = info.fields.get("audio_epoch")
            self._call_request = info.fields.get("call_req")
            self._listen_needed = self._handsfree
            self._session_id = secrets.token_hex(8)
            self._running, self._reason = True, "running"

    def stop(self, reason="stopped", *, _expected_generation=None):
        with self._lock:
            if _expected_generation is not None and _expected_generation != self._generation:
                return
            self._generation += 1
            self._starting = False
            self._cancelling += 1
            self._running, self._reason = False, reason
            self._reset_turn()
        # Never call backend teardown while holding the callback's queue lock.
        try:
            self.sink.cancel()
        except Exception:
            with self._lock:
                self._reason = reason + ":backend_cancel_failed"
        finally:
            with self._lock:
                self._cancelling -= 1

    def _valid_info(self, info):
        if info.fields.get("guard") != "0":
            self.stop("microphone_locked")
            return False
        if info.boot != self._boot:
            self.stop("device_rebooted")
            return False
        if info.state == 4:
            self.stop("device_audio_error")
            return False
        if self._handsfree:
            if info.fields.get("controls") != "21" or info.fields.get("call_req") != self._call_request:
                self.stop("call_request_changed")
                return False
            if info.fields.get("audio_epoch") != self._audio_epoch:
                with self._lock:
                    old_turn = self._turn_id if self._sink_accepted else None
                    self._generation += 1
                    self._reset_turn()
                    self._audio_epoch = info.fields["audio_epoch"]
                    self._baseline_id = info.id
                    self._listen_needed = True
                if old_turn is not None:
                    self.sink.interrupt(old_turn)
                return False
        return True

    def _current(self, generation):
        with self._lock:
            return self._running and generation == self._generation and not self._should_stop()

    def submit_reply(self, turn_id, samples, *, channels=1, sample_rate=REPLY_RATE, final=False):
        """Thread-safe bounded offer. False means nothing was accepted.

        Rejected chunks must not be skipped silently: the backend must retain
        them in its own bounded queue or stop the session. Stale turns are never
        played. Format errors raise ValueError without mutating stream state.
        """
        with self._lock:
            if not self._running or self._should_stop() or turn_id != self._turn_id or self._reply_final:
                return False
            mono = self._resampler.validate(samples, channels, sample_rate)
            size = self._resampler.output_size(len(mono), final=final)
            if self._pending_bytes + size > self.max_pending_bytes:
                return False
            data = self._resampler.feed_mono(mono, final=final)
            if mono and "first_reply_at" not in self._turn_metrics:
                self._turn_metrics["first_reply_at"] = self.clock()
            self._pending_bytes += len(data)
            self._tail.extend(data)
            chunk_bytes = (STREAM_PLAY_CHUNK_BYTES if self._stream_play
                           and self.max_pending_bytes >= STREAM_PLAY_CHUNK_BYTES
                           else PLAY_CHUNK_BYTES)
            while len(self._tail) >= chunk_bytes:
                self._chunks.append(bytes(self._tail[:chunk_bytes]))
                del self._tail[:chunk_bytes]
            if final and self._tail:
                self._chunks.append(bytes(self._tail))
                self._tail.clear()
            self._reply_final = final
            return True

    def tick(self, info=None):
        """Run one bounded step. No operation automatically retries a claim."""
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                if not self._running:
                    return
                generation = self._generation
            if self._should_stop():
                self.stop("stop_requested")
                return
            self._operation = "read_state"
            info = self.client.info() if info is None else info
            self._last_info = info
            if not self._current(generation) or not self._valid_info(info):
                return
            with self._lock:
                expired = self._turn_id and self.clock() >= self._turn_deadline
            if expired:
                self.stop("reply_timeout")
                return
            if self._handsfree and self._listen_needed and self._turn_id is None:
                # Session startup keeps the existing mic-off preflight. A
                # separate, epoch-scoped command begins each ON-authorized turn.
                if info.fields.get("mic") != "0" or info.state not in (0, 2):
                    return
                self._operation = "start_listening"
                after = self.client.listen(info)
                with self._lock:
                    self._audio_epoch = after.fields["audio_epoch"]
                    self._baseline_id = info.id
                    self._listen_needed = False
                return
            if self._recording is not None:
                self._offer_recording(generation)
                return
            if self._turn_id is None:
                if (info.available and not info.consumed and info.state == 2
                        and info.fields.get("mic") == "0" and info.expires_ms > 0
                        and info.id != self._baseline_id and (info.boot, info.id) not in self._claims):
                    self._claim(generation, info.id, info if self._stream_play else None)
                return
            if self._stream_play:
                self._advance_stream_reply(generation, info)
            else:
                self._advance_reply(generation, info)
        except GenerationChanged:
            # The device already stopped obsolete playback. Next tick reads
            # its new epoch, invalidates local queues and restarts listening.
            pass
        except (TransportError, OSError, ValueError) as exc:
            self._record_error(exc)
            self.stop("transport_or_audio_error")
        except Exception as exc:
            self._record_error(exc, include_message=False)
            self.stop("backend_error")
        finally:
            self._tick_lock.release()

    def _record_error(self, exc, *, include_message=True):
        """Keep bounded metadata through stop(), never PCM or source text."""
        with self._lock:
            info = self._last_info
            self._last_error = {"type":type(exc).__name__, "operation":self._operation,
                "message":str(exc)[:180] if include_message else "Backend operation failed",
                "at":self.clock(), "pending_reply_bytes":self._pending_bytes,
                "queued_play_ids":list(self._enqueued_ids), "timing":dict(self._turn_metrics),
                "playback_events":list(self._play_events),
                "device":{key:info.fields.get(key) for key in
                    ("boot", "state", "guard", "mic", "audio_epoch", "call_req", "call_state",
                     "play_queue", "play_accepted", "play_done", "play_result")}
                    if info is not None else None}
            diagnostics = getattr(self.client, "transport_diagnostics", None)
            if callable(diagnostics):
                self._last_error["transport"] = diagnostics()

    def _claim(self, generation, identifier, info=None):
        # Fresh check immediately before claiming. No stale info can authorize it.
        info = self.client.info() if info is None else info
        if not self._current(generation) or not self._valid_info(info):
            return
        if (info.id != identifier or not info.available or info.consumed or info.state != 2
                or info.fields.get("mic") != "0" or info.expires_ms <= 0):
            return
        if self._handsfree and info.fields.get("record_epoch") != self._audio_epoch:
            raise GenerationChanged("Old recording generation")
        self._claims.append((info.boot, info.id))  # Mark BEFORE any USB attempt.
        expires = self.clock() + info.expires_ms / 1000
        capture_at = self.clock()
        self._operation = "capture_recording"
        pcm = self.client.capture(info)
        if not pcm or len(pcm) > MAX_CAPTURE_BYTES or len(pcm) % 2:
            raise ValueError("Invalid captured audio")
        with self._lock:
            if not self._current(generation):
                return
            self._reset_turn()
            self._turn_id = f"{self._session_id}:{info.boot}:{info.id}"
            self._recording = Recording(self._turn_id, pcm)
            self._recording_deadline = expires
            # The input is replayed into the call in real time. Its bounded
            # duration must not consume the reply window (at most 60 + 120 s).
            recording_seconds = len(pcm) / (RATE * 2)
            self._turn_deadline = self.clock() + recording_seconds + self.turn_timeout
            self._turn_metrics = {"capture_started_at":capture_at,
                                  "capture_finished_at":self.clock(), "recording_seconds":recording_seconds}
        self._offer_recording(generation)

    def _offer_recording(self, generation):
        self._operation = "offer_recording"
        info = self.client.info()  # Also immediately recheck before sink append.
        if not self._current(generation) or not self._valid_info(info):
            return
        with self._lock:
            recording = self._recording
            expired = self.clock() >= self._recording_deadline
        if expired:
            self.stop("recording_expired_before_delivery")
            return
        if info.fields.get("mic") != "0":
            return
        if not self._current(generation):
            return
        accepted = self.sink.offer(recording)
        if not self._current(generation):
            # Teardown raced an offer: clear accepted audio too.
            self.stop("stop_requested" if self._should_stop() else self.snapshot()["reason"])
            return
        if accepted:
            with self._lock:
                self._recording = None
                self._sink_accepted = True
                self._turn_metrics["offered_at"] = self.clock()

    def _finish_turn(self):
        self._turn_metrics["playback_finished_at"] = self.clock()
        self._turn_metrics["playback_events"] = list(self._play_events)
        self._last_turn_metrics = dict(self._turn_metrics)
        self._reset_turn()
        self._listen_needed = self._handsfree

    def _advance_stream_reply(self, generation, info):
        """Keep the next bounded chunk ready before the current one finishes."""
        if info.fields.get("play_stream") != "1":
            self.stop("stream_playback_capability_changed")
            return
        done = int(info.fields["play_done"])
        with self._lock:
            if done in self._enqueued_ids:
                while self._enqueued_ids.popleft() != done:
                    pass
            outstanding = bool(self._enqueued_ids)
        if outstanding and info.fields.get("play_result") in {"4", "5"}:
            self.stop("playback_cancelled" if info.fields["play_result"] == "4" else "playback_failed")
            return
        slots, queued = int(info.fields["play_slots"]), int(info.fields["play_queue"])
        if outstanding and queued == 0:
            self.stop("stream_playback_completion_missing")
            return
        with self._lock:
            if self._reply_final and not self._chunks and not self._tail and not outstanding and queued == 0:
                self._finish_turn()
                return
            if queued >= slots or not self._chunks:
                return
            parts, size = [], 0
            for part in self._chunks:
                if size + len(part) > MAX_STREAM_CHUNK_BYTES:
                    break
                parts.append(part)
                size += len(part)
        if not parts or info.fields.get("mic") != "0":
            return
        if not self._current(generation):
            return
        self._operation = "enqueue_reply"
        began_at = self.clock()
        identifier = self.client.enqueue_play(b"".join(parts), request=info)
        if not self._current(generation):
            return
        with self._lock:
            self._enqueued_ids.append(identifier)
            for _ in parts:
                self._chunks.popleft()
            self._pending_bytes -= size
            self._turn_metrics.setdefault("first_play_queued_at", self.clock())
            event = {"id":identifier, "bytes":size, "queue_before":queued,
                "done_before":done, "started_at":began_at, "accepted_at":self.clock()}
            transfer = getattr(self.client, "last_play_metrics", None)
            if isinstance(transfer, dict) and transfer.get("id") == identifier:
                event["transfer"] = dict(transfer)
            self._play_events.append(event)

    def _advance_reply(self, generation, info):
        self._operation = "legacy_play_reply"
        with self._lock:
            playing = self._playing_id
        if playing is not None:
            if info.fields.get("play_id") != str(playing):
                self.stop("unexpected_device_playback")
                return
            result = info.fields.get("play_result")
            if result in {"4", "5"}:
                self.stop("playback_cancelled" if result == "4" else "playback_failed")
                return
            if result != "3":
                return
            with self._lock:
                self._playing_id = None
        with self._lock:
            if self._reply_final and not self._chunks and not self._tail:
                self._finish_turn()
                return
            chunk = self._chunks[0] if self._chunks else None
        if chunk is None:
            return
        # Guard and microphone state must be fresh before EACH speaker chunk.
        info = self.client.info()
        if not self._current(generation) or not self._valid_info(info):
            return
        if info.fields.get("mic") != "0" or info.state not in (0, 2):
            return
        previous_play_id = int(info.fields.get("play_id", "0"))
        if self._handsfree:
            self.client.play(chunk, request=info)
        else:
            self.client.play(chunk)
        if not self._current(generation):
            return
        after = self.client.info()
        if not self._current(generation) or not self._valid_info(after):
            return
        expected_id = (previous_play_id + 1) & 0xFFFFFFFF
        if after.fields.get("play_id") != str(expected_id):
            self.stop("playback_not_verified")
            return
        with self._lock:
            self._playing_id = expected_id
            self._chunks.popleft()
            self._pending_bytes -= len(chunk)
