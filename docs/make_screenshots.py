"""README 用の画面写真（docs/images/*.png）を撮る。

個人の動画が写り込まないよう、ffmpeg の合成映像（マンデルブロ・テストパターン等）で作った
サンプル動画だけを並べた一時ライブラリで、専用のアプリ（使用中のアプリとは別の二重起動防止名・
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

# (フォルダ, 名前, lavfi の映像ソース, 秒数)
SAMPLES = [
    ("サンプル", "マンデルブロ集合をゆっくり拡大", "mandelbrot=s=1280x720:rate=30", 40),
    ("サンプル", "グラデーションの流れ", "gradients=s=1280x720:speed=0.02:rate=30", 95),
    ("サンプル", "テストパターン（HD）", "smptehdbars=s=1280x720:rate=30", 12),
    ("サンプル", "ライフゲーム", "life=s=1280x720:mold=10:rate=30:ratio=0.1:death_color=#1e2a3a:life_color=#29b6f6", 180),
    ("サンプル\\模様", "セルオートマトン", "cellauto=s=1280x720:rule=110:rate=30", 30),
    ("サンプル\\模様", "シェルピンスキーの図形", "sierpinski=s=1280x720:rate=30", 25),
    ("デモ", "カラーテスト", "testsrc2=s=1280x720:rate=60", 20),
    ("デモ", "ゾーンプレート", "zoneplate=s=1280x720:rate=30:kt2=2:ky=2", 15),
    ("デモ", "カラーテスト_120fps", "testsrc2=s=1280x720:rate=120", 20),
]


def make_library():
    ffmpeg = str(config.FFMPEG)
    for folder, name, src, sec in SAMPLES:
        d = LIB / folder
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{name}.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"{src},format=yuv420p",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", str(sec), "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                        "-c:a", "aac", "-shortest", str(out)], check=True)
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
        cdp.js("V.muted = true; openVideo(state.videos.find(v => v.name.startsWith('マンデルブロ')).id); true")
        wait_for(cdp, "V.readyState >= 3")
        cdp.js("V.currentTime = 14; setBoost(1); true")
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
        cdp.js("[...document.querySelectorAll('.modal .dir')].find(b => b.title.endsWith('模様')).click(); true")
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
