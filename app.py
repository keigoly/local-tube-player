"""youtube-ui-player — ローカル動画をYouTube風UIで再生し、fps変換を投げるサーバ。

起動: python app.py  →  http://127.0.0.1:5560
"""
import asyncio
import json
import logging
import mimetypes
import re
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import fileops
import jobs
import library

STATIC = Path(__file__).parent / "static"

_scan_state = {"running": False, "done": 0, "total": 0, "current": "",
               "last": None, "result": None}


@asynccontextmanager
async def lifespan(_app):
    library.init_db()
    jobs.start_workers()
    # 初回（索引が空）と、旧版の索引にコーデック情報が欠けているときは自動で走査する
    if library.needs_scan():
        _start_scan()
    yield


app = FastAPI(title="youtube-ui-player", lifespan=lifespan)


class _LocalOnly:
    """すべてのリクエストで接続元を確かめる。StreamingResponse を壊さないよう素の ASGI で書く。

    - Host が 127.0.0.1 / localhost 以外は拒否（DNS リバインディングで動画一覧＝パスを読ませない）
    - GET 以外は Origin / Sec-Fetch-Site も確かめる（他サイトからの CSRF でスキャンや変換を始めさせない）
    """

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            host = h.get("host", "")
            bad = urlsplit("//" + host).hostname not in ("127.0.0.1", "localhost")
            if not bad and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                origin = h.get("origin")
                bad = (bool(origin and urlsplit(origin).netloc != host)
                       or h.get("sec-fetch-site") not in (None, "same-origin", "none"))
            if bad:
                await JSONResponse({"detail": "許可されていない接続元です"}, status_code=403)(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(_LocalOnly)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ─────────────────────────── 画面 ───────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


# ─────────────────────────── API ───────────────────────────
@app.get("/api/videos")
def api_videos(q: str = "", folder: str = "", unconverted: int = 0):
    vids = library.list_videos(q=q or None, folder=folder or None,
                               only_unconverted=bool(unconverted))
    active = {j["video_id"]: j for j in jobs.list_jobs()
              if j["status"] in ("queued", "running")}
    for v in vids:
        v["job"] = active.get(v["id"])
    return vids


@app.get("/api/folders")
def api_folders():
    return library.folders()


def _start_scan():
    if _scan_state["running"]:
        return
    _scan_state.update(running=True, done=0, total=0, current="", result=None)

    def run():
        def progress(i, total, path):
            _scan_state.update(done=i, total=total, current=Path(path).name)
        try:
            added, updated, removed = library.scan(progress)
            _scan_state["result"] = {"added": added, "updated": updated,
                                     "removed": removed}
        except Exception as e:
            _scan_state["result"] = {"error": str(e)}
        finally:
            _scan_state.update(running=False, last=time.time(), current="")

    threading.Thread(target=run, daemon=True).start()


@app.post("/api/scan")
def api_scan():
    _start_scan()
    return {"started": True}


@app.get("/api/scan/status")
def api_scan_status():
    return _scan_state


# ──────────────────── ファイル操作（⋮メニュー） ────────────────────
def _same_origin(request: Request):
    """ファイルを消せるAPIなので、この画面以外からの呼び出しを拒否する。

    普段使いのブラウザで開いた悪意あるサイトが、裏で
    fetch("http://127.0.0.1:5560/api/videos/1/delete", {method: "POST", mode: "no-cors"})
    を送る攻撃（CSRF）や、DNS リバインディングを防ぐ。
    """
    host = request.headers.get("host", "")
    if urlsplit("//" + host).hostname not in ("127.0.0.1", "localhost"):
        raise HTTPException(403, "許可されていない接続元です")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != host:
        raise HTTPException(403, "許可されていない接続元です")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, "許可されていない接続元です")


def _fileop(fn, *args):
    try:
        return fn(*args)
    except fileops.OpError as e:
        raise HTTPException(e.status, e.message)


class MoveBody(BaseModel):
    dest: str
    new_folder: str = ""


# ─────────── ファイルを開く（エクスプローラーでダブルクリック → MyLocalTube.exe "%1"）───────────
# アプリ版（desktop.py）が設定する。開いた動画をウィンドウに表示する関数。ブラウザ版では None
open_hook = None


class OpenBody(BaseModel):
    path: str


@app.post("/api/open", dependencies=[Depends(_same_origin)])
def api_open(body: OpenBody):
    """パスで指定した動画を開く。2つ目に起動された MyLocalTube.exe が、既に開いている
    アプリへファイルを渡すのにも使う（新しいウィンドウを増やさない）。"""
    v = library.open_path(body.path)
    if not v:
        raise HTTPException(404, f"ファイルが見つかりません: {body.path}")
    if open_hook:
        open_hook(v)
    return v


@app.get("/api/features")
def api_features():
    """画面が出し分けに使う。120fps 変換ツールが無い環境では 120fps 関連の UI を出さない。"""
    return {"fps120": jobs.available(), "minPerMin": config.CONVERTER_MIN_PER_MIN}


@app.get("/api/dirs")
def api_dirs():
    """移動先に選べるフォルダの一覧。"""
    return fileops.list_dirs()


@app.post("/api/videos/{vid}/move", dependencies=[Depends(_same_origin)])
def api_move(vid: int, body: MoveBody):
    return _fileop(fileops.move_video, vid, body.dest, body.new_folder)


@app.post("/api/videos/{vid}/delete", dependencies=[Depends(_same_origin)])
def api_delete(vid: int):
    return _fileop(fileops.delete_video, vid)


@app.post("/api/videos/{vid}/reveal", dependencies=[Depends(_same_origin)])
def api_reveal(vid: int):
    return _fileop(fileops.reveal, vid)


@app.get("/thumb/{vid}")
def thumb(vid: int):
    p = library.ensure_thumb(vid)
    if not p:
        raise HTTPException(404, "サムネイルを作れませんでした")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ───────────────────── 動画のストリーミング ─────────────────────
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 1 << 20


def _ranged_file(path: Path, start: int, end: int):
    """指定範囲を読み出すジェネレータ。必ず _ClosingStream で包んで返すこと。

    途中で切断されたとき、Starlette はこのジェネレータを閉じないので、そのままだと
    GC が走るまでファイルを開いたまま残り、移動も削除もできなかった。
    """
    with open(path, "rb") as f:
        f.seek(start)
        left = end - start + 1
        while left > 0:
            data = f.read(min(CHUNK, left))
            if not data:
                break
            left -= len(data)
            yield data


class _ClosingStream(StreamingResponse):
    """送り終わったとき・途中で切断されたときに、必ず on_close を呼ぶ StreamingResponse。

    Starlette は切断されても中身のジェネレータを閉じないので、ジェネレータの finally に
    後始末を書いても GC まで実行されない。詰め替え配信の ffmpeg が元動画を掴んだまま残り、
    その動画を移動・削除できなくなっていた。
    """

    def __init__(self, content, on_close, **kw):
        super().__init__(content, **kw)
        self._on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def _remux_stream(path: Path):
    """ブラウザが読めないコンテナ(mkv/ts等)を、再エンコードせず MP4 へ詰め替えて流す。

    映像・音声はコピーするだけなので軽いが、シークはできない。
    戻り値: (ジェネレータ, ffmpeg を止める関数)
    """
    cmd = [str(config.FFMPEG), "-v", "error", "-i", str(path),
           "-c", "copy", "-f", "mp4",
           "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=library.NO_WINDOW)

    def stop():
        if proc.poll() is None:
            proc.kill()             # 読み出し中のスレッドも EOF で抜ける

    return _remux_chunks(proc), stop


def _remux_chunks(proc):
    try:
        while True:
            data = proc.stdout.read(CHUNK)
            if not data:
                break
            yield data
    finally:
        if proc.poll() is None:
            proc.kill()


@app.get("/media/{vid}")
def media(vid: int, request: Request):
    v = library.get_video(vid)
    if not v:
        raise HTTPException(404, "動画が見つかりません")
    path = Path(v["path"])
    if not path.exists():
        raise HTTPException(404, "ファイルが存在しません（再スキャンしてください）")

    # ブラウザが直接読めない形式は詰め替えて流す（シーク不可）
    if not v["native"]:
        chunks, stop = _remux_stream(path)
        return _ClosingStream(chunks, on_close=stop, media_type="video/mp4",
                              headers={"Cache-Control": "no-store"})

    # Starlette の FileResponse は使わない。uvicorn は切断後の送信を黙って捨てるため、
    # FileResponse は切断に気づかずファイルの最後まで読み続けてから閉じる。
    # シークのたびに中断されたリクエストがディスクから残りを読み切り、その間は移動も削除もできなかった
    # （大きな動画だと、切断してから解放されるまで数秒かかっていた）。
    # StreamingResponse は切断を監視して読み出しを止めるので、それを _ClosingStream で包む。
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    size = path.stat().st_size
    rng = request.headers.get("range")
    status = 200
    start, end = 0, size - 1
    if rng:
        m = _RANGE_RE.fullmatch(rng.strip())
        if not m or m.groups() == ("", ""):
            raise HTTPException(416, "Range指定が不正です")
        s, e = m.groups()
        if s == "":                         # bytes=-N … 末尾 N バイト
            start = max(0, size - int(e))
        else:
            start = int(s)
            if e:
                end = min(int(e), size - 1)
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    chunks = _ranged_file(path, start, end)

    def close():
        try:
            chunks.close()                  # ジェネレータの with open(...) を抜けさせる
        except ValueError as e:
            # 「実行中のジェネレータは閉じられない」。anyio の to_thread.run_sync は既定
            # （abandon_on_cancel=False）でスレッドの読み出しが終わるまでキャンセルを待つので
            # 起きないはずだが、起きたら GC まで掴みっぱなしになるので記録は残す。
            logging.getLogger("mlt.media").warning(
                "phase=stream_close ok=false vid=%s err=%r", vid, e)

    return _ClosingStream(chunks, on_close=close, status_code=status,
                          media_type=mime, headers=headers)


# ─────────────────────────── ジョブ ───────────────────────────
@app.post("/api/jobs")
async def api_create_job(request: Request):
    body = await request.json()
    vid = int(body.get("video_id", 0))
    try:
        job, created = jobs.enqueue(vid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"job": job, "created": created}


@app.get("/api/jobs")
def api_jobs():
    return jobs.list_jobs()


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: int):
    j = jobs.cancel(jid)
    if not j:
        raise HTTPException(404, "ジョブが見つかりません")
    return j


_shutting_down = threading.Event()


def request_shutdown():
    """アプリ版の終了時に呼ぶ。開きっぱなしの SSE を自分から閉じる。

    Python 3.12 の asyncio はサーバを閉じるとき全接続の終了を待つので、
    SSE が開いたままだと WebView2 が接続を切るまで（約5秒）停止できなかった。
    """
    _shutting_down.set()


@app.get("/api/events")
async def api_events(request: Request):
    """ジョブの進捗をSSEで流す。"""
    q = jobs.subscribe()

    async def gen():
        try:
            for j in jobs.list_jobs():
                yield f"data: {json.dumps(j, ensure_ascii=False, default=str)}\n\n"
            while True:
                if _shutting_down.is_set() or await request.is_disconnected():
                    break
                try:
                    j = q.get_nowait()
                    yield f"data: {json.dumps(j, ensure_ascii=False, default=str)}\n\n"
                except Exception:
                    await asyncio.sleep(0.4)
                    yield ": keep-alive\n\n"
        finally:
            jobs.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(f"\n  youtube-ui-player  ->  http://{config.HOST}:{config.PORT}")
    print(f"  ライブラリ: {', '.join(str(p) for p in config.LIBRARY_ROOTS)}")
    print("  このウィンドウを閉じると停止します。\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
