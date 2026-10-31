#pragma once
// TLS接続先（Cloudflare Quick Tunnel）を確認するためのルート証明書。
// 秘密情報ではありませんが、配布版では空にしてあります。
// 使う前に、Google Trust Services の公式ページ https://pki.goog/roots.pem から
// 「GTS Root R4」の -----BEGIN CERTIFICATE----- 〜 -----END CERTIFICATE----- を
// 下の YOUR_ROOT_CA_PEM_HERE と置き換えて貼り付けてください（Codexに頼めばやってくれます）。
// 空のままだと、本体は安全のためWi-Fi音声中継に接続しません。
static constexpr char DOT_WIFI_ROOT_CA[] = R"DOTCA(
YOUR_ROOT_CA_PEM_HERE
)DOTCA";
