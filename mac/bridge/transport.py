"""Bounded USB transport for physically triggered StopWatch recordings.

No method starts a microphone. Audio remains in memory unless the caller saves it.
No command is retried: audio_get consumes a recording even if reception fails.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
import wave
import zlib

RATE = 16000
MAX_AUDIO_BYTES = 320000  # Playback remains capped at ten seconds per command.
MAX_CAPTURE_BYTES = RATE * 2 * 60
MAX_STREAM_CHUNK_BYTES = 64000
MAX_LINE = 1024
UINT32_MAX = (1 << 32) - 1
CALL_STATES = frozenset({"off", "wait", "ready", "active", "error"})
CALL_LEASE_MS = 30000


class TransportError(Exception):
    pass


class ProtocolError(TransportError):
    pass


class AudioTimeout(TransportError):
    pass


class NoRecording(TransportError):
    pass


class DeviceError(TransportError):
    pass


class GenerationChanged(DeviceError):
    """A deliberate device interrupt invalidated an in-flight audio turn."""
    pass


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & UINT32_MAX


def validate_pcm(data: bytes) -> None:
    if not data or len(data) > MAX_AUDIO_BYTES or len(data) % 2:
        raise ValueError("Audio must contain 1–160000 PCM16 mono samples (at most 10 seconds).")


def _uint(text: str, key: str) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", text):
        raise ProtocolError(f"Invalid {key}")
    value = int(text)
    if value > UINT32_MAX:
        raise ProtocolError(f"Invalid {key}")
    return value


def _hex(text: str, key: str) -> int:
    if not re.fullmatch(r"[0-9a-fA-F]{8}", text):
        raise ProtocolError(f"Invalid {key}")
    return int(text, 16)


def parse_fields(line: bytes, prefix: bytes) -> dict[str, str]:
    if not line.startswith(prefix + b" "):
        raise ProtocolError("Unexpected response")
    try:
        fields = {}
        for item in line[len(prefix) + 1:].decode("ascii").split():
            key, value = item.split("=", 1)
            if key in fields or not key or not value:
                raise ValueError()
            fields[key] = value
        return fields
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("Malformed response fields") from exc


@dataclass(frozen=True)
class AudioInfo:
    boot: str
    id: int
    bytes: int
    crc32: int
    available: bool
    consumed: bool
    state: int
    expires_ms: int
    fields: dict[str, str]

    @classmethod
    def parse(cls, line: bytes) -> "AudioInfo":
        fields = parse_fields(line, b"AUDIO_INFO")
        required = {"boot", "id", "bytes", "crc32", "available", "consumed", "state", "expires_ms"}
        if not required.issubset(fields):
            raise ProtocolError("Incomplete AUDIO_INFO")
        _hex(fields["boot"], "boot")
        size = _uint(fields["bytes"], "bytes")
        if size > MAX_CAPTURE_BYTES or size % 2:
            raise ProtocolError("Invalid recording size")
        if fields["available"] not in {"0", "1"} or fields["consumed"] not in {"0", "1"}:
            raise ProtocolError("Invalid recording flags")
        available, consumed = fields["available"] == "1", fields["consumed"] == "1"
        if available and (not size or consumed):
            raise ProtocolError("Contradictory recording flags")
        state = _uint(fields["state"], "state")
        if state > 7:
            raise ProtocolError("Unknown audio state")
        if "sync" in fields and fields["sync"] not in {"0", "1"}:
            raise ProtocolError("Invalid sync flag")
        if "guard" in fields and fields["guard"] not in {"0", "1"}:
            raise ProtocolError("Invalid microphone guard flag")
        for key in ("worker", "fatal", "guard_pending", "guard_test"):
            if key in fields and fields[key] not in {"0", "1"}:
                raise ProtocolError(f"Invalid {key} flag")
        call_keys = {"call_req", "call_want", "call_state", "call_lease_ms"}
        if call_keys.intersection(fields):
            if not call_keys.issubset(fields):
                raise ProtocolError("Incomplete call-control state")
            _uint(fields["call_req"], "call_req")
            if fields["call_want"] not in {"0", "1"} or fields["call_state"] not in CALL_STATES:
                raise ProtocolError("Invalid call-control state")
            if _uint(fields["call_lease_ms"], "call_lease_ms") > CALL_LEASE_MS:
                raise ProtocolError("Invalid call-control lease")
        if fields.get("controls") == "21":
            if not {"audio_epoch", "record_epoch", "listen_state"}.issubset(fields):
                raise ProtocolError("Incomplete hands-free controls")
            for key in ("audio_epoch", "record_epoch"):
                _uint(fields[key], key)
            if fields["listen_state"] not in {"idle", "listening", "ready", "waiting", "playing"}:
                raise ProtocolError("Invalid listening state")
        if fields.get("play_stream") == "1":
            if not {"play_queue", "play_slots", "play_accepted", "play_done"}.issubset(fields):
                raise ProtocolError("Incomplete streaming playback state")
            slots = _uint(fields["play_slots"], "play_slots")
            if not 1 <= slots <= 3 or _uint(fields["play_queue"], "play_queue") > slots:
                raise ProtocolError("Invalid streaming playback queue")
            for key in ("play_accepted", "play_done"):
                _uint(fields[key], key)
        return cls(fields["boot"].lower(), _uint(fields["id"], "id"), size,
                   _hex(fields["crc32"], "crc32"), available, consumed,
                   state, _uint(fields["expires_ms"], "expires_ms"), fields)


class Client:
    """One client owns an already-open, timeout-bounded serial connection.

    Configure serial read timeout <= 0.1 s and write timeout <= 1 s. Only one
    process may own the port. Use open_device() for the interprocess lock.
    """
    def __init__(self, connection, *, clock=time.monotonic, sleep=time.sleep,
                 command_timeout=3.0, inactivity_timeout=2.0, transfer_timeout=20.0):
        self.connection = connection
        self.clock, self.sleep = clock, sleep
        self.command_timeout = command_timeout
        self.inactivity_timeout = inactivity_timeout
        self.transfer_timeout = transfer_timeout
        self._buffer = bytearray()
        self._recover_at = 0.0
        self._boot = None

    def transport_diagnostics(self):
        """Bounded metadata retained by the transport after a lost connection."""
        return getattr(self.connection, "failure", None)

    def _begin(self, command: bytes) -> None:
        if self._recover_at:
            self._recover()
        self.connection.reset_input_buffer()
        self._buffer.clear()
        self._write(command + b"\n", self.clock() + self.command_timeout)

    def _recover(self) -> None:
        if self._recover_at > self.clock():
            self.sleep(self._recover_at - self.clock())
        self.connection.reset_input_buffer()
        self._buffer.clear()
        # audio_info is the only read-only query allowed during quarantine.
        # Re-read boot, since the device may have rebooted during the failure.
        try:
            self._write(b"audio_info\n", self.clock() + self.command_timeout)
            self._boot = AudioInfo.parse(self._response(b"AUDIO_INFO", self.clock() + self.command_timeout)).boot
            self._write(f"audio_sync {self._boot}\n".encode("ascii"), self.clock() + self.command_timeout)
            if self._response(b"AUDIO_SYNC", self.clock() + self.command_timeout) != b"AUDIO_SYNC":
                raise ProtocolError("Unexpected sync response")
            self._recover_at = 0.0
        except Exception:
            self._failed()
            raise

    def _failed(self) -> None:
        # Firmware drains malformed/late payloads until 300 ms of quiet.
        self._recover_at = self.clock() + 0.55
        self._buffer.clear()

    def _write(self, data: bytes, deadline: float, *, paced=False) -> None:
        position = 0
        paced_bytes = 0
        last_progress = self.clock()
        while position < len(data):
            if self.clock() >= min(deadline, last_progress + self.inactivity_timeout):
                raise AudioTimeout("USB write timed out; command was not retried.")
            chunk_size = min(4096 - paced_bytes if paced else 4096, len(data) - position)
            count = self.connection.write(data[position:position + chunk_size])
            if not isinstance(count, int) or count < 0 or count > chunk_size:
                raise ProtocolError("Invalid serial write result")
            if count:
                position += count
                paced_bytes += count
                last_progress = self.clock()
                if paced and paced_bytes >= 4096:
                    self.sleep(0.035)
                    paced_bytes = 0
            else:
                self.sleep(0.005)

    def _line(self, deadline: float) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE:
                    raise ProtocolError("Response line exceeds limit")
                line = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[:newline + 1]
                return line
            if len(self._buffer) > MAX_LINE:
                raise ProtocolError("Response line exceeds limit")
            if self.clock() >= deadline:
                raise AudioTimeout("No complete USB response; command was not retried.")
            part = self.connection.read(256)
            if part:
                self._buffer.extend(part)
            else:
                self.sleep(0.005)

    def _response(self, expected: bytes, deadline: float) -> bytes:
        for _ in range(32):
            line = self._line(deadline)
            if line == expected or line.startswith(expected + b" "):
                return line
            if line.startswith(b"AUDIO_ERROR"):
                # Device errors have fixed reason words; never dump binary/audio.
                reason = line[11:].strip()
                if not re.fullmatch(rb"[a-z_]{1,32}", reason):
                    reason = b"unspecified"
                if reason == b"audio_stale":
                    raise GenerationChanged("Device audio generation changed")
                if reason == b"timeout":
                    # Replace the failed link locked/OFF; never retry its PCM.
                    raise AudioTimeout("Device audio receive timed out; payload was not retried")
                raise DeviceError("Device rejected audio operation: " + reason.decode("ascii"))
        raise ProtocolError("Too many unrelated response lines")

    def _read_audio(self, size: int) -> bytes:
        output = bytearray()
        if self._buffer:
            count = min(size, len(self._buffer))
            output.extend(self._buffer[:count])
            del self._buffer[:count]
        deadline = self.clock() + self.transfer_timeout
        last_progress = self.clock()
        while len(output) < size:
            if self.clock() >= min(deadline, last_progress + self.inactivity_timeout):
                raise AudioTimeout("Recording reception timed out; consumed audio will not be retried.")
            part = self.connection.read(min(4096, size - len(output)))
            if part:
                output.extend(part)
                last_progress = self.clock()
            else:
                self.sleep(0.005)
        return bytes(output)

    def status(self) -> dict[str, str]:
        try:
            self._begin(b"status")
            return parse_fields(self._response(b"DOT_READY", self.clock() + self.command_timeout), b"DOT_READY")
        except Exception:
            self._failed()
            raise

    def info(self) -> AudioInfo:
        try:
            self._begin(b"audio_info")
            info = AudioInfo.parse(self._response(b"AUDIO_INFO", self.clock() + self.command_timeout))
            self._boot = info.boot
            if info.fields.get("sync") == "1":
                self._recover_at = self.clock() + 0.55
            return info
        except Exception:
            self._failed()
            raise

    def capture(self, info: AudioInfo | None = None) -> bytes:
        """Claim one completed recording of at most 60 seconds, without retry.

        The existing uint32 byte-count/CRC wire format needs no new command.
        Keep the transfer timeout unchanged: increasing the speech duration
        must not hold the sole control loop beyond its call-control lease.
        """
        info = self.info() if info is None else info
        if not info.available or info.consumed:
            raise NoRecording("No unconsumed blue-button recording is available.")
        if not 0 < info.bytes <= MAX_CAPTURE_BYTES or info.bytes % 2:
            raise ProtocolError("Audio payload exceeds bounds")
        try:
            self._boot = info.boot
            if info.fields.get("controls") == "21":
                if info.fields["record_epoch"] != info.fields["audio_epoch"]:
                    raise GenerationChanged("Recording belongs to an old audio generation")
                command = f"audio_get2 {self._scope(info)} {info.id}"
            else:
                command = f"audio_get {info.id}"
            self._begin(command.encode("ascii"))
            line = self._response(b"AUDIO_DATA", self.clock() + self.command_timeout)
            try:
                _, identifier, count, checksum = line.decode("ascii").split()
            except (ValueError, UnicodeDecodeError) as exc:
                raise ProtocolError("Malformed audio header") from exc
            size = _uint(count, "bytes")
            actual_crc = _hex(checksum, "crc32")
            if _uint(identifier, "id") != info.id or size != info.bytes or actual_crc != info.crc32:
                raise ProtocolError("Recording changed or header is inconsistent; claim was not retried.")
            if not size or size > MAX_CAPTURE_BYTES or size % 2:
                raise ProtocolError("Audio payload exceeds bounds")
            data = self._read_audio(size)
            if crc32(data) != actual_crc:
                raise ProtocolError("Recording CRC failed; consumed audio will not be retried.")
            return data
        except GenerationChanged:
            raise
        except Exception:
            self._failed()
            raise

    @staticmethod
    def _scope(info):
        return f"{info.boot} {info.fields['call_req']} {info.fields['audio_epoch']}"

    def listen(self, request: AudioInfo) -> AudioInfo:
        """Start one ON-authorized VAD cycle; never retry a lost command."""
        if (request.fields.get("controls") != "21" or request.fields.get("call_state") != "active"
                or request.fields.get("guard") != "0"):
            raise ValueError("A current active hands-free request is required")
        expected = (_uint(request.fields["audio_epoch"], "audio_epoch") + 1) & UINT32_MAX
        def verified(after):
            if (after.boot != request.boot or after.fields.get("call_req") != request.fields["call_req"]
                    or after.fields.get("audio_epoch") != str(expected)
                    or after.fields.get("call_state") != "active" or after.fields.get("guard") != "0"
                    or after.fields.get("listen_state") not in {"listening", "ready", "waiting"}):
                raise GenerationChanged("Listening generation changed")
            return after
        try:
            self._begin(f"audio_listen {self._scope(request)}".encode("ascii"))
            response = parse_fields(self._response(b"AUDIO_LISTEN", self.clock() + self.command_timeout), b"AUDIO_LISTEN")
            if response != {"epoch": str(expected)}:
                raise ProtocolError("Listening acknowledgment did not match")
            return verified(self.info())
        except AudioTimeout:
            # The command may have reached the device. Only a read-back proving
            # its exact successor generation may recover; never send it twice.
            self._failed()
            return verified(self.info())
        except GenerationChanged:
            raise
        except Exception:
            self._failed()
            raise

    def play(self, data: bytes, *, request: AudioInfo | None = None) -> None:
        """Queue validated PCM for playback. Success does not prove audibility."""
        validate_pcm(data)
        try:
            current = self.info()
            if current.fields.get("controls") == "21":
                if request is None:
                    raise ValueError("Generation-scoped playback is required")
                if self._scope(current) != self._scope(request):
                    raise GenerationChanged("Playback belongs to an old audio generation")
                command = f"audio_play2 {self._scope(request)} {len(data)} {crc32(data):08x}"
            else:
                command = f"audio_play {len(data)} {crc32(data):08x}"
            self._begin(command.encode("ascii"))
            ready = self._response(b"AUDIO_READY", self.clock() + self.command_timeout)
            if ready != b"AUDIO_READY":
                raise ProtocolError("Unexpected ready response")
            self._write(data, self.clock() + self.transfer_timeout, paced=True)
            queued = self._response(b"AUDIO_QUEUED", self.clock() + self.command_timeout)
            if queued != b"AUDIO_QUEUED":
                raise ProtocolError("Unexpected queued response")
        except GenerationChanged:
            raise
        except Exception:
            self._failed()
            raise

    def enqueue_play(self, data: bytes, *, request: AudioInfo) -> int:
        """Append to the epoch-scoped device queue, including during playback.

        The caller provides this control tick's metadata. The firmware checks
        guard/call/epoch atomically; no repeated round trip is needed before
        READY. A lost acknowledgement is never retried.
        """
        self.last_play_metrics = None
        validate_pcm(data)
        fields = request.fields
        if (len(data) > MAX_STREAM_CHUNK_BYTES or fields.get("play_stream") != "1"
                or fields.get("guard") != "0" or fields.get("mic") != "0"
                or fields.get("call_state") != "active"
                or int(fields["play_queue"]) >= int(fields["play_slots"])):
            raise ValueError("A current active playback queue with a free slot is required")
        expected_id = (int(fields["play_accepted"]) + 1) & UINT32_MAX
        burst = (fields.get("play_burst") == "1"
                 and getattr(self.connection, "supports_burst_playback", False) is True)
        began_at = self.clock()
        try:
            self._begin(f"audio_play3 {self._scope(request)} {len(data)} {crc32(data):08x}".encode("ascii"))
            if self._response(b"AUDIO_READY", self.clock() + self.command_timeout) != b"AUDIO_READY":
                raise ProtocolError("Unexpected stream ready response")
            ready_at = self.clock()
            # v25 WSS can hold a complete maximum-sized stream chunk. Keep
            # READY and CRC/epoch acceptance; older firmware and USB stay paced.
            self._write(data, self.clock() + self.transfer_timeout, paced=not burst)
            sent_at = self.clock()
            queued = parse_fields(self._response(b"AUDIO_QUEUED", self.clock() + self.command_timeout), b"AUDIO_QUEUED")
            if queued != {"id":str(expected_id)}:
                raise ProtocolError("Streaming playback acceptance did not match")
            self.last_play_metrics = {"id":expected_id, "burst":burst,
                "ready_ms":round((ready_at-began_at)*1000, 1),
                "send_ms":round((sent_at-ready_at)*1000, 1),
                "accept_ms":round((self.clock()-sent_at)*1000, 1)}
            return expected_id
        except GenerationChanged:
            raise
        except Exception:
            self._failed()
            raise

    def set_guard(self, enabled: bool) -> AudioInfo:
        """Lock/unlock physical recording; unlocking never starts a microphone.

        Firmware defaults to locked at every boot. Only an explicit caller
        action may unlock. An unlocked device still requires a fresh physical
        release and press; no blue-button event is synthesized here.
        """
        if not isinstance(enabled, bool):
            raise ValueError("Microphone guard must be a boolean.")
        try:
            before = self.info()
            if before.fields.get("guard") not in {"0", "1"}:
                raise ProtocolError("Firmware does not support the microphone guard.")
            if not enabled and (before.fields.get("worker") not in {None, "1"}
                                or before.fields.get("fatal") not in {None, "0"}
                                or before.fields.get("guard_pending") not in {None, "0"}):
                raise ProtocolError("Audio worker is not ready; recording remains locked.")
            value = int(enabled)
            self._begin(f"audio_guard {value}".encode("ascii"))
            expected = f"AUDIO_GUARD {value}".encode("ascii")
            if self._response(b"AUDIO_GUARD", self.clock() + self.command_timeout) != expected:
                raise ProtocolError("Microphone guard acknowledgement did not match.")
            after = self.info()
            if after.fields.get("guard") != str(value):
                raise ProtocolError("Microphone guard could not be verified.")
            if enabled and after.fields.get("mic") != "0":
                raise ProtocolError("Guard is set but microphone shutdown is not verified.")
            if after.fields.get("guard_pending") not in {None, "0"}:
                raise ProtocolError("Microphone shutdown acknowledgement is still pending.")
            return after
        except Exception:
            self._failed()
            raise

    def ack_call(self, request: AudioInfo, state: str, *, lease_ms=CALL_LEASE_MS) -> AudioInfo:
        """Acknowledge an existing on-watch request, never create one.

        The firmware compares both boot and request sequence atomically. Only
        an ACTIVE acknowledgment can enable physical PTT; the caller must
        verify the live call input and prepared backend before sending it.
        OFF is local relay shutdown, not proof that the app call has ended.
        """
        fields = request.fields
        if state not in CALL_STATES or "call_req" not in fields:
            raise ValueError("Unsupported call acknowledgment")
        sequence = _uint(fields["call_req"], "call_req")
        if fields.get("call_want") != ("0" if state == "off" else "1"):
            raise ValueError("Acknowledgment does not match the on-watch request")
        if type(lease_ms) is not int or (lease_ms != 0 if state == "off" else not 1000 <= lease_ms <= CALL_LEASE_MS):
            raise ValueError("Invalid call acknowledgment lease")
        try:
            self._begin(f"call_ack {request.boot} {sequence} {state} {lease_ms}".encode("ascii"))
            result = parse_fields(self._response(b"CALL_ACK", self.clock() + self.command_timeout), b"CALL_ACK")
            if result != {"boot": request.boot, "req": str(sequence), "state": state}:
                raise ProtocolError("Call acknowledgment did not match the request")
            after = self.info()
            if (after.boot != request.boot or after.fields.get("call_req") != str(sequence)
                    or after.fields.get("call_want") != fields["call_want"]
                    or after.fields.get("call_state") != state):
                raise ProtocolError("Call request changed during acknowledgment")
            if state == "active" and (after.fields.get("guard") != "0"
                                      or after.fields.get("guard_pending") != "0"):
                raise ProtocolError("Active PTT guard state could not be verified")
            if state != "active" and any(after.fields.get(key) != value for key, value in
                                        {"guard": "1", "guard_pending": "0", "mic": "0"}.items()):
                raise ProtocolError("Inactive call shutdown could not be verified")
            return after
        except Exception:
            self._failed()
            raise


@contextmanager
def open_device(port: str):
    """Lock USB with DTR asserted for CDC and RTS deasserted (no reset).

    Other bridge clients honor this lock. Existing wifi_setup/check_device tools
    do not: stop them before running a bridge client.
    """
    import serial
    digest = hashlib.sha256(os.path.realpath(port).encode()).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f"dot-watch-serial-{digest}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransportError("Another dot bridge process owns this USB port.") from exc
        device = serial.Serial(port=None, baudrate=115200, timeout=0.05, write_timeout=1)
        # ESP32-S3 HWCDC requires host DTR to acknowledge the CDC connection.
        # RTS stays low: do not apply the flashing/reset handshake.
        device.dtr = True
        device.rts = False
        device.port = port
        try:
            device.open()
            yield Client(device)
        finally:
            device.close()


def load_wav(path: Path) -> bytes:
    """Accept only bounded, uncompressed mono PCM16 16 kHz WAV."""
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, RATE, "NONE"):
            raise ValueError("WAV must be uncompressed mono PCM16 at 16000 Hz.")
        frames = source.getnframes()
        if not 0 < frames <= MAX_AUDIO_BYTES // 2:
            raise ValueError("WAV must be between one sample and 10 seconds long.")
        data = source.readframes(frames)
        if len(data) != frames * 2:
            raise ValueError("WAV sample data is truncated.")
        validate_pcm(data)
        return data


def save_wav(path: Path, data: bytes) -> None:
    validate_pcm(data)
    # Explicit caller-provided destination; never overwrite an existing file.
    with path.open("xb") as target:
        os.fchmod(target.fileno(), 0o600)
        with wave.open(target, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(RATE)
            output.writeframes(data)
