"""OS ごとに違う処理の窓口。

Windows では platform_win.py、それ以外（macOS）では platform_mac.py の同じ名前の関数を使う。
desktop.py / fileops.py / jobs.py は OS の API を直接呼ばず、ここを通す（sys.platform の分岐を散らさない）。

名前を platform.py にしないこと: desktop.py はリポジトリを sys.path の先頭に入れるので、
標準ライブラリの platform（pywebview や uvicorn が使う）を隠してしまう。
"""
import sys

if sys.platform == "win32":
    import platform_win as _impl
    NAME = "win"
else:
    import platform_mac as _impl
    NAME = "mac" if sys.platform == "darwin" else sys.platform

# 二重起動の防止
try_single_instance_lock = _impl.try_single_instance_lock
activate_window = _impl.activate_window
set_app_id = _impl.set_app_id
# ダイアログ
alert = _impl.alert
confirm = _impl.confirm
# ウィンドウ（hwnd は window_handle() の値。0 のときは操作しないこと）
window_handle = _impl.window_handle
window_rect = _impl.window_rect
client_size = _impl.client_size
work_area = _impl.work_area
is_zoomed = _impl.is_zoomed
is_minimized = _impl.is_minimized
move_window = _impl.move_window
restore_and_focus = _impl.restore_and_focus
# ファイル
recycle = _impl.recycle
reveal = _impl.reveal
# プロセス
kill_process_tree = _impl.kill_process_tree


def describe():
    """起動ログ用: OS・CPU・Python・pywebview の版。"""
    import platform
    from importlib import metadata
    try:
        pywebview = metadata.version("pywebview")
    except metadata.PackageNotFoundError:
        pywebview = "none"
    return (f"name={NAME} os={platform.platform()} arch={platform.machine()} "
            f"python={platform.python_version()} pywebview={pywebview}")
