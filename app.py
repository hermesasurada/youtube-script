import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, Response, render_template, request, send_file

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app      = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

TIMESTAMP_RE = re.compile(r'\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->')

WHISPER_EXE = os.path.join(BASE_DIR, "whisper.cpp-windows-vulkan", "whisper-cli.exe")
MODEL_PATH  = os.path.join(BASE_DIR, "whisper.cpp-windows-vulkan", "ggml-large-v3-turbo-q5_0.bin")
FFPROBE_EXE = os.path.join(BASE_DIR, "ffprobe.exe")
AUDIO_DIR   = os.environ.get("AUDIO_DIR", BASE_DIR)
RES_DIR     = os.path.join(BASE_DIR, "res")
AUDIO_EXT   = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma", ".mp4", ".webm"}

DEFAULT_PROMPT = """\
YouTube 영상의 전사 텍스트를 구조화된 요약본으로 재작성해줘.
---
전사 텍스트:
{transcript}
"""

PROMPT_FILE  = os.path.join(BASE_DIR, "prompt.txt")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# view_count, like_count 제외
_META_KEYS = [
    "id", "title", "uploader", "channel", "channel_url",
    "duration", "upload_date", "webpage_url",
    "categories", "tags",
]

# md 프론트매터 출력 순서
_MD_META_ORDER = [
    "title", "uploader", "channel", "channel_url",
    "duration", "upload_date", "webpage_url", "id",
    "categories", "tags", "source_file",
]

_genai_model = None
_genai_lock  = threading.Lock()
_duration_cache: dict[str, tuple[float, float]] = {}


# ── Helpers ───────────────────────────────────────────────────────────

def _json(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False),
                    status=status, mimetype="application/json")


def _safe_stem(title: str, maxlen: int = 60) -> str:
    s = re.sub(r'[^\w\s]', '', title)
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'_+', '_', s)
    return (s.strip('_') or 'untitled')[:maxlen]


def _dated_dir() -> str:
    d = os.path.join(RES_DIR, datetime.now().strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    return d


def _format_duration(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _dur_tag(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s") if secs > 0 else "0s"


def unique_path(directory: str, stem: str, ext: str) -> str:
    path = os.path.join(directory, stem + ext)
    i = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem}_{i}{ext}")
        i += 1
    return path


def _check_res_path(txt_path: str):
    abs_path = os.path.realpath(txt_path.strip())
    res_real = os.path.realpath(RES_DIR)
    if not (abs_path.startswith(res_real + os.sep) or abs_path == res_real):
        return None, _json({"error": "접근 거부"}, 403)
    return abs_path, None


def get_file_duration(file_path: str) -> float:
    if not os.path.exists(FFPROBE_EXE):
        return 0.0
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return 0.0
    cached = _duration_cache.get(file_path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        r = subprocess.run(
            [FFPROBE_EXE, "-v", "quiet", "-print_format", "json", "-show_format", file_path],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
        )
        duration = float(json.loads(r.stdout).get("format", {}).get("duration") or 0)
    except Exception:
        duration = 0.0
    _duration_cache[file_path] = (mtime, duration)
    return duration


def _get_gemini_model():
    global _genai_model
    if _genai_model is not None:
        return _genai_model
    with _genai_lock:
        if _genai_model is None:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            _genai_model = genai.GenerativeModel(GEMINI_MODEL)
    return _genai_model


# ── Markdown I/O ──────────────────────────────────────────────────────

def _save_md(md_path: str, meta: dict, transcript: str) -> None:
    lines = ["---"]
    written = set()
    for key in _MD_META_ORDER:
        val = meta.get(key)
        if val is None or val == "" or val == []:
            continue
        written.add(key)
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {val}")
    # 정의된 순서에 없는 추가 키
    for key, val in meta.items():
        if key in written or val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {val}")
    lines += ["---", ""]
    if meta.get("title"):
        lines += [f"# {meta['title']}", ""]
    lines.append(transcript)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _parse_yaml_front_matter(text: str) -> dict:
    result: dict = {}
    current_list_key = None
    for line in text.splitlines():
        if line.startswith("  - "):
            if current_list_key is not None:
                result[current_list_key].append(line[4:].strip())
        elif ": " in line:
            k, v = line.split(": ", 1)
            result[k.strip()] = v.strip()
            current_list_key = None
        elif line.endswith(":") and not line.startswith(" "):
            k = line[:-1].strip()
            result[k] = []
            current_list_key = k
        else:
            current_list_key = None
    return result


def _parse_md(md_path: str) -> tuple[dict, str]:
    with open(md_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    if not content.startswith("---\n"):
        return {}, content.strip()
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content.strip()
    meta = _parse_yaml_front_matter(content[4:end])
    body = content[end + 5:].strip()
    # H1 제목 줄 건너뜀
    if body.startswith("# "):
        nl = body.find("\n")
        body = body[nl + 1:].strip() if nl != -1 else ""
    return meta, body


# ── Transcription core ────────────────────────────────────────────────

def _emit_progress(q: queue.Queue, line: str, total: float) -> None:
    m = TIMESTAMP_RE.search(line)
    if m and total > 0:
        current = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                   + int(m.group(3)) + int(m.group(4)) / 1000)
        q.put({"type": "progress", "pct": min(99, int(current / total * 100))})


def _stage(q: queue.Queue, stage: str) -> None:
    q.put({"type": "stage", "stage": stage})


def _parse_transcript(json_path: str) -> str | None:
    if not os.path.exists(json_path):
        return None
    with open(json_path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    cleaned, prev = [], ""
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if text and text != prev:
            cleaned.append(text)
            prev = text
    os.remove(json_path)
    return "\n".join(cleaned)


def _run_whisper(job_id: str, audio_path: str, language: str,
                 threads: str, total: float) -> int:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    proc = subprocess.Popen(
        [WHISPER_EXE, "-m", MODEL_PATH, "-f", audio_path,
         "--language", language, "--threads", threads,
         "--output-json", "--temperature", "0",
         "--best-of", "5", "--no-speech-thold", "0.8"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=BASE_DIR,
    )
    jobs[job_id]["proc"] = proc
    for raw in iter(proc.stdout.readline, b""):
        if stop.is_set():
            proc.kill()
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line.startswith("whisper_print_timings"):
            continue
        q.put(line)
        _emit_progress(q, line, total)
    proc.wait()
    jobs[job_id]["proc"] = None
    return proc.returncode


def _finish_transcription(job_id: str, audio_path: str, rc: int, md_path: str) -> None:
    q = jobs[job_id]["queue"]
    if rc == 0:
        transcript = _parse_transcript(audio_path + ".json")
        if transcript is not None:
            _save_md(md_path, jobs[job_id].get("meta", {}), transcript)
            jobs[job_id].update({"status": "done", "result": transcript, "output_file": md_path})
            q.put(f"✅ 전사 저장됨: {os.path.basename(md_path)}")
            q.put({"type": "progress", "pct": 100})
            q.put(None)
            return
    jobs[job_id]["status"] = "error"
    q.put(None)


# ── Job runners ───────────────────────────────────────────────────────

def run_job(job_id: str, params: dict) -> None:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    url  = params["url"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 6))

    q.put("영상 정보 가져오는 중...")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title    = info.get("title", "audio")
        uploader = info.get("uploader") or info.get("channel") or "—"
        total    = float(info.get("duration") or 0)
    except Exception as e:
        q.put(f"오류: {e}")
        jobs[job_id]["status"] = "error"
        q.put(None)
        return

    jobs[job_id]["total_duration"] = total
    jobs[job_id]["meta"] = {k: info[k] for k in _META_KEYS if info.get(k) is not None}
    if total > 0:
        q.put(f"영상 길이: {_format_duration(total)}")
        q.put({"type": "duration", "seconds": total})
    q.put({"type": "videoinfo", "title": title, "uploader": uploader})

    out_dir    = _dated_dir()
    ts         = datetime.now().strftime("%Y%m%d%H%M")
    stem       = f"{ts}_{_dur_tag(total)}_{_safe_stem(title)}"
    audio_path = unique_path(out_dir, stem, ".mp3")
    stem_final = os.path.splitext(os.path.basename(audio_path))[0]

    _stage(q, "download")
    q.put(f"다운로드 중: {title}")

    def _progress_hook(d):
        if stop.is_set():
            raise Exception("사용자가 취소함")
        if d["status"] == "downloading":
            q.put(f"  다운로드: {d.get('_percent_str', '').strip()}  {d.get('_speed_str', '').strip()}")
        elif d["status"] == "finished":
            q.put("다운로드 완료")

    try:
        with yt_dlp.YoutubeDL({
            "format": "bestaudio/best",
            "outtmpl": os.path.splitext(audio_path)[0],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "quiet": True,
            "progress_hooks": [_progress_hook],
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 1,
        }) as ydl:
            ydl.download([url])
    except Exception as e:
        jobs[job_id]["status"] = "cancelled" if stop.is_set() else "error"
        if not stop.is_set():
            q.put(f"다운로드 오류: {e}")
        q.put(None)
        return

    if stop.is_set() or not os.path.exists(audio_path):
        jobs[job_id]["status"] = "cancelled" if stop.is_set() else "error"
        q.put(None)
        return

    q.put(f"저장됨: {os.path.basename(audio_path)}")
    _stage(q, "transcribe")
    q.put("Whisper 전사 시작...")
    rc = _run_whisper(job_id, audio_path, lang, thr, total)

    if stop.is_set():
        jobs[job_id]["status"] = "cancelled"
        q.put("중지됨.")
        q.put(None)
        return

    _finish_transcription(job_id, audio_path, rc, unique_path(out_dir, stem_final, ".md"))


def run_file_job(job_id: str, file_path: str, params: dict) -> None:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 6))

    q.put(f"파일: {os.path.basename(file_path)}")

    title, uploader = "N/A", "N/A"
    meta_json = os.path.splitext(file_path)[0] + ".json"
    if os.path.exists(meta_json):
        try:
            with open(meta_json, encoding="utf-8") as f:
                m = json.load(f)
            title    = m.get("title") or "N/A"
            uploader = m.get("uploader") or m.get("channel") or "N/A"
        except Exception:
            pass
    q.put({"type": "videoinfo", "title": title, "uploader": uploader})

    total = get_file_duration(file_path)
    jobs[job_id]["total_duration"] = total
    jobs[job_id]["meta"] = {
        "title":       title if title != "N/A" else "",
        "uploader":    uploader if uploader != "N/A" else "",
        "duration":    total,
        "source_file": os.path.basename(file_path),
    }
    if total > 0:
        q.put(f"파일 길이: {_format_duration(total)}")
        q.put({"type": "duration", "seconds": total})

    _stage(q, "transcribe")
    q.put("Whisper 전사 시작...")
    rc = _run_whisper(job_id, file_path, lang, thr, total)

    if stop.is_set():
        jobs[job_id]["status"] = "cancelled"
        q.put("중지됨.")
        q.put(None)
        return

    out_dir  = _dated_dir()
    stem     = datetime.now().strftime("%Y%m%d%H%M_") + os.path.splitext(os.path.basename(file_path))[0]
    _finish_transcription(job_id, file_path, rc, unique_path(out_dir, stem, ".md"))


def _cleanup_old_jobs() -> None:
    while True:
        time.sleep(1800)
        cutoff = time.time() - 3600
        with jobs_lock:
            stale = [jid for jid, j in jobs.items()
                     if j["status"] != "running" and j["start_time"] < cutoff]
            for jid in stale:
                jobs.pop(jid)


threading.Thread(target=_cleanup_old_jobs, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", default_prompt=DEFAULT_PROMPT)


@app.route("/files")
def list_files():
    files, seen = [], set()

    def _scan(directory: str) -> None:
        try:
            for fname in os.listdir(directory):
                if os.path.splitext(fname)[1].lower() not in AUDIO_EXT:
                    continue
                path = os.path.join(directory, fname)
                if not os.path.isfile(path) or path in seen:
                    continue
                seen.add(path)
                files.append({
                    "name":     fname,
                    "path":     path,
                    "size":     os.path.getsize(path),
                    "duration": get_file_duration(path),
                })
        except Exception:
            pass

    if os.path.isdir(RES_DIR):
        for sub in sorted(os.listdir(RES_DIR), reverse=True):
            sub_path = os.path.join(RES_DIR, sub)
            if os.path.isdir(sub_path):
                _scan(sub_path)
    if AUDIO_DIR not in (BASE_DIR, RES_DIR):
        _scan(AUDIO_DIR)

    files.sort(key=lambda x: x["name"].lower())
    return _json({"files": files, "dir": RES_DIR})


@app.route("/start", methods=["POST"])
def start():
    data   = request.get_json(force=True)
    source = data.get("source", "url")

    if source == "file":
        file_path = (data.get("file_path") or "").strip()
        if not file_path or not os.path.exists(file_path):
            return _json({"error": "파일을 찾을 수 없습니다."}, 400)
    else:
        url = (data.get("url") or "").strip()
        if not url:
            return _json({"error": "URL을 입력해주세요."}, 400)

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status":         "running",
            "queue":          queue.Queue(),
            "result":         None,
            "output_file":    None,
            "start_time":     time.time(),
            "total_duration": 0,
            "proc":           None,
            "stop_event":     threading.Event(),
            "meta":           {},
        }

    target = run_file_job if source == "file" else run_job
    args   = (job_id, file_path, data) if source == "file" else (job_id, data)
    threading.Thread(target=target, args=args, daemon=True).start()
    return _json({"job_id": job_id})


@app.route("/stop/<job_id>", methods=["POST"])
def stop_job(job_id: str):
    if job_id not in jobs:
        return _json({"error": "Not found"}, 404)
    job = jobs[job_id]
    job["stop_event"].set()
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    return _json({"ok": True})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    if job_id not in jobs:
        return "Not found", 404

    def generate():
        q = jobs[job_id]["queue"]
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
    if job_id not in jobs:
        return _json({"error": "Not found"}, 404)
    job = jobs[job_id]
    return _json({
        "status":   job["status"],
        "result":   job["result"],
        "filename": os.path.basename(job["output_file"]) if job["output_file"] else None,
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    if job_id not in jobs:
        return "Not found", 404
    path = jobs[job_id].get("output_file")
    if not path or not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)


@app.route("/summarize/<job_id>", methods=["POST"])
def summarize(job_id: str):
    if job_id not in jobs:
        return _json({"error": "Not found"}, 404)
    data       = request.get_json(force=True)
    transcript = jobs[job_id].get("result")
    if not transcript:
        return _json({"error": "전사 결과가 없습니다."}, 400)

    if not os.environ.get("GEMINI_API_KEY"):
        def _err():
            yield f"event: error\ndata: {json.dumps('GEMINI_API_KEY가 .env 파일에 없습니다.')}\n\n"
        return Response(_err(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})

    prompt = (data.get("prompt") or DEFAULT_PROMPT).replace("{transcript}", transcript)

    def generate():
        try:
            for chunk in _get_gemini_model().generate_content(prompt, stream=True):
                if chunk.text:
                    yield f"data: {json.dumps(chunk.text)}\n\n"
            yield "event: done\ndata: \n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(str(e))}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/prompt", methods=["GET"])
def get_prompt():
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, encoding="utf-8") as f:
            return _json({"prompt": f.read()})
    return _json({"prompt": DEFAULT_PROMPT})


@app.route("/prompt", methods=["POST"])
def save_prompt():
    data = request.get_json(force=True)
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(data.get("prompt", ""))
    return _json({"ok": True})


@app.route("/history")
def get_history():
    if not os.path.isdir(RES_DIR):
        return _json({"items": []})
    try:
        date_dirs = sorted(
            [d for d in os.listdir(RES_DIR) if os.path.isdir(os.path.join(RES_DIR, d))],
            reverse=True,
        )
    except Exception:
        return _json({"items": []})

    items = []
    for date_dir in date_dirs:
        date_path = os.path.join(RES_DIR, date_dir)
        try:
            fnames = sorted(os.listdir(date_path), reverse=True)
        except Exception:
            continue
        for fname in fnames:
            if not fname.endswith(".md"):
                continue
            md_path = os.path.join(date_path, fname)
            try:
                meta, transcript = _parse_md(md_path)
            except Exception:
                continue
            items.append({
                "date":        date_dir,
                "stem":        fname[:-3],
                "title":       meta.get("title") or fname[:-3],
                "uploader":    meta.get("uploader") or meta.get("channel") or "—",
                "duration":    float(meta.get("duration") or 0),
                "webpage_url": meta.get("webpage_url") or "",
                "categories":  meta.get("categories") or [],
                "tags":        meta.get("tags") or [],
                "channel_url": meta.get("channel_url") or "",
                "has_txt":     bool(transcript),
                "txt_path":    md_path,
            })
    return _json({"items": items})


@app.route("/history/text", methods=["POST"])
def history_text():
    abs_path, err = _check_res_path(request.get_json(force=True).get("txt_path") or "")
    if err:
        return err
    if not os.path.exists(abs_path):
        return _json({"error": "파일 없음"}, 404)
    try:
        _, transcript = _parse_md(abs_path)
        return _json({"text": transcript})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/history/explore", methods=["POST"])
def history_explore():
    abs_path, err = _check_res_path(request.get_json(force=True).get("txt_path") or "")
    if err:
        return err
    try:
        if os.path.exists(abs_path):
            subprocess.Popen(["explorer", f"/select,{abs_path}"])
        else:
            subprocess.Popen(["explorer", os.path.dirname(abs_path)])
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/info", methods=["POST"])
def video_info():
    if yt_dlp is None:
        return _json({"error": "yt-dlp not available"}, 400)
    data = request.get_json(force=True)
    url  = (data.get("url") or "").strip()
    if not url:
        return _json({"error": "URL 없음"}, 400)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return _json({
            "title":    info.get("title") or "",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": float(info.get("duration") or 0),
        })
    except Exception as e:
        return _json({"error": str(e)}, 400)


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5000, threaded=True)
