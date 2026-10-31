#!/usr/bin/env python3
"""Explicit lifecycle CLI. Help/default are inert; --check is read-only.

No path unlocks recording. Native audio is imported only after run preflight.
The actual button fault and native audio route must be resolved before use.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from relay import RelaySession
from transport import open_device


@dataclass(frozen=True)
class Config:
    port: str = "/dev/cu.usbmodem1101"
    reply_pids: tuple[int, ...] = ()
    audio_python: str = sys.executable
    startup_timeout: float = 15.0
    audio_app: str | None = None
    reply_worker_v2: bool = False
    call_app: str = "codex"


def preflight(client):
    """Only metadata reads. Never repairs sync, unlocks, or initializes native audio."""
    info = client.info()
    fields = info.fields
    blockers = []
    if fields.get("guard") == "1":
        blockers.append("録音はロックされています。この実行機能はロックを解除しません。")
    elif fields.get("guard") != "0":
        blockers.append("本体の録音ロック状態を確認できません。")
    if fields.get("mic") != "0":
        blockers.append("マイクが停止していることを確認できません。青ボタンを離してください。")
    if fields.get("blue") == "1":
        blockers.append("青ボタンが押された状態を検出しています。ボタンの調整が必要です。")
    elif fields.get("blue") != "0":
        blockers.append("青ボタンの状態を確認できません。")
    if fields.get("worker") not in {None, "1"}:
        blockers.append("本体の音声処理が動作可能な状態ではありません。")
    if fields.get("fatal") not in {None, "0"}:
        blockers.append("本体が回復を要する音声エラーを報告しています。")
    if fields.get("guard_pending") not in {None, "0"}:
        blockers.append("録音ロックの停止処理が完了していません。")
    if info.state == 4:
        blockers.append("本体が音声エラーを報告しています。")
    if fields.get("protocol_test") not in {None, "1"}:
        blockers.append("本体の通信自己テストが成功していません。")
    if fields.get("sync") == "1":
        blockers.append("USB通信が再同期待ちです。確認モードでは設定を変更しません。")
        # Client.status() would otherwise trigger its automatic sync recovery.
        version = "未確認（再同期待ち）"
    else:
        version = client.status().get("version", "不明")
    return {"version": version, "guard": fields.get("guard"), "mic": fields.get("mic"),
            "blue": fields.get("blue"), "boot": info.boot, "id": info.id,
            "blockers": blockers}


def print_report(report, emit=print):
    emit("本体: " + report["version"])
    emit("録音ロック: " + ("有効" if report["guard"] == "1" else "解除済み" if report["guard"] == "0" else "未確認"))
    emit("マイク: " + ("停止中" if report["mic"] == "0" else "停止を確認できません"))
    if report["blue"] is not None:
        emit("青ボタン: " + ("離された状態" if report["blue"] == "0" else "押された状態"))
    for blocker in report["blockers"]:
        emit("開始できない理由: " + blocker)
    if not report["blockers"]:
        emit("本体側の開始条件は満たしています。指定アプリの音声経路は未確認です。")
    emit("Your dotとの通話接続は、この確認では検証していません。")


def check_device(config, *, connector=open_device, emit=print):
    try:
        with connector(config.port) as client:
            report = preflight(client)
        print_report(report, emit)
        return 2 if report["blockers"] else 0
    except Exception as exc:
        emit("本体の状態を読み取れませんでした（" + type(exc).__name__ + "）。USB接続を確認してください。")
        return 3


def pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError):
        return False


def resolve_audio_app(value):
    """Resolve only the exact supplied bundle/binary. Never launch or discover one."""
    supplied = Path(value).expanduser()
    executable = supplied / "Contents" / "MacOS" / "DotWatchAudio" if supplied.suffix == ".app" else supplied
    if (executable.name != "DotWatchAudio" or executable.parent.name != "MacOS"
            or executable.parent.parent.name != "Contents"
            or executable.parent.parent.parent.suffix != ".app"):
        raise ValueError("--audio-app は指定した.app、またはそのContents/MacOS/DotWatchAudioを指定してください。")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("指定したアプリのDotWatchAudio実行ファイルが見つからないか、実行できません。")
    return str(executable.resolve())


def load_backend(config):
    """The sole lazy native adapter import; never called by check/simulation."""
    if type(config.reply_worker_v2) is not bool or config.reply_worker_v2 and config.audio_app is None:
        raise ValueError("Reply worker v2 requires an explicitly selected application bundle")
    folder = Path(__file__).resolve().parent.parent / "mac-audio"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    from relay_backend import CoreAudioRelayBackend
    commands = {}
    if config.audio_app is not None:
        executable = resolve_audio_app(config.audio_app)
        # Both operations must use the same signed app identity. Passing a .app
        # executable as python_executable would incorrectly append a .py path.
        commands = {"worker_command": [executable, "--internal-relay", "--worker"],
                    "cleanup_command": [executable, "--internal-relay-cleanup"]}
        if config.reply_worker_v2:
            commands["worker_command"].append("--external-input")
    return CoreAudioRelayBackend(reply_pids=config.reply_pids,
                                python_executable=config.audio_python,
                                startup_timeout=config.startup_timeout, **commands)


def run_session(config, *, connector=open_device, backend_factory=load_backend,
                session_factory=RelaySession, emit=print, sleep=time.sleep,
                should_stop=lambda: False, check_pid=pid_exists):
    """Explicit run; injectable factories let tests use no native/device APIs."""
    if not config.reply_pids or any(type(pid) is not int or not 1 <= pid <= 2147483647 for pid in config.reply_pids):
        emit("返答元アプリのプロセスIDを明示的に指定してください。自動選択はしません。")
        return 2
    if not 1 <= config.startup_timeout <= 30:
        emit("音声処理の開始待ちは1〜30秒で指定してください。")
        return 2
    if any(not check_pid(pid) for pid in config.reply_pids):
        emit("指定したプロセスIDが見つからないか、確認できません。IDを確認してください。")
        return 2
    backend, session = None, None
    result = 0
    try:
        with connector(config.port) as client:
            try:
                report = preflight(client)
                print_report(report, emit)
                if report["blockers"]:
                    result = 2
                elif should_stop():
                    emit("開始前に停止しました。")
                else:
                    # Constructor/import follows device preflight; backend.start
                    # follows the session's second, fresh guard/mic check.
                    backend = backend_factory(config)
                    session = session_factory(client, backend, should_stop=should_stop)
                    session.start()
                    if not should_stop():
                        backend.start(submit_reply=session.submit_reply, on_error=session.stop)
                        if not backend.ready:
                            raise RuntimeError("Backend did not become ready")
                        emit("指定した音声処理を開始しました。終了は Control+C。実際のYour dotとの通話は別途確認が必要です。")
                        while not should_stop():
                            session.tick()
                            snapshot = session.snapshot()
                            if not snapshot["running"]:
                                emit("中継を停止しました: " + snapshot["reason"])
                                result = 0 if should_stop() else 3
                                break
                            if not backend.ready:
                                emit("音声処理が停止したため、中継を終了します。")
                                result = 3
                                break
                            sleep(0.05)
            except KeyboardInterrupt:
                emit("停止しています。")
            except Exception as exc:
                if should_stop():
                    emit("停止要求を受け取り、終了しています。")
                    result = 0
                else:
                    emit("中継を開始・継続できませんでした（" + type(exc).__name__ + "）。音声経路の確認が必要です。")
                    result = 3
            finally:
                # All control-loop USB work has finished before cleanup. Backend
                # callbacks only feed RAM queues, never access the serial port.
                if session is not None:
                    session.stop("runner_stopped")
                    if session.snapshot()["reason"].endswith(":backend_cancel_failed"):
                        emit("Mac側の音声処理の終了確認が必要です。")
                        result = 4
                elif backend is not None:
                    try:
                        backend.cancel()
                    except Exception:
                        emit("Mac側の音声処理の終了確認が必要です。")
                        result = 4
                if backend is not None and not getattr(backend, "stopped", False):
                    emit("Mac側の音声処理が完全に終了したことを確認できません。")
                    result = 4
                # After both backend directions are stopped, lock via the same
                # owned serial connection. Never race a second serial opener.
                try:
                    info = client.set_guard(True)
                    if info.fields.get("guard") != "1" or info.fields.get("mic") != "0":
                        raise RuntimeError("Guard shutdown unverified")
                    emit("録音をロックし、マイクの停止を確認しました。")
                except Exception:
                    emit("停止後の録音ロックを確認できませんでした。本体の接続とマイク状態を確認してください。")
                    result = 4
    except KeyboardInterrupt:
        emit("停止しました。本体との接続状態を確認してください。")
        result = 4
    except Exception as exc:
        emit("USBを開けませんでした（" + type(exc).__name__ + "）。ほかの設定ツールを閉じてください。")
        result = 3
    return result


def simulate(*, run=subprocess.run, emit=print):
    """Reuse the existing fake-only cross-module integration test."""
    folder = Path(__file__).resolve().parent.parent / "mac-audio"
    command = [sys.executable, "-m", "unittest",
               "test_relay_backend.BackendTests.test_actual_session_pipe_backend_fir_and_fake_usb_end_to_end"]
    emit("合成音だけで中継を確認します。実機・マイク・CoreAudioは起動しません。")
    try:
        completed = run(command, cwd=str(folder), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        emit("模擬確認を完了できませんでした（" + type(exc).__name__ + "）。")
        return 3
    if completed.returncode:
        emit("模擬確認に失敗しました。mac-audio/test_relay_backend.py の結果を確認してください。")
        return 3
    emit("模擬確認に成功しました。実機での録音・Your dotの通話成功を示すものではありません。")
    return 0


def positive_pid(value):
    try:
        pid = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("正のプロセスIDを指定してください。")
    if not 1 <= pid <= 2147483647:
        raise argparse.ArgumentTypeError("1〜2147483647のプロセスIDを指定してください。")
    return pid


def main(argv=None):
    parser = argparse.ArgumentParser(description="dot音声中継。引数なしは説明のみで、録音や接続を開始しません。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="本体の開始条件だけを読み取る（設定変更なし）")
    modes.add_argument("--simulate", action="store_true", help="生成音・模擬USBの統合テストのみ")
    modes.add_argument("--run", action="store_true", help="明示的な中継開始。録音ロックは解除しない")
    parser.add_argument("--port", default=Config.port)
    parser.add_argument("--reply-pid", action="append", type=positive_pid, default=[], help="返答を取り込むアプリの正確なプロセスID。複数は繰り返し指定")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--audio-python", help="Mac音声依存関係を導入済みのPythonの絶対パス")
    audio.add_argument("--audio-app", help="使用するDotWatchAudioの.app、またはそのContents/MacOS/DotWatchAudioのパス")
    parser.add_argument("--startup-timeout", type=float, default=15, help="Mac音声処理の開始待ち上限（1〜30秒）")
    args = parser.parse_args(argv)
    if not (args.check or args.simulate or args.run):
        parser.print_help()
        return 0
    if not 1 <= args.startup_timeout <= 30:
        parser.error("--startup-timeout は1〜30秒で指定してください。")
    config = Config(args.port, tuple(dict.fromkeys(args.reply_pid)), args.audio_python or sys.executable,
                    args.startup_timeout, args.audio_app)
    if args.simulate:
        return simulate()
    if args.check:
        return check_device(config)
    if not config.reply_pids:
        parser.error("--run には --reply-pid の明示指定が必要です。")
    if config.audio_app is not None:
        try:
            executable = resolve_audio_app(config.audio_app)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        config = Config(config.port, config.reply_pids, config.audio_python,
                        config.startup_timeout, executable)
    else:
        audio_python = Path(config.audio_python)
        if not audio_python.is_absolute() or not audio_python.is_file() or not os.access(audio_python, os.X_OK):
            parser.error("--audio-python は実行できるPythonの絶対パスを指定してください。")
    stop = threading.Event()
    previous = {}
    # Foreground CLI only; signal handlers are not changed on import or --check.
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        return run_session(config, should_stop=stop.is_set)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
