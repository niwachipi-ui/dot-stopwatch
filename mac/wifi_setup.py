"""Loopback-only USB Wi-Fi provisioning. Does not record audio or expose files."""
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
import html, os, secrets, time, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bridge'))
from transport import open_device, TransportError

TOKEN = secrets.token_urlsafe(32)
PORT = 8770
# USBポート名が違うときは、環境変数 DOT_USB_PORT で指定できます（配布版で追加）
DEVICE = os.environ.get('DOT_USB_PORT', '/dev/cu.usbmodem1101')

def exchange(command):
    # Share the bridge's exclusive USB lock: setup must never steal live PCM.
    with open_device(DEVICE) as client:
        info = client.info()
        if info.fields.get('sync') != '0':
            raise TransportError('Device requires an explicit recovery')
        if command != b'status':
            required = {'guard':'1', 'mic':'0', 'guard_pending':'0', 'call_want':'0'}
            if any(info.fields.get(k) != v for k,v in required.items()) or info.state != 0:
                return 'WIFI_BUSY'
        client._begin(command)
        end = time.monotonic() + 3
        if command == b'status':
            client._response(b'DOT_READY', end)
            return client._response(b'VOICE_TEST', end).decode('ascii')
        for _ in range(8):
            line = client._line(end)
            if line in (b'WIFI_CONNECTING', b'WIFI_INVALID', b'WIFI_BUSY'):
                return line.decode('ascii')
        raise TransportError('No Wi-Fi configuration acknowledgement')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def page(self, message=''):
        body = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>dotのWi-Fi設定</title><style>body{{font:18px system-ui;background:#fff5fa;color:#30262c;max-width:540px;margin:6vh auto;padding:24px}}h1{{font-size:28px}}label{{display:block;margin-top:20px}}input,button{{box-sizing:border-box;width:100%;padding:14px;font:inherit;border-radius:12px;border:1px solid #dacbd3}}button{{background:#d82d83;color:white;margin-top:24px}}small{{display:block;line-height:1.7;margin:20px 0}}.message{{white-space:pre-wrap}}</style><h1>dotのWi-Fi設定</h1><p>スマホのテザリング先を登録します。</p><small>StopWatchはUSBでこのMacにつないでください。iPhoneの「設定 → インターネット共有」で共有をオンにし、「互換性を優先」をオンにしてください。</small><form method="post" action="/configure"><input type="hidden" name="token" value="{TOKEN}"><label>テザリングのネットワーク名<input name="ssid" required maxlength="32" autocomplete="off"></label><label>テザリングのパスワード<input name="password" type="password" maxlength="63" autocomplete="off"></label><button>StopWatchに設定を送る</button></form><p class="message">{html.escape(message)}</p><p><a href="/status">接続状態を確認する</a></p><small>このページはこのMac内だけで動きます。パスワードをログやMacのファイルには保存しません。本体には再接続用として保存します。<br>この設定ではWi-Fi接続だけを準備します。自宅Macへの音声中継は引き続き準備中です。<br>本体は音声OFFにして設定してください。このページから録音は開始しません。</small></html>'''.encode()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{PORT}': self.send_error(403); return
        if self.path == '/': self.page(); return
        if self.path == '/status':
            try:
                status = exchange(b'status')
                connected = 'wifi=1 ' in status
                message = 'Wi-Fi接続済みです。' if connected else 'Wi-Fiは未接続です。テザリング設定を確認してください。'
                if 'VOICE_TEST' not in status: message = '本体からの応答を確認できませんでした。USB接続を確認して再試行してください。'
                self.page(message)
            except (OSError, TransportError): self.page('本体に接続できませんでした。USB接続を確認してください。')
        else: self.send_error(404)
    def do_POST(self):
        if self.path != '/configure' or self.headers.get('Host') != f'127.0.0.1:{PORT}' or self.headers.get('Origin') != f'http://127.0.0.1:{PORT}': self.send_error(403); return
        try: length = int(self.headers.get('Content-Length','0'))
        except ValueError: self.send_error(400); return
        if not 0 < length < 2048: self.send_error(400); return
        fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        if not secrets.compare_digest(fields.get('token',[''])[0],TOKEN): self.send_error(403); return
        ssid = fields.get('ssid',[''])[0]; password = fields.get('password',[''])[0]
        if not 1 <= len(ssid.encode()) <= 32 or len(password.encode()) > 63 or any(c in ssid+password for c in '\r\n\t\0'):
            self.page('ネットワーク名またはパスワードの形式を確認してください。'); return
        try:
            reply = exchange(('wifi\t'+ssid+'\t'+password).encode())
            self.page('設定を送りました。数秒待って「接続状態を確認する」を押してください。' if 'WIFI_CONNECTING' in reply else '本体の音声をOFFにして、中継が停止していることを確認してください。' if reply == 'WIFI_BUSY' else '設定完了を確認できませんでした。もう一度お試しください。')
        except (OSError, TransportError): self.page('USB接続を確認してください。')

if __name__ == '__main__':
    print(f'Wi-Fi setup: http://127.0.0.1:{PORT}/', flush=True)
    HTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
