"""アップデートの確認と適用（2026-10-06）。

公開しているリポジトリ（config.UPDATE_REPO）の GitHub の最新の Release と、手元の版（VERSION）を比べる。
新しい版があれば、画面が「アップデート / このバージョンはスキップ / あとで」を聞く。
アップデートは git で Release のタグまで早送りする（git clone で入れた人だけ。手元のファイルを書き換えて
いれば自動では行わない）。requirements.txt が変わっていれば部品を入れ直す。再起動は画面とアプリ版（desktop.py）が行う。
GitHub への問い合わせは 1 日 1 回まで（data/update.json に覚える）。止めるには config_local.py で UPDATE_CHECK = False。
Release は launcher/release.py で作る。
"""
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import config

log = logging.getLogger("mlt.update")
HERE = Path(__file__).resolve().parent
CHECK_EVERY_S = 24 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # pythonw から git を起こしても黒い窓を出さない


class UpdateError(Exception):
    """画面にそのまま出す理由。"""


def current_version() -> str:
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _parse(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest, current) -> bool:
    a, b = _parse(latest), _parse(current)
    return bool(a and b and a > b)


# ─────────────────────────── 覚えておくこと ───────────────────────────
def _state_file() -> Path:
    return config.DATA_DIR / "update.json"


def _load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


# ─────────────────────────── 確認 ───────────────────────────
def fetch_latest():
    """GitHub の最新の Release。Release が 1 つも無ければ None。"""
    url = f"{config.UPDATE_API.rstrip('/')}/repos/{config.UPDATE_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "MyLocalTube"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    tag = str(d.get("tag_name") or "")
    return {"version": tag.lstrip("v"), "tag": tag, "name": d.get("name") or tag,
            "notes": d.get("body") or "", "url": d.get("html_url") or "",
            "published": d.get("published_at") or ""}


def check(force=False):
    """最新の Release（問い合わせに失敗したら前回の結果）。GitHub へは 1 日 1 回まで。"""
    st = _load()
    if not force and time.time() - st.get("last_check", 0) < CHECK_EVERY_S:
        return st.get("latest")
    t = time.perf_counter()
    try:
        latest, err = fetch_latest(), None
        st.update(last_check=time.time(), latest=latest)
        _save(st)
    except Exception as e:          # noqa: BLE001 — オフラインなど。次の起動でまた確かめる
        latest, err = st.get("latest"), f"{type(e).__name__}: {e}"
    log.info("phase=update_check ok=%s ms=%d current=%s latest=%s err=%s", err is None,
             (time.perf_counter() - t) * 1000, current_version(), latest and latest["version"], err)
    return latest


def _git(*args, timeout=60) -> str:
    git = shutil.which("git")
    if not git:
        raise UpdateError("git が見つかりません")
    try:
        r = subprocess.run([git, *args], cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} が時間内に終わりませんでした")
    if r.returncode != 0:
        raise UpdateError(f"git {args[0]} に失敗しました: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def auto_reason():
    """自動でアップデートできない理由（できるなら None）。"""
    if not (HERE / ".git").exists():
        return "git clone で入れたものではないため、自動ではアップデートできません"
    if not shutil.which("git"):
        return "git が見つからないため、自動ではアップデートできません"
    try:
        if _git("status", "--porcelain", "--untracked-files=no", timeout=20):
            return "手元でファイルが書き換えられているため、自動ではアップデートできません"
        if _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10) == "HEAD":
            return "ブランチから外れているため、自動ではアップデートできません"
    except UpdateError as e:
        return str(e)
    return None


def status(force=False) -> dict:
    """画面に出すアップデートの状態。"""
    cur = current_version()
    out = {"enabled": bool(config.UPDATE_CHECK), "current": cur, "latest": None,
           "available": False, "skipped": False, "auto": False, "reason": None}
    if not config.UPDATE_CHECK:
        return out
    latest = check(force)
    if not latest:
        return out
    out.update(latest=latest, available=is_newer(latest["version"], cur),
               skipped=_load().get("skipped") == latest["version"])
    if out["available"]:
        reason = auto_reason()
        out.update(auto=reason is None, reason=reason)
    return out


def skip(version: str) -> None:
    st = _load()
    st["skipped"] = version
    _save(st)
    log.info("phase=update_skip version=%s current=%s", version, current_version())


# ─────────────────────────── 適用 ───────────────────────────
def apply(version: str) -> dict:
    """Release のタグまで早送りする。終わったら画面がアプリを再起動する。"""
    t0 = time.perf_counter()
    cur = current_version()
    try:
        reason = auto_reason()
        if reason:
            raise UpdateError(reason)
        latest = _load().get("latest") or {}
        if latest.get("version") != version:
            raise UpdateError("アップデートの情報が古くなっています。アプリを開き直してください")
        tag = latest.get("tag") or f"v{version}"
        old = _git("rev-parse", "HEAD")
        _git("fetch", "--tags", "origin", timeout=180)
        new = _git("rev-parse", f"refs/tags/{tag}^{{commit}}")
        changed = _git("diff", "--name-only", old, new).splitlines() if old != new else []
        _git("merge", "--ff-only", new)
        pip = False
        if "requirements.txt" in changed:
            py = Path(sys.executable)
            if py.name.lower() == "pythonw.exe":
                py = py.with_name("python.exe")
            try:
                r = subprocess.run([str(py), "-m", "pip", "install", "-r", "requirements.txt"], cwd=HERE,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=900, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise UpdateError("部品（requirements.txt）の入れ直しが時間内に終わりませんでした")
            if r.returncode != 0:
                raise UpdateError("部品（requirements.txt）の入れ直しに失敗しました: " +
                                  (r.stderr or r.stdout).strip()[-300:])
            pip = True
        launcher = any(p.startswith("launcher/") for p in changed)
    except UpdateError as e:
        log.warning("phase=update_apply ok=false version=%s current=%s ms=%d err=%s", version, cur,
                    (time.perf_counter() - t0) * 1000, e)
        raise
    log.info("phase=update_apply ok=true from=%s to=%s files=%d pip=%s launcher=%s ms=%d", cur,
             current_version(), len(changed), pip, launcher, (time.perf_counter() - t0) * 1000)
    return {"from": cur, "to": current_version(), "pip": pip, "launcher": launcher}
