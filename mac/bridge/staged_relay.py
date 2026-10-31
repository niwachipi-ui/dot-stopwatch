#!/usr/bin/env python3
"""Explicit prepare/start/stop/call-ended console; import/help are inert.

Preparation publishes one silent virtual microphone while the device stays
locked. The same provider survives ordinary relay errors and stop requests.
Only call-ended tears it down. A native-worker failure/crash can still remove
the device, and the consuming app's physical-input fallback is outside our
control. No defaults, real call, physical Mac mic or frozen app are modified.
"""
from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time

from relay import RelaySession
from relay_runner import Config, load_backend, pid_exists, positive_pid, resolve_audio_app
from transport import open_device


class StageError(RuntimeError):
    pass


class BlueButtonUnstable(StageError):
    """Valid button metadata is pressed or changed during release observation."""


class RetainedProviderSink:
    """Cancel future offers without deleting an input selected by a live call.

This intentionally differs from RelaySession's normal purge-and-close sink:
already-offered PCM (at most ten seconds) cannot be revoked in the frozen
worker. Session RAM is cleared, late replies are discarded, and only finish
calls the real backend.cancel(). Never reuse a cancelled session.
"""
    def __init__(self, backend):
        self.backend = backend
        self.enabled = True

    def offer(self, recording):
        return self.enabled and self.backend.offer(recording)

    def cancel(self):
        self.enabled = False

    def interrupt(self, turn_id):
        if self.enabled:
            self.backend.interrupt(turn_id)


class StagedController:
    def __init__(self, client, config, *, backend_factory=load_backend,
                 session_factory=RelaySession, emit=print, clock=time.monotonic,
                 sleep=time.sleep, check_pid=pid_exists):
        self.client, self.config = client, config
        self.backend_factory, self.session_factory = backend_factory, session_factory
        self.emit, self.clock, self.sleep, self.check_pid = emit, clock, sleep, check_pid
        self.backend = self.session = self.sink = None
        self.state, self.reason = "idle", "not_prepared"
        self.boot = self.identity = None
        self.pause_requested = threading.Event()
        self._native_failed = threading.Event()
        self._native_reason = ""
        self.finish_ok = False
        self._next_status_poll = 0.0

    def report(self, event, **values):
        self.emit(json.dumps({"event": event, "state": self.state, **values}, ensure_ascii=False))

    def status(self):
        """Read current metadata on this controller's already-owned serial link."""
        diagnostics = getattr(self.client, "transport_diagnostics", None)
        try:
            info = self.client.info()
            device = {key: info.fields.get(key) for key in
                      ("guard", "mic", "guard_pending", "blue", "worker", "fatal",
                       "blue_down", "blue_up", "call_req", "call_want", "call_state",
                       "audio_epoch", "record_epoch", "listen_state", "last_samples",
                       "last_peak", "play_id", "play_result", "play_count", "volume")}
            for key in ("play_queue", "play_slots", "play_accepted", "play_done", "play_burst", "last_clip", "last_rms", "last_dc", "last_end"):
                if key in info.fields:
                    device[key] = info.fields[key]
            device.update(id=info.id, boot=info.boot, state=info.state)
            self.report("status", reason=self.reason, microphone=self.identity, device=device,
                        transport=diagnostics() if callable(diagnostics) else None,
                        relay=self.session.snapshot() if self.session else None,
                        reply_timing=getattr(getattr(self.backend,"reply_backend",None),"last_turn_metrics",None))
        except Exception as exc:
            self.report("status", reason=self.reason, microphone=self.identity, device=None,
                        transport=diagnostics() if callable(diagnostics) else None,
                        error=type(exc).__name__, message="本体の現在の状態を読み取れませんでした。",
                        relay=self.session.snapshot() if self.session else None,
                        reply_timing=getattr(getattr(self.backend,"reply_backend",None),"last_turn_metrics",None))

    def _lock_device(self):
        try:
            info = self.client.set_guard(True)
            if (info.fields.get("guard") != "1" or info.fields.get("mic") != "0"
                    or info.fields.get("guard_pending") != "0"):
                raise StageError("Lock acknowledgement is incomplete")
            self.report("locked", message="本体の録音をロックしました。")
            return True
        except Exception:
            self.report("lock_unconfirmed", message="本体の録音停止を確認できません。接続と本体を確認してください。")
            return False

    @staticmethod
    def _healthy(info, guard):
        fields = info.fields
        required = {"guard": guard, "mic": "0", "guard_pending": "0",
                    "worker": "1", "fatal": "0", "sync": "0", "protocol_test": "1"}
        if any(fields.get(key) != value for key, value in required.items()) or info.state != 0:
            raise StageError("本体の録音ロック・停止・正常動作を確認できません。")

    def _backend_error(self, reason):
        # Called on a native-reader thread: never touch USB here.
        self._native_reason = str(reason)[:240]
        self._native_failed.set()
        self.pause_requested.set()

    def _submit_reply(self, turn_id, samples, **options):
        session = self.session
        if (self.pause_requested.is_set() or self.state != "active" or session is None
                or not session.snapshot()["running"]):
            return True  # Discard, avoiding backpressure teardown of the provider.
        if turn_id != session.snapshot()["turn_id"]:
            return True  # A deliberate interrupt invalidated this old reply.
        try:
            accepted = session.submit_reply(turn_id, samples, **options)
        except Exception:
            accepted = False
        if not accepted and turn_id == session.snapshot()["turn_id"]:
            # False would make the frozen backend remove the provider. Keep it
            # published, discard this reply, and ask the control thread to lock.
            self.pause_requested.set()
        return True

    @staticmethod
    def valid_input(identity):
        return (isinstance(identity, dict) and identity.get("public") is True
                and identity.get("name") == "Dot Watch Mic"
                and str(identity.get("uid", "")).startswith("local.dot-watch.")
                and identity.get("channels") == 1 and identity.get("sample_rate") == 48000)

    def prepare(self):
        if self.state != "idle":
            raise StageError("準備済みです。終了後に新しい実行を開始してください。")
        if (not self.config.reply_pids
                or any(type(pid) is not int or not 1 <= pid <= 2147483647
                       or not self.check_pid(pid) for pid in self.config.reply_pids)):
            raise StageError("返答元の正確なプロセスIDを確認してください。")
        self.state = "preparing"
        try:
            if not self._lock_device():
                raise StageError("録音停止を確認できないため準備を中止しました。")
            info = self.client.info()
            self._healthy(info, "1")
            self.boot = info.boot
            self.backend = self.backend_factory(self.config)
            self.backend.start(submit_reply=self._submit_reply, on_error=self._backend_error)
            identity = self.backend.public_input_identity
            if not self.backend.ready or not self.valid_input(identity):
                raise StageError("準備した仮想マイクの識別情報を確認できません。")
            info = self.client.info()
            self._healthy(info, "1")
            if info.boot != self.boot:
                raise StageError("準備中に本体が再起動しました。")
            self.identity = dict(identity)
            self.state = "prepared"
            self.report("ready", microphone=dict(identity), reply_pids=list(self.config.reply_pids),
                        message=f"録音はロック中です。この{identity['name']}を選んで通話を準備し、青ボタンを離して start と入力してください。")
        except Exception:
            # A publisher may already exist even when preparation fails. Retain
            # it until the human confirms the call is closed.
            self.pause("prepare_failed")
            raise

    @staticmethod
    def _button_metadata(info):
        values = tuple(info.fields.get(key) for key in ("blue_down", "blue_up"))
        if (info.fields.get("blue") not in {"0", "1"}
                or any(not isinstance(value, str) or not value.isdecimal() for value in values)):
            raise StageError("青ボタンの状態・変化回数を確認できません。")
        return values

    def _released(self):
        deadline = self.clock() + 1.0
        edges = None
        while True:
            if self.pause_requested.is_set() or self._native_failed.is_set() or not self.backend.ready:
                raise StageError("開始が中止されました。")
            info = self.client.info()
            self._healthy(info, "1")
            if info.boot != self.boot:
                raise StageError("青ボタンの確認中に本体が再起動しました。")
            values = self._button_metadata(info)
            if info.fields["blue"] == "1":
                raise BlueButtonUnstable("青ボタンを離した安定状態を確認できません。録音はロック中です。")
            if edges is not None and values != edges:
                raise BlueButtonUnstable("青ボタンの揺らぎを検出しました。録音はロック中です。")
            edges = values
            if self.clock() >= deadline:
                return
            self.sleep(.1)

    def start(self):
        if self.state != "prepared":
            raise StageError("prepare 完了後、一度だけ開始できます。停止後は通話を終了してください。")
        try:
            self._released()
            current = self.backend.public_input_identity
            if not current or current.get("uid") != self.identity["uid"]:
                raise StageError("選択した仮想マイクが変わりました。開始しません。")
            if any(not self.check_pid(pid) for pid in self.config.reply_pids):
                raise StageError("指定した返答元プロセスが終了しました。")
            if self.pause_requested.is_set():
                raise StageError("開始が中止されました。")
            info = self._unlock_device()  # Only an explicit start/request unlocks.
            self._healthy(info, "0")
            if info.boot != self.boot or info.fields.get("blue") != "0":
                raise StageError("解除後の本体状態が変化しました。")
            self.sink = RetainedProviderSink(self.backend)
            self.session = self.session_factory(self.client, self.sink,
                                                should_stop=self.pause_requested.is_set)
            self.session.start()
            self.state = "active"
            self.report("started", microphone_uid=self.identity["uid"],
                        message=("通話中は自動で聞き取ります。青短押しで割り込み、画面OFFで停止します。"
                                 if info.fields.get("controls") == "21" else
                                 "青ボタンを押して話し、離してください。stop は録音停止、call-ended は通話終了後の後片付けです。"))
        except Exception as exc:
            try:
                if self._defer_start_error(exc):
                    return
            except Exception:
                pass  # A failed recovery check must still execute the original teardown.
            self.pause("start_rejected")
            raise

    def _defer_start_error(self, error):
        return False  # Default/ephemeral providers retain their fail-closed policy.

    def _unlock_device(self):
        """Hook for newer firmware's atomic, on-watch request acknowledgment."""
        return self.client.set_guard(False)

    def pause(self, reason="stop_requested"):
        if self.state == "finished":
            return
        self.pause_requested.set()
        self.state, self.reason = "paused", reason
        if self.sink is not None:
            self.sink.cancel()
        self._lock_device()  # Main/control thread only; promptly inhibit physical recording.
        if self.session is not None:
            self.session.stop(reason)
        self.report("call_end_required", provider_ready=bool(self.backend and self.backend.ready),
                    message="録音中継を停止しました。仮想マイクは維持します。既に渡した音声は最大10秒残る場合があります。Your dotの通話を終了してから call-ended と入力してください。")

    def _tick_session(self):
        self.session.tick()

    def _prepared_info(self):
        return self.client.info()

    def poll(self):
        if self.state in {"idle", "finished"}:
            return
        if self._native_failed.is_set() or (self.backend is not None and not self.backend.ready):
            if self.reason != "native_failed":
                self.pause("native_failed")
                self.report("urgent_call_end", message="仮想マイクの維持に失敗しました。すぐにYour dotの通話を終了してください。アプリ側の別マイクへの切替は防げません。")
            return
        if self.pause_requested.is_set():
            if self.state != "paused":
                self.pause()
            return
        if self.state == "active":
            try:
                self._tick_session()
                snapshot = self.session.snapshot()
                if not snapshot["running"]:
                    self.pause(snapshot["reason"])
            except Exception:
                self.pause("unexpected_relay_error")
        elif self.state == "prepared" and self.clock() >= self._next_status_poll:
            self._next_status_poll = self.clock() + 1.0
            info = None
            try:
                info = self._prepared_info()
                self._healthy(info, "1")
                if info.boot != self.boot:
                    raise StageError("Device rebooted")
            except Exception as exc:
                self.report("prepared_state_error", error_type=type(exc).__name__,
                            message=str(exc)[:180],
                            device={key: info.fields.get(key) for key in
                                    ("boot", "state", "guard", "mic", "guard_pending",
                                     "worker", "fatal", "sync", "protocol_test",
                                     "call_req", "call_want", "call_state")}
                            if info is not None else None)
                self.pause("device_state_changed")

    def finish(self):
        """Explicit human assertion that the consuming call has ended."""
        if self.state == "finished":
            return
        self.pause_requested.set()
        locked = self._lock_device()
        if self.session is not None:
            self.session.stop("call_ended")
        clean = True
        if self.backend is not None:
            try:
                self.backend.cancel()
                clean = self.backend.stopped and not self.backend.last_error
            except Exception:
                clean = False
        self.state = "finished"
        self.finish_ok = bool(locked and clean)
        self.report("finished", locked=locked, cleanup_verified=bool(clean),
                    message="通話終了後の後片付けを実行しました。" if locked and clean else "録音ロックまたは音声処理の終了確認が必要です。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="録音ロックを維持してDot Watch Micを準備する対話コンソール。")
    parser.add_argument("--run", action="store_true", help="コンソールを開く。prepareまで音声処理を開始しない")
    parser.add_argument("--port", default=Config.port)
    parser.add_argument("--reply-pid", action="append", type=positive_pid, default=[])
    parser.add_argument("--audio-app", help="既存の署名済みDot Watch Audio.app")
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    if not args.reply_pid or not args.audio_app:
        parser.error("--runには--reply-pidと--audio-appを明示してください。")
    if not sys.stdin.isatty():
        parser.error("対話端末で実行してください。通話終了の確認が必要です。")
    executable = resolve_audio_app(args.audio_app)
    config = Config(port=args.port, reply_pids=tuple(dict.fromkeys(args.reply_pid)), audio_app=executable)
    return run_console(config)


def run_console(config, *, controller_factory=StagedController, input_name="Dot Watch Mic",
                connector=None):
    """Shared explicit terminal lifecycle; caller has validated its CLI config."""
    commands = queue.Queue(maxsize=8)
    interrupted = threading.Event()
    console_closed = threading.Event()
    controller_ref = []

    def request_pause(*_):
        # Safe in the signal/console reader threads; USB stays on the main loop.
        if controller_ref:
            controller_ref[0].pause_requested.set()
        interrupted.set()

    def read_commands():
        while True:
            line = sys.stdin.readline(129)
            if not line:
                console_closed.set()
                request_pause()
                return
            if len(line) > 128:
                request_pause()
                while line and not line.endswith("\n"):
                    line = sys.stdin.readline(129)
                command = "invalid"
            else:
                command = line.strip()
            if command in {"stop", "call-ended"}:
                request_pause()
            try:
                commands.put_nowait(command)
            except queue.Full:
                request_pause()

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with (connector or open_device)(config.port) as client:
            for sig in previous:
                signal.signal(sig, request_pause)
            controller = controller_factory(client, config)
            controller_ref.append(controller)
            try:
                print(f"prepare → {input_name}を選択して通話を準備 → start。stop/Ctrl+Cで録音停止。通話を終了したらcall-ended。", flush=True)
                threading.Thread(target=read_commands, daemon=True).start()
                while controller.state != "finished":
                    if interrupted.is_set():
                        interrupted.clear()
                        controller.pause("console_interrupted")
                    if console_closed.is_set():
                        console_closed.clear()
                        controller.report("console_closed", message="端末入力が閉じました。録音を停止し、仮想マイクは意図的に維持します。まずYour dotの通話を終了してください。この端末でcall-endedを入力できないため、その後にプロセスと仮想マイクの後片付けが必要です。Ctrl+Cによる通常の一時停止では入力を続けられます。")
                    try:
                        command = commands.get(timeout=.02 if controller.state == "active" else .1)
                    except queue.Empty:
                        command = None
                    if command is not None:
                        try:
                            if command == "prepare": controller.prepare()
                            elif command == "start": controller.start()
                            elif command == "stop": controller.pause()
                            elif command == "call-ended": controller.finish()
                            elif command == "status": controller.status()
                            else: print("使用できる入力: prepare / start / stop / status / call-ended", flush=True)
                        except Exception as exc:
                            controller.report("command_error", message=str(exc)[:240])
                    controller.poll()
            finally:
                if controller.state != "finished":
                    try:
                        controller.pause("console_exit")
                    except Exception:
                        pass  # Preserve the original failure if the terminal vanished.
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0 if controller.finish_ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
