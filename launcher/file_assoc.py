"""MyLocalTube を「動画を開けるアプリ」として Windows に登録する（このユーザーだけ・管理者権限不要）。

    .venv\\Scripts\\python.exe launcher\\file_assoc.py register     登録して「既定のアプリ」の設定画面を開く
    .venv\\Scripts\\python.exe launcher\\file_assoc.py status       いまの既定のアプリを表示
    .venv\\Scripts\\python.exe launcher\\file_assoc.py unregister   登録を消す（既定も元のアプリに戻る）

既定のアプリそのものは Windows の仕様でアプリからは変えられない（改ざん防止のハッシュで守られている）。
登録したあと、開いた設定画面で MyLocalTube を選ぶのは人の操作になる。

登録する拡張子に .ts を入れていないのは、MPEG-2 映像の .ts が再生できないため。
exe を別の場所へ動かしたら register をやり直すこと（コマンドに exe の場所を書き込んでいるため）。
"""
import ctypes
import os
import sys
import winreg
from pathlib import Path

EXE = Path(__file__).resolve().parent.parent / "MyLocalTube.exe"
EXTS = [".mp4", ".m4v", ".mkv", ".webm", ".mov"]
PROGID = "MyLocalTube.Video"
APP_NAME = "MyLocalTube"                          # RegisteredApplications に載せる名前
CAPS = r"Software\MyLocalTube\Capabilities"
CLASSES = r"Software\Classes"
HKCU = winreg.HKEY_CURRENT_USER


def _set(path, name, value, kind=winreg.REG_SZ):
    with winreg.CreateKeyEx(HKCU, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, kind, value)


def _delete_tree(path):
    """自分で作ったキーだけを消す（無ければ何もしない）。"""
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + sub)
        winreg.DeleteKey(HKCU, path)
    except FileNotFoundError:
        pass


def _delete_value(path, name):
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_WRITE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def _notify():
    """エクスプローラーに関連付けが変わったことを知らせる（SHCNE_ASSOCCHANGED）。"""
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def register():
    if not EXE.exists():
        sys.exit(f"MyLocalTube.exe が見つかりません: {EXE}（launcher\\build.cmd で作ってください）")
    command = f'"{EXE}" "%1"'
    icon = f'"{EXE}",0'
    # ProgID: ダブルクリックで実行するコマンドとアイコン
    _set(f"{CLASSES}\\{PROGID}", "", "MyLocalTube の動画")
    _set(f"{CLASSES}\\{PROGID}\\DefaultIcon", "", icon)
    _set(f"{CLASSES}\\{PROGID}\\shell\\open", "FriendlyAppName", APP_NAME)
    _set(f"{CLASSES}\\{PROGID}\\shell\\open\\command", "", command)
    # 「プログラムから開く」の一覧に出す
    app = f"{CLASSES}\\Applications\\MyLocalTube.exe"
    _set(app, "FriendlyAppName", APP_NAME)
    _set(f"{app}\\DefaultIcon", "", icon)
    _set(f"{app}\\shell\\open\\command", "", command)
    for ext in EXTS:
        _set(f"{app}\\SupportedTypes", ext, "")
        _set(f"{CLASSES}\\{ext}\\OpenWithProgids", PROGID, b"", winreg.REG_NONE)
    # 「既定のアプリ」の設定画面に MyLocalTube として出す
    _set(CAPS, "ApplicationName", APP_NAME)
    _set(CAPS, "ApplicationDescription", "ローカルの動画を YouTube 風の画面で再生します")
    _set(CAPS, "ApplicationIcon", icon)
    for ext in EXTS:
        _set(f"{CAPS}\\FileAssociations", ext, PROGID)
    _set(r"Software\RegisteredApplications", APP_NAME, CAPS)
    _notify()
    print(f"登録しました: {', '.join(EXTS)} → {EXE}")


def unregister():
    for ext in EXTS:
        _delete_value(f"{CLASSES}\\{ext}\\OpenWithProgids", PROGID)
    _delete_tree(f"{CLASSES}\\{PROGID}")
    _delete_tree(f"{CLASSES}\\Applications\\MyLocalTube.exe")
    _delete_value(r"Software\RegisteredApplications", APP_NAME)
    _delete_tree(r"Software\MyLocalTube")
    _notify()
    print("登録を消しました")


def status():
    registered = False
    try:
        with winreg.OpenKey(HKCU, r"Software\RegisteredApplications") as k:
            registered = winreg.QueryValueEx(k, APP_NAME)[0] == CAPS
    except FileNotFoundError:
        pass
    print(f"MyLocalTube の登録: {'あり' if registered else 'なし'}")
    for ext in EXTS + [".ts"]:
        try:
            with winreg.OpenKey(HKCU, rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\UserChoice") as k:
                prog = winreg.QueryValueEx(k, "ProgId")[0]
        except FileNotFoundError:
            prog = "(未設定)"
        mark = "  ← MyLocalTube" if prog == PROGID else ""
        print(f"  {ext:6s} 既定: {prog}{mark}")


def open_settings():
    """MyLocalTube の「既定のアプリ」設定ページを開く（Windows 11）。"""
    os.startfile(f"ms-settings:defaultapps?registeredAppUser={APP_NAME}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "register":
        register()
        open_settings()
    elif cmd == "unregister":
        unregister()
    else:
        status()
