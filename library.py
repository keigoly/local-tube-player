"""ライブラリの走査と索引。

動画は移動もコピーもしない。パスと付随情報を SQLite に持つだけ。
"""
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import config

_lock = threading.Lock()

# コンソールを持たないアプリ版（pythonw）から ffmpeg 等を起動すると、
# 子プロセスごとに黒いコンソール窓が一瞬開いてしまう。それを抑止するフラグ。
# ブラウザ版（python.exe）でも害はないので、外部コマンドの起動には必ず付けること。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,      -- 拡張子を除いたファイル名
    rel_dir       TEXT NOT NULL,      -- ルートからの相対フォルダ（=「チャンネル」扱い）
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL    NOT NULL,
    duration      REAL,               -- 秒。取得できなければ NULL
    width         INTEGER,
    height        INTEGER,
    fps           REAL,
    native        INTEGER NOT NULL,   -- 1ならブラウザが直接再生できる＝シーク可
    is_converted  INTEGER NOT NULL,   -- 1ならこのファイル自体が変換済み出力
    converted_id  INTEGER,            -- 変換済み版が別にある場合その id
    seen          INTEGER NOT NULL DEFAULT 1,
    vcodec        TEXT,               -- 映像コーデック。NULL=未調査 / ''=映像なし
    acodec        TEXT,
    playable      INTEGER             -- 1=画面で再生できる / 0=一覧に出さない / NULL=未調査
);
CREATE INDEX IF NOT EXISTS idx_videos_dir  ON videos(rel_dir);
CREATE INDEX IF NOT EXISTS idx_videos_name ON videos(name);
"""

# v0.1 の索引に後から足した列。init_db() で無ければ追加する。
_ADDED_COLUMNS = [("vcodec", "TEXT"), ("acodec", "TEXT"), ("playable", "INTEGER")]

# 一覧に出す条件。コーデック未調査（旧版の索引の残り）のうちは拡張子で推定する。
_VISIBLE = "COALESCE(playable, native) = 1"

_STREAM_RE = re.compile(r"Stream #\S+.*?: (Video|Audio): (\w+)")


def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 走査は50件ごとにしかコミットしないので、その間に移動・削除が来ても
    # 「database is locked」で失敗しないよう待ち時間を長めに取る（既定は5秒）
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        for name, typ in _ADDED_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {typ}")


def needs_scan():
    """索引が空か、コーデック未調査の動画が残っていれば True。"""
    with connect() as conn:
        return conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM videos) "
            "OR EXISTS (SELECT 1 FROM videos WHERE vcodec IS NULL)").fetchone()[0] == 1


def _skip_dir(p: Path) -> bool:
    n = p.name
    return n in config.EXCLUDE_DIR_NAMES or n.startswith(config.EXCLUDE_DIR_PREFIXES)


def iter_video_files():
    """設定したルート配下の動画ファイルを列挙する。"""
    for root in config.LIBRARY_ROOTS:
        root = Path(root)
        if not root.exists():
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = list(d.iterdir())
            except (PermissionError, OSError):
                continue
            for e in entries:
                if e.is_dir():
                    if not _skip_dir(e):
                        stack.append(e)
                elif e.suffix.lower() in config.VIDEO_EXTS:
                    yield root, e


def probe(path: Path):
    """解像度・fps・長さを返す。取得できなければ None 混じりで返す。

    ffprobe が無い環境（ffmpeg.exe だけ置いている場合など）でも動くよう cv2 を使う。
    ヘッダを読むだけなので軽い。
    """
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None, None, None, None
        fps = cap.get(cv2.CAP_PROP_FPS) or None
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
        cap.release()
        dur = (count / fps) if (fps and count > 0) else None
        return dur, w, h, fps
    except Exception:
        return None, None, None, None


def probe_codecs(path: Path):
    """映像・音声のコーデック名と、画面で再生できるかを返す: (vcodec, acodec, playable)。

    cv2 はコーデック名を返さないので ffmpeg -i のヘッダ表示を読む（1本あたり約80ms）。
    映像が無ければ vcodec は ''（映像ストリームを含まない .ts などがありうる）。
    ffmpeg を起動できなかったときは (None, None, None) ＝次回の走査で取り直す。
    """
    try:
        r = subprocess.run([str(config.FFMPEG), "-hide_banner", "-i", str(path)],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception:
        return None, None, None
    vcodec = acodec = ""
    for line in r.stderr.decode("utf-8", "ignore").splitlines():
        m = _STREAM_RE.search(line)
        if not m:
            continue
        kind, codec = m.groups()
        if kind == "Video" and not vcodec and "attached pic" not in line:
            vcodec = codec                  # カバー画像は映像扱いしない
        elif kind == "Audio" and not acodec:
            acodec = codec
    return vcodec, acodec, 1 if vcodec in config.PLAYABLE_VCODECS else 0


def scan(progress=None):
    """走査して索引を更新する。戻り値: (追加, 更新, 削除) の件数。"""
    init_db()
    added = updated = 0
    with _lock, connect() as conn:
        conn.execute("UPDATE videos SET seen = 0")
        files = list(iter_video_files())
        total = len(files)
        for i, (root, f) in enumerate(files, 1):
            if progress:
                progress(i, total, str(f))
            # 初回走査は本数が多いと時間がかかる。最後にまとめてコミットすると
            # その間UIが空のままになるので、こまめに確定させて途中経過を見せる。
            if i % 50 == 0:
                conn.commit()
            try:
                st = f.stat()
            except OSError:
                continue
            path = str(f)
            row = conn.execute(
                "SELECT id, size, mtime, vcodec FROM videos WHERE path = ?", (path,)
            ).fetchone()

            # 中身が変わっていなければメタデータの取り直しはしない
            if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                conn.execute("UPDATE videos SET seen = 1 WHERE id = ?", (row["id"],))
                if row["vcodec"] is None:       # v0.1 の索引にはコーデック情報がない
                    conn.execute(
                        "UPDATE videos SET vcodec = ?, acodec = ?, playable = ? WHERE id = ?",
                        probe_codecs(f) + (row["id"],))
                continue

            dur, w, h, fps = probe(f)
            vcodec, acodec, playable = probe_codecs(f)
            ext = f.suffix.lower()
            try:
                rel_dir = str(f.parent.relative_to(root))
            except ValueError:
                rel_dir = f.parent.name
            rec = (
                path, f.stem, "" if rel_dir == "." else rel_dir, ext,
                st.st_size, st.st_mtime, dur, w, h, fps,
                1 if ext in config.NATIVE_EXTS else 0,
                1 if f.stem.endswith(config.CONVERTED_SUFFIX) else 0,
                vcodec, acodec, playable,
            )
            if row:
                conn.execute(
                    "UPDATE videos SET name=?, rel_dir=?, ext=?, size=?, mtime=?, "
                    "duration=?, width=?, height=?, fps=?, native=?, is_converted=?, "
                    "vcodec=?, acodec=?, playable=?, seen=1 "
                    "WHERE id=?", rec[1:] + (row["id"],))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO videos (path,name,rel_dir,ext,size,mtime,duration,"
                    "width,height,fps,native,is_converted,vcodec,acodec,playable,seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)", rec)
                added += 1

        cur = conn.execute("DELETE FROM videos WHERE seen = 0")
        removed = cur.rowcount
        _link_converted(conn)
    return added, updated, removed


def _link_converted(conn):
    """「元動画」と「その変換済み版」を突き合わせる。

    変換ツールは `<元名>_120fps.<拡張子>` を同じフォルダに作る約束なので、
    名前とフォルダで対応が取れる。
    """
    conn.execute("UPDATE videos SET converted_id = NULL")
    rows = conn.execute("SELECT id, path, name, is_converted FROM videos").fetchall()
    by_key = {}
    for r in rows:
        by_key[(str(Path(r["path"]).parent).lower(), r["name"].lower())] = r["id"]
    for r in rows:
        if r["is_converted"]:
            continue
        key = (str(Path(r["path"]).parent).lower(),
               (r["name"] + config.CONVERTED_SUFFIX).lower())
        cid = by_key.get(key)
        if cid:
            conn.execute("UPDATE videos SET converted_id = ? WHERE id = ?", (cid, r["id"]))


def list_videos(q=None, folder=None, only_unconverted=False):
    """画面に出す動画の一覧。再生できない動画（MPEG-2 映像の .ts 等）は含めない。"""
    with connect() as conn:
        sql = f"SELECT * FROM videos WHERE {_VISIBLE}"
        args = []
        if q:
            sql += " AND name LIKE ?"
            args.append(f"%{q}%")
        if folder:
            sql += " AND rel_dir = ?"
            args.append(folder)
        if only_unconverted:
            sql += " AND is_converted = 0 AND converted_id IS NULL"
        sql += " ORDER BY mtime DESC"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ── ライブラリの外の動画（エクスプローラーでダブルクリックして開いたもの）──
# 索引（DB）には入れない（次の走査で消えるうえ、一覧に混ざってしまう）。
# アプリを閉じるまでの間だけ、負の id で覚えておく。/media/{id} や /thumb/{id} はそのまま使える。
_external = {}
_external_lock = threading.Lock()


def open_path(path):
    """ファイルのパスから、再生に使う動画情報を返す。見つからなければ None。

    ライブラリ内のファイルなら索引の行（再生できない .ts なら playable=0 のまま返す）、
    ライブラリ外なら一時的な情報（id は負の数、external=1）。
    """
    p = Path(os.path.realpath(path))
    if not p.is_file():
        return None
    with connect() as conn:
        r = conn.execute("SELECT * FROM videos WHERE path = ? COLLATE NOCASE", (str(p),)).fetchone()
    if r:
        return dict(r)
    key = str(p).lower()
    with _external_lock:
        for v in _external.values():
            if v["path"].lower() == key:
                return dict(v)
    dur, w, h, fps = probe(p)
    vcodec, acodec, playable = probe_codecs(p)
    st = p.stat()
    ext = p.suffix.lower()
    with _external_lock:
        vid = -(len(_external) + 1)
        v = {"id": vid, "path": str(p), "name": p.stem, "rel_dir": str(p.parent), "ext": ext,
             "size": st.st_size, "mtime": st.st_mtime, "duration": dur, "width": w, "height": h,
             "fps": fps, "native": 1 if ext in config.NATIVE_EXTS else 0,
             "is_converted": 1 if p.stem.endswith(config.CONVERTED_SUFFIX) else 0,
             "converted_id": None, "vcodec": vcodec, "acodec": acodec, "playable": playable,
             "external": 1}
        _external[vid] = v
    return dict(v)


def get_video(vid):
    if vid < 0:                              # ライブラリの外の動画（open_path で開いたもの）
        with _external_lock:
            v = _external.get(vid)
        return dict(v) if v else None
    with connect() as conn:
        r = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        return dict(r) if r else None


def folders():
    with connect() as conn:
        # 再生できない動画しか無いフォルダはサイドバーにも出さない
        return [dict(r) for r in conn.execute(
            f"SELECT rel_dir, COUNT(*) AS n FROM videos WHERE {_VISIBLE} "
            "GROUP BY rel_dir ORDER BY rel_dir"
        ).fetchall()]


def thumb_file(vid) -> Path:
    return config.THUMB_DIR / f"{vid}.jpg"


def ensure_thumb(vid) -> Path | None:
    """サムネイルを作る（既にあれば何もしない）。走査を軽くするため遅延生成。"""
    out = thumb_file(vid)
    if out.exists() and out.stat().st_size > 0:
        return out
    v = get_video(vid)
    if not v:
        return None
    config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    pos = (v["duration"] or 0) * config.THUMB_POSITION
    cmd = [str(config.FFMPEG), "-v", "error", "-y",
           "-ss", f"{max(0.0, pos):.2f}", "-i", v["path"],
           "-frames:v", "1", "-vf", f"scale={config.THUMB_WIDTH}:-2",
           "-q:v", "4", str(out)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, check=False, creationflags=NO_WINDOW)
    except Exception:
        return None
    return out if out.exists() and out.stat().st_size > 0 else None
