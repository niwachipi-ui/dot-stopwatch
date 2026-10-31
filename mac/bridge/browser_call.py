"""Authenticated loopback control for the user's local dot-call extension.

Only call state crosses this channel; no audio, page text, cookies or URLs.
The separate device tunnel cannot access this listener. Import opens nothing.
"""
import hmac
import json
import logging
import re
import secrets
import threading
import time

PORT = 8774
PATH = "/dot-call/v1"
LEASE_SECONDS = 8.0
# Native output + reply workers each have a 15-second startup ceiling.
# Browser/content still require a live 8-second lease from this socket worker.
WATCH_REFRESH_SECONDS = 35.0
STATES = frozenset({"starting", "active", "ended", "error"})


class BrowserCall:
    def __init__(self, token, extension_id, *, port=PORT, clock=time.monotonic, emit=None):
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("Invalid local control key")
        if not re.fullmatch(r"[a-p]{32}", extension_id):
            raise ValueError("Invalid extension ID")
        self._token, self.extension_id = token, extension_id
        self.port, self.clock = port, clock
        self.emit = emit or (lambda _: None)
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._server = self._thread = self._peer = None
        self._epoch = secrets.token_hex(16)
        self._seq = 1
        self._key = None
        self._want = False
        self._deadline = 0.0
        self._state, self._error = "offline", ""
        self._last_report = 0.0
        self._browser_ready = False
        self._ended_confirmed = False
        self._pending_on = False
        self._off_retry_at = None
        self._terminal = set()

    def _end(self, *, state="ended", error=""):
        if self._key is not None:
            self._terminal.add(self._key)
        if self._want:
            self._seq += 1
            self._ended_confirmed = False
        self._want = False
        self._pending_on = False
        self._state, self._error = state, error

    def request_on(self, key):
        if (not isinstance(key, tuple) or len(key) != 2
                or not all(isinstance(v, str) and 0 < len(v) <= 64 for v in key)):
            raise ValueError("Invalid watch request identity")
        with self._lock:
            if key in self._terminal:
                return
            if self._key != key:
                if len(self._terminal) >= 4096:
                    raise RuntimeError("Restart local call control after 4096 requests")
                if self._key is not None:
                    self._terminal.add(self._key)
                if self._want:
                    self._want = False
                    self._seq += 1
                    self._ended_confirmed = False
                self._key = key
                self._state, self._error = "starting", ""
                # Never queue an ON for a future browser connection.
                if self._peer is None or not self._browser_ready:
                    self._want = False
                    self._end(state="error", error="extension_offline")
                    return
                self._pending_on = True
                self._activate_pending()
                self._last_report = self.clock()
            if self._want or self._pending_on:
                self._deadline = self.clock() + WATCH_REFRESH_SECONDS

    def _activate_pending(self):
        if self._pending_on and self._ended_confirmed:
            self._pending_on = False
            self._want = True
            self._ended_confirmed = False
            self._seq += 1

    def request_off(self):
        with self._lock:
            self._end()

    def status(self, key=None):
        with self._lock:
            self._expire()
            if key is not None and key != self._key:
                state, error = ("ended", "") if key in self._terminal else ("idle", "")
            else:
                state, error = self._state, self._error
            return {"connected": self._peer is not None and self._browser_ready,
                    "state": state, "error": error}

    def _expire(self):
        now = self.clock()
        if (self._want or self._pending_on) and now >= self._deadline:
            self._end(state="error", error="watch_lease_expired")
        elif (self._want or self._pending_on) and now - self._last_report > LEASE_SECONDS:
            self._end(state="error", error="browser_state_stale")

    def _command(self):
        with self._lock:
            self._expire()
            # Retrying OFF is safe after a page reload or initial missing tab;
            # a failed ON is always consumed and needs a new physical request.
            if self._off_retry_at is not None and self.clock() >= self._off_retry_at:
                self._off_retry_at = None
                if not self._want:
                    self._seq += 1
            return {"type": "call", "epoch": self._epoch, "seq": self._seq,
                    "want": self._want, "lease_ms": int(LEASE_SECONDS * 1000)}

    def _state_message(self, message):
        if (not isinstance(message, dict)
                or set(message) - {"type", "epoch", "seq", "state", "error"}
                or message.get("type") != "state"
                or type(message.get("seq")) is not int
                or message.get("state") not in STATES
                or ("error" in message and (not isinstance(message["error"], str)
                    or not re.fullmatch(r"[a-z_]{0,48}", message["error"])))):
            raise ValueError("Invalid browser state")
        with self._lock:
            if message.get("epoch") != self._epoch or message["seq"] != self._seq:
                return  # Delayed ACK cannot affect a newer OFF/ON.
            self._last_report = self.clock()
            if self._want:
                if message["state"] in {"ended", "error"}:
                    self._end(state=message["state"], error="browser_call_ended")
                else:
                    self._state = message["state"]
            elif message["state"] == "error":
                if self._pending_on:
                    self._end(state="error", error="browser_end_failed")
                self._error = "browser_end_failed"
                self._browser_ready = False
                if self._off_retry_at is None:
                    self._off_retry_at = self.clock() + 3.0
            elif message["state"] == "ended":
                newly_ready = not self._browser_ready
                self._browser_ready = True
                self._ended_confirmed = True
                self._off_retry_at = None
                self._activate_pending()
                if newly_ready:
                    self.emit({"event": "chrome_extension_ready", "ready": True})

    def _handler(self, peer):
        accepted = False
        try:
            hello = json.loads(peer.recv(timeout=3))
            if (not isinstance(hello, dict) or set(hello) != {"type", "v", "token"}
                    or hello.get("type") != "hello" or type(hello.get("v")) is not int
                    or hello["v"] != 1 or not isinstance(hello.get("token"), str)
                    or not hmac.compare_digest(hello["token"], self._token)):
                peer.close(1008, "authentication failed")
                return
            with self._lock:
                if self._peer is not None or self._closed.is_set():
                    peer.close(1008, "already connected")
                    return
                self._end()
                self._epoch = secrets.token_hex(16)
                self._seq += 1
                self._peer = peer
                self._browser_ready = False
                self._ended_confirmed = False
                self._off_retry_at = None
                self._state, self._error = "idle", ""
                accepted = True
            self.emit({"event": "chrome_extension_connected", "ready": False})
            peer.send(json.dumps({"type": "ready", "v": 1, "epoch": self._epoch}))
            next_send = 0.0
            while not self._closed.is_set():
                # A single socket worker orders all commands and acknowledgments.
                if self.clock() >= next_send:
                    peer.send(json.dumps(self._command()))
                    next_send = self.clock() + .5
                try:
                    data = peer.recv(timeout=.1)
                except TimeoutError:
                    continue
                if not isinstance(data, str):
                    raise ValueError("Expected bounded JSON")
                value = json.loads(data)
                if value == {"type": "ping"}:
                    peer.send('{"type":"pong"}')
                else:
                    self._state_message(value)
        except Exception:
            # Do not log peer payloads, key material or third-party text.
            try:
                peer.close(1008, "local control closed")
            except Exception:
                pass
        finally:
            if accepted:
                with self._lock:
                    if self._peer is peer:
                        self._end(state="error", error="extension_disconnected")
                        self._peer = None
                        self._browser_ready = False
                self.emit({"event": "chrome_extension_disconnected", "ready": False})

    def start(self):
        from websockets.sync.server import serve
        from http import HTTPStatus
        def request(connection, request):
            if request.path != PATH:
                return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        logger = logging.Logger("dotwatch.browser-control")
        logger.addHandler(logging.NullHandler())
        self._server = serve(self._handler, "127.0.0.1", self.port,
                             origins=["chrome-extension://" + self.extension_id],
                             process_request=request, compression=None, max_size=2048,
                             max_queue=4, ping_interval=10, ping_timeout=5,
                             open_timeout=3, close_timeout=.3, logger=logger)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self.request_off()
        with self._lock:
            peer = self._peer
        if peer is not None:
            # The extension also owns an independent lease/disconnect teardown.
            # Let the sole writer deliver OFF before closing the socket.
            time.sleep(.55)
        self._closed.set()
        if peer is not None:
            peer.close(1000, "host stopped")
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2)

