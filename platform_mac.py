"""macOS 用の OS 依存処理（まだ空の実装）。

osdeps.py から読み込まれる（Windows 以外のとき）。関数の名前と引数は platform_win.py とそろえる。

いまは形だけをそろえ、呼ばれたことをログに残す（phase=platform fn=<名前> impl=stub ms=）。
Mac で起動したときに、どの処理がまだ無いかを data/desktop.log で確かめるため。
本物の実装（NSWindow・NSFileManager・open -R・fcntl.flock など）は次の段階で入れる。

空の実装の約束:
  - 成功したふりをしない。ファイルをごみ箱へ送る・Finder で表示するは NotImplementedError で止める
    （黙って完全削除しない・何もせずに「できました」と返さない）
  - ウィンドウの操作は何もしない。window_handle() が 0 を返すので、呼び出し側はそもそも操作しない
"""
import functools
import logging
import time

log = logging.getLogger("mlt.platform")


def _stub(fn):
    """呼ばれたことと、かかった時間をログに残す。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            log.info("phase=platform fn=%s impl=stub ms=%d", fn.__name__,
                     (time.perf_counter() - t0) * 1000)
    return wrapper


# ─────────────────────── 二重起動の防止 ───────────────────────
@_stub
def try_single_instance_lock(name):
    """まだ二重起動を防がない（いつでも取れたことにする）。"""
    return object()


@_stub
def activate_window(title):
    return False


@_stub
def set_app_id(app_id):
    """macOS では .app の Info.plist が同じ役目をするので、ここでは何もしない。"""


# ─────────────────────────── ダイアログ ───────────────────────────
@_stub
def alert(owner, text, title, error=False):
    """まだ画面に出さない。文言が失われないようにログへ残す。"""
    log.warning("phase=platform fn=alert error=%s text=%r", error, text)


@_stub
def confirm(owner, message, title):
    """まだ確認を出さない。閉じられなくならないよう、OK（True）として扱う。"""
    log.warning("phase=platform fn=confirm answer=True(stub) message=%r", message)
    return True


# ─────────────────────────── ウィンドウ ───────────────────────────
@_stub
def window_handle(window):
    """0 を返す。呼び出し側はウィンドウを操作しない（動画に合わせた大きさ変更などは無効）。"""
    return 0


@_stub
def window_rect(hwnd):
    return 0, 0, 0, 0


@_stub
def client_size(hwnd):
    return 0, 0


@_stub
def work_area(hwnd):
    return 0, 0, 0, 0


@_stub
def is_zoomed(hwnd):
    return False


@_stub
def is_minimized(hwnd):
    return False


@_stub
def move_window(hwnd, x, y, w, h):
    pass


@_stub
def restore_and_focus(hwnd):
    pass


# ─────────────────────── ごみ箱・Finder ───────────────────────
@_stub
def recycle(path):
    raise NotImplementedError("macOS のゴミ箱へ移す処理はまだありません")


@_stub
def reveal(path):
    raise NotImplementedError("macOS の「Finder で表示」はまだありません")


# ─────────────────────────── プロセス ───────────────────────────
@_stub
def kill_process_tree(proc):
    """いまは本体だけを止める（子プロセスは残りうる。変換ツールは macOS ではまだ使わない）。"""
    try:
        proc.kill()
    except Exception:
        pass
