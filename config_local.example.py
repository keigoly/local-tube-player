"""自分の環境用の設定のひな形。config_local.py という名前でコピーして書き換える。

config_local.py は .gitignore 済み（動画フォルダの場所などの個人情報をリポジトリに入れないため）。
ここに書いた値が config.py の既定値を上書きする。書かなかった値は既定値のまま。
"""
from pathlib import Path

# 動画を探すフォルダ（複数可。サブフォルダも見る）
LIBRARY_ROOTS = [
    Path(r"D:\Videos"),
]

# ffmpeg.exe の場所（PATH に通っていれば書かなくてよい）
# FFMPEG = Path(r"C:\tools\ffmpeg\bin\ffmpeg.exe")

# 120fps 変換ツール（任意。仕様は README の「120fps 変換ツールの仕様」）
# CONVERTER_PY = Path(r"C:\tools\fps120\to120.py")
# CONVERTER_PYTHON = r"C:\Python312\python.exe"   # 変換ツールを動かす Python（既定は PATH 上の python）
# CONVERTER_MIN_PER_MIN = 5.0                    # 変換時間の目安（動画1分あたり何分）。変換前の確認に出す
# EXCLUDE_DIR_NAMES = {"_work"}                 # 走査しないフォルダ名（変換ツールの作業フォルダなど）
# EXCLUDE_DIR_PREFIXES = ("_tmp_",)             # 走査しないフォルダ名の先頭
