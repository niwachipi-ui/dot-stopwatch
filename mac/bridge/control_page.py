#!/usr/bin/env python3
"""Loopback-only, explicit controls. Never captures or uploads microphone audio."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
import json
import secrets
import time

from transport import TransportError, open_device
from usb_audio import tone


class RequestRejected(Exception):
    pass


class ControlApp:
    def __init__(self, *, port=8771, device="/dev/cu.usbmodem1101", connector=open_device,
                 allow_unlock=False, clock=time.monotonic, sleep=time.sleep):
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port")
        self.port, self.device, self.connector = port, device, connector
        self.allow_unlock, self.clock, self.sleep = allow_unlock, clock, sleep
        self.token = secrets.token_urlsafe(32)
        self.host = f"127.0.0.1:{port}"
        self.origin = "http://" + self.host

    def validate_post(self, host, origin, content_type, length, body):
        if host != self.host or origin != self.origin:
            raise RequestRejected("origin")
        if content_type != "application/json" or not 0 < length <= 1024 or len(body) != length:
            raise RequestRejected("body")
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise RequestRejected("json")
        if not isinstance(request, dict) or set(request) != {"token", "action"}:
            raise RequestRejected("fields")
        if not isinstance(request["token"], str) or not request["token"].isascii() or not secrets.compare_digest(request["token"], self.token):
            raise RequestRejected("token")
        if not isinstance(request["action"], str) or request["action"] not in {"lock", "unlock", "speaker"}:
            raise RequestRejected("action")
        if request["action"] == "unlock" and not self.allow_unlock:
            raise RequestRejected("recording_under_repair")
        return request["action"]

    def describe(self, client):
        info = client.info()
        device = client.status()
        guard = info.fields.get("guard")
        blue = info.fields.get("blue")
        return {"connected": True, "version": device.get("version", "unknown"),
                "guard": guard == "1" if guard in {"0", "1"} else None,
                "guard_pending": info.fields.get("guard_pending") == "1",
                "audio_fault": info.fields.get("fatal") == "1",
                "button_pressed": blue == "1" if blue in {"0", "1"} else None,
                "recordings": info.id,
                "microphone": info.fields.get("mic") == "1",
                "state": info.state, "dot_connected": False,
                "unlock_available": self.allow_unlock}

    def status(self):
        with self.connector(self.device) as client:
            return self.describe(client)

    def action(self, action):
        # Each request owns the same interprocess serial lock as the CLI.
        # There is intentionally no capture/audio_get route.
        if action not in {"lock", "unlock", "speaker"}:
            raise RequestRejected("action")
        if action == "unlock" and not self.allow_unlock:
            raise RequestRejected("recording_under_repair")
        with self.connector(self.device) as client:
            if action == "unlock":
                client.set_guard(False)
                message = "青ボタンで録音できる状態です。今は録音を開始していません。"
            else:
                client.set_guard(True)
                if action == "speaker":
                    client.play(tone())
                    deadline = self.clock() + 3
                    result = ""
                    while self.clock() < deadline:
                        playback = client.info()
                        result = playback.fields.get("play_result", "")
                        if result in {"3", "4", "5"}:
                            break
                        self.sleep(0.1)
                    if result == "4":
                        message = "確認音の再生は本体で中断されました。マイクはロックしたままです。"
                    elif result == "5":
                        message = "本体で確認音を再生できませんでした。マイクはロックしたままです。"
                    elif result == "3":
                        message = "本体から再生処理の完了が返りました。実際に音が聞こえたかは本体で確認してください。マイクはロックしたままです。"
                    else:
                        message = "確認音を送りましたが、再生完了はまだ確認できません。マイクはロックしたままです。"
                else:
                    message = "マイクをロックしました。青ボタンを押しても録音しません。"
            result = self.describe(client)
            result["message"] = message
            return result

    def page(self, nonce):
        # The token is generated locally from URL-safe characters, never user input.
        return PAGE.replace("__TOKEN__", self.token).replace("__NONCE__", nonce).encode("utf-8")


PAGE = '''<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>バッグのdot・音の確認</title>
<style>body{font:17px system-ui,sans-serif;background:#fff5fa;color:#342832;max-width:620px;margin:5vh auto;padding:24px;line-height:1.65}h1{font-size:28px}h2{font-size:19px;margin-top:0}.card{background:white;border:1px solid #e6d6df;border-radius:18px;padding:20px;margin:18px 0}.badge{display:inline-block;border-radius:20px;background:#ece6ed;padding:4px 12px;font-weight:bold}button{width:100%;padding:14px 18px;border-radius:12px;border:1px solid #cdbfc8;background:white;color:#342832;font:inherit;cursor:pointer;margin-top:10px}button.primary{background:#b82071;color:white;border-color:#b82071}button:disabled{opacity:.5;cursor:default}small{display:block;color:#66565f;font-size:14px}.message{min-height:2em;white-space:pre-wrap}#mic{font-weight:bold;color:#b82071}</style>
<h1>バッグのdot・音の確認</h1>
<p>USBにつないだStopWatchを、このMacから確認できます。</p>
<div class="card"><span id="connection" class="badge">接続を確認中</span><p id="version"></p><p id="guard">マイクの状態を確認中です。</p><p id="mic"></p><p id="button-state"></p><small id="recordings"></small><button id="refresh">状態を更新する</button><small>青ボタンが何にも触れない置き方で、状態を更新して確認できます。この操作では録音しません。</small></div>
<div class="card"><h2>青ボタンで録音</h2><p>青ボタンの押し離しは確認できました。会話への音声接続を準備中のため、このページから録音を有効にすることはできません。</p><button id="lock" class="primary" disabled>マイクをロックする</button><button id="unlock" disabled>音声接続を準備中です</button><small>ロックすると本体内の録音も消えます。このページは音声を取得・保存・送信しません。</small></div>
<div class="card"><h2>スピーカーの確認</h2><p>0.3秒の小さな確認音を本体から鳴らします。先にマイクをロックします。</p><button id="speaker" disabled>マイクをロックして音を確認する</button></div>
<p id="message" class="message" aria-live="polite"></p>
<div class="card"><h2>Your dotとの会話は、まだ接続されていません</h2><p>このページは録音ボタンとスピーカーの確認用です。本人からの返事はまだ届きません。</p><small>このMac内だけで開けるページです。iPhoneのテザリングだけで会話する機能は未完成です。ほかのUSB設定・確認ツールを同時に操作しないでください。</small></div>
<script nonce="__NONCE__">
const token="__TOKEN__";let busy=false;
const $=id=>document.getElementById(id);
function render(data){
 $("connection").textContent=data.connected?"StopWatch 接続済み":"StopWatchに接続できません";
 $("version").textContent=data.connected?"本体の確認版："+data.version:"USB接続を確認してください。";
 $("guard").textContent=!data.connected?"接続後にマイクの状態を確認できます。":data.guard_pending?"録音の停止を確認中です。ロック完了はまだ確認できません。":data.audio_fault?"本体の音声処理を起動できませんでした。":data.guard===true?"マイクはロックされています。":data.guard===false?"マイクのロックが解除されています。確認後はロックしてください。":"録音ロックに対応する本体の更新が必要です。";
 $("mic").textContent=data.microphone?"本体のマイクが動作中です。":"";
 $("button-state").textContent=!data.connected?"":data.button_pressed===true?"青ボタン：押された状態の信号を検出しています。":data.button_pressed===false?"青ボタン：離された状態の信号です。":"青ボタンの状態は取得できません。";
 $("recordings").textContent=data.connected&&Number.isInteger(data.recordings)?"今回の起動後の録音："+data.recordings+"回":"";
 for(const id of ["lock","unlock","speaker"])$(id).disabled=busy||!data.connected||data.guard===null||data.guard===undefined;
 $("unlock").disabled=$("unlock").disabled||!data.unlock_available;
 $("speaker").disabled=$("speaker").disabled||data.guard_pending||data.audio_fault;
 if(data.message)$("message").textContent=data.message;
}
async function request(action){
 if(busy)return;busy=true;for(const id of ["lock","unlock","speaker","refresh"])$(id).disabled=true;
 try{
  const response=await fetch(action?"/action":"/status",action?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,action})}:{cache:"no-store"});
  const data=await response.json();busy=false;render(data);if(!response.ok)$("message").textContent=data.message||"確認できませんでした。もう一度お試しください。";
 }catch(error){busy=false;render({connected:false});$("message").textContent="このMacの確認ページとの接続が切れました。";}
 finally{$("refresh").disabled=false;}
}
for(const action of ["lock","unlock","speaker"])$(action).onclick=()=>request(action);
$("refresh").onclick=()=>request();request();
</script></html>'''


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def reply(self, code, body, content_type="application/json", *, nonce=None):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; style-src 'unsafe-inline'; script-src 'nonce-" + (nonce or "none") + "'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != app.host:
                self.reply(403, {"message": "このMac内のURLで開いてください。"})
                return
            if self.path == "/":
                nonce = secrets.token_urlsafe(24)
                self.reply(200, app.page(nonce), "text/html", nonce=nonce)
            elif self.path == "/status":
                self.device_request(app.status)
            else:
                self.reply(404, {"message": "ページがありません。"})

        def do_POST(self):
            if self.path != "/action":
                self.reply(404, {"message": "操作がありません。"})
                return
            try:
                if self.headers.get("Host") != app.host or self.headers.get("Origin") != app.origin:
                    raise RequestRejected("origin")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise RequestRejected("length")
                action = app.validate_post(self.headers.get("Host"), self.headers.get("Origin"),
                                           self.headers.get("Content-Type"), length, self.rfile.read(length))
            except (ValueError, OSError, RequestRejected):
                self.reply(403, {"message": "ページを開き直して操作してください。"})
                return
            self.device_request(lambda: app.action(action))

        def device_request(self, operation):
            try:
                self.reply(200, operation())
            except (TransportError, OSError, ValueError):
                self.reply(503, {"connected": False, "dot_connected": False,
                                 "message": "本体の状態を確認できませんでした。USB接続を確認し、ほかの設定ツールを閉じて再試行してください。"})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--device", default="/dev/cu.usbmodem1101")
    args = parser.parse_args(argv)
    app = ControlApp(port=args.port, device=args.device)
    server = HTTPServer(("127.0.0.1", args.port), make_handler(app))
    print(f"dot audio controls: {app.origin}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
