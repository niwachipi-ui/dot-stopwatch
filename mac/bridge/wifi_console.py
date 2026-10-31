"""HAL-only Wi-Fi console with fresh, locked recovery after network loss.

No provider, listener, thread, signal handler, or device is opened on import.
The generic console retains its distinct ephemeral-provider lifetime policy.
"""
import json
import queue
import signal
import sys
import threading
import time

from transport import AudioTimeout, Client
from wifi_transport import WifiListener, read_token


def stopped_audio(controller):
    """Positive teardown evidence, never inferred merely from a paused label."""
    try:
        if controller._native_failed.is_set():
            return False
        backend = controller.backend
        if backend is not None and (not backend.stopped or backend.last_error):
            return False
        if controller.sink is not None and controller.sink.enabled:
            return False
        if controller.session is not None:
            state = controller.session.snapshot()
            if (state["running"] or state["pending_reply_bytes"] or state["pending_recording_bytes"]
                    or state["playing_id"] is not None or state["queued_play_ids"]):
                return False
        return True
    except Exception:
        return False


def recoverable_link_failure(controller, primary_error=None):
    if (controller.state != "paused"
            or controller.reason not in {"watch_control_failed", "transport_or_audio_error"}
            or not stopped_audio(controller)):
        return False
    if controller.reason == "transport_or_audio_error" and controller.session is not None:
        error = controller.session.snapshot().get("last_error") or {}
        primary_error = error.get("type")
    if primary_error not in {"AudioTimeout", "TransportError"}:
        return False
    failure = controller.client.transport_diagnostics()
    if not isinstance(failure, dict) or failure.get("reason") not in {
            "peer_disconnected", "send_failed", "write_timeout", "command_timeout"}:
        return False
    # Protocol violations and authentication/shape errors are never retried.
    return all(failure.get(key) in {None, 1000, 1001, 1006, 1011}
               for key in ("rcvd_code", "sent_code"))


class WifiConsole:
    """One control thread and one console reader across all link generations."""
    def __init__(self, config, *, token_file, port, accept_timeout, burst_playback,
                 controller_factory, listener_factory=WifiListener, token_reader=read_token,
                 clock=time.monotonic, emit=print):
        self.config, self.token_file, self.port = config, token_file, port
        self.accept_timeout, self.burst_playback = accept_timeout, burst_playback
        self.controller_factory, self.listener_factory = controller_factory, listener_factory
        self.token_reader, self.clock, self.emit = token_reader, clock, emit
        self.commands = queue.Queue(maxsize=8)
        self.stop_requested = threading.Event()
        self.stop_reason = None
        self.controller = self.listener = None
        self.generation = 0
        self.prepared_once = False
        self.restoring = False
        self.reconnect_attempts = 0
        self.retry_at = 0.0
        self.accept_deadline = 0.0
        self.finished = False
        self.exit_code = 4
        self.primary_error = None
        self.close_observe_deadline = None

    def report(self, event, **values):
        self.emit(json.dumps({"event": event, "generation": self.generation, **values}, ensure_ascii=False))

    def request_stop(self, reason="stop_requested"):
        # Console-reader/signal context: never operate network or native audio.
        if not self.stop_requested.is_set():
            self.stop_reason = reason
        self.stop_requested.set()
        controller = self.controller
        if controller is not None:
            controller.pause_requested.set()

    def submit(self, command):
        if command in {"stop", "call-ended"}:
            self.request_stop(command)
            return
        try:
            self.commands.put_nowait((self.generation, command))
        except queue.Full:
            self.request_stop("console_overflow")

    def _close_listener(self):
        if self.listener is not None:
            listener, self.listener = self.listener, None
            listener.close()

    def close(self, reason="console_exit"):
        if self.finished:
            return
        try:
            if self.controller is not None:
                self.controller.pause(reason)
        finally:
            try:
                self._close_listener()
            finally:
                self.finished = True

    def _attach(self, stream):
        stream.supports_burst_playback = self.burst_playback
        self.generation += 1
        self.primary_error = self.close_observe_deadline = None
        self.controller = self.controller_factory(Client(stream), self.config)
        attached = self.controller
        original_report = self.controller.report
        def report(event, **values):
            if (self.controller is attached and event in {"watch_control_error", "prepared_state_error"}
                    and self.primary_error is None):
                self.primary_error = values.get("error_type")
                if self.primary_error == "AudioTimeout":
                    note = getattr(stream, "note_response_timeout", None)
                    if callable(note):
                        try:
                            note()
                        except Exception:
                            pass  # Diagnostic collection cannot interrupt audio teardown.
            original_report(event, **values)
        self.controller.report = report

    def _observe_failed_link(self):
        controller = self.controller
        if (controller.state != "paused" or not stopped_audio(controller)
                or controller.reason not in {"watch_control_failed", "transport_or_audio_error"}):
            return
        primary = self.primary_error
        if controller.reason == "transport_or_audio_error" and controller.session is not None:
            primary = (controller.session.snapshot().get("last_error") or {}).get("type")
        if primary not in {"AudioTimeout", "TransportError"} or controller.client.transport_diagnostics():
            return
        if self.close_observe_deadline is None:
            self.close_observe_deadline = self.clock() + 10.0
        stream = controller.client.connection
        try:
            stream.read(1)  # At most its 50ms timeout; discard, never replay or interpret as a new response.
        except Exception:
            pass  # The stream retains bounded close metadata; classification below remains strict.
        if self.clock() >= self.close_observe_deadline and not controller.client.transport_diagnostics():
            stream.close("command_timeout")  # Invalidate the owned link before accepting any new peer.

    def _schedule_retry(self):
        self._close_listener()
        self.controller = None
        self.generation += 1  # Invalidates already queued start/prepare commands.
        self.reconnect_attempts += 1
        delay = min(10.0, 2.0 ** min(self.reconnect_attempts - 1, 4))
        self.retry_at = self.clock() + delay
        self.report("wifi_reconnect_wait", retry_seconds=delay,
                    message="音声処理の停止を確認しました。Wi-Fi再接続後も録音はロックし、新しいONを待ちます。")

    def _commands(self):
        # Bounded work leaves stop/accept/poll responsive even under input spam.
        for _ in range(8):
            try:
                generation, command = self.commands.get_nowait()
            except queue.Empty:
                return
            if self.stop_requested.is_set():
                return
            controller = self.controller
            if generation != self.generation or controller is None:
                self.report("command_ignored", message="接続待ちです。接続後に入力してください。")
                continue
            try:
                if command == "prepare":
                    controller.prepare()
                    self.prepared_once = controller.state == "prepared"
                elif command == "start":
                    controller.start()
                elif command == "status":
                    controller.status()
                else:
                    self.report("command_error", message="使用できる入力: prepare / start / stop / status / call-ended")
            except Exception as exc:
                controller.report("command_error", error_type=type(exc).__name__, message=str(exc)[:240])

    def _verify_restored_off(self):
        controller = self.controller
        info = controller.client.info()
        controller._healthy(info, "1")
        if (info.boot != controller.boot or "call_req" not in info.fields
                or info.fields.get("call_want") != "0" or info.fields.get("call_state") != "off"
                or info.fields.get("call_lease_ms") != "0"):
            raise RuntimeError("再接続後の新しい録音ロック・OFF状態を確認できません。")

    def step(self):
        """One bounded accept/backoff/control step; caller owns the pacing."""
        if self.finished:
            return
        if self.stop_requested.is_set():
            self.close(self.stop_reason or "stop_requested")
            if self.controller is None or stopped_audio(self.controller):
                self.exit_code = 130 if self.stop_reason == "console_interrupted" else 0
            return
        self._commands()
        if self.stop_requested.is_set():
            return
        if self.controller is not None:
            self.controller.poll()
            if self.prepared_once and not self.stop_requested.is_set():
                self._observe_failed_link()
            if (not self.stop_requested.is_set() and self.prepared_once
                    and recoverable_link_failure(self.controller, self.primary_error)):
                self.restoring = True
                self._schedule_retry()
            return
        if self.clock() < self.retry_at:
            return
        if self.listener is None:
            self.listener = self.listener_factory(self.token_reader(self.token_file), port=self.port)
            self.listener.start()
            self.accept_deadline = self.clock() + self.accept_timeout
            self.report("wifi_listening", port=self.listener.port, restoring=self.restoring)
        try:
            stream = self.listener.accept(min(.1, max(.001, self.accept_deadline - self.clock())))
        except AudioTimeout:
            if self.clock() >= self.accept_deadline:
                if self.restoring:
                    self._schedule_retry()
                else:
                    raise
            return
        if self.stop_requested.is_set():
            stream.close()
            return
        self._attach(stream)
        if self.stop_requested.is_set():
            self.controller.pause_requested.set()
            return
        self.report("wifi_connected", restoring=self.restoring)
        if self.restoring:
            # Fresh Client + controller + backend. prepare invalidates any old
            # ON and purges device buffers; it cannot unlock or replay PCM.
            try:
                if self.stop_requested.is_set():
                    self.controller.pause_requested.set()
                    return
                self.controller.prepare()
                if self.stop_requested.is_set():
                    return
                self._verify_restored_off()
            except Exception:
                self.controller.pause("reconnect_prepare_failed")
                raise
            self.restoring = False
            self.report("wifi_restored_locked", message="Wi-Fi再接続と録音ロックを確認しました。本体画面で新しくONを押してください。")


def run_wifi_console(config, **options):
    console = WifiConsole(config, **options)

    def read_commands():
        while not console.stop_requested.is_set():
            line = sys.stdin.readline(129)
            if not line:
                console.request_stop("console_eof")
                return
            if len(line) > 128:
                console.request_stop("console_overflow")
                return
            console.submit(line.strip())

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: console.request_stop("console_interrupted"))
        threading.Thread(target=read_commands, daemon=True).start()
        while not console.finished:
            console.step()
            console.stop_requested.wait(.02 if console.controller is not None
                                         and console.controller.state == "active" else .1)
        return console.exit_code
    finally:
        try:
            console.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
