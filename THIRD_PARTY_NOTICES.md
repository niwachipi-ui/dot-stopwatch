# 同梱・利用しているライブラリのライセンス

| ライブラリ | 使い方 | ライセンス |
| --- | --- | --- |
| arduinoWebSockets 2.7.2（Links2004） | `firmware/lib/WebSockets/` に同梱。`firmware/vendor.patch` の変更（送受信サイズの上限などを外から指定できるようにしたもの）を当ててあります | GNU LGPL 2.1（`firmware/lib/WebSockets/LICENSE`） |
| └ libb64 | arduinoWebSockets に含まれる | Public Domain（`firmware/lib/WebSockets/src/libb64/LICENSE`） |
| └ libsha1 | arduinoWebSockets に含まれる | Public Domain |
| M5Unified（M5Stack） | ビルド時に PlatformIO が自動でダウンロード（同梱なし） | MIT License |
| M5GFX（M5Stack） | ビルド時に PlatformIO が自動でダウンロード（同梱なし） | MIT License |
| Arduino-ESP32 / ESP-IDF（Espressif） | PlatformIO の espressif32 プラットフォーム（同梱なし） | Apache License 2.0 ほか |
| websockets、pyserial、numpy、sounddevice、PyObjC、PyInstaller など | Mac側で pip がダウンロード（同梱なし。`mac/requirements-*.txt`） | 各ライブラリのライセンス |
| BlackHole 2ch（Existential Audio） | 各自で公式サイトから入れる（同梱なし） | GPL-3.0 |
| cloudflared（Cloudflare） | 各自で入れる（同梱なし） | Apache License 2.0 |

M5Unified・M5GFX の著作権表示（MIT License）：
Copyright (c) M5Stack. 詳細は https://github.com/m5stack/M5Unified と https://github.com/m5stack/M5GFX の LICENSE を参照してください。
