# MyLocalTube

手元の動画フォルダを **YouTube 風の画面で再生する** Windows 向けデスクトップアプリ。
動画は移動もコピーもせず、フォルダを索引するだけ。エクスプローラーでダブルクリックした動画もこのアプリで開ける。

A Windows desktop app that plays your local video folders in a YouTube-like UI (no copying, just indexing).

## できること

- 動画フォルダをサムネイル付きの一覧で表示。フォルダ別・検索
- **YouTube 風の操作バー**（動画に重ねて表示。カーソルを止めると隠れる）
  - 赤いシークバー（カーソル位置の時刻表示・ドラッグ）/ 音量 / 全画面 / 左上の「←」で一覧へ
  - **音量ブースト**（クリックで 200%、ボタンの上のホイールで 150〜400%）
  - **再生速度**（ボタンの上のホイールで 0.25 刻み、Shift+ホイールで 0.05 刻み。クリックで標準に戻す）
  - キー操作は YouTube と同じ（下表）
- ウィンドウが動画の縦横比に合わせて変わる（左右・上下の黒帯を出さない）
- **⋮ メニュー / ⚙ 設定**: フォルダへ移動（新しいフォルダも作れる）・ごみ箱へ移動（元に戻せる）・エクスプローラーで表示
- **既定のアプリ**にできる（.mp4 / .m4v / .mkv / .webm / .mov。開いていれば同じウィンドウで切り替わる）
- 再生できない形式（MPEG-2 映像の .ts など）は一覧に出さない
- 任意: 外部の **120fps 変換ツール**を設定すると、「120」ボタンから変換・120fps 版との切り替えができる

## 動作環境

- Windows 10 / 11（64bit）。WebView2 ランタイム（Windows 11 は標準で入っている）
- Python 3.11 以上
- ffmpeg（`ffmpeg.exe`。PATH に通すか `config_local.py` で場所を指定）

## セットアップ

```bat
git clone https://github.com/keigoly/local-tube-player.git
cd local-tube-player
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

rem 動画フォルダの場所などを設定（config_local.py は git 管理外）
copy config_local.example.py config_local.py
notepad config_local.py

rem 起動用の MyLocalTube.exe とアイコンを作る（.NET Framework 4 付属の C# コンパイラを使う）
launcher\build.cmd
```

`MyLocalTube.exe` をダブルクリックすると起動する（ショートカットを作ってデスクトップやスタートメニューに置くとよい）。
初回は動画フォルダを走査する。あとから増えた動画は右上の「再スキャン」で拾う。
ブラウザで使いたい場合は `start.cmd`（http://127.0.0.1:5560 が開く）。

### 既定のアプリにする

```bat
.venv\Scripts\python launcher\file_assoc.py register
```

開いた「既定のアプリ」の設定画面で MyLocalTube を選ぶ（Windows の仕様で、この最後の選択だけはアプリからできない）。
`status` で現在の既定を確認、`unregister` で登録を消せる。.ts は登録しない（MPEG-2 は再生できないため）。

## キー操作

| キー | 動作 |
|---|---|
| Space / k | 再生・一時停止 |
| ← / → | 5 秒戻る / 進む |
| j / l | 10 秒戻る / 進む |
| ↑ / ↓ | 音量 ±5% |
| m | ミュート |
| f / ダブルクリック | 全画面 |
| 0〜9 | 0%〜90% の位置へ |
| , / . | 一時停止中にコマ送り |
| < / > | 再生速度を下げる / 上げる |
| Esc | 全画面を抜ける・一覧に戻る |

## 120fps 変換ツールの仕様（任意）

変換ツールは本リポジトリに含まれない。次の約束を満たすスクリプトを `config_local.py` の `CONVERTER_PY` に指定すると、
「120」ボタン・変換ジョブが使えるようになる（指定しなければ関連する画面は出ない）。

- `<CONVERTER_PYTHON> <CONVERTER_PY> <入力動画のパス>` で実行される（作業フォルダはスクリプトのあるフォルダ）
- 入力と同じフォルダに `<名前>_120fps<拡張子>` を作る。既にあれば何もせず終了してよい
- 進捗は tqdm 形式で標準出力に出す。`[1/2] 名前: 45%|...| 30/62` や `[2/3] 名前: 45%|` の形を解析して画面に出す
- 取り消し・アプリ終了時はプロセスツリーごと止められる（作りかけの出力はアプリ側で消す）
- 任意: `[シーン検出] …` `[補間倍率] …` `[作業フォルダ] …` の行は注記として画面に出す。
  `スキップ:` で始まる行は「出力が既にあった」、`[エラー]` `[AI Error Log]` `[エンコードエラー]` で始まる行はエラーの注記として扱う
- 作業フォルダをライブラリの中に作る場合は、`config_local.py` の `EXCLUDE_DIR_NAMES` / `EXCLUDE_DIR_PREFIXES` で走査から外す
- `CONVERTER_MIN_PER_MIN`（動画1分あたり何分かかるか）を指定すると、変換前の確認に所要時間の目安を出す

## 構成

| ファイル | 役割 |
|---|---|
| `desktop.py` | アプリ版の本体。サーバをスレッドで起動し、WebView2 のウィンドウで表示 |
| `app.py` | FastAPI サーバ。Range 配信・詰め替え配信・API・進捗の SSE |
| `library.py` | 走査・索引（SQLite）・コーデック判定・サムネイル |
| `fileops.py` | 移動・ごみ箱・エクスプローラー表示 |
| `jobs.py` | 120fps 変換ジョブのキュー |
| `config.py` / `config_local.example.py` | 設定の既定値 / 自分用の設定のひな形 |
| `static/index.html` | 画面（ビルド不要の単一 HTML） |
| `launcher/` | 起動用 exe のソース（C#）・アイコン生成・既定のアプリ登録 |
| `data/` | 索引 DB・サムネイル・ログ（自動で作られる。git 管理外） |

設計の経緯・実装の要点・踏んだ不具合は `DEVELOPMENT.md`。

## 制限

- Windows 専用（macOS 版は予定）
- mkv などは再生時に MP4 へ詰め替えて配信するため、シークできない
- 移動は同じドライブの中だけ

## ライセンス

MIT（`LICENSE`）。YouTube は Google LLC の商標です。本プロジェクトは YouTube / Google とは関係ありません。
