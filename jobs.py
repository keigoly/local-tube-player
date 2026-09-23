"""fps変換ジョブのキュー。

変換は動画の長さの数倍かかる長時間処理なので、
「登録したら即レスポンス、実行はワーカーで」という形にする。
同じ動画を二重に積んでも壊れない（変換ツールは出力が既にあればスキップする約束）。
"""
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import config
import library

# tqdm の行から「フェーズ名」と進捗を拾う。変換ツールは2種類の形式を出してよい（README の仕様）。
#   件数型: "[2/2] 変換:  48%|#####    | 30/62 [02:11<02:20]"
_PHASE_RE = re.compile(r"\[(\d)/(\d)\]\s*([^:]+?):.*?\|\s*(\d+)/(\d+)")
#   割合型: "[2/3] 補間:  45%|####     | 45% [00:30<00:36]"
_PHASE_PCT_RE = re.compile(r"\[(\d)/(\d)\]\s*([^:]+?):\s*(\d+)%\|")
# 進捗バー以外で拾いたい行（README の仕様にある任意の注記行）
_NOTE_RE = re.compile(r"^\[(シーン検出|補間倍率|作業フォルダ)\]\s*(.+)$")

# フェーズごとの重み（全体の進捗率を出すため）。2フェーズと3フェーズの変換ツールを想定。
_WEIGHTS = {2: [0.10, 0.90], 3: [0.10, 0.65, 0.25]}

_jobs = {}
_jobs_lock = threading.Lock()
_queue = queue.Queue()
_seq = 0
_listeners = []
_listeners_lock = threading.Lock()


def _public(job):
    """外に出してよい形にする。

    ジョブ辞書には `_proc`（Popenオブジェクト）など JSON にできない内部キーが
    混ざるので、API / SSE に渡す前に必ずここを通すこと。
    """
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _publish(job):
    """更新をSSEの購読者へ配る。"""
    payload = _public(job)
    with _listeners_lock:
        targets = list(_listeners)
    for q in targets:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def subscribe():
    q = queue.Queue(maxsize=200)
    with _listeners_lock:
        _listeners.append(q)
    return q


def unsubscribe(q):
    with _listeners_lock:
        if q in _listeners:
            _listeners.remove(q)


def list_jobs():
    with _jobs_lock:
        return [_public(j) for j in
                sorted(_jobs.values(), key=lambda j: j["id"], reverse=True)]


def get_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        return _public(j) if j else None


def find_active(video_id):
    with _jobs_lock:
        for j in _jobs.values():
            if j["video_id"] == video_id and j["status"] in ("queued", "running"):
                return _public(j)
    return None


def available():
    """120fps 変換ツールが設定されているか（config.CONVERTER_PY）。無ければ画面にも出さない。"""
    return bool(config.CONVERTER_PY) and Path(config.CONVERTER_PY).is_file()


def enqueue(video_id):
    """ジョブを積んで即座に返す。既に積まれていればそれを返す（冪等）。"""
    if not available():
        raise ValueError("120fps変換ツールが設定されていません（config_local.py の CONVERTER_PY）")
    existing = find_active(video_id)
    if existing:
        return existing, False

    v = library.get_video(video_id)
    if not v:
        raise ValueError("動画が見つかりません")
    if v.get("external"):
        raise ValueError("ライブラリの外の動画は120fps化できません")

    src = Path(v["path"])
    out = src.with_name(f"{src.stem}{config.CONVERTED_SUFFIX}{src.suffix}")

    global _seq
    with _jobs_lock:
        _seq += 1
        job = {
            "id": _seq, "video_id": video_id, "name": v["name"],
            "path": str(src), "output": str(out),
            "status": "queued", "phase": "", "phase_index": 0, "phase_total": 0,
            "progress": 0.0, "message": "順番待ち", "note": "",
            "created": time.time(), "started": None, "finished": None,
        }
        _jobs[job["id"]] = job
    _publish(job)
    _queue.put(job["id"])
    return _public(job), True


def _kill_tree(proc):
    """変換ツールをその子プロセス（AI 補間・ffmpeg など）ごと止める。

    proc.terminate() は python 本体しか止めない。すると ffmpeg は標準入力が閉じたのを
    「入力終わり」と受け取り、途中までの動画を *_120fps.mp4 として正常に書き終えてしまう。
    変換ツールは出力が既にあると次回スキップするので、壊れた変換結果が確定してしまう。
    """
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=library.NO_WINDOW)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _remove_partial(job):
    """このジョブが作りかけた出力を消す。ジョブ開始前からあったファイルには触らない。"""
    if job.get("_out_existed", True):
        return
    out = Path(job["output"])
    for _ in range(10):                 # 子プロセスのハンドル解放を少し待つ
        try:
            out.unlink(missing_ok=True)
            _update(job, note="作りかけの出力を削除しました")
            return
        except PermissionError:
            time.sleep(0.5)
        except OSError:
            return
    _update(job, note=f"作りかけの出力を削除できませんでした: {out.name}")


def active_count():
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))


def shutdown(timeout=20.0):
    """アプリ終了時に呼ぶ。待機中は取り消し、実行中は止めて後始末が済むまで待つ。"""
    with _jobs_lock:
        ids = [j["id"] for j in _jobs.values() if j["status"] in ("queued", "running")]
    for jid in ids:
        cancel(jid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _jobs_lock:
            if not any(j["status"] == "running" for j in _jobs.values()):
                return True
        time.sleep(0.2)
    return False


def cancel(jid):
    proc = None
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            return None
        if job["status"] == "queued":
            job.update(status="cancelled", message="取り消しました",
                       finished=time.time())
        elif job["status"] == "running":
            job["_cancel"] = True
            job["message"] = "停止しています…"
            proc = job.get("_proc")
        else:
            return _public(job)
        snapshot = _public(job)
    if proc is not None:
        _kill_tree(proc)
    _publish(snapshot)
    return snapshot


def _update(job, **kw):
    with _jobs_lock:
        job.update(kw)
        snapshot = _public(job)
    _publish(snapshot)


def _report(job, idx, total_phases, label, inner, detail):
    """フェーズ内の進み具合を全体の進捗率に直して反映する。"""
    weights = _WEIGHTS.get(total_phases, [1.0 / total_phases] * total_phases)
    overall = sum(weights[:idx - 1]) + weights[idx - 1] * max(0.0, min(inner, 1.0))
    _update(job, phase=label, phase_index=idx, phase_total=total_phases,
            progress=round(min(overall, 0.999), 4),
            message=f"{label} {detail}")


def _read_stream(proc, job):
    """変換ツールの出力を読んで進捗に変換する。

    tqdm は \r で行を上書きするので、\r と \n の両方で区切る。
    """
    buf = b""
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if chunk in (b"\r", b"\n"):
            line = buf.decode("utf-8", "ignore").strip()
            buf = b""
            if not line:
                continue
            m = _PHASE_RE.search(line)
            if m:
                idx, tot, label, cur, total = m.groups()
                idx, tot, cur, total = int(idx), int(tot), int(cur), int(total)
                _report(job, idx, tot, label.strip(),
                        cur / total if total else 0.0, f"{cur}/{total}")
                continue
            m = _PHASE_PCT_RE.search(line)
            if m:
                idx, tot, label, pct = m.groups()
                idx, tot, pct = int(idx), int(tot), int(pct)
                _report(job, idx, tot, label.strip(), pct / 100.0, f"{pct}%")
                continue
            m = _NOTE_RE.match(line)
            if m:
                _update(job, note=f"{m.group(1)}: {m.group(2)}"[:200])
                continue
            if line.startswith("スキップ:"):
                job["_skipped"] = True
                _update(job, message="変換済みのためスキップしました")
            elif line.startswith("[エラー]") or line.startswith("[AI Error Log]") \
                    or line.startswith("[エンコードエラー]"):
                _update(job, note=line[:200])
        else:
            buf += chunk
            if len(buf) > 4096:       # 暴走防止
                buf = buf[-1024:]


def _run(job):
    _update(job, status="running", started=time.time(), message="開始しました")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [str(config.CONVERTER_PYTHON), str(config.CONVERTER_PY), job["path"]]
    with _jobs_lock:
        job["_out_existed"] = Path(job["output"]).exists()

    try:
        proc = subprocess.Popen(
            # 変換ツールは自分のフォルダを基準に相対パスでモデル等を探すことがあるので、そこで動かす
            cmd, cwd=str(Path(config.CONVERTER_PY).parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            creationflags=library.NO_WINDOW,
        )
    except Exception as e:
        _update(job, status="error", message=f"起動できません: {e}",
                finished=time.time())
        return

    with _jobs_lock:
        job["_proc"] = proc
        # 起動の直前（status=running にしてから _proc を入れるまで）に取り消されると、
        # cancel() は止める相手がいないまま _cancel だけを立てる。ここで拾わないと
        # 変換が最後まで走り、完成した出力が「作りかけ」として消されてしまう。
        cancel_requested = job.get("_cancel", False)
    if cancel_requested:
        _kill_tree(proc)
    reader = threading.Thread(target=_read_stream, args=(proc, job), daemon=True)
    reader.start()
    rc = proc.wait()
    reader.join(timeout=5)

    with _jobs_lock:
        cancelled = job.get("_cancel", False)
        skipped = job.get("_skipped", False)
        job.pop("_proc", None)

    if cancelled:
        _remove_partial(job)
        _update(job, status="cancelled", message="取り消しました",
                progress=0.0, finished=time.time())
    elif rc == 0 and Path(job["output"]).exists():
        _update(job, status="done", progress=1.0, finished=time.time(),
                message="変換済みのためスキップしました" if skipped else "完了しました")
        library.scan()                      # 出来上がった動画を索引に載せる
        # 「完了」は索引の更新前に届くので、画面が読み直しても変換済み版がまだ無い。
        # 索引に載ったあとにもう一度知らせて、一覧と 120 ボタンを更新させる
        _update(job, indexed=True)
    else:
        _remove_partial(job)
        _update(job, status="error", finished=time.time(),
                message=f"失敗しました (終了コード {rc})")


def _worker():
    while True:
        jid = _queue.get()
        with _jobs_lock:
            job = _jobs.get(jid)
            skip = (not job) or job["status"] != "queued"
        if skip:
            _queue.task_done()
            continue
        try:
            _run(job)
        except Exception as e:
            _update(job, status="error", message=f"内部エラー: {e}",
                    finished=time.time())
        finally:
            _queue.task_done()


def start_workers():
    for _ in range(max(1, config.MAX_CONCURRENT_JOBS)):
        threading.Thread(target=_worker, daemon=True).start()
