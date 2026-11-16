"""라우트: 메인 화면·전사 작업(/, /m, /files, /start, /stop, /stream, /result, /download).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import os
import queue
import threading
import time
import uuid
from flask import Response, render_template, request, send_file
from yt_web import app
import yt_core
import yt_web
import yt_jobs
import yt_queue

# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # 원격 접속이면 전사 UI를 숨긴 이력 모드로 렌더(조회·읽음·삭제만 가능)
    return render_template("index.html", default_prompt=yt_core.DEFAULT_PROMPT, remote=yt_web._is_remote())


@app.route("/m")
def mobile_history():
    return render_template("mobile.html")


@app.route("/files")
def list_files():
    files, seen = [], set()

    def _scan(directory: str) -> None:
        try:
            for fname in os.listdir(directory):
                if os.path.splitext(fname)[1].lower() not in yt_core.AUDIO_EXT:
                    continue
                path = os.path.join(directory, fname)
                if not os.path.isfile(path) or path in seen:
                    continue
                seen.add(path)
                files.append({
                    "name":     fname,
                    "path":     path,
                    "size":     os.path.getsize(path),
                    "duration": yt_core.get_file_duration(path),
                })
        except Exception:
            pass

    if os.path.isdir(yt_core.RES_DIR):
        for sub in sorted(os.listdir(yt_core.RES_DIR), reverse=True):
            sub_path = os.path.join(yt_core.RES_DIR, sub)
            if os.path.isdir(sub_path):
                _scan(sub_path)
    if yt_core.AUDIO_DIR not in (yt_core.BASE_DIR, yt_core.RES_DIR):
        _scan(yt_core.AUDIO_DIR)

    files.sort(key=lambda x: x["name"].lower())
    return yt_core._json({"files": files, "dir": yt_core.RES_DIR})


@app.route("/start", methods=["POST"])
def start():
    data   = request.get_json(force=True)
    source = data.get("source", "url")

    if source == "file":
        file_path = (data.get("file_path") or "").strip()
        if not file_path or not os.path.exists(file_path):
            return yt_core._json({"error": "파일을 찾을 수 없습니다."}, 400)
    else:
        url = (data.get("url") or "").strip()
        if not url:
            return yt_core._json({"error": "URL을 입력해주세요."}, 400)
        if not data.get("start_sec"):
            data["start_sec"] = yt_queue._extract_start_sec(url)

    job_id = str(uuid.uuid4())
    with yt_core.jobs_lock:
        yt_core.jobs[job_id] = {
            "status":         "running",
            "queue":          queue.Queue(),
            "result":         None,
            "output_file":    None,
            "start_time":     time.time(),
            "total_duration": 0,
            "proc":           None,
            "stop_event":     threading.Event(),
            "meta":           {},
            "error_stage":    "",
            "error_message":  "",
            "last_log":       "",
        }

    target = yt_jobs.run_file_job if source == "file" else yt_jobs.run_job
    args   = (job_id, file_path, data) if source == "file" else (job_id, data)
    threading.Thread(target=yt_jobs._job_guard, args=(target,) + args, daemon=True).start()
    return yt_core._json({"job_id": job_id})


@app.route("/stop/<job_id>", methods=["POST"])
def stop_job(job_id: str):
    job = yt_core.jobs.get(job_id)
    if job is None:
        return yt_core._json({"error": "Not found"}, 404)
    job["stop_event"].set()
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    return yt_core._json({"ok": True})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    job = yt_core.jobs.get(job_id)
    if job is None:
        return "Not found", 404

    def generate():
        q = job["queue"]
        while True:
            try:
                item = q.get(timeout=60)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is None:
                yield "event: done\ndata: \n\n"
                break
            if isinstance(item, dict):
                yield f"event: {item['type']}\ndata: {json.dumps(item)}\n\n"
            else:
                yield f"data: {str(item).replace(chr(10), ' ')}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/result/<job_id>")
def result(job_id: str):
    job = yt_core.jobs.get(job_id)
    if job is None:
        return yt_core._json({"error": "Not found"}, 404)
    return yt_core._json({
        "status":   job["status"],
        "result":   job["result"],
        "filename": os.path.basename(job["output_file"]) if job["output_file"] else None,
        "txt_path": job["output_file"],  # 요약은 이 경로 기반(잡 생명주기와 분리)
        "error_stage": job.get("error_stage") or "",
        "error_message": job.get("error_message") or "",
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    job = yt_core.jobs.get(job_id)
    if job is None:
        return "Not found", 404
    path = job.get("output_file")
    if not path or not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)
