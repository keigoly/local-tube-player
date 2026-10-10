"""Windows 用の OS 依存処理（Win32 API・エクスプローラー・taskkill）。

osdeps.py から読み込まれる。ほかのモジュールはここを直接 import せず、osdeps 経由で使う。
macOS 用の同じ名前の関数は platform_mac.py にある（関数の名前と引数はそろえること）。

ここに置くのは OS の API を呼ぶ薄い部品だけ。画面に出す文言・ログ・判断（何秒待つか、失敗したら
どうするか）は呼び出し側（desktop.py / fileops.py / jobs.py）に残す。
中身は desktop.py / fileops.py / jobs.py から動作を変えずに移したもの（macOS 移植の Step 1-0）。
"""
import ctypes
import os
import subprocess
import uuid
from ctypes import wintypes
from pathlib import Path

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
    if _user32.IsIconic(hwnd):
        _restore(hwnd)              # 最小化されていても戻す
    _user32.SetForegroundWindow(hwnd)
    return True


def _restore(hwnd):
    """最小化された窓を、タスクバーのボタンを押したときと同じ WM_SYSCOMMAND / SC_RESTORE で戻す。

    ShowWindow(SW_RESTORE) を別のプロセス・スレッドから呼ぶと、窓は戻るのに WebView2 の描画が
    窓に映らないまま残り、音だけ出て画面が真っ黒になった（2026-10-10。ページ側は描画を続けていて、
    窓の大きさを変えると映る）。送り先が固まっていたら待たずに諦める（SMTO_ABORTIFHUNG・最大 5 秒）。
    """
    WM_SYSCOMMAND, SC_RESTORE, SMTO_ABORTIFHUNG = 0x0112, 0xF120, 0x0002
    result = ctypes.c_size_t()
    _user32.SendMessageTimeoutW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0, SMTO_ABORTIFHUNG, 5000,
                                ctypes.byref(result))


def set_app_id(app_id):
    """タスクバーで python 本体と別のアプリとして扱わせる（アイコンとグループ分け）。"""
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


# ─────────────────────── タスクバーのピン留め ───────────────────────
_PINNED_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Internet Explorer" / \
    "Quick Launch" / "User Pinned" / "TaskBar"


def pinned_shortcuts(app_id):
    """タスクバーのピン留めのうち、app_id を持つもの。[(ファイル名, 行き先が python か), ...]

    同じ ID のピン留めがあると、タスクバーは実行中のウィンドウをそのピン留めの
    アイコンと名前で表示する。ウィンドウからピン留めすると python が行き先になるため、それを見分ける。
    .lnk の中身は解析せず、ID と python の実行ファイル名が含まれるかをバイト列で調べるだけ。
    """
    key = app_id.encode("utf-16-le")
    found = []
    for lnk in sorted(_PINNED_DIR.glob("*.lnk")):
        data = lnk.read_bytes()
        if key not in data:
            continue
        lower = data.lower()
        to_python = any(n in lower for n in (b"pythonw.exe", b"python.exe",
                                             "pythonw.exe".encode("utf-16-le"),
                                             "python.exe".encode("utf-16-le")))
        found.append((lnk.name, to_python))
    return found


# SHGetPropertyStoreForWindow（ウィンドウごとの AppUserModel の値）で使う型
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    # 値は文字列（VT_LPWSTR）だけを使う。大きさは本物に合わせる（x64 で 24 バイト）
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                ("pwszVal", ctypes.c_wchar_p), ("_pad", ctypes.c_void_p)]


_IID_IPropertyStore = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
_FMTID_AppUserModel = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
_PID_RELAUNCH_COMMAND, _PID_RELAUNCH_ICON, _PID_RELAUNCH_NAME, _PID_APP_ID = 2, 3, 4, 5
_VT_LPWSTR = 31
_shell32 = ctypes.OleDLL("shell32")          # HRESULT が失敗なら OSError
_shell32.SHGetPropertyStoreForWindow.argtypes = [
    wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32 = ctypes.WinDLL("ole32")


def _com_method(obj, index, restype, *argtypes):
    """COM オブジェクトの vtable の index 番目の関数。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def set_relaunch_info(hwnd, app_id, command, display_name, icon):
    """ウィンドウからタスクバーにピン留めしたとき、python 本体ではなく command を
    display_name と icon（"パス,番号"）で登録させる（System.AppUserModel.Relaunch*）。

    Relaunch* はウィンドウ自身に ID が付いているときだけ使われるので、ID も付ける（プロセスの ID と同じ値）。
    IPropertyStore の vtable: 2=Release, 6=SetValue, 7=Commit。
    """
    hr = _ole32.CoInitializeEx(None, 0x2)    # COINIT_APARTMENTTHREADED（呼ばれるのは pywebview のイベントのスレッド）
    store = ctypes.c_void_p()
    try:
        _shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(_IID_IPropertyStore),
                                             ctypes.byref(store))
        set_value = _com_method(store, 6, ctypes.HRESULT,
                                ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
        # ID は最後に付ける（付いた時点で Relaunch* がそろっているように）
        for pid, value in ((_PID_RELAUNCH_COMMAND, command), (_PID_RELAUNCH_NAME, display_name),
                           (_PID_RELAUNCH_ICON, icon), (_PID_APP_ID, app_id)):
            set_value(store, _PROPERTYKEY(_FMTID_AppUserModel, pid),
                      _PROPVARIANT(vt=_VT_LPWSTR, pwszVal=value))
        _com_method(store, 7, ctypes.HRESULT)(store)
    finally:
        if store:
            _com_method(store, 2, ctypes.c_ulong)(store)
        if hr >= 0:
            _ole32.CoUninitialize()


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
        _restore(hwnd)                       # ShowWindow(SW_RESTORE) では画面が黒くなる（_restore を参照）
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
