#!/usr/bin/env python3
"""Explicit USB pairing for the Wi-Fi experiment. Default/help changes nothing.

Secret values are read from an owner-only file, never command-line arguments or
console output. The existing USB lock prevents interfering with observations.
"""
import argparse
import re
import sys
import time

from transport import open_device
from wifi_transport import read_token


def hostname(value):
    if (len(value) > 253 or '.' not in value or not re.fullmatch(r'[a-zA-Z0-9.-]+', value)
            or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', x)
                   for x in value.split('.'))):
        raise argparse.ArgumentTypeError('ホスト名だけを指定してください。URLやポート番号は含めません。')
    return value.lower()


def require_off(client):
    info = client.info()
    expected = {'guard': '1', 'guard_pending': '0', 'mic': '0', 'sync': '0',
                'state': '0', 'worker': '1', 'fatal': '0', 'call_want': '0', 'call_state': 'off'}
    if any(info.fields.get(k) != v for k, v in expected.items()):
        raise RuntimeError('Pairing requires an already locked, idle device')
    status = client.status()
    if not status.get('version', '').startswith('wifi-voice-'):
        raise RuntimeError('Wi-Fi experimental firmware is required')
    return info


def configure(client, host=None, token=None, *, clear=False):
    before = require_off(client)
    command = b'net_clear' if clear else ('net_pair\t' + hostname(host) + '\t' + token).encode('ascii')
    client._begin(command)
    deadline = time.monotonic() + 4
    expected = b'NET_CLEARED' if clear else b'NET_PAIRED'
    # Inspect only exact fixed responses; never print an unexpected serial line.
    for _ in range(12):
        line = client._line(deadline)
        if line == expected:
            break
        if line.startswith((b'NET_ERROR', b'AUDIO_ERROR', b'COMMAND_ERROR')):
            raise RuntimeError('Device rejected pairing')
    else:
        raise RuntimeError('Pairing acknowledgement missing')
    after = client.info()
    if after.boot != before.boot or after.fields.get('guard') != '1' or after.fields.get('mic') != '0':
        raise RuntimeError('Post-pairing state not confirmed')
    return after


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true', help='明示した接続先とキーを本体へ保存')
    actions.add_argument('--clear', action='store_true', help='本体のWi-Fi音声接続設定だけを削除')
    parser.add_argument('--host', type=hostname)
    parser.add_argument('--token-file')
    parser.add_argument('--port', default='/dev/cu.usbmodem1101')
    args = parser.parse_args(argv)
    if not args.apply and not args.clear:
        parser.print_help()
        return 0
    if args.apply and (not args.host or not args.token_file):
        parser.error('--applyには--hostと--token-fileが必要です。')
    try:
        token = read_token(args.token_file) if args.apply else None
        with open_device(args.port) as client:
            configure(client, args.host, token, clear=args.clear)
        print('接続設定を削除しました。' if args.clear else '接続設定を保存しました。録音はロック中です。')
        return 0
    except Exception as exc:
        print('本体への設定を完了できませんでした（' + type(exc).__name__ + '）。'
              '観測中は変更せず、対応版とUSBの使用状況を確認してください。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
