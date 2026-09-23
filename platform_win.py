"""Windows 用の OS 依存処理（Win32 API・エクスプローラー・taskkill）。

osdeps.py から読み込まれる。ほかのモジュールはここを直接 import せず、osdeps 経由で使う。
macOS 用の同じ名前の関数は platform_mac.py にある（関数の名前と引数はそろえること）。

ここに置くのは OS の API を呼ぶ薄い部品だけ。画面に出す文言・ログ・判断（何秒待つか、失敗したら
どうするか）は呼び出し側（desktop.py / fileops.py / jobs.py）に残す。
中身は desktop.py / fileops.py / jobs.py から動作を変えずに移したもの（macOS 移植の Step 1-0）。
"""
import ctypes
import subprocess
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ─────────────────────── 二重起動の防止 ───────────────────────
def try_single_instance_lock(name):
    """名前付きミューテックスを作る。既にあれば（ほかのプロセスが持っていれば）False。
    それ以外はハンドルを返す。プロセス終了まで保持すること（GCさせない）。"""
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = _kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return handle
    _kernel32.CloseHandle(handle)
    return False


def activate_window(title):
    """タイトルでウィンドウを探し、あれば前面に出して True。"""
    hwnd = _user32.FindWindowW(None, title)
    if not hwnd:
        return False
    _user32.ShowWindow(hwnd, 9)     # SW_RESTORE（最小化されていても戻す）
    _user32.SetForegroundWindow(hwnd)
    return True


def set_app_id(app_id):
    """タスクバーで python 本体と別のアプリとして扱わせる（アイコンとグループ分け）。"""
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


# ─────────────────────────── ダイアログ ───────────────────────────
def alert(owner, text, title, error=False):
    """OK だけのメッセージ。error=True ならエラー、False なら警告のアイコン。owner は window_handle()（0 なら無し）。"""
    MB_ICONERROR, MB_ICONWARNING = 0x10, 0x30
    _user32.MessageBoxW(owner, text, title, MB_ICONERROR if error else MB_ICONWARNING)


def confirm(owner, message, title):
    """OK / キャンセルの確認。既定のボタンはキャンセル。OK なら True。"""
    MB_OKCANCEL, MB_ICONWARNING, MB_DEFBUTTON2, IDOK = 0x1, 0x30, 0x100, 1
    return _user32.MessageBoxW(owner, message, title,
                               MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2) == IDOK


# ─────────────────────────── ウィンドウ ───────────────────────────
# 大きさ・位置はすべて実ピクセル（pywebview の resize は DPI 換算が入るため使わない）
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def window_handle(window):
    """pywebview のウィンドウの HWND。取れなければ 0（呼び出し側はウィンドウを操作しない）。"""
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return 0


def window_rect(hwnd):
    """枠とタイトルバーを含むウィンドウの (x, y, 幅, 高さ)。"""
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def client_size(hwnd):
    """表示領域（枠とタイトルバーを除く）の (幅, 高さ)。"""
    cr = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(cr))
    return cr.right, cr.bottom


def work_area(hwnd):
    """ウィンドウがある画面の、タスクバーを除いた範囲 (左, 上, 右, 下)。"""
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(_user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    work = mi.rcWork
    return work.left, work.top, work.right, work.bottom


def is_zoomed(hwnd):
    """最大化されているか。"""
    return bool(_user32.IsZoomed(hwnd))


def is_minimized(hwnd):
    return bool(_user32.IsIconic(hwnd))


def move_window(hwnd, x, y, w, h):
    """位置と大きさを変える（前後関係とアクティブ状態は変えない）。"""
    SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
    _user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)


def restore_and_focus(hwnd):
    """最小化されていたら戻し、前面に出す。"""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    _user32.SetForegroundWindow(hwnd)


# ─────────────────────── ごみ箱・エクスプローラー ───────────────────────
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


_FO_DELETE = 0x3
_FOF_SILENT = 0x4
_FOF_NOCONFIRMATION = 0x10
_FOF_ALLOWUNDO = 0x40              # ごみ箱へ
_FOF_NOERRORUI = 0x400
_FOF_WANTNUKEWARNING = 0x4000      # ごみ箱に入らず完全削除になるときは警告を出す


def recycle(path):
    """ごみ箱へ送る。送れたら True、ユーザーが取りやめたら False。

    ごみ箱に入らないときは黙って消さず、Windows の警告ダイアログを出させる（FOF_WANTNUKEWARNING）。
    使用中（共有違反）などで送れなければ PermissionError（呼び出し側が再試行する）。
    """
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = str(path) + "\0"            # 末尾は \0 が2つ必要（1つは ctypes が付ける）
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                 | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        return False
    if rc != 0 or path.exists():
        # 使用中（共有違反）のときもここに来るので、呼び出し側（fileops._retry）に再試行させる
        raise PermissionError(rc, f"SHFileOperation code=0x{rc:x}")
    return True


def reveal(path):
    """エクスプローラーで、ファイルを選んだ状態でフォルダを開く。"""
    # 引数はこの形でないと explorer がパスの空白で切ってしまう
    subprocess.Popen(f'explorer /select,"{path}"')


# ─────────────────────────── プロセス ───────────────────────────
def kill_process_tree(proc):
    """プロセスを子プロセスごと止める（taskkill /T）。失敗しても例外は出さない。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
