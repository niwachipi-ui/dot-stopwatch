"""Explicit BlackHole output; import is inert, no input/default-device APIs.

The original frozen worker uses a best-effort 250 ms lead-in. Explicit v2 mode
requires a scoped arming acknowledgement before omitting that lead-in.
"""
import argparse
import base64
import ctypes as C
import json
import os
from pathlib import Path
import queue
import select
import signal
import subprocess
import sys
import threading
import time

UID = "BlackHole2ch_UID"
NAME = "BlackHole 2ch"
RATE = 48000
MAX_INPUT_SECONDS = 60
MAX_PCM = 16000 * MAX_INPUT_SECONDS * 2
LEAD_FRAMES = 12000
MAX_LINE = ((MAX_PCM + 2) // 3) * 4 + 4096


def validate_pcm(pcm):
    if not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_PCM or len(pcm) % 2:
        raise ValueError("Expected at most sixty seconds of PCM16 mono at 16 kHz")


def stereo_samples(pcm):
    """Bounded signed PCM16 conversion; linear 3x interpolation, duplicated L/R."""
    validate_pcm(pcm)
    import numpy as np
    values = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    scaled = np.interp(np.arange(len(values) * 3) / 3, np.arange(len(values)), values).astype(np.float32)
    return np.repeat(scaled[:, None], 2, axis=1)


def verify_hal(native, uid):
    """Read-only exact UID, channel and input eligibility validation."""
    from coreaudio_tap import ASBD, output_clock_uid
    if uid != UID:
        raise ValueError("Only the explicitly requested BlackHole2ch_UID is supported")
    matches = [d for d in native.list_devices() if d.get("uid") == uid]
    if len(matches) != 1 or matches[0].get("name") != NAME:
        raise RuntimeError("Exact BlackHole 2ch device is absent or ambiguous")
    device = matches[0]["object_id"]
    if not native.uint(device, "livn") or any(native.uint(device, "dflt", scope) != 1
                                               for scope in ("inpt", "outp")):
        raise RuntimeError("BlackHole is not alive or eligible for input/output")
    for scope in ("inpt", "outp"):
        channels = 0
        for stream in native.object_ids(device, "stm#", scope):
            raw = native.property_bytes(stream, "sfmt")
            if len(raw) != C.sizeof(ASBD):
                raise RuntimeError("Unexpected stream format layout")
            channels += ASBD.from_buffer_copy(raw).channels_per_frame
        if channels != 2:
            raise RuntimeError("BlackHole must have exactly two input/output channels")
    # Frozen reply worker requires an output-only speaker clock. Never change it.
    clock_uid = output_clock_uid(native, native.uint(1, "dOut"))
    if clock_uid == uid:
        raise RuntimeError("BlackHole must not be the default output")
    return {"uid": uid, "name": NAME, "device_id": device, "public": True,
            "channels": 2, "sample_rate": RATE, "clock_output_uid": clock_uid,
            "input_eligible": True, "turn_sync": "best_effort_250ms_lead_in"}


def output_index(devices, apis):
    matches = [(i, d) for i, d in enumerate(devices) if d.get("name") == NAME]
    if len(matches) != 1:
        raise RuntimeError("BlackHole PortAudio device is absent or ambiguous")
    index, device = matches[0]
    api = device.get("hostapi")
    if (type(api) is not int or not 0 <= api < len(apis) or apis[api].get("name") != "Core Audio"
            or device.get("max_input_channels") != 2 or device.get("max_output_channels") != 2):
        raise RuntimeError("BlackHole PortAudio layout is not Core Audio stereo")
    return index


class OutputProcess:
    """Killable output-only worker; every request has a bounded response wait."""
    def __init__(self, uid, python_executable, *, command=None):
        if uid != UID:
            raise ValueError("Only BlackHole2ch_UID is supported")
        self.uid = uid
        self.command = command or [str(python_executable), str(Path(__file__).resolve()), "--worker", "--uid", uid]
        self.process = None
        self.identity = None
        self.error = None
        self._responses = queue.Queue(maxsize=4)
        self._lock = threading.RLock()
        self._reader = None

    @property
    def ready(self):
        return bool(self.process and self.process.poll() is None and self.identity and not self.error)

    def _read(self, process):
        try:
            while True:
                line = process.stdout.readline(8193)
                if not line:
                    break
                if len(line) > 8192 or not line.endswith("\n"):
                    raise RuntimeError("Invalid output worker response")
                value = json.loads(line)
                if "error" in value:
                    self.error = str(value["error"])[:240]
                self._responses.put_nowait(value)
        except Exception as exc:
            self.error = type(exc).__name__

    def _response(self, expected, timeout=2):
        try:
            value = self._responses.get(timeout=timeout)
            if self.error or expected not in value:
                raise RuntimeError(self.error or "Unexpected output response")
            return value[expected]
        except Exception:
            self.cancel()
            raise

    def start(self):
        with self._lock:
            if self.process is not None:
                raise RuntimeError("Output worker already started")
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
            os.set_blocking(self.process.stdin.fileno(), False)
            self._reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
            self._reader.start()
            self.identity = self._response("ready", timeout=15)
            if (self.identity.get("uid") != self.uid or self.identity.get("name") != NAME
                    or self.identity.get("channels") != 2 or not self.identity.get("input_eligible")):
                self.cancel()
                raise RuntimeError("Output worker identity mismatch")

    def _request(self, payload, expected):
        with self._lock:
            if not self.ready:
                raise RuntimeError("Output worker is unavailable")
            try:
                data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                deadline = time.monotonic() + 2
                fd = self.process.stdin.fileno()
                offset = 0
                while offset < len(data):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                        raise TimeoutError("Output worker write exceeded two seconds")
                    try:
                        offset += os.write(fd, data[offset:offset + 4096])
                    except BlockingIOError:
                        continue
            except Exception:
                self.cancel()
                raise
            return self._response(expected)

    def reserve(self):
        return self._request({"reserve": True}, "reserved")

    def release(self, token):
        self._request({"release": token}, "released")

    def play(self, token, pcm, *, armed=False):
        validate_pcm(pcm)
        if type(armed) is not bool:
            raise ValueError("Armed dispatch must be explicit")
        command = {"play": token, "pcm": base64.b64encode(pcm).decode("ascii")}
        if armed:
            command["armed"] = True
        self._request(command, "queued")

    def purge(self):
        if self._request({"purge": True}, "purged") is not True:
            raise RuntimeError("Output purge was not acknowledged")

    def cancel(self):
        with self._lock:
            process = self.process
            self.identity = None
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=2)
                if self._reader is not None:
                    self._reader.join(timeout=1)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
                self.process = None


class HALRelayBackend:
    """Existing frozen reply capture plus an explicit persistent HAL input sink."""
    def __init__(self, reply_backend, output, *, should_stop=lambda: False,
                 require_armed_ack=False):
        if type(require_armed_ack) is not bool:
            raise ValueError("Reply-worker v2 selection must be explicit")
        self.reply_backend, self.output = reply_backend, output
        self.require_armed_ack = require_armed_ack
        self.last_error = None
        self._lock = threading.RLock()
        self.should_stop = should_stop
        self._restart_gate = threading.Lock()
        self._cancelled = threading.Event()
        self._restart_thread = None
        self._restarting = False
        self._restart_deadline = 0
        self._generation = 0
        self._submit = self._on_error = None

    @property
    def ready(self):
        with self._lock:
            if self._restarting and time.monotonic() >= self._restart_deadline:
                self.last_error = "Reply worker restart timed out"
            return (not self._cancelled.is_set() and not self.last_error and self.output.ready
                    and (self._restarting or self.reply_backend.ready))

    @property
    def stopped(self):
        return self.output.process is None and self.reply_backend.stopped

    @property
    def public_input_identity(self):
        identity = dict(self.output.identity) if self.output.identity else None
        if identity is not None and self.require_armed_ack:
            identity["turn_sync"] = "scoped_armed_ack"
        return identity

    def start(self, *, submit_reply, on_error):
        self._submit, self._on_error = submit_reply, on_error
        self._cancelled.clear()
        try:
            self.output.start()  # Silence only; missing driver fails before tap startup.
            self._start_reply(self._generation)
        except Exception:
            self.cancel()
            raise

    def _start_reply(self, generation):
        def submit(*args, **kwargs):
            with self._lock:
                current = generation == self._generation and not self._cancelled.is_set()
            return self._submit(*args, **kwargs) if current else True
        def failed(reason):
            with self._lock:
                current = generation == self._generation and not self._cancelled.is_set()
            if current:
                self._on_error(reason)
        self.reply_backend.start(submit_reply=submit, on_error=failed)
        if self.require_armed_ack and getattr(self.reply_backend, "armed_ack_supported", False) is not True:
            raise RuntimeError("Selected reply worker does not support scoped arming acknowledgements")

    def interrupt(self, _turn_id):
        """Discard local audio and restart only the unchanged reply worker.

        BlackHole remains present and silent. New recordings stay in the
        bounded RelaySession queue until offer() can use the fresh worker.
        This cannot stop generation/playback inside the actual Your dot app.
        """
        with self._lock:
            if self._cancelled.is_set() or self.should_stop():
                raise RuntimeError("Cannot interrupt a stopped relay")
            if self._restarting:
                return  # No new offer was possible during the existing restart.
            self._generation += 1
            generation = self._generation
            self._restarting = True
            self._restart_deadline = time.monotonic() + 30
            self._restart_thread = threading.Thread(target=self._restart_reply,
                                                     args=(generation,), daemon=True)
            self._restart_thread.start()

    def _restart_reply(self, generation):
        try:
            with self._restart_gate:
                if self._cancelled.is_set():
                    return
                self.output.purge()
                self.reply_backend.cancel()
                if not self.reply_backend.stopped or self.reply_backend.last_error:
                    raise RuntimeError("Old reply worker cleanup could not be verified")
                if self._cancelled.is_set() or self.should_stop():
                    return
                if time.monotonic() >= self._restart_deadline:
                    raise RuntimeError("Reply worker restart timed out")
                self._start_reply(generation)  # Same backend configuration/binary/PIDs.
                if self._cancelled.is_set() or self.should_stop():
                    self.reply_backend.cancel()
                    return
                if not self.reply_backend.ready or time.monotonic() >= self._restart_deadline:
                    raise RuntimeError("Reply worker restart was not ready in time")
        except Exception:
            with self._lock:
                self.last_error = "Reply worker restart failed"
            if not self._cancelled.is_set():
                self._on_error(self.last_error)
        finally:
            with self._lock:
                if generation == self._generation:
                    self._restarting = False

    def offer(self, recording):
        from relay_backend import validate_recording
        turn, pcm = validate_recording(recording)
        try:
            with self._lock:
                if not self.ready or self._restarting or self.should_stop():
                    return False
                token = self.output.reserve()  # Fresh UID/layout check and one slot reserved.
                if token is None:
                    return False
                if self.should_stop():
                    self.output.release(token)
                    return False
                if not self.reply_backend.offer(recording):
                    self.output.release(token)
                    return False
                if self.should_stop():
                    pass  # Cancel after releasing the reply callback's lock.
                elif self.require_armed_ack:
                    if (getattr(self.reply_backend, "armed_ack_supported", False) is not True
                            or not self.reply_backend.wait_armed(turn, timeout=2)):
                        raise RuntimeError("Reply worker did not acknowledge the accepted turn")
                    if not self.should_stop() and not self._cancelled.is_set():
                        self.output.play(token, pcm, armed=True)
                        if getattr(self.reply_backend, "input_started_supported", False):
                            self.reply_backend.input_started(turn)
                        return True
                else:
                    self.output.play(token, pcm)
                    if getattr(self.reply_backend, "input_started_supported", False):
                        self.reply_backend.input_started(turn)
                    return True
        except Exception:
            # Teardown may drain callbacks that acquire self._lock. Do it only
            # after releasing that lock, and never retry accepted recordings.
            self.cancel()
            raise
        self.cancel()
        return False

    def cancel(self):
        self._cancelled.set()
        try:
            self.output.cancel()  # Purge/stop before waiting for reply cleanup.
        finally:
            if not self._restart_gate.acquire(timeout=35):
                self.last_error = "Reply restart cleanup exceeded its deadline"
                raise TimeoutError(self.last_error)
            try:
                self.reply_backend.cancel()
            finally:
                self._restart_gate.release()
        self.last_error = self.last_error or self.reply_backend.last_error or self.output.error


def worker(uid):
    import numpy as np
    import sounddevice as sd
    from coreaudio_tap import Native
    native = Native()
    identity = verify_hal(native, uid)
    index = output_index(sd.query_devices(), sd.query_hostapis())
    library = C.CDLL(sd._libname)
    get_device = library.PaMacCore_GetStreamOutputDevice
    get_device.argtypes, get_device.restype = [C.c_void_p], C.c_uint32
    stop, lock = threading.Event(), threading.Lock()
    signals = lambda *_: stop.set()
    signal.signal(signal.SIGTERM, signals)
    signal.signal(signal.SIGINT, signals)
    commands = queue.Queue(maxsize=2)
    pending, position, lead, reserved, counter = None, 0, 0, None, 0

    def render(data, frames, _timing, status):
        nonlocal pending, position, lead
        data.fill(0)
        if stop.is_set():
            return
        with lock:
            if status:
                stop.set()
                return
            offset = min(frames, lead)
            lead -= offset
            if pending is not None and offset < frames:
                count = min(frames - offset, len(pending) - position)
                data[offset:offset + count] = pending[position:position + count]
                position += count
                if position == len(pending):
                    pending.fill(0)
                    pending = None

    def read():
        try:
            while not stop.is_set():
                line = sys.stdin.readline(MAX_LINE + 1)
                if not line:
                    stop.set()
                    return
                if len(line) > MAX_LINE or not line.endswith("\n"):
                    raise ValueError("Command exceeds limit")
                commands.put(json.loads(line), timeout=1)
                line = None
        except Exception:
            stop.set()

    def emit(value):
        print(json.dumps(value, separators=(",", ":")), flush=True)

    # Open stopped, verify the public PortAudio CoreAudio ID, then start zeros.
    stream = sd.OutputStream(device=index, samplerate=RATE, channels=2, dtype="float32",
                             blocksize=480, callback=render)
    try:
        device = get_device(int(sd._ffi.cast("uintptr_t", stream._ptr)))
        fresh = verify_hal(native, uid)
        if device != identity["device_id"] or fresh != identity:
            raise RuntimeError("Opened output does not match the exact HAL identity")
        stream.start()
        threading.Thread(target=read, daemon=True).start()
        emit({"ready": identity})
        while not stop.is_set():
            try:
                command = commands.get(timeout=.1)
            except queue.Empty:
                continue
            if set(command) == {"reserve"} and command["reserve"] is True:
                if verify_hal(native, uid) != identity:
                    raise RuntimeError("HAL device changed before dispatch")
                with lock:
                    token = None
                    if reserved is None and pending is None:
                        counter += 1
                        token = reserved = counter
                emit({"reserved": token})
            elif set(command) == {"release"} and command["release"] == reserved and reserved is not None:
                reserved = None
                emit({"released": True})
            elif set(command) == {"purge"} and command["purge"] is True:
                with lock:
                    if pending is not None:
                        pending.fill(0)
                    pending, position, lead, reserved = None, 0, 0, None
                    counter += 1
                emit({"purged": True})
            elif (set(command) == {"play", "pcm"}
                  or set(command) == {"play", "pcm", "armed"} and command["armed"] is True
                  ) and command["play"] == reserved and reserved is not None:
                if verify_hal(native, uid) != identity:
                    raise RuntimeError("HAL device changed after slot reservation")
                pcm = base64.b64decode(command["pcm"], validate=True)
                stereo = stereo_samples(pcm)
                with lock:
                    pending, position, lead, reserved = (
                        stereo, 0, 0 if command.get("armed") is True else LEAD_FRAMES, None)
                pcm = stereo = None
                command.clear()
                emit({"queued": True})
            else:
                raise RuntimeError("Unexpected HAL output command")
    finally:
        stop.set()
        with lock:
            if pending is not None:
                pending.fill(0)
            pending = None
        stream.abort()
        stream.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--uid", choices=[UID])
    args = parser.parse_args(argv)
    if not args.worker or args.uid != UID:
        parser.print_help()
        return 0
    try:
        worker(args.uid)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:240]}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
