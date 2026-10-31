"""Single-use, authenticated WebSocket transport behind a local TLS tunnel.

The listener is deliberately fixed to IPv4 loopback. The public tunnel must
provide WSS and validate TLS at the device. No tunnel, audio, or USB is opened
on import. Each accepted peer gets a fresh Client; reconnect never retries PCM.
"""
from contextlib import contextmanager
import hmac
import logging
import os
from pathlib import Path
import queue
import re
import socket
import stat
import threading
import time

from transport import AudioTimeout, Client, ProtocolError, TransportError

PATH = "/dot-watch/v1"
SUBPROTOCOL = "dotwatch.v1"
FRAME_BYTES = 4096
HOST = "127.0.0.1"
PORT = 8772


def read_token(path):
    """Read only an owner-only regular token file; never echo its contents."""
    descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise ValueError("The pairing token must be an owned regular file with mode 0600")
        data = source.read(66)
    if not re.fullmatch(rb"[0-9a-f]{64}\n?", data):
        raise ValueError("Invalid pairing token file format")
    return data.rstrip(b"\n").decode("ascii")


class WebSocketStream:
    """Serial-like bytes with one reader and a deadline-bounded send worker.

    No socket read timeout is changed: idle read timeouts belong to recv(). A
    blocked send is interrupted by shutting down its socket, not by waiting
    forever for a WebSocket closing handshake or for the peer to drain audio.
    """
    supports_burst_playback = True  # Still requires the device's play_burst=1 capability.

    def __init__(self, websocket, *, read_timeout=.05, write_timeout=1.0):
        self.websocket = websocket
        self.read_timeout, self.write_timeout = read_timeout, write_timeout
        self.closed = threading.Event()
        self._failure = None
        self._response_timeout = None
        self._failure_lock = threading.Lock()
        self._buffer = bytearray()
        self._outgoing = queue.Queue(maxsize=1)
        self._write_lock = threading.Lock()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    @property
    def failure(self):
        with self._failure_lock:
            if self._failure is None:
                return None
            result = dict(self._failure)
            if self._response_timeout is not None:
                result["response_timeout"] = dict(self._response_timeout)
            return result

    def _receive_queue_state(self):
        result = {}
        assembler = getattr(self.websocket, "recv_messages", None)
        paused = getattr(assembler, "paused", None)
        if type(paused) is bool:
            result["receive_flow_paused"] = paused
        frames = getattr(assembler, "frames", None)
        if isinstance(frames, queue.SimpleQueue):
            result["queued_frames"] = frames.qsize()
        return result

    def note_response_timeout(self):
        # The library clears its paused flag while closing. Capture the first
        # failed command before cleanup to preserve meaningful queue evidence.
        snapshot = {"at": time.monotonic(), **self._receive_queue_state()}
        with self._failure_lock:
            if self._response_timeout is None:
                self._response_timeout = snapshot

    def close(self, reason=None, error=None):
        if reason is not None:
            # Keep the first local cause plus numeric WebSocket close codes.
            # Exception text / peer close reasons may contain private content.
            with self._failure_lock:
                if self._failure is None:
                    self._failure = {"reason":reason, "at":time.monotonic()}
                    if error is not None:
                        self._failure["error_type"] = type(error).__name__
                        for name in ("rcvd", "sent"):
                            code = getattr(getattr(error, name, None), "code", None)
                            # websockets uses IntEnum for locally generated
                            # closes (including keepalive timeout: 1011).
                            if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 65535:
                                self._failure[name + "_code"] = int(code)
                        # Recognize only the library's fixed local timeout,
                        # never retain arbitrary local or peer reason strings.
                        sent = getattr(error, "sent", None)
                        if (getattr(sent, "code", None) == 1011
                                and getattr(sent, "reason", None) == "keepalive ping timeout"):
                            self._failure["local_keepalive_timeout"] = True
                    # Numeric/boolean receive-queue metadata helps distinguish
                    # network silence from locally paused WebSocket reads.
                    self._failure.update(self._receive_queue_state())
        with self._write_lock:
            self.closed.set()
            self._buffer.clear()
            while True:
                try:
                    data, completed, error = self._outgoing.get_nowait()
                except queue.Empty:
                    break
                data = None
                error.append(True)
                completed.set()
        # shutdown bypasses any send-context lock held by the blocked writer.
        try:
            self.websocket.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.websocket.socket.close()
        except OSError:
            pass

    def _write_loop(self):
        while not self.closed.is_set():
            try:
                data, completed, error = self._outgoing.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if self.closed.is_set():
                    raise TransportError("Wi-Fi connection is closed")
                self.websocket.send(data)
            except Exception as exc:
                error.append(True)
                self.close("send_failed", exc)
            finally:
                data = None
                completed.set()

    def _receive(self, timeout):
        if self.closed.is_set():
            raise TransportError("Wi-Fi connection is closed")
        try:
            message = self.websocket.recv(timeout=timeout)
        except TimeoutError:
            return None
        except Exception as exc:
            self.close("peer_disconnected", exc)
            raise TransportError("Wi-Fi peer disconnected") from None
        if not isinstance(message, bytes) or not 0 < len(message) <= FRAME_BYTES:
            self.close("invalid_frame")
            raise ProtocolError("Expected one bounded binary WebSocket message")
        return message

    def read(self, count):
        if not self._buffer:
            message = self._receive(self.read_timeout)
            if message is None:
                return b""
            self._buffer.extend(message)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def reset_input_buffer(self):
        self._buffer.clear()
        # Drain only data already received. An unsolicited endless stream is
        # rejected instead of consuming unbounded time/memory before a command.
        for _ in range(16):
            if self._receive(0) is None:
                return
        self.close("unsolicited_data")
        raise ProtocolError("Excess unsolicited Wi-Fi data")

    def write(self, data):
        if self.closed.is_set():
            raise TransportError("Wi-Fi connection is closed")
        if not isinstance(data, bytes) or not 0 < len(data) <= FRAME_BYTES:
            raise ValueError("Write must contain 1–4096 bytes")
        completed, error = threading.Event(), []
        try:
            with self._write_lock:
                if self.closed.is_set():
                    raise TransportError("Wi-Fi connection is closed")
                self._outgoing.put_nowait((data, completed, error))
        except queue.Full:
            self.close("concurrent_write")
            raise TransportError("Concurrent Wi-Fi write rejected") from None
        if not completed.wait(self.write_timeout):
            self.close("write_timeout")
            raise AudioTimeout("Wi-Fi write timed out; payload was not retried")
        if error:
            raise TransportError("Wi-Fi write failed; payload was not retried")
        return len(data)


class WifiListener:
    """One authenticated peer for this listener's entire lifetime.

    HTTP request headers and payloads are never logged, including on malformed
    handshakes. Four bounded handshakes may run concurrently; only one peer may
    complete an authenticated upgrade. A disconnect requires a new listener.
    """
    def __init__(self, token, *, port=PORT):
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("Invalid pairing token")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid loopback port")
        self._authorization = ("Bearer " + token).encode("ascii")
        self.port = port
        self._claimed = False
        self._claim_lock = threading.Lock()
        self._accepted = queue.Queue(maxsize=1)
        self._stream = None
        self._server = self._thread = None
        self._stop = threading.Event()

    def _process_request(self, connection, request):
        from http import HTTPStatus
        try:
            authorization = request.headers.get_all("Authorization")
            protocols = request.headers.get_all("Sec-WebSocket-Protocol")
            valid = (request.path == PATH and not request.headers.get_all("Origin")
                     and len(authorization) == 1 and len(protocols) == 1
                     and protocols[0] == SUBPROTOCOL
                     and hmac.compare_digest(authorization[0].encode("utf-8"), self._authorization))
        except Exception:
            valid = False
        if not valid:
            return connection.respond(HTTPStatus.FORBIDDEN, "Connection rejected.\n")
        with self._claim_lock:
            if self._claimed or self._stop.is_set():
                return connection.respond(HTTPStatus.CONFLICT, "Session already claimed.\n")
            # A failed upgrade consumes this claim too: never swap identities
            # behind a controller that may have already prepared audio.
            self._claimed = True
        return None

    def _handle(self, websocket):
        if websocket.subprotocol != SUBPROTOCOL:
            return
        stream = WebSocketStream(websocket)
        self._stream = stream
        self._accepted.put_nowait(stream)
        while not self._stop.is_set() and not stream.closed.wait(.1):
            pass
        stream.close()

    def start(self):
        from websockets.sync.server import ServerConnection, serve
        if self._server is not None:
            raise RuntimeError("Listener already started")
        slots = threading.BoundedSemaphore(4)
        class LimitedConnection(ServerConnection):
            def __init__(self, sock, protocol, **kwargs):
                if not slots.acquire(blocking=False):
                    sock.close()
                    raise RuntimeError("Handshake capacity reached")
                try:
                    super().__init__(sock, protocol, **kwargs)
                except BaseException:
                    slots.release()
                    raise

            def handshake(self, *args, **kwargs):
                try:
                    return super().handshake(*args, **kwargs)
                finally:
                    slots.release()
        # Use an isolated logger; global debug logging cannot reveal auth or PCM.
        logger = logging.Logger("dotwatch.wifi.silent")
        logger.disabled = True
        self._server = serve(self._handle, HOST, self.port, origins=[None],
            subprotocols=[SUBPROTOCOL], process_request=self._process_request,
            compression=None, server_header=None, open_timeout=3, close_timeout=.5,
            ping_interval=5, ping_timeout=5, max_size=FRAME_BYTES, max_queue=4,
            create_connection=LimitedConnection, logger=logger, backlog=4)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def accept(self, timeout=60):
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            try:
                return self._accepted.get(timeout=min(.1, max(0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise AudioTimeout("No authenticated StopWatch connected before timeout") from None
        raise TransportError("Wi-Fi listener closed")

    def close(self):
        self._stop.set()
        if self._stream is not None:
            self._stream.close()
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=1)


@contextmanager
def open_wifi_device(token_file, *, port=PORT, accept_timeout=60, on_listening=None, burst_playback=False):
    if type(burst_playback) is not bool:
        raise ValueError("burst_playback must be a boolean")
    listener = WifiListener(read_token(token_file), port=port)
    try:
        listener.start()
        if on_listening:
            on_listening(listener.port)
        stream = listener.accept(accept_timeout)
        stream.supports_burst_playback = burst_playback
        yield Client(stream)
    finally:
        listener.close()
