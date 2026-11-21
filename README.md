# Logi Mouse (no G HUB) — Stream Deck plugin

Logicool / Logitech G のワイヤレスマウスを **G HUB なし** で Stream Deck から操作するプラグイン。
HID++ 2.0 でレシーバー経由のマウスと直接通信します（外部ライブラリ不要、Python 標準ライブラリのみ）。

> **非公式ツールです。** Logitech / Logicool および Elgato とは無関係です。自己責任でご利用ください。

動作確認: PRO X Wireless (PRO X SUPERLIGHT) + LIGHTSPEED レシーバー (046D:C547), Windows 11, Stream Deck 7.6

## アクション

| アクション | 内容 |
|---|---|
| バッテリー | 残量 % とバー表示（緑 >50% / 黄 >20% / 赤）。20 秒ごと更新、押すと即更新。スリープ中は `OFF` |
| DPI切替 | 押すたびに DPI リストを順に切替（設定欄で変更可、既定 `400,800,1600,3200`） |
| レポートレート切替 | 125 → 250 → 500 → 1000 Hz |
| オンボード/ホスト切替 | `BOARD` = マウス内蔵メモリの設定 / `HOST` = プラグインが制御 |

DPI / レートを変更するとマウスは HOST モードになり、指定値は `%APPDATA%\logimouse-streamdeck.json` に保存されます。
スリープ復帰などでマウスが BOARD に戻った場合は、プラグインが自動で HOST + 保存値を再適用します。
MODE キーで BOARD に戻すと自動制御は止まります。

> HOST モードではボタン割り当てが初期状態になります。G HUB で作ったボタン割り当て・マクロを使う場合は、
> オンボードメモリに保存して BOARD モードで使ってください。

## 必要なもの

- Windows 10/11
- Stream Deck 6.4 以降（同梱の Node.js 20 でランチャーを起動）
- Python 3（`pythonw.exe`。既定で `%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe`、なければ PATH 上を使用。
  環境変数 `LOGIMOUSE_PYTHON` で指定も可）

## インストール

このフォルダを `com.local.logimouse.sdPlugin` という名前で
`%APPDATA%\Elgato\StreamDeck\Plugins\` にコピーし、Stream Deck を再起動。

## 構成

- `bin/launch.js` — Stream Deck が起動する Node ランチャー。`plugin.py` を pythonw で起動するだけ
- `plugin.py` — Stream Deck WebSocket クライアント + 各アクション + キー画像 (SVG) 描画
- `hidpp.py` — Windows HID (ctypes) 経由の HID++ 2.0 クライアント。単体実行で状態表示: `python hidpp.py`
- `pi/dpi.html` — DPI リスト設定用 Property Inspector
- `imgs/` — アイコン

使用している HID++ 機能: `0x0005` DeviceName, `0x1004` UnifiedBattery (`0x1000` にフォールバック),
`0x2201` AdjustableDPI, `0x8060` ReportRate, `0x8100` OnboardProfiles

## ライセンス

MIT License（[LICENSE](LICENSE)）

## ログ

`plugin.log`（プラグインフォルダ内）と `%APPDATA%\Elgato\StreamDeck\logs\com.local.logimouse0.log`
