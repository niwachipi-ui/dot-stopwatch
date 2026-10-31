#!/usr/bin/env python3
"""Explicit BlackHole2ch staged relay; default/help opens nothing.

Requires an already installed driver and user-selected actual call input.
250 ms dispatch lead-in is best effort: frozen reply worker has no armed ACK.
"""
import argparse
import os
from pathlib import Path
import sys

from relay_runner import Config, load_backend, positive_pid, resolve_audio_app
from staged_relay import BlueButtonUnstable, StageError, StagedController, run_console
from transport import CALL_LEASE_MS, MAX_AUDIO_BYTES, TransportError

UID, NAME = "BlackHole2ch_UID", "BlackHole 2ch"
ACTIVE_LEASE_REFRESH_SECONDS = 5.0
BUTTON_WAIT_SECONDS = 30.0
BROWSER_START_SECONDS = 30.0
BROWSER_STATES = frozenset({"starting", "active", "ended", "error", "offline", "idle"})
BROWSER_RESTARTABLE = frozenset({"browser_call_ended", "browser_call_failed",
                                "browser_call_lost", "browser_call_start_timeout"})
CALL_APP_BUNDLES = {
    "codex": frozenset({"com.openai.codex", "com.openai.codex.helper"}),
    "chrome": frozenset({"com.google.Chrome", "com.google.Chrome.helper"}),
}


class ButtonWaitExpired(StageError):
    """The same locked ON request exhausted its bounded button wait."""


class BrowserCallStopped(StageError):
    """A browser-only failure during the final guarded start observation."""
    def __init__(self, reason):
        if reason not in BROWSER_RESTARTABLE:
            raise ValueError("Unknown browser stop reason")
        self.reason = reason
        super().__init__("Chrome通話の開始条件が変わりました。録音は開始しません。")


def live_dot_input_verified(pids, *, call_app="codex"):
    """Read public routing metadata for the explicitly configured call PIDs.

    This neither starts a call nor accesses samples, UI, or application files.
    A PID that was reused by another application fails the bundle check.
    Chrome must be explicitly selected: its process tap may include audio from
    other tabs using the same browser audio process, not only the dot tab.
    """
    allowed_bundles = CALL_APP_BUNDLES.get(call_app)
    if allowed_bundles is None:
        return False
    folder = str(Path(__file__).resolve().parent.parent / "mac-audio")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    from coreaudio_tap import Native
    from process_input_check import check_process_inputs
    try:
        native = Native()
        result = check_process_inputs(native, UID, pids)
        return result["ok"] is True and bool(result["processes"]) and all(
            native.string(entry["process_object_id"], "pbid") in allowed_bundles
            for entry in result["processes"])
    except (KeyError, RuntimeError, ValueError, OSError):
        return False


class HALStagedController(StagedController):
    def __init__(self, *args, check_call=None, browser_call=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.call_app not in CALL_APP_BUNDLES:
            raise ValueError("Unsupported call application")
        self.check_call = check_call if check_call is not None else (
            lambda pids: live_dot_input_verified(pids, call_app=self.config.call_app))
        if browser_call is not None and self.config.call_app != "chrome":
            raise ValueError("Browser call control requires the explicit Chrome profile")
        self.browser_call = browser_call
        self._browser_key = None
        self._browser_consumed_key = None
        self._browser_start_deadline = 0.0
        self._browser_reported_state = None
        self._watch_request = None
        self._watch_ack = None
        self._next_watch_poll = self._next_call_check = 0.0
        self._next_active_ack = 0.0
        self._current_poll_info = None
        self._on_transition_key = None
        self._on_transition_deadline = 0.0
        self._on_transition_pending = False
        self._button_wait_key = None
        self._button_wait_deadline = self._next_button_wait_ack = 0.0

    def prepare(self):
        super().prepare()
        # A recovered controller never inherits an old call's permission to
        # start. The device is now locked; invalidate the browser's old scope.
        self._stop_browser_call()
        self.reason = "prepared"
        self.backend.should_stop = self.pause_requested.is_set
        self.report("watch_controls", message=(
            "StopWatch画面のONでChromeのYour dot通話を開始し、OFFで通話を終了します。通話とBlackHole入力を確認できるまで録音はロック中です。"
            if self.browser_call is not None else
            "StopWatch画面のONで中継開始を要求します。MacのYour dot通話とBlackHole入力を確認できるまで録音はロック中です。画面のOFFはStopWatchの中継停止で、Macの通話終了は別操作です。"))

    def _browser_status(self):
        if self._browser_key is None:
            return {"connected": False, "state": "idle"}
        status = self.browser_call.status(self._browser_key)
        if (not isinstance(status, dict) or type(status.get("connected")) is not bool
                or status.get("state") not in BROWSER_STATES):
            raise StageError("Chrome通話の状態を確認できません。")
        # Browser errors can include page content. Keep only the bounded
        # protocol vocabulary in controller diagnostics, never raw text.
        return {"connected": status["connected"], "state": status["state"]}

    def _stop_browser_call(self):
        if self._browser_key is not None:
            self._browser_consumed_key = self._browser_key
        self._browser_key = None
        self._browser_start_deadline = 0.0
        self._browser_reported_state = None
        if self.browser_call is None:
            return True
        try:
            self.browser_call.request_off()
            self.report("browser_call_end_requested",
                        message="ChromeのYour dot通話に終了を要求しました。")
            return True
        except Exception as exc:
            self.report("browser_call_end_unconfirmed", error_type=type(exc).__name__,
                        message="Chrome通話への終了要求を確認できません。通話期限の失効を待ちます。")
            return False

    def _sync_browser_call(self, info):
        """Refresh only a fresh physical ON; terminal requests never restart.

        A bounded native-audio operation may hold this control loop for up to
        30 seconds. The adapter therefore permits 35 seconds without a fresh
        watch observation; its browser/socket lease is independently 8 seconds.
        A physical OFF stops the device microphone immediately, but the actual
        Chrome hang-up can wait for that operation (or the adapter deadline).
        """
        if self.browser_call is None:
            return True
        if ("call_req" not in info.fields or info.fields.get("call_want") != "1"
                or info.boot != self.boot):
            raise StageError("Chrome通話を開始する本体のON要求を確認できません。")
        key = (info.boot, info.fields["call_req"])
        if self.state != "active":
            self._healthy(info, "1")
            if info.fields.get("call_state") != "wait":
                raise StageError("Chrome通話を待つ本体の状態が正常ではありません。")
        if key == self._browser_consumed_key:
            self.reason = "waiting_for_fresh_watch_on"
            return False  # Even a permissive adapter must not replay an old ON.
        if self._browser_key != key:
            self._browser_key = key
            self._browser_start_deadline = self.clock() + BROWSER_START_SECONDS
            self._browser_reported_state = None
            self.browser_call.request_on(key)
        else:
            # A browser-side end or error consumes this physical ON. Read it
            # before lease refresh, so even a permissive adapter cannot retry.
            status = self._browser_status()
            if status["state"] in {"ended", "error"}:
                self.report("browser_call_stopped", connected=status["connected"], browser_state=status["state"])
                self.pause("browser_call_ended" if status["state"] == "ended" else "browser_call_failed")
                return False
            self.browser_call.request_on(key)
        status = self._browser_status()
        if status != self._browser_reported_state:
            self._browser_reported_state = status
            self.report("browser_call", connected=status["connected"], browser_state=status["state"])
        if status["state"] in {"ended", "error"}:
            self.pause("browser_call_ended" if status["state"] == "ended" else "browser_call_failed")
            return False
        active = status == {"connected": True, "state": "active"}
        if self.state == "active":
            if not active:
                self.pause("browser_call_lost")
                return False
        elif self.clock() >= self._browser_start_deadline:
            self.report("browser_call_timeout", timeout_seconds=BROWSER_START_SECONDS)
            self.pause("browser_call_start_timeout")
            return False
        else:
            self.reason = "waiting_for_call_input" if active else "waiting_for_browser_call"
        return True

    @staticmethod
    def valid_input(identity):
        return (isinstance(identity, dict) and identity.get("uid") == UID
                and identity.get("name") == NAME and identity.get("public") is True
                and identity.get("input_eligible") is True and identity.get("channels") == 2
                and identity.get("sample_rate") == 48000)

    def _call_verified(self):
        try:
            if self.browser_call is not None and self._browser_status() != {"connected": True, "state": "active"}:
                return False
            return self.check_call(self.config.reply_pids) is True
        except Exception:
            return False

    def start(self):
        if self.state != "prepared":
            raise StageError("prepare 完了後に開始できます。停止後は新しく準備してください。")
        request = self.client.info()
        if "call_req" in request.fields and request.fields.get("call_want") != "1":
            raise StageError("StopWatchの画面でONを押してください。録音はロック中です。")
        try:
            if not self._sync_browser_call(request):
                return
        except Exception:
            self.pause("browser_call_failed")
            raise
        if not self._call_verified():
            if self.browser_call is not None:
                return  # The explicit physical ON is waiting, still locked.
            raise StageError("Your dotの通話とBlackHole入力を確認できません。Macで通話を準備してください。")
        self._watch_request = request if "call_req" in request.fields else None
        super().start()
        if self.state != "active":
            return  # A locked button retry did not start the relay.
        self._button_wait_key = None
        self.reason = "running"
        self._current_poll_info = None  # start/unlock changes the prior snapshot
        now = self.clock()
        self._next_call_check = now + 1.0
        self._next_active_ack = now + ACTIVE_LEASE_REFRESH_SECONDS

    def _unlock_device(self):
        # Recheck after the one-second released-button observation. For v19,
        # firmware also atomically rejects a touch OFF/reboot/new request here.
        if self.browser_call is not None:
            status = self._browser_status()
            if status != {"connected": True, "state": "active"}:
                reason = ("browser_call_ended" if status["state"] == "ended" else
                          "browser_call_failed" if status["state"] == "error" else "browser_call_lost")
                raise BrowserCallStopped(reason)
            if self.clock() >= self._browser_start_deadline:
                raise BrowserCallStopped("browser_call_start_timeout")
        if not self._call_verified():
            raise StageError("開始前にYour dotの音声入力が変わりました。録音は開始しません。")
        if self._watch_request is None:
            return super()._unlock_device()  # v18 compatibility only.
        key = (self._watch_request.boot, self._watch_request.fields["call_req"])
        if self._button_wait_key == key and self.clock() >= self._button_wait_deadline:
            raise ButtonWaitExpired("青ボタンの安定待機が時間切れになりました。")
        return self.client.ack_call(self._watch_request, "active", lease_ms=CALL_LEASE_MS)

    def _wait_for_button(self, info, *, unstable=False):
        """Keep only a healthy, locked ON waiting; never extend its first deadline."""
        self._healthy(info, "1")
        self._button_metadata(info)
        if (info.boot != self.boot or info.fields.get("call_want") != "1"
                or info.fields.get("call_state") != "wait"):
            raise StageError("青ボタン待機中のON要求が正常ではありません。")
        key = (info.boot, info.fields["call_req"])
        if self._button_wait_key != key:
            self._button_wait_key = None
        if self._button_wait_key is None:
            if not unstable:
                return False
            self._button_wait_key = key
            self._button_wait_deadline = self.clock() + BUTTON_WAIT_SECONDS
            self._next_button_wait_ack = 0.0
            self.report("watch_button_wait", request=key[1], timeout_seconds=BUTTON_WAIT_SECONDS,
                        message="青ボタンが安定して離されるのを待っています。録音はロック中です。")
        if self.clock() >= self._button_wait_deadline:
            self.report("watch_button_timeout", request=key[1],
                        message="青ボタンの安定待機を終了します。OFFへ戻し、次のONを待ちます。")
            self.pause("watch_off")  # Standby requires confirmed guard lock and native teardown.
            self._button_wait_key = None
            self._watch_request = self._watch_ack = None
            self._current_poll_info = None
            return True
        if self.clock() >= self._next_button_wait_ack:
            self._ack_watch(info, "wait")
            self._next_button_wait_ack = self.clock() + ACTIVE_LEASE_REFRESH_SECONDS
        self.reason = "waiting_for_button_release"
        return unstable or info.fields["blue"] == "1"

    def _defer_start_error(self, error):
        if self.browser_call is not None and isinstance(error, BrowserCallStopped):
            self.pause(error.reason)
            return True
        if (not isinstance(error, (BlueButtonUnstable, ButtonWaitExpired))
                or self.state != "prepared" or self.session is not None or self.sink is not None
                or self._watch_request is None or self.pause_requested.is_set()
                or self._native_failed.is_set() or not self.backend.ready
                or not self.valid_input(self.backend.public_input_identity)
                or any(not self.check_pid(pid) for pid in self.config.reply_pids)
                or not self._call_verified()):
            return False
        fresh = self.client.info()
        if (fresh.boot != self._watch_request.boot
                or fresh.fields.get("call_req") != self._watch_request.fields["call_req"]):
            return False
        self._current_poll_info = fresh
        return self._wait_for_button(fresh, unstable=True)

    def _ack_watch(self, info, state):
        acknowledged = self.client.ack_call(info, state, lease_ms=0 if state == "off" else CALL_LEASE_MS)
        self._current_poll_info = acknowledged
        self._watch_ack = (acknowledged.boot, acknowledged.fields["call_req"], state)
        if state == "active":
            self._next_active_ack = self.clock() + ACTIVE_LEASE_REFRESH_SECONDS
        return acknowledged

    def _recover_watch_off(self, request, error):
        """Recognize OFF racing an active heartbeat, without retrying its ACK."""
        try:
            fresh = self.client.info()  # One read-back; never resend the heartbeat.
        except Exception:
            return False
        if (fresh.boot != self.boot or fresh.boot != request.boot
                or fresh.fields.get("call_req") in {None, request.fields.get("call_req")}
                or any(fresh.fields.get(key) != value for key, value in
                       {"call_want": "0", "call_state": "off", "guard": "1",
                        "mic": "0", "guard_pending": "0"}.items())):
            return False
        try:
            # Keep the original diagnostic even when the fresh OFF explains it.
            self.report("watch_control_error", error_type=type(error).__name__,
                        message=str(error)[:180])
        finally:
            self.pause("watch_off")
        self._watch_request = None
        self._watch_ack = None
        return True

    def _resume_watch(self, request):
        """Fresh ON after a verified watch OFF; faults never enter standby."""
        self._healthy(request, "1")
        if request.boot != self.boot or request.fields.get("call_want") != "1" or not self._call_verified():
            raise StageError("新しいON要求と通話の入力を確認できません。")
        self._ack_watch(request, "wait")
        self.state = "preparing"
        self.pause_requested.clear()
        self._native_failed.clear()
        self._native_reason = ""
        try:
            self.backend = self.backend_factory(self.config)
            self.backend.should_stop = self.pause_requested.is_set
            self.backend.start(submit_reply=self._submit_reply, on_error=self._backend_error)
            identity = self.backend.public_input_identity
            if not self.backend.ready or not self.valid_input(identity):
                raise StageError("音声中継を再準備できませんでした。")
            fresh = self.client.info()
            self._healthy(fresh, "1")
            if (fresh.boot != request.boot or fresh.fields.get("call_req") != request.fields["call_req"]
                    or fresh.fields.get("call_want") != "1"):
                raise StageError("準備中にON要求が変わりました。")
            self.identity = dict(identity)
            self.state = "prepared"
            self.start()
        except Exception:
            self.pause("restart_failed")
            raise

    def _poll_watch(self):
        """All USB and call decisions remain on the sole console/control loop."""
        now = self.clock()
        if now < self._next_watch_poll:
            return
        self._next_watch_poll = now + .15
        info = self.client.info()
        self._current_poll_info = info
        if "call_req" not in info.fields:
            if self.browser_call is not None:
                raise StageError("Chrome通話の操作にはON/OFF要求に対応した本体が必要です。")
            # Older firmware has no lease. Still detect a lost live call route.
            if self.state == "active" and now >= self._next_call_check:
                self._next_call_check = now + 1.0
                if not self._call_verified():
                    self.pause("call_input_changed")
            return
        if self.boot is not None and info.boot != self.boot:
            raise StageError("Device rebooted during the relay")
        key = (info.boot, info.fields["call_req"])
        if self.state in {"prepared", "standby"} and info.fields.get("guard_pending") == "1":
            # Both physical ON and OFF lock/wipe audio on the device worker.
            # A short pending acknowledgement is expected. It never authorizes
            # audio, and only these exact request/state pairs may settle.
            expected = {"guard": "1", "mic": "0", "worker": "1", "fatal": "0",
                        "sync": "0", "protocol_test": "1"}
            request_state = (info.fields.get("call_want"), info.fields.get("call_state"))
            if (info.state != 0 or any(info.fields.get(k) != v for k, v in expected.items())
                    or request_state not in {("1", "wait"), ("0", "off")}
                    or (request_state == ("0", "off") and info.fields.get("call_lease_ms") != "0")):
                raise StageError("ON/OFF直後の本体停止状態が正常ではありません。")
            if self._on_transition_key != key:
                self._on_transition_key = key
                self._on_transition_deadline = now + 2.0
                self.report("watch_on_settling" if request_state[0] == "1" else "watch_off_settling",
                            message="本体のON/OFF切替処理の完了を確認しています。")
            if now >= self._on_transition_deadline:
                raise StageError("ON/OFF切替後の録音停止確認が時間切れになりました。")
            self._on_transition_pending = True
            return
        self._on_transition_key = None
        if info.fields["call_want"] == "0":
            self._button_wait_key = None
            if self.state == "active" or self.browser_call is not None and self._browser_key is not None:
                self.pause("watch_off")
                self._watch_request = self._watch_ack = None
                self._current_poll_info = None
                return  # The next poll must read any newer physical ON afresh.
            self._healthy(info, "1")
            if info.fields.get("call_state") != "off" or info.fields.get("call_lease_ms") != "0":
                raise StageError("本体のOFF状態と通話期限の解除を確認できません。")
            self._watch_request = None
            # Physical OFF already expired the lease and locked the device.
            # Sending a redundant OFF ACK can race a new ON (or another OFF)
            # and reject its stale request. Observe only; never resend old OFF.
            self._watch_ack = (*key, "off")
            self.reason = "prepared" if self.state == "prepared" else "watch_off"
            return
        if self.state == "active":
            if (self._watch_request is None or key != (self._watch_request.boot,
                    self._watch_request.fields["call_req"])
                    or info.fields["call_state"] != "active" or info.fields.get("guard") != "0"):
                self.pause("watch_request_changed")
                return
            if not self._sync_browser_call(info):
                return
            if now >= self._next_call_check:
                self._next_call_check = now + 1.0
                if not self._call_verified():
                    self.pause("call_input_changed")
                    return
            # Route checks stay frequent; the 30-second device lease needs
            # fewer network round trips. Physical OFF is still checked above
            # on every watch poll, independently of this refresh deadline.
            # A long completed utterance may occupy the sole transport reader
            # for up to 20 seconds. Renew the same physical request first;
            # never send competing lease commands during the PCM transfer.
            long_capture_ready = (info.available and not info.consumed and info.state == 2
                                  and info.bytes > MAX_AUDIO_BYTES)
            if now >= self._next_active_ack or long_capture_ready:
                try:
                    self._ack_watch(info, "active")
                except TransportError as exc:
                    if not self._recover_watch_off(info, exc):
                        raise
            return
        if self.state in {"prepared", "standby"}:
            if not self._sync_browser_call(info):
                return
            if self._wait_for_button(info, unstable=info.fields.get("blue") == "1"):
                return
        if now < self._next_call_check:
            return
        self._next_call_check = now + 1.0
        if self.state == "standby":
            if self._call_verified():
                self._resume_watch(info)
            else:
                self._ack_watch(info, "wait")
        elif self.state == "prepared":
            if self._call_verified():
                self.start()  # An explicit physical ON request, never auto-record.
            else:
                self._ack_watch(info, "wait")
        else:
            # A stopped backend is deliberately not recreated by a bag touch.
            self._ack_watch(info, "error")

    def pause(self, reason="stop_requested"):
        if self.state == "finished":
            return
        if self.state == "paused":
            reason = self.reason  # Keep the first failure even through a later explicit stop.
        self.pause_requested.set()
        self.state, self.reason = "paused", reason
        if self.sink is not None:
            self.sink.cancel()
        try:
            locked = self._lock_device()
        except Exception:
            # A lost terminal/log sink must not prevent native audio teardown.
            locked = False
        if self.session is not None:
            self.session.stop(reason)
        clean = True
        if self.backend is not None:
            try:
                self.backend.cancel()
                clean = self.backend.stopped
            except Exception:
                clean = False
        self._stop_browser_call()  # Stop/wipe local audio before ending the real call.
        browser_retry = self.browser_call is not None and reason in BROWSER_RESTARTABLE
        if browser_retry and locked and clean:
            try:
                fresh = self.client.info()
                self._healthy(fresh, "1")
                browser_retry = fresh.boot == self.boot
            except Exception:
                browser_retry = False  # A concurrent device fault remains paused.
        if (reason == "watch_off" or browser_retry) and locked and clean:
            self.state, self.reason = "standby", reason
            self.session = self.sink = None
            self._watch_request = self._watch_ack = self._current_poll_info = None
            self.pause_requested.clear()
            self.report("watch_standby", locked=True, output_stopped=True, reason=reason,
                        message=("本体の音声はOFFです。Chrome通話にも終了を要求しました。画面のONで新しい通話を開始できます。"
                                 if self.browser_call is not None else
                                 "本体の音声はOFFです。画面のONで再開できます。MacのYour dot通話自体の終了は別操作です。"))
            return
        message = ("録音と音声送信の停止を確認しました。" if locked and clean
                   else "本体の録音停止または音声送信の終了を確認できません。接続と本体を確認してください。")
        self.report("call_end_required", locked=locked, output_stopped=clean,
                    message=message + "インストール済みのBlackHole入力は削除しません。Your dotの通話を終了してから call-ended と入力してください。")

    def poll(self):
        self._current_poll_info = None
        self._on_transition_pending = False
        if self.state == "idle":
            # The connector has already accepted the device. Explicit prepare
            # owns the initial lock/health check; polling before it can mistake
            # a reconnect's pending stop for a fault and make prepare impossible.
            return
        if self.state in {"finished", "paused"}:
            # A failed connection cannot recover in this console. Retain the
            # first cause; explicit status can still report offline diagnostics.
            return
        if self.pause_requested.is_set() and self.state not in {"idle", "paused"}:
            self.pause("stop_requested")
            return
        if self.state not in {"idle", "paused", "standby"} and (
                self._native_failed.is_set() or (self.backend is not None and not self.backend.ready)):
            self.pause("audio_worker_failed")
            return
        try:
            self._poll_watch()
        except Exception as exc:
            if self.reason != "watch_control_failed":
                try:
                    # Transport exceptions contain fixed protocol diagnostics,
                    # never PCM, credentials, or a transcript.
                    self.report("watch_control_error", error_type=type(exc).__name__,
                                message=str(exc)[:180],
                                device={key: self._current_poll_info.fields.get(key) for key in
                                        ("boot", "state", "guard", "mic", "guard_pending",
                                         "worker", "fatal", "sync", "protocol_test",
                                         "call_req", "call_want", "call_state", "call_lease_ms")}
                                if self._current_poll_info is not None else None)
                finally:
                    self.pause("watch_control_failed")
            return
        if self._on_transition_pending or self.state in {"paused", "standby"}:
            return
        if self.state == "prepared" and self._current_poll_info is None:
            return  # The bounded watch poll is not due yet; do not race a second read.
        super().poll()

    def finish(self):
        if self.state == "finished":
            return
        try:
            super().finish()
        finally:
            self._stop_browser_call()

    def _tick_session(self):
        # Reuse only a snapshot read in this exact control-loop iteration.
        # Scoped firmware commands still atomically reject OFF/old epochs.
        self.session.tick(info=self._current_poll_info)

    def _prepared_info(self):
        # The same poll already obtained and checked this snapshot. A second
        # Wi-Fi request adds latency without strengthening scope validation.
        return self._current_poll_info if self._current_poll_info is not None else self.client.info()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--port", default=Config.port)
    parser.add_argument("--reply-pid", action="append", type=positive_pid, default=[])
    parser.add_argument("--call-app", choices=CALL_APP_BUNDLES, default="codex",
                        help="通話元アプリ。Chromeは明示指定した音声プロセスだけを使用します")
    parser.add_argument("--audio-app")
    parser.add_argument("--audio-python", help="numpy/sounddeviceを備えた出力専用worker用Python")
    parser.add_argument("--reply-worker-v2", action="store_true",
                        help="明示したv2音声アプリの準備応答を確認し、入力の250ms待機を省きます")
    parser.add_argument("--hal-uid", choices=[UID])
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    if not args.reply_pid or not args.audio_app or not args.audio_python or args.hal_uid != UID:
        parser.error("--reply-pid、--audio-app、--audio-python、--hal-uidを明示してください。")
    if not sys.stdin.isatty():
        parser.error("対話端末で実行してください。")
    executable = resolve_audio_app(args.audio_app)
    # Preserve a virtual environment's executable path: resolving its symlink
    # selects the base Python instead and loses the environment's dependencies.
    python = Path(os.path.abspath(Path(args.audio_python).expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("音声用Pythonが見つからないか、実行できません。")
    config = Config(port=args.port, reply_pids=tuple(dict.fromkeys(args.reply_pid)),
                    audio_app=executable, audio_python=str(python),
                    reply_worker_v2=args.reply_worker_v2, call_app=args.call_app)

    def backend_factory(value):
        reply = load_backend(value)  # Imports only; exact frozen executable retained.
        from hal_output import HALRelayBackend, OutputProcess
        return HALRelayBackend(reply, OutputProcess(UID, value.audio_python),
                               require_armed_ack=value.reply_worker_v2)

    def controller_factory(client, value):
        return HALStagedController(client, value, backend_factory=backend_factory)

    print("試験用: v2音声アプリの返答収録準備を毎回確認してから入力を送ります。" if args.reply_worker_v2 else
          "試験用: 返答収録の開始同期は250msの待機のみで、実際のYour dot会話は未検証です。", flush=True)
    print("prepare後に本体画面のONで開始要求。v21では通話中に自動聴取します。OFFは本体の中継停止で、MacのYour dot通話は手動で終了します。", flush=True)
    return run_console(config, controller_factory=controller_factory, input_name=NAME)


if __name__ == "__main__":
    raise SystemExit(main())
