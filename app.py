import glob
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime

from flask import Flask, Response, redirect, render_template, request, send_file

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("yt-script")


def _raise_fd_limit():
    """launchd 기본 소프트 한도(256)는 너무 낮다 — 가능한 만큼 상향(FD 고갈 방지)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for target in (16384, 10240, 8192, 4096):
            cap = target if hard == resource.RLIM_INFINITY else min(target, hard)
            if cap <= soft:
                return
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
                log.info("FD soft limit raised: %d -> %d", soft, cap)
                return
            except (ValueError, OSError):
                continue
    except Exception as e:
        log.warning("could not raise FD limit: %s", e)


_raise_fd_limit()

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import db
import keyframe_report

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app      = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

TIMESTAMP_RE = re.compile(r'\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->')

# ── 플랫폼 어댑터 ─────────────────────────────────────────────────────
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
EXE    = ".exe" if IS_WIN else ""

def _resolve_binary(name: str) -> str:
    """프로젝트 루트의 바이너리를 우선 사용, 없으면 PATH에서 찾음."""
    local = os.path.join(BASE_DIR, f"{name}{EXE}")
    if os.path.exists(local):
        return local
    found = shutil.which(name)
    return found if found else local

# whisper.cpp 디렉토리: 환경변수 우선 → Windows는 -windows-vulkan, 그 외는 whisper.cpp
WHISPER_DIR = os.environ.get(
    "WHISPER_DIR",
    os.path.join(BASE_DIR, "whisper.cpp-windows-vulkan" if IS_WIN else "whisper.cpp"),
)
WHISPER_EXE = os.environ.get("WHISPER_EXE", os.path.join(WHISPER_DIR, f"whisper-cli{EXE}"))
MODEL_PATH  = os.environ.get("MODEL_PATH",  os.path.join(WHISPER_DIR, "ggml-large-v3-turbo-q5_0.bin"))

FFPROBE_EXE     = _resolve_binary("ffprobe")
FFMPEG_LOCATION = os.path.dirname(_resolve_binary("ffmpeg"))

AUDIO_DIR   = os.environ.get("AUDIO_DIR", BASE_DIR)
RES_DIR     = os.path.join(BASE_DIR, "res")
SUMMARY_DIR = os.path.join(RES_DIR, "summary")
AUDIO_EXT   = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma", ".mp4", ".webm"}

DEFAULT_PROMPT = """\
YouTube 영상의 전사 텍스트를 구조화된 요약본으로 재작성해줘.
---
전사 텍스트:
{transcript}
"""

PROMPT_FILE  = os.path.join(BASE_DIR, "prompt.txt")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# 요약 백엔드: "claude" (Claude.app OAuth, 기본) | "gemini" (GEMINI_API_KEY 필요)
SUMMARY_BACKEND = os.environ.get("SUMMARY_BACKEND", "claude").lower()


def _resolve_claude_bin() -> str:
    """claude CLI 경로 해석: env → PATH → Claude.app 최신 설치본(버전 하드코딩 제거)."""
    env = os.environ.get("CLAUDE_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("claude")
    if found:
        return found
    base = os.path.expanduser("~/Library/Application Support/Claude/claude-code")
    cands = glob.glob(os.path.join(base, "*", "claude.app/Contents/MacOS/claude"))
    cands = [c for c in cands if os.path.exists(c)]
    if cands:
        cands.sort(key=os.path.getmtime)  # 가장 최근 설치본
        return cands[-1]
    return env or "claude"


CLAUDE_BIN   = _resolve_claude_bin()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "opus")

# 전사(whisper)는 무거우므로 동시 실행을 제한해 스래싱 방지.
_transcribe_sem = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_TRANSCRIBE", "1")))

# 전사 완료 후 다운로드한 오디오(mp3)를 삭제할지 여부. KEEP_AUDIO=1 이면 보존.
# 단, 사용자가 직접 올린 로컬 파일(file 잡)은 이 값과 무관하게 항상 보존한다.
DELETE_AUDIO_AFTER = os.environ.get("KEEP_AUDIO", "0") != "1"

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


def _nfc(value):
    """iOS/macOS의 자모분리(NFD) 한글을 정상 결합형(NFC)으로 변환한다."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value


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
    try:
        db.upsert(md_path)
    except Exception:
        pass


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
    # 주의: JSON 삭제는 md 저장이 확정된 뒤 _finish_transcription에서 수행한다.
    return "\n".join(cleaned)


def _run_whisper(job_id: str, audio_path: str, language: str,
                 threads: str, total: float) -> int:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]

    # 동시 전사 제한: 슬롯을 못 얻으면 대기 안내 후 블로킹 획득.
    if not _transcribe_sem.acquire(blocking=False):
        q.put("다른 전사가 진행 중입니다 — 대기열에서 대기...")
        _transcribe_sem.acquire()
    try:
        if stop.is_set():
            return -1
        proc = subprocess.Popen(
            [WHISPER_EXE, "-m", MODEL_PATH, "-f", audio_path,
             "--language", language, "--threads", threads,
             "--output-json", "--temperature", "0",
             "--best-of", "5", "--beam-size", "5",
             "--no-speech-thold", "0.8", "--flash-attn"],
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
    finally:
        _transcribe_sem.release()


def _finish_transcription(job_id: str, audio_path: str, rc: int, md_path: str,
                          delete_audio: bool = False) -> None:
    q = jobs[job_id]["queue"]
    json_path = audio_path + ".json"
    if rc == 0:
        transcript = _parse_transcript(json_path)
        if transcript is not None:
            _save_md(md_path, jobs[job_id].get("meta", {}), transcript)
            jobs[job_id].update({"status": "done", "result": transcript, "output_file": md_path})
            # md 저장이 확정된 뒤에만 whisper JSON 삭제(실패 시 재전사 없이 복구 가능).
            try:
                os.remove(json_path)
            except OSError:
                pass
            # 전사본(.md)이 단일 진실 소스. 다운로드한 오디오는 저장 확정 후 삭제(용량 회수).
            # delete_audio=True(URL 잡)이고 KEEP_AUDIO 미설정일 때만. 사용자 업로드 파일은 보존.
            if delete_audio and DELETE_AUDIO_AFTER:
                try:
                    os.remove(audio_path)
                    log.info("source audio removed: %s", os.path.basename(audio_path))
                except OSError as e:
                    log.warning("audio cleanup failed: %s (%s)", os.path.basename(audio_path), e)
            log.info("transcription done: %s", os.path.basename(md_path))
            q.put(f"✅ 전사 저장됨: {os.path.basename(md_path)}")
            q.put({"type": "progress", "pct": 100})
            q.put(None)
            return
    jobs[job_id]["status"] = "error"
    log.warning("transcription failed (rc=%s): %s", rc, os.path.basename(md_path))
    q.put(None)


def _transcribe_and_finish(job_id: str, audio_path: str, lang: str,
                           thr: str, total: float, md_path: str,
                           delete_audio: bool = False) -> None:
    """전사 공통 꼬리: stage 전환 → whisper 실행 → 취소 확인 → 결과 저장.

    run_job / run_file_job 양쪽이 공유한다.
    delete_audio=True 면 전사 성공 시 원본 오디오 삭제(URL 다운로드 건 전용).
    """
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    _stage(q, "transcribe")
    q.put("Whisper 전사 시작...")
    rc = _run_whisper(job_id, audio_path, lang, thr, total)
    if stop.is_set():
        jobs[job_id]["status"] = "cancelled"
        q.put("중지됨.")
        q.put(None)
        return
    _finish_transcription(job_id, audio_path, rc, md_path, delete_audio=delete_audio)


# ── Job runners ───────────────────────────────────────────────────────

def run_job(job_id: str, params: dict) -> None:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    url  = params["url"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 8))

    q.put("영상 정보 가져오는 중...")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title    = _nfc(info.get("title", "audio"))
        uploader = _nfc(info.get("uploader") or info.get("channel") or "—")
        total    = float(info.get("duration") or 0)
    except Exception as e:
        q.put(f"오류: {e}")
        jobs[job_id]["status"] = "error"
        q.put(None)
        return

    jobs[job_id]["total_duration"] = total
    jobs[job_id]["meta"] = {k: _nfc(info[k]) for k in _META_KEYS if info.get(k) is not None}
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
            "ffmpeg_location": FFMPEG_LOCATION,
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
    _transcribe_and_finish(job_id, audio_path, lang, thr, total,
                           unique_path(out_dir, stem_final, ".md"),
                           delete_audio=True)  # URL 다운로드본은 전사 후 삭제


def run_file_job(job_id: str, file_path: str, params: dict) -> None:
    q    = jobs[job_id]["queue"]
    stop = jobs[job_id]["stop_event"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 8))

    q.put(f"파일: {os.path.basename(file_path)}")

    title, uploader = "N/A", "N/A"
    meta_json = os.path.splitext(file_path)[0] + ".json"
    if os.path.exists(meta_json):
        try:
            with open(meta_json, encoding="utf-8") as f:
                m = json.load(f)
            title    = _nfc(m.get("title") or "N/A")
            uploader = _nfc(m.get("uploader") or m.get("channel") or "N/A")
        except Exception:
            pass
    q.put({"type": "videoinfo", "title": title, "uploader": uploader})

    total = get_file_duration(file_path)
    jobs[job_id]["total_duration"] = total
    jobs[job_id]["meta"] = {
        "title":       title if title != "N/A" else "",
        "uploader":    uploader if uploader != "N/A" else "",
        "duration":    total,
        "source_file": _nfc(os.path.basename(file_path)),
    }
    if total > 0:
        q.put(f"파일 길이: {_format_duration(total)}")
        q.put({"type": "duration", "seconds": total})

    out_dir = _dated_dir()
    stem    = datetime.now().strftime("%Y%m%d%H%M_") + os.path.splitext(os.path.basename(file_path))[0]
    _transcribe_and_finish(job_id, file_path, lang, thr, total,
                           unique_path(out_dir, stem, ".md"))


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


# ── SQLite 인덱스 초기화 + 백그라운드 reindex ─────────────────────────
db.init()


def _bg_reindex():
    try:
        s = db.reindex_all()
        log.info("db reindex: %s", s)
    except Exception as e:
        log.error("db reindex error: %s", e)


threading.Thread(target=_bg_reindex, daemon=True).start()


# ── Remote-access gating ──────────────────────────────────────────────
# 로컬(=동일 머신)에서는 모든 라우트 허용. 외부(Tailscale/LAN)에서는
# 이력조회 관련 라우트만 허용하고, 그 외 경로는 이력 페이지로 리다이렉트한다.
#  - 모바일 UA → /m (모바일 이력)
#  - 데스크톱  → /  (index.html이 원격 읽기전용 이력 모드로 렌더)
_LOOPBACK = {"127.0.0.1", "::1"}
# 원격에서 허용되는 데이터 엔드포인트(이력 조회/요약/읽음/삭제). 페이지(/, /m)는 별도 처리.
_REMOTE_DATA_ALLOWED = {
    "/history", "/history/text", "/history/mark_read", "/history/item", "/summary/content",
}
_MOBILE_UA = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.I)


def _is_remote() -> bool:
    return request.remote_addr not in _LOOPBACK


@app.teardown_request
def _close_db_conn(exc):
    # 요청 처리 스레드의 SQLite 연결을 닫아 FD 누적 방지(개발서버 thread-per-request).
    try:
        db.close_conn()
    except Exception:
        pass


@app.before_request
def _restrict_remote_access():
    if request.remote_addr in _LOOPBACK:
        return
    p = request.path
    # 데이터/정적 엔드포인트는 그대로 허용 (/sframe = 요약 키프레임 이미지)
    if (p in _REMOTE_DATA_ALLOWED
            or p.startswith("/m/")
            or p.startswith("/static/")
            or p.startswith("/sframe/")):
        return
    is_mobile = bool(_MOBILE_UA.search(request.headers.get("User-Agent", "")))
    # 이력 페이지: 디바이스에 맞는 쪽으로 정규화
    if p in ("/", "/m"):
        if p == "/" and is_mobile:
            return redirect("/m", 302)
        if p == "/m" and not is_mobile:
            return redirect("/", 302)
        return  # 디바이스에 맞는 페이지 → 통과
    # 그 외 모든 경로 → 403 문구 없이 이력 페이지로 이동
    return redirect("/m" if is_mobile else "/", 302)


# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # 원격 접속이면 전사 UI를 숨긴 읽기전용 이력 모드로 렌더
    return render_template("index.html", default_prompt=DEFAULT_PROMPT, remote=_is_remote())


@app.route("/m")
def mobile_history():
    return render_template("mobile.html")


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
    job = jobs.get(job_id)
    if job is None:
        return _json({"error": "Not found"}, 404)
    job["stop_event"].set()
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    return _json({"ok": True})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    job = jobs.get(job_id)
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
    job = jobs.get(job_id)
    if job is None:
        return _json({"error": "Not found"}, 404)
    return _json({
        "status":   job["status"],
        "result":   job["result"],
        "filename": os.path.basename(job["output_file"]) if job["output_file"] else None,
        "txt_path": job["output_file"],  # 요약은 이 경로 기반(잡 생명주기와 분리)
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return "Not found", 404
    path = job.get("output_file")
    if not path or not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)


def _summary_path_for_md(md_path: str) -> str | None:
    """전사 md 경로 → res/summary/{date}/{stem}.md 경로 산출(디렉토리 생성 포함)."""
    try:
        rel = os.path.relpath(md_path, RES_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    p = os.path.join(SUMMARY_DIR, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _md_from_summary_path(save_path: str) -> str | None:
    """summary md 경로에서 짝이 되는 전사 md 경로 역산."""
    try:
        rel = os.path.relpath(save_path, SUMMARY_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return os.path.join(RES_DIR, rel)


def _reindex_summary(save_path: str) -> None:
    md = _md_from_summary_path(save_path)
    if md:
        try:
            db.upsert_summary_only(md)
        except Exception:
            pass


def _summarize_with_claude(prompt: str, save_path: str | None):
    """Claude Code CLI로 요약 → SSE 청크 생성, 완료 시 save_path에 저장.

    프롬프트는 argv가 아니라 stdin으로 전달한다(긴 전사로 인한 ARG_MAX 초과 회피).
    """
    proc = subprocess.Popen(
        [CLAUDE_BIN, "-p",
         "--output-format", "stream-json",
         "--include-partial-messages",
         "--model", CLAUDE_MODEL,
         "--verbose"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        yield f"event: error\ndata: {json.dumps('프롬프트 전달 실패: ' + str(e))}\n\n"
        return
    chunks: list[str] = []
    final: str | None = None
    error_msg: str | None = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "stream_event":
                sub = ev.get("event") or {}
                if sub.get("type") == "content_block_delta":
                    delta = sub.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            chunks.append(text)
                            yield f"data: {json.dumps(text)}\n\n"
            elif t == "result":
                if ev.get("is_error"):
                    error_msg = ev.get("result") or "Claude CLI 오류"
                else:
                    final = ev.get("result") or "".join(chunks)
        proc.wait(timeout=10)
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        error_msg = str(e)

    if error_msg:
        yield f"event: error\ndata: {json.dumps(error_msg)}\n\n"
        return

    full = final if final is not None else "".join(chunks)
    if full and save_path:
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(full)
        except Exception as e:
            yield f"event: error\ndata: {json.dumps('저장 실패: ' + str(e))}\n\n"
            return
        _reindex_summary(save_path)
    yield "event: done\ndata: \n\n"


def _summarize_with_gemini(prompt: str, save_path: str | None):
    if not os.environ.get("GEMINI_API_KEY"):
        yield f"event: error\ndata: {json.dumps('GEMINI_API_KEY가 .env 파일에 없습니다.')}\n\n"
        return
    chunks: list[str] = []
    try:
        for chunk in _get_gemini_model().generate_content(prompt, stream=True):
            if chunk.text:
                chunks.append(chunk.text)
                yield f"data: {json.dumps(chunk.text)}\n\n"
    except Exception as e:
        yield f"event: error\ndata: {json.dumps(str(e))}\n\n"
        return

    full = "".join(chunks)
    if full and save_path:
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(full)
        except Exception as e:
            yield f"event: error\ndata: {json.dumps('저장 실패: ' + str(e))}\n\n"
            return
        _reindex_summary(save_path)
    yield "event: done\ndata: \n\n"


@app.route("/summarize", methods=["POST"])
def summarize():
    """전사 md 파일 경로(txt_path) 기반 요약. 잡 생명주기/메모리 상태와 무관하게 동작."""
    data = request.get_json(force=True) or {}
    abs_path, err = _check_res_path(data.get("txt_path") or "")
    if err:
        return err
    if not os.path.isfile(abs_path):
        return _json({"error": "전사 파일을 찾을 수 없습니다."}, 404)

    # 전사 md(메타+본문) 전체를 프롬프트에 주입 — 메타 표 작성을 위해 필요.
    with open(abs_path, encoding="utf-8", errors="replace") as f:
        transcript_blob = f.read()
    if not transcript_blob.strip():
        return _json({"error": "전사 결과가 없습니다."}, 400)

    prompt = (data.get("prompt") or DEFAULT_PROMPT).replace("{transcript}", transcript_blob)
    save_path = _summary_path_for_md(abs_path)
    log.info("summarize start: %s (backend=%s)", os.path.basename(abs_path), SUMMARY_BACKEND)

    if SUMMARY_BACKEND == "gemini":
        gen = _summarize_with_gemini(prompt, save_path)
    else:
        gen = _summarize_with_claude(prompt, save_path)

    return Response(gen, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/prompt", methods=["GET"])
def get_prompt():
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, encoding="utf-8") as f:
            return _json({"prompt": f.read()})
    return _json({"prompt": DEFAULT_PROMPT})


@app.route("/prompt", methods=["POST"])
def save_prompt():
    data = request.get_json(force=True) or {}
    prompt = data.get("prompt", "")
    # {transcript} 자리표시자 필수 — 없으면 요약 시 전사 본문이 주입되지 않음.
    if "{transcript}" not in prompt:
        return _json({"error": "{transcript} 자리표시자가 필요합니다."}, 400)
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(prompt)
    return _json({"ok": True})


@app.route("/history")
def get_history():
    q = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread_only") == "1"
    try:
        items = db.search(q, unread_only=unread_only) if q else db.list_items(unread_only=unread_only)
    except Exception:
        items = []
    return _json({"items": items})


@app.route("/history/mark_read", methods=["PATCH"])
def mark_read():
    data = request.get_json(force=True)
    txt_path = (data.get("txt_path") or "").strip()
    is_read  = bool(data.get("is_read", True))
    abs_path, err = _check_res_path(txt_path)
    if err:
        return err
    ok = db.mark_read(abs_path, is_read)
    if not ok:
        return _json({"error": "항목을 찾을 수 없습니다."}, 404)
    return _json({"ok": True, "is_read": is_read})


@app.route("/history/item", methods=["DELETE"])
def history_delete():
    data = request.get_json(force=True)
    txt_path = (data.get("txt_path") or "").strip()
    abs_path, err = _check_res_path(txt_path)
    if err:
        return err
    # summary 경로 계산 (DB에서 가져오거나 경로 규칙으로 유추)
    try:
        rel = os.path.relpath(abs_path, RES_DIR)
        summary_path = os.path.join(SUMMARY_DIR, rel)
    except ValueError:
        summary_path = None
    # DB에서 먼저 제거
    db.delete(abs_path)
    # 파일 삭제
    deleted = []
    errors = []
    for p in filter(None, [abs_path, summary_path]):
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(p)
            except OSError as e:
                errors.append(str(e))
    return _json({"ok": True, "deleted": deleted, "errors": errors})


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


def _reveal_in_file_manager(abs_path: str) -> None:
    """OS별 파일 탐색기에서 해당 파일/폴더를 보여준다."""
    exists  = os.path.exists(abs_path)
    target  = abs_path if exists else os.path.dirname(abs_path)
    if IS_WIN:
        if exists:
            subprocess.Popen(["explorer", f"/select,{abs_path}"])
        else:
            subprocess.Popen(["explorer", target])
    elif IS_MAC:
        if exists:
            subprocess.Popen(["open", "-R", abs_path])
        else:
            subprocess.Popen(["open", target])
    else:  # Linux 등
        subprocess.Popen(["xdg-open", target])


@app.route("/history/explore", methods=["POST"])
def history_explore():
    abs_path, err = _check_res_path(request.get_json(force=True).get("txt_path") or "")
    if err:
        return err
    try:
        _reveal_in_file_manager(abs_path)
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
            "title":    _nfc(info.get("title") or ""),
            "uploader": _nfc(info.get("uploader") or info.get("channel") or ""),
            "duration": float(info.get("duration") or 0),
        })
    except Exception as e:
        return _json({"error": str(e)}, 400)


def _parse_summary_title(md_path: str) -> str:
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:]
    except Exception:
        pass
    return os.path.basename(md_path)[:-3]


@app.route("/summary")
def summary_view():
    return render_template("summary.html")


@app.route("/summary/list")
def summary_list():
    if not os.path.isdir(SUMMARY_DIR):
        return _json({"groups": []})
    try:
        date_dirs = sorted(
            [d for d in os.listdir(SUMMARY_DIR)
             if os.path.isdir(os.path.join(SUMMARY_DIR, d))],
            reverse=True,
        )
    except Exception:
        return _json({"groups": []})

    groups = []
    for date_dir in date_dirs:
        date_path = os.path.join(SUMMARY_DIR, date_dir)
        try:
            fnames = sorted(
                [f for f in os.listdir(date_path) if f.endswith(".md")],
                reverse=True,
            )
        except Exception:
            continue
        files = []
        for fname in fnames:
            md_path = os.path.join(date_path, fname)
            files.append({
                "name": fname,
                "title": _parse_summary_title(md_path),
                "path": md_path,
            })
        if files:
            groups.append({"date": date_dir, "files": files})
    return _json({"groups": groups})


@app.route("/summary/content", methods=["POST"])
def summary_content():
    path = (request.get_json(force=True).get("path") or "").strip()
    abs_path = os.path.realpath(path)
    summary_real = os.path.realpath(SUMMARY_DIR)
    if not (abs_path.startswith(summary_real + os.sep) or abs_path == summary_real):
        return _json({"error": "접근 거부"}, 403)
    if not os.path.isfile(abs_path):
        return _json({"error": "파일 없음"}, 404)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return _json({"content": content})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/sframe/<path:rel>")
def serve_summary_frame(rel: str):
    """요약 키프레임 이미지 서빙 (res/summary 하위로 경로 검증)."""
    p = os.path.realpath(os.path.join(SUMMARY_DIR, rel))
    base = os.path.realpath(SUMMARY_DIR)
    if not p.startswith(base + os.sep):
        return "denied", 403
    if not os.path.isfile(p):
        return "not found", 404
    return send_file(p)


@app.route("/keyframes", methods=["POST"])
def keyframes():
    """전사 md(txt_path) 기반으로 영상 키프레임을 추출·정렬해 요약에 합친다.

    요약이 먼저 있어야 함(섹션 정렬 대상). 영상 다운로드 실패 시 요약은 그대로 유지(폴백).
    """
    data = request.get_json(force=True) or {}
    abs_path, err = _check_res_path(data.get("txt_path") or "")
    if err:
        return err
    summary_path = _summary_path_for_md(abs_path)
    if not summary_path or not os.path.isfile(summary_path):
        return _json({"ok": False, "reason": "no_summary", "error": "요약이 먼저 필요합니다."}, 400)

    rel = os.path.relpath(summary_path, SUMMARY_DIR)        # {date}/{stem}.md
    date_dir = rel.split(os.sep)[0]
    stem = os.path.splitext(os.path.basename(summary_path))[0]
    frames_dir = os.path.join(SUMMARY_DIR, date_dir, stem + ".frames")
    url_base = f"/sframe/{date_dir}/{stem}.frames"

    try:
        res = keyframe_report.generate_keyframes(
            summary_path, (data.get("url") or "").strip(), frames_dir, url_base)
    except Exception as e:
        log.warning("keyframes error: %s", e)
        return _json({"ok": False, "reason": "error", "error": str(e)})

    if res.get("ok") and res.get("n_frames"):
        try:
            db.upsert_summary_only(abs_path)   # 검색 인덱스 갱신
        except Exception:
            pass
        try:
            with open(summary_path, encoding="utf-8", errors="replace") as f:
                res["summary_md"] = f.read()   # 갱신된 요약(라이브 재렌더용)
        except Exception:
            pass
    log.info("keyframes: %s (%s)", os.path.basename(summary_path), res)
    return _json(res)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    log.info("starting server on %s:%s — claude=%s, backend=%s, model=%s",
             host, port, CLAUDE_BIN, SUMMARY_BACKEND, CLAUDE_MODEL)
    app.run(debug=False, host=host, port=port, threaded=True)
