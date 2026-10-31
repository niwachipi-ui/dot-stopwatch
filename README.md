# dot for M5Stack StopWatch（配布版）

バッグにぶら下げたM5Stack StopWatchで、ChatGPTのdots（Your dot）と音声で話すためのプログラム一式です。
コードが書けない作者が、Codexと一緒に9日間・約30回作り直して作りました。**まだ実験中のおもちゃ**です。

> 難しいところは、作者もぜんぶCodexにやってもらいました。
> このフォルダをCodexに渡して「このREADMEのとおりに準備して」と頼むのがいちばん早いです。

## 中身

| フォルダ | 中身 |
| --- | --- |
| `firmware/` | StopWatchに書き込むプログラム（PlatformIO＋Arduino、ESP32-S3）。作者が最後に使ったv33相当 |
| `firmware/lib/WebSockets/` | 通信用ライブラリ（arduinoWebSockets 2.7.2を少しだけ改造したもの。LGPL 2.1） |
| `mac/wifi_setup.py` | StopWatchにiPhoneテザリングのWi-Fiを登録する画面（Macの中だけで動く） |
| `mac/bridge/` | Macの中継プログラム（StopWatch ⇄ Mac） |
| `mac/mac-audio/` | Macの音声アプリ「Dot Watch Audio v3」のもと（Chromeの音を取り込んでStopWatchへ返す） |
| `mac/tools/quick-tunnel.yml` | Cloudflareの一時トンネル用の空の設定 |
| `LICENSE` | MIT License（このパッケージのオリジナル部分） |
| `THIRD_PARTY_NOTICES.md` | 使っているライブラリのライセンス一覧 |

**入っていないもの（自分で用意します）**：Wi-Fiの名前とパスワード、本体とMacをつなぐ「合言葉（キー）」、トンネルのURL、ChatGPTのアカウント。どれも人に見せないでください。

## 必要なもの

- M5Stack StopWatch、データ通信できるUSB-Cケーブル
- Mac（macOS 14.2以降。中継役として家で起動しておく）
- iPhone（外でStopWatchをネットにつなぐテザリング用）
- dotsが使えるChatGPTのプラン、Google Chrome
- Codex（全部作ってくれる担当）

## 全体の流れ

```
StopWatch（このファームウェア）
  ↓ iPhoneのテザリング（Wi-Fi）
Cloudflareの一時トンネル（cloudflared）
  ↓
Mac：中継プログラム ＋ 仮想オーディオ「BlackHole 2ch」
  ↓
Chromeで開いたdotsの音声通話
```

## 手順

コマンドは、このフォルダ（`dot_release`）の中で実行します。分からなければ、そのままCodexに貼って「これやって」で大丈夫です。

### 0. 最初に、StopWatchの元の中身をバックアップ

元に戻せるように、書き込む前に本体のフラッシュを丸ごと保存しておきます（作者もやりました）。
Codexに「StopWatchのフラッシュ16MBを丸ごとバックアップして」と頼めばOKです。

### 1. Macの準備（Codexにお願いする）

1. Python 3（作者は3.12）を入れる
2. PlatformIO：`python3 -m pip install platformio`
3. 中継用の環境：`python3 -m venv .venv-wifi && .venv-wifi/bin/pip install -r mac/requirements-wifi.txt`
4. 音声用の環境：`python3 -m venv .venv-audio && .venv-audio/bin/pip install -r mac/requirements-audio.txt`
5. cloudflared：`brew install cloudflared`（または公式サイトから）
6. BlackHole 2ch：公式サイト https://existential.audio/blackhole/ から入れる。**インストールは自分で同意して進めます。入れたあとMacを再起動**
7. 音声アプリを作る：`.venv-audio/bin/pyinstaller --distpath mac/mac-audio/dist mac/mac-audio/DotWatchAudioV3.spec`
   → `mac/mac-audio/dist/Dot Watch Audio v3.app` ができます。初めて動かしたときに「システムオーディオ録音」の許可を聞かれたら許可します

### 2. ルート証明書を貼る（1回だけ）

`firmware/src/wifi_root_ca.h` の `YOUR_ROOT_CA_PEM_HERE` を、Google Trust Servicesの公式ページ https://pki.goog/roots.pem にある「GTS Root R4」の証明書に置き換えます。
これは公開されている証明書で、秘密ではありません（本体が「本物のトンネルにつないでいるか」を確かめるために使います）。空のままだと本体は安全のためつながりません。

### 3. ファームウェアを書き込む

1. StopWatchをUSB-CでMacにつなぐ
2. 書き込みモードにする：電源（リセット）ボタンを約2秒長押しして、緑のLEDが点いたら離す（M5Stack公式の手順）
3. `pio run -d firmware -t upload`
4. 書き込みが終わったら、本体を一度リセットして画面にdotが出ればOK

### 4. Wi-Fi（iPhoneのテザリング）を登録する

1. iPhoneの「設定 → インターネット共有」をON、「互換性を優先」もON
2. StopWatchはUSBでMacにつないだまま、画面は通話OFF（赤）の状態にしておく
3. `.venv-wifi/bin/python mac/wifi_setup.py` を実行して、Chromeで http://127.0.0.1:8770/ を開く
4. 「テザリングのネットワーク名」と「パスワード」を入れて「StopWatchに設定を送る」
5. 数秒待って「接続状態を確認する」を押し、「Wi-Fi接続済みです」が出ればOK

パスワードはMacのファイルには保存されず、本体の中にだけ保存されます。
USBのポート名が違ってつながらないときは、`DOT_USB_PORT=/dev/cu.usbmodemXXXX` を頭に付けて実行します（Codexに「StopWatchのUSBポートを調べて」と頼めば分かります）。

### 5. 本体とMacの「合言葉（キー）」を作る（1回だけ）

```sh
mkdir -p ~/dot-private && chmod 700 ~/dot-private
python3 -c "import secrets; print(secrets.token_hex(32))" > ~/dot-private/token.txt
chmod 600 ~/dot-private/token.txt
```

このファイルの中身は誰にも見せないでください（スクショにも写さない）。

### 6. トンネルを起動して、本体に登録する

1. トンネルを起動（このウィンドウは開いたままにします）：
   `cloudflared tunnel --config mac/tools/quick-tunnel.yml --url http://127.0.0.1:8772 --metrics 127.0.0.1:8773`
2. 表示される `https://（ランダムな文字）.trycloudflare.com` の、`https://` を除いたホスト名をコピー
3. StopWatchをUSBでつないだまま（通話OFFの状態で）登録：
   `.venv-wifi/bin/python mac/bridge/wifi_pair.py --apply --host （コピーしたホスト名） --token-file ~/dot-private/token.txt`
4. 「接続設定を保存しました。」が出ればOK

**トンネルを起動し直すと、ホスト名が毎回変わります。** そのたびに3をやり直します（キーは同じものでOK）。

### 7. Chromeでdotsの通話を準備する

1. Chromeで https://chatgpt.com/dots を開いて、Your dotとの音声通話を始める
2. Chromeのマイクを「BlackHole 2ch」に変える（Chromeの「設定 → プライバシーとセキュリティ → サイトの設定 → マイク」。作者はここを自分で手で変えました）
3. Macの音の出力は、いつものスピーカーのままでOK

### 8. 中継を起動する

1. Chromeの通話の音を出しているプロセス番号（PID）を調べる。ここはCodexに「Chromeのdots通話の音を出しているプロセスIDを調べて」と頼むのが確実です
2. 中継を起動：

```sh
.venv-wifi/bin/python mac/bridge/wifi_hal_relay.py --run --call-app chrome \
  --token-file ~/dot-private/token.txt \
  --reply-pid （調べたPID） \
  --audio-app "mac/mac-audio/dist/Dot Watch Audio v3.app" \
  --audio-python .venv-audio/bin/python \
  --reply-worker-v2
```

（返事の途切れが気になるときは `--burst-playback` を足すと速くなることがあります。作者の最後の環境ではONでした）

### 9. 話してみる

1. StopWatchのUSBを抜く（iPhoneのテザリングでつながります）
2. 画面の緑の受話器（ON）をタッチ → 話しかける
3. 赤（OFF）で終了。最後にChromeの通話も終わらせて、中継とトンネルは Control＋C で止めます

- 音量は画面のまん中を上下にスワイプ。起動直後は100/255（約39%）です
- **何も聞こえないときは、まず音量が0になっていないか確認**
- 1回に話せるのは最大60秒。青ボタンは不調のため無効にしてあります

## 知っておいてほしいこと（正直な弱点）

- 家のMacを起動したまま、Chromeでdotの通話を開いておく必要があります
- 一時トンネルはURLが毎回変わるので、毎日気軽に使える形にはまだなっていません
- Chromeのほかのタブの音を拾ってしまうことがあります
- 通信はCloudflareのトンネルを通ります。キーがないとつながりませんが、端から端までの暗号化ではありません
- dotは自分のChatGPTアカウントにつながっています。落としたとき・人に貸すときは気をつけてください
- 個人で楽しむためのものです。動作の保証はありません（LICENSEのとおり）

## 自分好みに変える

画面のキャラや動きは、Codexに頼めば変えられます。自分で描いたキャラや好きなキャラに変えるのも、個人で楽しむ範囲ならOKです。
例：「dotの見た目を自分で描いたキャラに変えたい。画像を貼るので、この絵っぽく画面上で動いて欲しい」
