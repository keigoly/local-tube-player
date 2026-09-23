# launcher/ — アプリ版の起動用 exe

## 1. このディレクトリの役割

`MyLocalTube.exe`（プロジェクト直下に出力）を作るためのソース一式。

| ファイル | 役割 |
|---|---|
| `MyLocalTube.cs` | ランチャ本体（C#）。exe と同じフォルダの `.venv\Scripts\pythonw.exe desktop.py` を起動して即終了する |
| `make_icon.py` | アイコン `static/app.ico` と README 用の `docs/images/icon.png` を生成する（UIのロゴと同じ水色の角丸＋再生マーク） |
| `build.cmd` | 上の2つから exe とアイコンを作り直す |
| `file_assoc.py` | 動画ファイルの関連付け（既定のアプリ候補への登録）。`register` / `status` / `unregister` |

### 既定のアプリ（ダブルクリックで MyLocalTube が開く）

`.venv\Scripts\python.exe launcher\file_assoc.py register` で、.mp4 / .m4v / .mkv / .webm / .mov を
このユーザーの範囲（HKCU）に登録し、Windows の「既定のアプリ」設定ページを開く。
**既定そのものの切り替えは Windows の仕様でアプリからはできない**（UserChoice がハッシュで保護されている）ので、
開いた画面で MyLocalTube を選ぶのは人の操作。`status` で現在の既定を確認できる。
.ts は登録しない（MPEG-2 映像は再生できない）。exe を移動したら `register` をやり直す。

ファイルを渡されたときの動き（desktop.py）: 引数のファイルを `/api/open` で開く。既にアプリが開いていれば
新しいウィンドウは作らず、`data/desktop.port` のポートへ渡して終了する。
ライブラリ外のファイルは索引に入れず、負の id で一時的に扱う（移動・削除・120fps化は不可、エクスプローラー表示のみ）。

### なぜ PyInstaller で固めず、薄いランチャにしたか

- **コードを直しても exe を作り直さなくてよい。** 画面（index.html）やサーバ（*.py）は
  毎回ソースから読み込まれる。PyInstaller だと変更のたびに再ビルドが必要で、
  忘れると「直したのに反映されない」状態になる
- **データの置き場所が変わらない。** PyInstaller の onedir は再ビルドで出力フォルダを
  作り直すので、中に `data/`（索引DB・サムネイル）を置くと消える
- C# コンパイラ（`csc.exe`）は .NET Framework 4 に同梱されていて、Windows 10/11 なら追加インストール不要。
  出来上がる exe は約19KB

`.venv` はプロジェクト直下にあるので、**exe はプロジェクト直下から動かさないこと**。
別の場所から起動したいときはショートカットを作る（デスクトップやスタートメニューに置く）。

## 2. 現在の問題点

- **タスクバーへのピン留めは不完全。** 実行中のウィンドウは pythonw.exe のものなので、
  ウィンドウから「タスクバーにピン留め」すると python がピン留めされてしまう。
  デスクトップのショートカットを右クリックしてピン留めするのが確実
  （desktop.py で AppUserModelID を設定しているので、アイコンとグループ分けは正しく出る）

## 3. バグ修正時の手順

`../DEVELOPMENT.md` §6 の段階的アプローチ（Step 1 → 2 → 3）に従う。

1. **Step 1: 見える化** — 起動の失敗は `data/desktop.log` を見る（`phase=start` →
   `server_start` → `window_shown` → `page_loaded` の順に、起動からの ms が記録される）。
   ランチャ自体が失敗したときは、exe がメッセージボックスで不足ファイルのパスを出す
2. **Step 2: 最小改修** — `MyLocalTube.cs` を直したら `build.cmd` を実行する。
   `build.cmd` は **ASCII のみで書くこと**（cmd.exe は .cmd を cp932 で読むため、
   UTF-8 の日本語を書くと行末が壊れることがある）
3. **Step 3: 確認** — exe をダブルクリックしてウィンドウが開くこと、
   もう一度ダブルクリックすると既存のウィンドウが前面に出ること、
   閉じてすぐ起動し直しても開くことを確かめる

## 4. 関連ドキュメント

- `../DEVELOPMENT.md` — プロジェクト全体の開発メモ（§4.2 アプリ化・§4.4 既定のアプリ）
- `../desktop.py` — ランチャが起動するアプリ本体
