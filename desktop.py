"""MyLocalTube をブラウザではなく独立したウィンドウ（デスクトップアプリ）として起動する。

起動:
  MyLocalTube.exe（launcher/ でビルドするランチャ）→ .venv\\Scripts\\pythonw.exe desktop.py
  開発時は `.venv\\Scripts\\python.exe desktop.py` でもよい（ログがコンソールにも出る）

仕組み:
  - app.py の FastAPI サーバを、このプロセスの別スレッドで起動する
  - WebView2（Windows 標準の Edge エンジン）のウィンドウでそのURLを開く
  - ウィンドウを閉じるとサーバも止まる（変換ジョブがあれば確認してから止める）

ブラウザ版（start.cmd / python app.py）はそのまま使える。
ログ: data/desktop.log
"""
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402

TITLE = "MyLocalTube"
ICON = HERE / "static" / "app.ico"
LOG_PATH = config.DATA_DIR / "desktop.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
MUTEX_NAME = "Local\\MyLocalTube.Desktop"
APP_ID = "MyLocalTube.Desktop"          # タスクバーでの識別子
PORT_FILE = config.DATA_DIR / "desktop.port"   # 起動中のアプリのポート（2つ目の起動がファイルを渡す先）
RUN_ID = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"   # ログの追跡用ID

_T0 = time.perf_counter()
log = logging.getLogger("mlt.desktop")      # "mlt.*"（fileops など）も同じファイルへ出す

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _ms():
    return int((time.perf_counter() - _T0) * 1000)


# ─────────────────────────── ログ ───────────────────────────
def _setup_logging():
    """pythonw には標準出力がないので、print や uvicorn のログもファイルへ向ける。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    handlers = [logging.StreamHandler(stream)]
    if sys.stdout is not stream:            # コンソールから起動したときは画面にも出す
        handlers.append(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter(f"%(asctime)s %(levelname)s run={RUN_ID} %(name)s %(message)s")
    root = logging.getLogger("mlt")
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)
    root.propagate = False


# ─────────────────────── 二重起動の防止 ───────────────────────
def _acquire_single_instance(wait_exit=40.0):
    """既に起動していれば、そのウィンドウを前面に出して None を返す。

    ウィンドウが無いのに起動済みなのは、前回分が終了処理の途中ということ。
    そのときは終わるのを待ってから起動する（何も開かずに終わるのを防ぐ）。
    変換ジョブを止めながらの終了は最大で約35秒かかる（jobs.shutdown 20秒 + taskkill
    15秒）ので、待ち時間はそれより長くする。それでも終わらなければ理由を表示する。
    """
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    deadline = time.time() + wait_exit
    while True:
        handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return handle                   # プロセス終了まで保持する（GCさせない）
        _kernel32.CloseHandle(handle)
        hwnd = _user32.FindWindowW(None, TITLE)
        if hwnd:
            _user32.ShowWindow(hwnd, 9)     # SW_RESTORE（最小化されていても戻す）
            _user32.SetForegroundWindow(hwnd)
            return None
        if time.time() > deadline:
            log.warning("phase=start ok=false previous_instance_still_closing=true")
            _user32.MessageBoxW(0, "前回の MyLocalTube がまだ終了処理中です。\n"
                                   "少し待ってからもう一度起動してください。", TITLE, 0x30)
            return None
        time.sleep(0.2)


def _set_app_id():
    """タスクバーで python 本体と別のアプリとして扱わせる（アイコンとグループ分け）。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as e:
        log.warning("phase=app_id ok=false err=%r", e)


# ─────────────────────────── サーバ ───────────────────────────
def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((config.HOST, port))
            return True
        except OSError:
            return False


def _is_our_server(port):
    """そのポートで動いているのが MyLocalTube（ブラウザ版など）かどうか。"""
    try:
        url = f"http://{config.HOST}:{port}/api/scan/status"
        with urllib.request.urlopen(url, timeout=2) as r:
            return "running" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((config.HOST, 0))
        return s.getsockname()[1]


class _Server:
    """uvicorn をスレッドで動かす。ブラウザ版が同じポートで動いていればそれに相乗りする。"""

    def __init__(self):
        self.server = None
        self.thread = None
        self.port = config.PORT
        self.attached = False

    def start(self, timeout=30.0):
        t0 = time.perf_counter()
        if not _port_free(self.port):
            if _is_our_server(self.port):
                # 変換ジョブのキューはプロセスごとに1つ。サーバを2つ立てると
                # GPU を奪い合う変換が同時に走りうるので、既存のほうを使う。
                self.attached = True
                log.info("phase=server_start mode=attach port=%d ok=true", self.port)
                return f"http://{config.HOST}:{self.port}/"
            busy = self.port
            self.port = _free_port()
            log.warning("phase=server_start port_busy=%d fallback_port=%d", busy, self.port)

        import uvicorn

        import app as app_module
        self.server = uvicorn.Server(uvicorn.Config(
            app_module.app, host=config.HOST, port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        deadline = time.time() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.time() > deadline:
                raise RuntimeError("サーバを起動できませんでした（data/desktop.log を参照）")
            time.sleep(0.05)
        log.info("phase=server_start mode=own port=%d ms=%d ok=true",
                 self.port, (time.perf_counter() - t0) * 1000)
        try:        # 2つ目に起動された MyLocalTube.exe がファイルを渡す先
            PORT_FILE.write_text(str(self.port), encoding="ascii")
        except OSError as e:
            log.warning("phase=server_start port_file_write ok=false err=%r", e)
        return f"http://{config.HOST}:{self.port}/"

    def stop(self):
        if self.attached or not self.server:
            return
        t0 = time.perf_counter()
        import jobs
        finished = jobs.shutdown(timeout=20)
        # ジョブ進捗の SSE（/api/events）を先に閉じる。開いたままだと uvicorn が
        # 接続の終了を待って5秒止まり、その間に再起動すると「起動済み」と判定されて
        # 何も開かなかった。
        import app as app_module
        app_module.request_shutdown()
        self.server.should_exit = True
        self.thread.join(timeout=5)
        log.info("phase=server_stop jobs_stopped=%s ms=%d", finished,
                 (time.perf_counter() - t0) * 1000)


# ─────────────────────── JS から呼べるAPI ───────────────────────
_window = None      # js_api の属性に持たせると pywebview が中身まで公開しようとするので外に置く


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _window_rect(hwnd):
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


class Bridge:
    """画面側（index.html）から window.pywebview.api.xxx() で呼ばれる。"""

    def __init__(self):
        self._fullscreen = False
        self._saved_rect = None     # 動画に合わせる前のウィンドウ位置・大きさ
        self._fitted_rect = None    # 合わせた後の位置・大きさ（ユーザーが変えたか判定する）
        self._pending_restore = False   # 全画面中に「一覧へ戻る」が来たら、全画面を抜けてから戻す

    def fit_window(self, video_w, video_h):
        """ウィンドウの表示領域を動画の縦横比に合わせる（左右・上下の黒帯をなくす）。

        幅はそのまま高さを合わせ、画面（タスクバーを除く）に収まらなければ高さを上限に幅を詰める。
        最大化中・全画面中は触らない。サイズは Win32 で実ピクセルのまま扱う
        （pywebview の resize は DPI 換算が入るため）。
        """
        hwnd = _hwnd()
        try:
            vw, vh = float(video_w), float(video_h)
        except (TypeError, ValueError):
            return False
        if not hwnd or vw <= 0 or vh <= 0 or self._fullscreen \
                or _user32.IsZoomed(hwnd) or _user32.IsIconic(hwnd):
            return False
        x, y, w, h = _window_rect(hwnd)
        cr = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(cr))
        cw, ch = cr.right, cr.bottom
        cur_cw, cur_ch = cw, ch                          # いま実際に表示されている大きさ
        frame_w, frame_h = w - cw, h - ch                # 枠とタイトルバーの分
        if self._saved_rect is not None and (x, y, w, h) == self._fitted_rect:
            # プレイヤーの中で別の動画に切り替えたとき（縦長 → 横長など）は、直前の動画に合わせた
            # 大きさではなく、ユーザーが決めていた元の大きさを基準にする
            x, y, w, h = self._saved_rect
            cw, ch = w - frame_w, h - frame_h
        _user32.MonitorFromWindow.restype = wintypes.HMONITOR
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW(_user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
        work = mi.rcWork
        max_cw = (work.right - work.left) - frame_w
        max_ch = (work.bottom - work.top) - frame_h
        aspect = vw / vh
        new_cw, new_ch = cw, round(cw / aspect)
        if new_ch > max_ch:
            new_ch, new_cw = max_ch, round(max_ch * aspect)
        if new_cw > max_cw:
            new_cw, new_ch = max_cw, round(max_cw / aspect)
        if abs(new_cw - cur_cw) <= 1 and abs(new_ch - cur_ch) <= 1:
            return True                                  # もう合っている
        nw, nh = new_cw + frame_w, new_ch + frame_h
        # 中心の位置を保ちつつ、画面からはみ出さないようにする
        nx = min(max(x + (w - nw) // 2, work.left), work.right - nw)
        ny = min(max(y + (h - nh) // 2, work.top), work.bottom - nh)
        if self._saved_rect is None:
            self._saved_rect = (x, y, w, h)
        SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
        _user32.SetWindowPos(hwnd, None, nx, ny, nw, nh, SWP_NOZORDER | SWP_NOACTIVATE)
        self._fitted_rect = _window_rect(hwnd)           # 最小サイズで止まることもあるので実測
        log.info("phase=fit_window video=%dx%d client=%dx%d->%dx%d", vw, vh, cur_cw, cur_ch,
                 new_cw, new_ch)
        return True

    def restore_window(self):
        """一覧に戻ったら、動画に合わせる前の大きさに戻す。
        再生中にユーザーが自分で大きさを変えていたら、その大きさを尊重して戻さない。"""
        if self._fullscreen:
            # ここで戻しても、全画面を抜けるときに pywebview が全画面前（＝動画に合わせた）
            # 大きさへ戻して上書きしてしまう。set_fullscreen(False) のあとで戻す
            self._pending_restore = True
            return False
        self._pending_restore = False
        hwnd = _hwnd()
        saved, fitted = self._saved_rect, self._fitted_rect
        self._saved_rect = self._fitted_rect = None
        if not hwnd or saved is None or _user32.IsZoomed(hwnd):
            return False
        if _window_rect(hwnd) != fitted:
            return False
        SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
        _user32.SetWindowPos(hwnd, None, *saved, SWP_NOZORDER | SWP_NOACTIVATE)
        log.info("phase=restore_window rect=%s", saved)
        return True

    def set_fullscreen(self, on):
        """WebView2 では動画の全画面化がウィンドウの枠の中だけで起きる。
        ウィンドウ自体も全画面にして、ブラウザで見たときと同じにする。"""
        on = bool(on)
        if _window is not None and on != self._fullscreen:
            self._fullscreen = on
            _window.toggle_fullscreen()
            log.info("phase=fullscreen on=%s", on)
            if not on and self._pending_restore:
                self.restore_window()
        return self._fullscreen

    def client_log(self, level, message):
        """画面側のエラーを desktop.log に残す（アプリ版は開発者ツールが見えないため）。"""
        log.log(logging.WARNING if level != "info" else logging.INFO,
                "phase=client %s", str(message)[:2000])


# ─────────────────────────── ウィンドウ ───────────────────────────
def _hwnd():
    try:
        return int(_window.native.Handle.ToInt64())
    except Exception:
        return 0


def _confirm(message):
    MB_OKCANCEL, MB_ICONWARNING, MB_DEFBUTTON2, IDOK = 0x1, 0x30, 0x100, 1
    return _user32.MessageBoxW(_hwnd(), message, TITLE,
                               MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2) == IDOK


def _on_closing():
    """False を返すと閉じるのを取りやめる。"""
    import jobs
    n = jobs.active_count()
    if n == 0:
        return True
    ok = _confirm(f"120fps変換のジョブが {n} 件あります（実行中・待機中）。\n\n"
                  "終了すると変換は中断され、作りかけのファイルは削除されます。\n"
                  "終了しますか？")
    log.info("phase=closing active_jobs=%d confirmed=%s", n, ok)
    return ok


def _initial_size():
    """画面の8割ほどの大きさで開く。"""
    try:
        import webview
        s = webview.screens[0]
        return max(1100, int(s.width * 0.82)), max(700, int(s.height * 0.84))
    except Exception:
        return 1500, 900


# ───────────── ファイルを開く（エクスプローラーのダブルクリック・「プログラムから開く」）─────────────
def _video_arg(argv):
    """起動時の引数から、開く動画ファイルを1つ取り出す（MyLocalTube.exe "%1"）。"""
    for a in argv:
        if os.path.isfile(a):
            return os.path.abspath(a)
    return None


def _post_open(port, path, timeout=30):
    body = json.dumps({"path": path}).encode("utf-8")
    req = urllib.request.Request(f"http://{config.HOST}:{port}/api/open", data=body,
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _show_video(v):
    """動画をウィンドウで開く。app.open_hook として /api/open から呼ばれる。"""
    if _window is None:
        return
    hwnd = _hwnd()
    if hwnd:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)          # 最小化されていたら戻す
        _user32.SetForegroundWindow(hwnd)
    try:
        _window.evaluate_js("openFromApp(" + json.dumps(v) + ")")
        log.info("phase=open id=%s external=%s playable=%s path=%s",
                 v.get("id"), v.get("external", 0), v.get("playable"), v.get("path"))
    except Exception as e:
        log.warning("phase=open ok=false err=%r", e)


def _forward_to_running(path):
    """既に開いているアプリへファイルを渡す（ウィンドウを増やさない）。"""
    t0 = time.perf_counter()
    try:
        port = int(PORT_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        port = config.PORT
    try:
        _post_open(port, path)
        log.info("phase=forward_open port=%d ms=%d ok=true path=%s",
                 port, (time.perf_counter() - t0) * 1000, path)
    except Exception as e:
        # 古い版のアプリが開いている（/api/open が無い）ときなど
        log.warning("phase=forward_open port=%d ok=false err=%r path=%s", port, e, path)
        _user32.MessageBoxW(0, "開いている MyLocalTube に動画を渡せませんでした。\n"
                               "MyLocalTube を一度閉じてから、もう一度開いてください。\n\n" + path,
                            TITLE, 0x30)


def main(on_ready=None, argv=None):
    """on_ready(window) … 画面の読み込み完了後に別スレッドで呼ぶ（動作確認用）。
    argv … 起動時の引数（既定は sys.argv[1:]）。動画ファイルがあればそれを開く。"""
    _setup_logging()
    argv = sys.argv[1:] if argv is None else argv
    log.info("phase=start argv=%s python=%s", argv, sys.executable)
    video = _video_arg(argv)

    mutex = _acquire_single_instance()
    if mutex is None:
        log.info("phase=start already_running=true -> activated existing window")
        if video:
            _forward_to_running(video)
        return 0
    _set_app_id()

    server = _Server()
    try:
        url = server.start()
    except Exception as e:
        log.exception("phase=server_start ok=false err=%r", e)
        _user32.MessageBoxW(0, f"起動できませんでした。\n\n{e}", TITLE, 0x10)
        return 1

    import webview
    global _window
    width, height = _initial_size()
    _window = webview.create_window(
        # 最小幅を小さめにしておくのは、縦長の動画でもウィンドウを動画に合わせられるように
        TITLE, url, width=width, height=height, min_size=(640, 400),
        background_color="#0f0f0f", js_api=Bridge())
    _window.events.closing += _on_closing
    if not server.attached:
        import app as app_module
        app_module.open_hook = _show_video

    def _open_initial():
        """起動時に渡された動画を開く。自分のサーバなら /api/open → open_hook が表示する。"""
        try:
            v = _post_open(server.port, video)
            if server.attached:              # 相乗り中は相手のサーバにフックが無いので自分で表示
                _show_video(v)
        except Exception as e:
            log.warning("phase=open_initial ok=false err=%r path=%s", e, video)
            _user32.MessageBoxW(_hwnd(), f"動画を開けませんでした。\n\n{video}\n\n{e}", TITLE, 0x30)

    opened = []                              # loaded は再読み込みのたびに来るので、開くのは最初の1回だけ

    def _loaded():
        log.info("phase=page_loaded ms_since_start=%d", _ms())
        if video and not opened:
            opened.append(True)
            threading.Thread(target=_open_initial, daemon=True).start()
        if on_ready:
            threading.Thread(target=on_ready, args=(_window,), daemon=True).start()
    _window.events.loaded += _loaded
    _window.events.shown += lambda: log.info("phase=window_shown ms_since_start=%d", _ms())

    debug = os.environ.get("MLT_DEBUG") == "1"      # 1 にすると開発者ツールが開く
    webview.start(gui="edgechromium", debug=debug, private_mode=False,
                  storage_path=str(config.DATA_DIR / "webview"), icon=str(ICON))

    log.info("phase=window_closed ms_since_start=%d", _ms())
    server.stop()
    log.info("phase=exit ok=true")
    return 0


if __name__ == "__main__":
    code = main()
    # pythonnet(.NET) の後始末やデーモンスレッドで終了が遅れると、
    # 見えないプロセスがポートを握ったまま残り、次の起動が相乗りモードになってしまう。
    logging.shutdown()
    os._exit(code)
