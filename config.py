"""youtube-ui-player（MyLocalTube）の設定。

自分の環境に合わせる値（動画フォルダの場所・ffmpeg・120fps変換ツール）は、このファイルではなく
同じフォルダの config_local.py に書く（git には含めない）。config_local.example.py をコピーして使う。
"""
import shutil
from pathlib import Path

# --- ライブラリ ---
# 走査するフォルダ（複数可）。サブフォルダも再帰的に見る。
LIBRARY_ROOTS = [Path.home() / "Videos"]
# 取り込む拡張子。mp4/webm はブラウザがそのまま再生できる。
# それ以外（mkv/ts/avi/mov）は再生時に ffmpeg で MP4 へ「詰め替え」て配信する
# （再エンコードはしないので軽いが、シークができない）。
VIDEO_EXTS = {".mp4", ".mkv", ".ts", ".webm", ".mov", ".avi", ".m4v"}
NATIVE_EXTS = {".mp4", ".m4v", ".webm"}     # ブラウザが直接読める＝シーク可能

# 画面で再生できる映像コーデック（ffmpeg の表記）。これ以外の動画は一覧に出さない。
# WebView2 は h264 / hevc / av1 / vp9 を再生できるが、mpeg2video（MPEG-2 映像の .ts など）は
# PIPELINE_ERROR_DECODE で再生できない。
# 容器（mkv/ts）は詰め替え配信で吸収できるが、コーデックは詰め替えでは変わらない。
PLAYABLE_VCODECS = {"h264", "hevc", "av1", "vp9", "vp8"}

# 走査から除外するフォルダ名・名前の先頭（120fps変換ツールの作業フォルダなど。config_local.py で指定）
EXCLUDE_DIR_NAMES = set()
EXCLUDE_DIR_PREFIXES = ()

# --- サーバ ---
HOST = "127.0.0.1"
PORT = 5560

# --- 外部ツール ---
# ffmpeg（サムネイル・コーデック判定・mkv 等の詰め替え配信に使う）。PATH に無ければ config_local.py でフルパスを指定
FFMPEG = Path(shutil.which("ffmpeg") or "ffmpeg")

# 120fps 変換（任意）。設定しなければ 120fps 関連のボタンやメニューは出ない。
# 求める仕様（README の「120fps 変換ツールの仕様」も参照）:
#   <CONVERTER_PYTHON> <CONVERTER_PY> <入力動画>  で実行され、
#   入力と同じフォルダに <名前>_120fps<拡張子> を作る。出力が既にあれば何もせず終了してよい。
#   進捗は tqdm 形式で標準出力に出す（jobs.py が解析して画面に進み具合を出す）。
CONVERTER_PY = None
CONVERTER_PYTHON = "python"
# 変換にかかる時間の目安（動画1分あたり何分か）。指定すると変換前の確認に所要時間の目安を出す
CONVERTER_MIN_PER_MIN = None
# 変換済み判定に使う接尾辞。変換ツールの出力名と揃えること。
CONVERTED_SUFFIX = "_120fps"

# --- アプリのデータ置き場（索引DB・サムネイル・ログ。git には含めない）---
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "library.db"
THUMB_DIR = DATA_DIR / "thumbs"

# サムネイルを切り出す位置（動画長に対する割合）。
# 冒頭は黒画面やロゴが多いので少し進めた位置から取る。
THUMB_POSITION = 0.18
THUMB_WIDTH = 480

# 同時に走らせる変換ジョブ数。変換ツール自体が GPU を使い切るので 1 が妥当。
MAX_CONCURRENT_JOBS = 1

# 自分の環境の設定で上書きする（config_local.py が無ければ上の既定値のまま）
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
