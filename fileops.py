"""動画ファイルの操作（⋮メニューの「フォルダへ移動」「ごみ箱へ移動」「エクスプローラーで表示」）。

方針:
  - 削除は Windows のごみ箱へ送る（元に戻せる）。ごみ箱に入らないときは黙って消さず、
    Windows の警告ダイアログを出させる（FOF_WANTNUKEWARNING）。
  - 移動は同じドライブ内の名前変更だけ（数GBの動画でも一瞬で終わる）。上書きはしない。
  - 元動画と 120fps 版は同じフォルダにあることで対応付けているので、移動は2本一緒に行う。
  - 操作できるのは config.LIBRARY_ROOTS の配下だけ。
"""
import logging
import os
import re
import time
import uuid
from pathlib import Path

import config
import jobs
import library
import osdeps

log = logging.getLogger("mlt.fileops")


class OpError(Exception):
    """画面にそのまま見せるエラー。status は HTTP ステータス。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ─────────────────────────── 共通 ───────────────────────────
def _roots():
    return [Path(r).resolve() for r in config.LIBRARY_ROOTS if Path(r).exists()]


def _root_of(p: Path):
    p = Path(p).resolve()
    for r in _roots():
        if p == r or p.is_relative_to(r):
            return r
    return None


def _rel_dir(d: Path, root: Path) -> str:
    rel = str(Path(d).resolve().relative_to(root))
    return "" if rel == "." else rel


def _retry(fn, what, budget=4.0, wait=0.4):
    """再生中の動画はサーバが読み出しのためにファイルを開いている。
    画面側が再生を止めてから手放すまで少しかかるので、共有違反は少し待って再試行する。

    回数ではなく時間で区切る。SHFileOperation は1回で約1秒かかる（内部で待つ）ため、
    回数で区切ると画面を長く待たせてしまった（1回で約1秒かかる）。
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            return fn()
        except PermissionError as e:
            if time.monotonic() + wait > deadline:
                raise OpError(423, f"{what}: ファイルが使用中です。再生や変換を止めてから"
                                   f"もう一度お試しください（{e.strerror}）")
            time.sleep(wait)


def _partner(v):
    """元動画 ⇔ 120fps版 の相方。ファイルが無ければ None。"""
    if v["converted_id"]:
        p = library.get_video(v["converted_id"])
    elif v["is_converted"]:
        with library.connect() as conn:
            r = conn.execute("SELECT * FROM videos WHERE converted_id = ?", (v["id"],)).fetchone()
        p = dict(r) if r else None
    else:
        p = None
    return p if p and Path(p["path"]).exists() else None


def _ensure_not_busy(videos, what):
    for v in videos:
        if jobs.find_active(v["id"]):
            raise OpError(409, f"120fps変換のジョブが実行中・待機中なので{what}できません"
                               f"（{v['name']}）")


def _get(vid):
    v = library.get_video(vid)
    if not v:
        raise OpError(404, "動画が見つかりません（一覧を更新してください）")
    if not _root_of(Path(v["path"]).parent):
        raise OpError(403, "ライブラリの外にあるファイルは操作できません")
    return v


# ─────────────────────── フォルダ一覧 ───────────────────────
def list_dirs():
    """移動先に選べるフォルダ（ライブラリ配下の全フォルダ。空のフォルダも含む）。"""
    counts = {f["rel_dir"]: f["n"] for f in library.folders()}
    roots = _roots()
    out = []
    for ri, root in enumerate(roots):
        stack = [root]
        while stack:
            d = stack.pop()
            rel = _rel_dir(d, root)
            if len(roots) == 1:
                label = rel or "（ライブラリの直下）"
            else:
                label = root.name + ("\\" + rel if rel else "")
            out.append({"path": str(d), "rel": rel, "label": label,
                        "count": counts.get(rel, 0),
                        # 並び順: ルートごとに、ルート自身 → 階層順（表示名で並べると
                        # 全角の「（ライブラリの直下）」が末尾に来てしまう）
                        "_key": (ri, [s.lower() for s in rel.split("\\")] if rel else [])})
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                        attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                        if attrs & 0x6:         # 隠し / システム
                            continue
                        if library._skip_dir(Path(e.path)):
                            continue
                        stack.append(Path(e.path))
            except OSError:
                continue
    out.sort(key=lambda x: x.pop("_key"))
    return out


# ─────────────────────────── 移動 ───────────────────────────
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _check_folder_name(name):
    name = name.strip()
    if not name:
        return None
    if _BAD_NAME.search(name) or name in (".", "..") or name.endswith((".", " ")):
        raise OpError(400, '新しいフォルダ名に使えない文字が含まれています（\\ / : * ? " < > | など）')
    if name.split(".")[0].upper() in _RESERVED:
        raise OpError(400, f"「{name}」はWindowsの予約名なのでフォルダ名に使えません")
    if len(name) > 120:
        raise OpError(400, "新しいフォルダ名が長すぎます")
    if name in config.EXCLUDE_DIR_NAMES or name.startswith(config.EXCLUDE_DIR_PREFIXES):
        raise OpError(400, f"「{name}」は走査対象外の名前なので使えません")
    return name


def move_video(vid, dest, new_folder=""):
    """動画を別のフォルダへ移す。120fps版（または元動画）があれば一緒に移す。"""
    op = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    v = _get(vid)
    group = [v] + ([p] if (p := _partner(v)) else [])
    _ensure_not_busy(group, "移動")

    if not dest:
        raise OpError(400, "移動先のフォルダを選んでください")
    root = _root_of(dest)
    if not root:
        raise OpError(403, "ライブラリの外へは移動できません")
    dest_dir = Path(dest).resolve()
    if not dest_dir.is_dir():
        raise OpError(404, "移動先のフォルダが見つかりません（一覧を開き直してください）")
    name = _check_folder_name(new_folder or "")
    if name:
        dest_dir = dest_dir / name

    src = Path(v["path"])
    if dest_dir == src.parent.resolve():
        raise OpError(400, "今と同じフォルダです")
    if os.path.splitdrive(str(dest_dir))[0].lower() != os.path.splitdrive(str(src))[0].lower():
        raise OpError(400, "別のドライブへの移動には対応していません")

    moves = [(Path(g["path"]), dest_dir / Path(g["path"]).name, g) for g in group]
    for s, t, _ in moves:
        if not s.exists():
            raise OpError(404, f"ファイルが見つかりません（再スキャンしてください）: {s.name}")
        if t.exists():
            raise OpError(409, f"移動先に同じ名前のファイルがあります: {t.name}")

    created = False
    if name and not dest_dir.exists():
        dest_dir.mkdir()
        created = True
    done = []

    def rollback():
        """ここまでに動かしたファイルを全部元に戻す。"""
        for s, t in reversed(done):
            try:
                os.rename(t, s)
            except OSError as re_err:
                log.error("op=%s phase=move_rollback ok=false file=%s err=%r", op, t, re_err)
        if created:
            try:
                dest_dir.rmdir()
            except OSError:
                pass

    try:
        for s, t, _ in moves:
            # os.rename は Windows では移動先が既にあると失敗する＝上書きしない
            _retry(lambda s=s, t=t: os.rename(s, t), "移動")
            done.append((s, t))
        rel = _rel_dir(dest_dir, root)
        # 索引の更新は1トランザクション。失敗したら（with が巻き戻す）ファイルも戻して
        # 「ファイルは新しい場所・索引は古い場所」のずれを残さない
        with library.connect() as conn:
            for _, t, g in moves:
                # 移動先と同じパスの古い索引（ファイルは既に無い）が残っていたら片付ける
                conn.execute("DELETE FROM videos WHERE path = ? AND id != ?", (str(t), g["id"]))
                # seen=1: 走査の途中で動かされても「消えた動画」として索引から落とされないように
                conn.execute("UPDATE videos SET path = ?, rel_dir = ?, seen = 1 WHERE id = ?",
                             (str(t), rel, g["id"]))
            library._link_converted(conn)
    except Exception as e:
        rollback()
        log.warning("op=%s phase=move vid=%s ok=false ms=%d err=%r", op, vid,
                    (time.perf_counter() - t0) * 1000, e)
        if isinstance(e, OpError):
            raise
        raise OpError(500, f"移動できませんでした: {e}")
    log.info("op=%s phase=move vid=%s ids=%s dest=%s new_folder=%s ok=true ms=%d", op, vid,
             [g["id"] for g in group], dest_dir, bool(created), (time.perf_counter() - t0) * 1000)
    return {"moved": [g["id"] for g in group], "dest": str(dest_dir), "rel_dir": rel,
            "created_folder": created}


# ─────────────────────── ごみ箱へ移動 ───────────────────────
def _recycle(path: Path):
    # 使用中（共有違反）で送れないときは osdeps.recycle が PermissionError を出す → _retry が再試行する
    if not osdeps.recycle(path):
        raise OpError(409, "ごみ箱へ移動するのを取りやめました")


def delete_video(vid):
    """動画をごみ箱へ送り、索引から外す。120fps版（相方）は残す。"""
    op = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    v = _get(vid)
    _ensure_not_busy([v], "削除")
    p = Path(v["path"])
    existed = p.exists()
    if existed:
        _retry(lambda: _recycle(p), "ごみ箱へ移動")
    with library.connect() as conn:
        conn.execute("DELETE FROM videos WHERE id = ?", (vid,))
        library._link_converted(conn)
    try:
        library.thumb_file(vid).unlink(missing_ok=True)
    except OSError:
        pass
    log.info("op=%s phase=delete vid=%s file_existed=%s ok=true ms=%d path=%s", op, vid,
             existed, (time.perf_counter() - t0) * 1000, p)
    return {"deleted": vid, "recycled": existed}


# ───────────────────── エクスプローラーで表示 ─────────────────────
def reveal(vid):
    # ライブラリ外の動画（ダブルクリックで開いたもの）も、エクスプローラーで表示するだけなら許す
    v = library.get_video(vid)
    if not v or not v.get("external"):
        v = _get(vid)
    p = Path(v["path"])
    if not p.exists():
        raise OpError(404, "ファイルが見つかりません（再スキャンしてください）")
    osdeps.reveal(p)
    return {"ok": True}
