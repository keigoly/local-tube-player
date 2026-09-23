"""README 用の画面写真（docs/images/*.png）を撮る。

個人の動画が写り込まないよう、フリー素材（Blender Foundation のオープンムービー、CC BY）から
切り出したサンプル動画だけを並べた一時ライブラリで、専用のアプリ（使用中のアプリとは別の二重起動防止名・
ポート・データ置き場）を起動して撮る。撮影中は 30 秒ほどウィンドウが表示される。

    .venv\\Scripts\\python docs\\make_screenshots.py
"""
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "images"
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="mlt_shots_"))
LIB = WORK / "library"
CDP_PORT = 9341
WIN_W, WIN_H = 1440, 870

# 素材: Blender Foundation のオープンムービー（どちらも CC BY 3.0）
#   Big Buck Bunny  (c) copyright 2008, Blender Foundation / www.bigbuckbunny.org
#   Sintel          (c) copyright Blender Foundation | durian.blender.org
# Sintel は必要な部分だけを ffmpeg で切り出す。Big Buck Bunny は zip でしか配られていないので、
# 初回だけ丸ごと（約 275MB）落としてキャッシュする。
SINTEL = "https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv"
BBB_ZIP = "https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4.zip"
BBB = "BBB"          # 下で実際のパスに置き換える
# (フォルダ, 名前, 素材, 開始秒, 秒数, fps（None ならそのまま）)
# サムネイルは動画の 18% の位置から作られるので、見せたい場面 − 秒数×0.18 を開始にしている
SAMPLES = [
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 1", BBB, 63, 40, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 2", BBB, 111, 50, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 3", BBB, 190, 30, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 4", BBB, 362, 45, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 5", BBB, 440, 25, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 6", BBB, 14, 35, None),
    ("オープンムービー\\Sintel", "Sintel 抜粋 1", SINTEL, 333, 40, None),
    ("オープンムービー\\Sintel", "Sintel 抜粋 2", SINTEL, 375, 30, None),
    ("オープンムービー\\Sintel", "Sintel 抜粋 3", SINTEL, 735, 25, None),
    ("オープンムービー\\Big Buck Bunny", "Big Buck Bunny 抜粋 2_120fps", BBB, 111, 50, 120),  # バッジ用
]


def _bbb_path():
    """Big Buck Bunny を用意する（初回だけ zip を落として取り出す）。"""
    import urllib.request
    import zipfile
    mp4 = CACHE / "bbb_sunflower_1080p_30fps_normal.mp4"
    if not mp4.exists():
        z = CACHE / "bbb_sunflower_1080p_30fps_normal.mp4.zip"
        if not z.exists():
            print("  downloading Big Buck Bunny (~275MB, first time only)...")
            urllib.request.urlretrieve(BBB_ZIP, z)
        with zipfile.ZipFile(z) as f:
            f.extract(mp4.name, CACHE)
    return str(mp4)


# 切り出した素材の置き場。次回からは使い回す（毎回ダウンロードしない）
CACHE = Path(tempfile.gettempdir()) / "mlt_screenshot_clips"


def _duration(path):
    import library
    return (library.probe(path)[0] or 0) if path.exists() else 0


def make_library():
    ffmpeg = str(config.FFMPEG)
    CACHE.mkdir(exist_ok=True)
    for folder, name, url, start, sec, fps in SAMPLES:
        if url == BBB:
            url = _bbb_path()
        clip = CACHE / f"{name}_{start}_{sec}_{fps}.mp4"   # 場面を変えたら取り直すよう、条件も名前に入れる
        # 1280x720 にそろえる（シネスコの作品は上下に帯を付ける）
        vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2"
        if fps:
            vf += f",fps={fps}"
        for attempt in range(3):              # 途中で切断されて短いファイルになったら取り直す
            if _duration(clip) >= sec * 0.95:
                break
            subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(start), "-i", url, "-t", str(sec),
                            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                            "-c:a", "aac", "-ac", "2", str(clip)], check=False)
        if _duration(clip) < sec * 0.95:
            sys.exit(f"素材を取得できませんでした: {name}（{url}）")
        d = LIB / folder
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(clip, d / f"{name}.mp4")
        print(f"  clip: {name} ({_duration(clip):.0f}s)")
    print(f"sample library: {LIB}")


# ─────────────── 撮影（WebView2 の開発者ツール用プロトコルで操作し、PrintWindow で撮る）───────────────
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")


def printwindow(hwnd, path):
    from PIL import Image
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)            # PW_RENDERFULLCONTENT（他の窓に隠れていても撮れる）

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    # Windows 11 のウィンドウ外周の見えない枠（影の領域）を切り落とす
    cr = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    border = (w - cr.right) // 2
    img = img.crop((border, 0, w - border, h - border))
    img.save(path, optimize=True)
    print(f"  saved {path.name} {img.size}")


class CDP:
    def __init__(self):
        from websockets.sync.client import connect
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5).read())
        page = next(t for t in targets if t.get("type") == "page")
        self.ws = connect(page["webSocketDebuggerUrl"], max_size=None)
        self.n = 0

    def call(self, method, params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                return m.get("result", {})

    def js(self, expr, gesture=False):
        res = self.call("Runtime.evaluate", {"expression": expr, "userGesture": gesture,
                                             "awaitPromise": True, "returnByValue": True})
        return res.get("result", {}).get("value")

    def move(self, x, y):
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def box(self, selector):
        return self.js(f"(() => {{ const r = document.querySelector({json.dumps(selector)})"
                       ".getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")


def wait_for(cdp, expr, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        if cdp.js(expr):
            return True
        time.sleep(0.3)
    raise TimeoutError(expr)


def shoot(window):
    import desktop
    try:
        hwnd = desktop._hwnd()
        cdp = CDP()
        # 一覧: サムネイルが全部出そろうまで待つ
        # 初回起動の走査が終わり（watchScan が止まり）、全部の動画が並ぶまで待つ
        wait_for(cdp, "!watchScan.timer && document.querySelectorAll('.card').length >= %d"
                 % len(SAMPLES), timeout=180)
        wait_for(cdp, "[...document.querySelectorAll('.card img')].every(i => i.complete && i.naturalWidth > 0)")
        cdp.move(5, 5)
        time.sleep(0.8)
        printwindow(hwnd, OUT / "library.png")

        # ⋮ メニュー（カードにカーソルを乗せて開いた状態）
        x, y, w, h = cdp.box(".card:nth-child(2) .kebab")
        cdp.move(x + w / 2, y + h / 2)
        cdp.js("document.querySelector('.card:nth-child(2) .kebab').click(); true")
        time.sleep(0.6)
        printwindow(hwnd, OUT / "menu.png")
        cdp.js("closeMenu(); true")

        # 再生画面: 操作バーを出し、シークバーにカーソルを乗せた状態
        cdp.js("V.muted = true; openVideo(state.videos.find(v => v.name === 'Big Buck Bunny 抜粋 2').id); true")
        wait_for(cdp, "V.readyState >= 3")
        cdp.js("V.currentTime = 10; setBoost(1); true")
        time.sleep(2.5)
        x, y, w, h = cdp.box("#ytProg")
        cdp.move(x + w * 0.60, y + h / 2)     # 1回目は「乗った」だけで時刻表示が更新されないので2回動かす
        time.sleep(0.2)
        cdp.move(x + w * 0.62, y + h / 2)
        time.sleep(0.8)
        printwindow(hwnd, OUT / "player.png")

        # 設定メニュー（⚙）
        cdp.js("E.set.click(); true")
        x, y, w, h = cdp.box("#ytSet")
        cdp.move(x + w / 2, y + h / 2)
        time.sleep(0.6)
        printwindow(hwnd, OUT / "settings.png")
        cdp.js("closeSettings(); true")

        # フォルダへ移動のダイアログ（撮るだけで移動はしない）
        cdp.js("moveVideo(state.current); true")
        wait_for(cdp, "!!document.querySelector('.modal .dir')")
        cdp.js("[...document.querySelectorAll('.modal .dir')].find(b => b.title.endsWith('Sintel')).click(); true")
        time.sleep(0.6)
        printwindow(hwnd, OUT / "move.png")
        cdp.js("document.querySelector('.modal [data-a=cancel]').click(); true")
    except Exception:
        traceback.print_exc()
    finally:
        window.destroy()


def main():
    if not shutil.which(str(config.FFMPEG)) and not Path(config.FFMPEG).exists():
        sys.exit("ffmpeg が見つかりません（config_local.py の FFMPEG を確認）")
    OUT.mkdir(exist_ok=True)
    make_library()
    # 撮影用の設定（個人の設定・データ・使用中のアプリとは完全に分ける）
    config.LIBRARY_ROOTS = [LIB]
    config.DATA_DIR = WORK / "data"
    config.DB_PATH = config.DATA_DIR / "library.db"
    config.THUMB_DIR = config.DATA_DIR / "thumbs"
    config.PORT = 5570
    config.CONVERTER_PY = Path(__file__)          # 「120」ボタンを出すためのダミー（変換はしない）
    config.CONVERTER_MIN_PER_MIN = None
    import webview

    import desktop
    webview.settings["REMOTE_DEBUGGING_PORT"] = CDP_PORT
    desktop.MUTEX_NAME = "Local\\MyLocalTube.Screenshots"
    desktop.PORT_FILE = config.DATA_DIR / "desktop.port"
    desktop.LOG_PATH = config.DATA_DIR / "desktop.log"
    desktop._initial_size = lambda: (WIN_W, WIN_H)
    shot_started = []

    def on_ready(window):
        if not shot_started:                  # loaded は再読み込みのたびに来る
            shot_started.append(True)
            threading.Thread(target=shoot, args=(window,), daemon=True).start()
    desktop.main(on_ready=on_ready, argv=[])
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"done: {OUT}")


if __name__ == "__main__":
    main()
    import os
    os._exit(0)
