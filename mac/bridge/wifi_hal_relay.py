#!/usr/bin/env python3
"""Your dot HAL relay over an authenticated, loopback-only WebSocket listener.

Start the WSS tunnel separately. Help/import opens no network, USB, or audio.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from hal_staged_relay import CALL_APP_BUNDLES, HALStagedController, NAME
from relay_runner import Config, load_backend, positive_pid, resolve_audio_app
from wifi_console import run_wifi_console as run_console
from transport import TransportError
from wifi_transport import PORT, open_wifi_device, read_token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--token-file", required=False)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--reply-pid", action="append", type=positive_pid, default=[])
    parser.add_argument("--call-app", choices=CALL_APP_BUNDLES, default="codex",
                        help="通話元アプリ。Chromeは明示指定した音声プロセスだけを使用します")
    parser.add_argument("--chrome-control-token-file",
                        help="Chrome通話の開始・終了も連動させるローカル拡張の鍵ファイル")
    parser.add_argument("--chrome-extension-id", help="許可するローカル拡張のID")
    parser.add_argument("--audio-app")
    parser.add_argument("--audio-python")
    parser.add_argument("--reply-worker-v2", action="store_true",
                        help="明示したv2音声アプリの準備応答を確認し、入力の250ms待機を省きます")
    parser.add_argument("--burst-playback", action="store_true",
                        help="試験用: 対応本体への音声転送で固定待機を省略します。既定は従来の送信間隔です")
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    if not args.token_file or not args.reply_pid or not args.audio_app or not args.audio_python:
        parser.error("--token-file、--reply-pid、--audio-app、--audio-pythonを明示してください。")
    if not 1024 <= args.port <= 65535 or not 1 <= args.wait_seconds <= 600:
        parser.error("ポートは1024〜65535、接続待ちは1〜600秒です。")
    if bool(args.chrome_control_token_file) != bool(args.chrome_extension_id):
        parser.error("Chromeのローカル鍵と拡張IDを両方指定してください。")
    if args.chrome_control_token_file and args.call_app != "chrome":
        parser.error("Chrome通話の連動には --call-app chrome が必要です。")
    if not sys.stdin.isatty():
        parser.error("対話端末で実行してください。")
    try:
        read_token(args.token_file)
        executable = resolve_audio_app(args.audio_app)
    except (OSError, ValueError):
        parser.error("ペアリングファイルの形式・権限または音声アプリを確認してください。")
    python = Path(os.path.abspath(Path(args.audio_python).expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("音声用Pythonが見つからないか、実行できません。")
    config = Config(reply_pids=tuple(dict.fromkeys(args.reply_pid)),
                    audio_app=executable, audio_python=str(python),
                    reply_worker_v2=args.reply_worker_v2, call_app=args.call_app)

    def backend_factory(value):
        reply = load_backend(value)
        from hal_output import HALRelayBackend, OutputProcess, UID
        return HALRelayBackend(reply, OutputProcess(UID, value.audio_python),
                               require_armed_ack=value.reply_worker_v2)

    browser_call = None
    try:
        if args.chrome_control_token_file:
            from browser_call import BrowserCall
            browser_call = BrowserCall(read_token(args.chrome_control_token_file),
                                       args.chrome_extension_id,
                                       emit=lambda event: print(json.dumps(event), flush=True)).start()
        def controller_factory(client, value):
            options = {"backend_factory": backend_factory}
            if browser_call is not None:
                options["browser_call"] = browser_call
            return HALStagedController(client, value, **options)
        if browser_call is not None:
            print("Chrome連動モード。本体ONでdot通話を開始し、OFFで通話も終了します。Chromeのdotページとローカル拡張を準備してください。", flush=True)
        else:
            print("WSS経由の試験用中継。MacのYour dot通話は手動で準備してください。本体ONで中継を要求、OFFで本体音声を停止します。", flush=True)
        return run_console(config, token_file=args.token_file, port=args.port,
                           accept_timeout=args.wait_seconds, burst_playback=args.burst_playback,
                           controller_factory=controller_factory)
    except KeyboardInterrupt:
        print("接続待ちを終了しました。", flush=True)
        return 130
    except (OSError, ValueError, RuntimeError, TransportError) as exc:
        print("Wi-Fi中継を終了しました（" + type(exc).__name__ + "）。接続と設定を確認してください。", flush=True)
        return 3
    finally:
        if browser_call is not None:
            browser_call.close()


if __name__ == "__main__":
    raise SystemExit(main())
