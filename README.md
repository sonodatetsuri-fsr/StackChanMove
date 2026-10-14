# StackChan Move

StackChan (M5Stack CoreS3 + 車輪台車) を、LAN 内の PC で動く Hub からブラウザで操作するソフトウェア。カメラ映像を見ながら走らせ、話しかけて音声認識し、ロボットに喋らせ、カメラに映っているものを VLM に質問できる。

- **駆動**: Web / CLI / REST、または ESP-NOW リモコン (M5StickC Plus + Hat MiniJoyC) で前後移動・旋回
- **カメラ**: ロボットの映像をブラウザに中継
- **音声**: 音声認識 (Whisper) と音声合成 (VOICEVOX)。プラグインで差し替え可能
- **VLM**: Ollama 上の VLM (既定 `qwen3.5:9b`) にカメラ画像について質問。質問・回答はテキスト / 音声を選べる
- **シミュレータ**: 実機無しで動作を試せる

> **注意**: Hub は認証を持たない。LAN 内で `http://<HubのIP>:8341/` を開ける人は誰でもロボットを動かせる。信頼できない LAN では起動しないこと。

## 必要なもの

- M5Stack CoreS3 を載せた StackChan + 車輪台車 (台車の部品は `cad/` の STL を 3D プリントする。組立方法は [`cad/README.md`](cad/README.md))
- M5StickC Plus + Hat MiniJoyC (任意。リモコンを使う場合)
- データ通信対応の USB Type-C ケーブル、2.4 GHz の Wi-Fi
- Hub 用 PC: Ubuntu 22.04 / 24.04 または Windows 10 (22H2) / 11 (x64)、空きディスク 25 GB 以上
- NVIDIA GPU (任意。VRAM 10 GB 程度あると Whisper と VLM を GPU で同時に動かせる。無くても CPU で動く)

## セットアップ

1. リポジトリを取得する (Windows は「Code → Download ZIP」を展開しても良い)。

```bash
git clone https://github.com/sonodatetsuri-fsr/StackChanMove.git && cd StackChanMove
```

2. セットアップを実行する。uv・Python 依存・VOICEVOX・Ollama と VLM モデル・Whisper モデル・PlatformIO をまとめて導入する (ダウンロード約 15 GB)。

   - Ubuntu: `bash setup_ubuntu.sh` (終わったら一度ログアウト・ログインする)
   - Windows: `setup_windows.bat` をダブルクリック

   最後に `SETUP OK` と、ロボットに設定する Hub URL (`ws://<PCのIP>:8341/ws/device`) が表示される。`SETUP INCOMPLETE` なら表示された対処をしてもう一度実行する (何度実行しても良い)。VLM を使わない場合は `--skip-vlm` を付ける。

3. CoreS3 を USB でつないでファームを書き込む。

   - Ubuntu: `bash flash_firmware_ubuntu.sh core`
   - Windows: `flash_firmware_windows.bat` をダブルクリック

   リモコンを使う場合は M5StickC Plus をつないで `remote` を指定する (Windows はコマンドプロンプトで `flash_firmware_windows.bat remote`)。

4. ロボットの画面を 3 秒長押しして設定画面を開き、Wi-Fi と手順 2 の Hub URL を入れて保存する。

## 実行方法

- Ubuntu: `bash start_ubuntu.sh`
- Windows: `start_windows.bat` をダブルクリック

VOICEVOX・Ollama・Hub が起動し、ブラウザで `http://localhost:8341/` が開く。ロボットの電源を入れると数秒で接続し、カメラ映像が出る。Ctrl+C で終了する。スマートフォンなど別の端末からは `http://<HubのIP>:8341/` を開く。

実機無しで試す場合は、Hub を起動した状態で別の端末から次を実行する。ブラウザに合成のカメラ画像が出て、駆動すると画像の姿勢表示が変わる。

```bash
uv run python tools/sim_device.py --hub ws://127.0.0.1:8341/ws/device --joy
```

## 操作方法

### ブラウザ

| パネル | 操作 |
|---|---|
| カメラ | 映像を表示。「映像の設定」で配信 ON/OFF・fps・画質を変える |
| 駆動 | 3×3 のパッドを押している間だけ動く。スライダで最大速度を変える |
| 駆動ソース | `Web で操作` / `コントローラで操作` を切り替える |
| カメラに質問 | 質問を入れて Enter。`音声で質問` ON で話しかけた言葉が質問に、`音声で回答` ON で回答をロボットが読み上げる |
| 音声 | `音声認識` の ON/OFF と認識結果の表示。「ロボットに話させる」に文を入れると喋る。口パクの ON/OFF |

キーボード: W/↑ 前進、S/↓ 後退、A/← ・ D/→ 旋回 (W/S と同時で弧を描く)、Q/E その場で旋回、Space 停止。

### リモコン

駆動ソースを `コントローラで操作` にして使う。

| 操作 | 動作 |
|---|---|
| スティックを倒す | 前後で前進・後退、左右で旋回 |
| スティックを押し込む | 非常停止 (駆動ソースが `Web で操作` に戻る) |
| 正面ボタンを 2 秒長押し | ペアリングをやり直す |
| 側面ボタン | 画面のページ切替 |

### コマンドライン

```bash
uv run python tools/drive_cli.py --hub ws://127.0.0.1:8341/ws/app --v 0.1 --w 0.5 --duration 3
```

3 秒間、前進しながら左旋回する (`--v` m/s、`--w` rad/s)。

```bash
curl -s -X POST http://127.0.0.1:8341/speak -H 'Content-Type: application/json' -d '{"text":"こんにちは"}'
```

ロボットが「こんにちは」と喋る。

```bash
curl -s -X POST http://127.0.0.1:8341/vlm/qa -H 'Content-Type: application/json' -d '{"question":"何が見えますか?"}'
```

カメラ画像についての回答が JSON で返る。状態は `curl -s http://127.0.0.1:8341/status`、最新のカメラ画像は `http://127.0.0.1:8341/camera/latest.jpg` で取れる。

## CAD データ

`cad/` に台車の部品の STL (単位 mm) がある。

| ファイル | 部品 | 外形 (mm) |
|---|---|---|
| `PartA.stl` | 上部 | 50 × 23 × 12 |
| `PartB.stl` | フレーム | 42 × 62 × 46.6 |
| `PartC.stl` | 車輪 | 29 × 29 × 15 |

必要な部品と組立方法は [`cad/README.md`](cad/README.md) を参照。

## 設定

Hub の設定は `.env` (雛形は `.env.example`、セットアップが作る) で変える。主なもの: `HUB_PORT` (8341)、`HUB_MAX_V` / `HUB_MAX_W` (速度上限)、`STT_MODEL` (Whisper のモデル)、`VOICEVOX_SPEAKER` (話者)、`VLM_MODEL` (VLM のモデル)。音声を使わない場合は `STT_PLUGIN=none` / `TTS_PLUGIN=none`、VLM を使わない場合は `VLM_BACKEND=none`。

ファームの設定は `firmware/include/config.h` (車輪の校正値など) で変え、書き込み直す。

VOICEVOX の音声を公開する場合は、[VOICEVOX 利用規約](https://voicevox.hiroshiba.jp/term/) と各キャラクターの規約に従ってクレジットを表記すること (既定の話者は ずんだもん)。

## トラブルシュート

- **ロボットが Hub につながらない**: PC とロボットが同じ LAN にいるか、Hub URL が正しいかを確認する。Windows は接続中のネットワークを「プライベート」にする。
- **リモコンが PC に認識されない**: 充電専用ケーブルでないか確認する。Ubuntu はセットアップ後に再ログインしたか確認する。
- **発話が途中で止まる**: 多くは Wi-Fi の質が原因。安定したアクセスポイントにつなぐ。
- **導入状態を確かめる**: `uv run python -m openmovehub.doctor` で各項目の OK / FAIL と対処が出る。

## ライセンス

[MIT License](LICENSE)
