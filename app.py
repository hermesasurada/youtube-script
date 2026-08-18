import json
import gzip
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
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
import document_io
import keyframe_report
import llm_gateway

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app      = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _static_version(rel_path: str) -> str:
    """정적 자원 URL 버전. 파일이 바뀔 때만 브라우저 캐시 키가 바뀐다."""
    try:
        return format(os.stat(os.path.join(BASE_DIR, "static", rel_path)).st_mtime_ns, "x")
    except OSError:
        return "0"


app.jinja_env.globals["static_v"] = _static_version

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
# 썸네일 사본 — 전사 시점에 받아 둔다. 영상이 나중에 비공개·멤버십 전용으로 바뀌면
# i.ytimg.com 이미지도 함께 막혀 이력 카드가 빈칸이 되므로, 그때 이 사본으로 폴백한다.
THUMB_DIR   = os.path.join(RES_DIR, "thumbs")
AUDIO_EXT   = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma", ".mp4", ".webm"}

DEFAULT_PROMPT = """\
YouTube 영상의 전사 텍스트를 구조화된 요약본으로 재작성해줘.
---
전사 텍스트:
{transcript}
"""

PROMPT_FILE = os.path.join(BASE_DIR, "prompt.txt")


def _resolve_claude_bin() -> str:
    return llm_gateway.resolve_claude_bin()


CLAUDE_BIN   = _resolve_claude_bin()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "opus")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "900"))

# GPT 옵션은 로컬에 로그인된 Codex CLI를 단일턴 모델 호출기로 사용한다.
GPT_BIN       = llm_gateway.resolve_codex_bin()
GPT_MODEL     = os.environ.get("GPT_MODEL", "gpt-5.6-sol")
GPT_TIMEOUT   = int(os.environ.get("GPT_TIMEOUT", "900"))

# 요약 폴백: Claude 실패(한도/장애/오류) 시 Grok CLI로 재시도.
GROK_BIN      = llm_gateway.resolve_grok_bin()
# 빈 값이면 -m 을 붙이지 않아 grok CLI 기본 모델을 그대로 쓴다(CLI가 올라가면 자동으로 따라감).
GROK_MODEL    = os.environ.get("GROK_MODEL", "")
GROK_FALLBACK = os.environ.get("GROK_FALLBACK", "1") != "0"   # 폴백 활성(기본 ON)
GROK_TIMEOUT  = int(os.environ.get("GROK_TIMEOUT", "900"))

# 불완전 전사 방어 — 라이브 다시보기(was_live)는 프래그먼트가 많아 일부만 받히는 일이 있다.
# 실제 사례: 3h44m 토론회에서 대기 구간만 받혀 전사가 "We'll be right back." 한 줄로 끝났는데도
# 정상 처리로 간주돼 요약·캡처까지 진행됐다. 두 지점에서 거른다.
#  A) 다운로드된 오디오 길이가 원본 duration 대비 이 비율 미만이면 전사 자체를 하지 않는다.
#  B) 전사 본문이 분당 이 글자수 미만이면(사실상 무음/공백) 실패로 내려 요약·캡처를 막는다.
MIN_AUDIO_RATIO   = float(os.environ.get("MIN_AUDIO_RATIO", "0.5"))
MIN_CHARS_PER_MIN = float(os.environ.get("MIN_CHARS_PER_MIN", "40"))
_TS_TAG_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")   # 전사 본문의 시각 마커

# 전사(whisper)는 무거우므로 동시 실행을 제한해 스래싱 방지.
_transcribe_sem = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_TRANSCRIBE", "1")))
_summarize_sem = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_SUMMARIZE", "1")))
# 키프레임(다운로드+ffmpeg+비전)도 무거우므로 동시 실행 제한(자원 경쟁 방지).
_keyframe_sem = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_KEYFRAMES", "1")))

# 키프레임 처리 상태 추적(UI 배지용): 현재 처리 중 영상 + 대기 건수
_kf_lock = threading.Lock()
_kf_jobs: dict = {}   # id -> {"title": str, "state": "queued"|"processing"}
_kf_seq = 0


def _kf_register(title: str) -> int:
    global _kf_seq
    with _kf_lock:
        _kf_seq += 1
        _kf_jobs[_kf_seq] = {"title": title, "state": "queued"}
        return _kf_seq


def _kf_set(kid: int, state: str) -> None:
    with _kf_lock:
        if kid in _kf_jobs:
            _kf_jobs[kid]["state"] = state


def _kf_unregister(kid: int) -> None:
    with _kf_lock:
        _kf_jobs.pop(kid, None)

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
    if len(_duration_cache) > 512:          # 무한 증가 방지 — 가장 오래된 항목부터 제거(FIFO)
        _duration_cache.pop(next(iter(_duration_cache)), None)
    return duration


# ── Markdown I/O ──────────────────────────────────────────────────────

def _save_md(md_path: str, meta: dict, transcript: str) -> None:
    document_io.atomic_write_text(
        md_path,
        document_io.render_markdown(meta, transcript, key_order=_MD_META_ORDER),
    )
    try:
        db.upsert(md_path)
    except Exception:
        pass


def _parse_yaml_front_matter(text: str) -> dict:
    return document_io.parse_frontmatter(text)


def _parse_md(md_path: str) -> tuple[dict, str]:
    return document_io.read_markdown(md_path)


# ── Transcription core ────────────────────────────────────────────────

def _emit_progress(q: queue.Queue, line: str, total: float) -> None:
    m = TIMESTAMP_RE.search(line)
    if m and total > 0:
        current = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                   + int(m.group(3)) + int(m.group(4)) / 1000)
        q.put({"type": "progress", "pct": min(99, int(current / total * 100))})


def _stage(q: queue.Queue, stage: str) -> None:
    q.put({"type": "stage", "stage": stage})


def _seg_start_sec(seg: dict) -> float | None:
    """whisper 세그먼트 시작 시각(초). offsets(ms) 우선, timestamps 문자열 폴백."""
    off = (seg.get("offsets") or {}).get("from")
    if isinstance(off, (int, float)):
        return off / 1000.0
    ts = (seg.get("timestamps") or {}).get("from")  # "00:00:04,000"
    if isinstance(ts, str):
        m = re.match(r"(\d+):(\d+):(\d+)", ts)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return None


def _ts_tag(sec: float) -> str:
    s = int(sec); h, r = divmod(s, 3600); m, s = divmod(r, 60)
    return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


def _parse_transcript(json_path: str) -> str | None:
    """whisper JSON → 본문. 약 15초 간격으로 [mm:ss] 시각 마커를 삽입해
    이후 요약기가 주제별 시작 시각을 라벨링(Tier3 타임스탬프 정렬)할 수 있게 한다."""
    if not os.path.exists(json_path):
        return None
    with open(json_path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    cleaned, prev, last_bucket = [], "", -1
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text or text == prev:
            continue
        prev = text
        start = _seg_start_sec(seg)
        if start is not None:
            bucket = int(start // 15)            # 약 15초 간격으로만 마커 삽입(가독성)
            if bucket != last_bucket:
                last_bucket = bucket
                text = f"{_ts_tag(start)} {text}"
        cleaned.append(text)
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
            if line.strip():
                jobs[job_id]["last_log"] = line.strip()
            q.put(line)
            _emit_progress(q, line, total)
        try:
            proc.wait(timeout=60)          # stdout EOF 후엔 즉시 끝나야 함 — 좀비 방지 안전망
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        jobs[job_id]["proc"] = None
        return proc.returncode
    finally:
        _transcribe_sem.release()


def _set_job_error(job_id: str, stage: str, message: object) -> str:
    """Persist a concise, UI-safe failure reason for the result endpoint."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(message or "알 수 없는 오류"))
    text = re.sub(r"\s+", " ", text).strip()[:800]
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job["status"] = "error"
            job["error_stage"] = stage
            job["error_message"] = text
    return text


_THUMB_SOURCES = ("mqdefault", "hqdefault", "sddefault", "maxresdefault")


def save_thumbnail(yt_id: str) -> str | None:
    """영상 썸네일을 res/thumbs/{yt_id}.jpg 로 내려받는다(이미 있으면 그대로 둔다).

    전사 시점에 한 번만 받아 두고, 이후 조회는 이 사본만 쓴다(외부 재요청 없음).
    나중에 비공개·멤버십 전용으로 바뀌면 원본 URL이 막히므로 그 전에 확보해 둔다.
    실패해도 파이프라인엔 영향이 없다(썸네일 자리가 빌 뿐).
    """
    yt_id = (yt_id or "").strip()
    if not yt_id or not re.fullmatch(r"[\w-]{5,20}", yt_id):
        return None
    dest = os.path.join(THUMB_DIR, f"{yt_id}.jpg")
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return dest
    os.makedirs(THUMB_DIR, exist_ok=True)
    for name in _THUMB_SOURCES:
        try:
            with urllib.request.urlopen(
                f"https://i.ytimg.com/vi/{yt_id}/{name}.jpg", timeout=15) as r:
                data = r.read()
        except Exception:
            continue
        # YouTube는 없는 해상도에 회색 자리표시(120x90, ~1KB)를 준다 — 크기로 거른다
        if len(data) < 2048:
            continue
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)      # 부분 파일이 보이지 않도록 원자적 교체
        log.info("thumbnail saved: %s (%s, %d bytes)", yt_id, name, len(data))
        return dest
    return None


def _finish_transcription(job_id: str, audio_path: str, rc: int, md_path: str,
                          delete_audio: bool = False) -> None:
    q = jobs[job_id]["queue"]
    json_path = audio_path + ".json"
    if rc == 0:
        transcript = _parse_transcript(json_path)
        # 방어 B: 길이 대비 본문이 사실상 비어 있으면(무음·잘린 오디오) 성공 처리하지 않는다.
        # 저장까지 하면 요약 LLM 호출과 캡처가 낭비되고 빈 요약본이 이력에 남는다.
        if transcript is not None:
            secs = float((jobs[job_id].get("meta") or {}).get("duration") or 0)
            mins = secs / 60
            body = _TS_TAG_RE.sub("", transcript).strip()   # [mm:ss] 마커는 본문 길이에서 제외
            if mins >= 1 and len(body) / mins < MIN_CHARS_PER_MIN:
                detail = (f"전사 내용이 비어 있습니다: {len(body)}자 / "
                          f"{_format_duration(secs)} (분당 {len(body) / mins:.0f}자)")
                _set_job_error(job_id, "transcribe", detail)
                log.warning("transcript too sparse: %s — %s", os.path.basename(md_path), detail)
                q.put(f"오류: {detail}")
                q.put(None)
                return
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
            # 썸네일 사본 확보(비공개 전환 대비). 실패해도 전사 결과엔 영향 없음.
            try:
                save_thumbnail((jobs[job_id].get("meta") or {}).get("id") or "")
            except Exception as e:
                log.warning("thumbnail save failed: %s", e)
            log.info("transcription done: %s", os.path.basename(md_path))
            q.put(f"✅ 전사 저장됨: {os.path.basename(md_path)}")
            q.put({"type": "progress", "pct": 100})
            q.put(None)
            return
    if rc == 0:
        detail = "Whisper 결과 파일을 읽을 수 없습니다."
    else:
        tail = jobs[job_id].get("last_log") or "상세 로그 없음"
        detail = f"Whisper 전사 실패 (종료 코드 {rc}): {tail}"
    _set_job_error(job_id, "transcribe", detail)
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
    # 방어 A: 받아온 오디오가 원본 길이보다 크게 짧으면 다운로드가 잘린 것 —
    # 전사·요약·캡처를 태우기 전에 여기서 실패로 끊는다(재시도는 큐 정책이 맡는다).
    if total > 0:
        actual = get_file_duration(audio_path)
        if actual > 0 and actual < total * MIN_AUDIO_RATIO:
            detail = (f"오디오가 잘렸습니다: {_format_duration(actual)} / 원본 "
                      f"{_format_duration(total)} ({actual / total:.0%})")
            _set_job_error(job_id, "download", detail)
            log.warning("audio truncated: %s (%.0f/%.0fs)", os.path.basename(audio_path), actual, total)
            q.put(f"오류: {detail}")
            q.put(None)
            return
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

def _job_guard(fn, job_id: str, *a) -> None:
    """워커 스레드 최상위 가드: 예상치 못한 예외로 스레드가 죽어도
    job status를 error로 내리고 SSE done 센티넬(None)을 넣어 UI가 멈추지 않게 한다."""
    try:
        fn(job_id, *a)
    except Exception as e:
        log.exception("job %s crashed", job_id)
        try:
            _set_job_error(job_id, "worker", f"처리기 내부 오류: {e}")
            j = jobs.get(job_id)
            if j:
                j["queue"].put(f"오류: {e}")
                j["queue"].put(None)
        except Exception:
            pass


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
        log.warning("video info fetch failed: %s — %s", url, e)
        detail = _set_job_error(job_id, "metadata", f"영상 정보 조회 실패: {e}")
        q.put(f"오류: {detail}")
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

    ydl_opts = {
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
    }
    try:
        # 403 재시도: 1차 즉시(다른 실험 버킷 기대) → 2차 75초 뒤(새 세션·새 토큰) →
        # 3차는 대체 클라이언트(tv_simply·web_embedded)로 전환. IP 단위 차단으로
        # 웹 계열이 전부 막혀도 이 둘은 통과하는 것이 실측으로 확인됐다(2026-08-18).
        _dl_retry_wait = {1: 0, 2: 75}
        for dl_attempt in (1, 2, 3):
            try:
                opts = dict(ydl_opts)
                if dl_attempt == 3:
                    opts["extractor_args"] = {
                        "youtube": {"player_client": ["tv_simply", "web_embedded"]}}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                break
            except Exception as e:
                wait = _dl_retry_wait.get(dl_attempt)
                if wait is None or "403" not in str(e) or stop.is_set():
                    raise
                nxt = "대체 클라이언트" if dl_attempt == 2 else "새 세션"
                log.info("download 403 — %s 후 %s으로 재시도(%d/2): %s",
                         f"{wait}초" if wait else "즉시", nxt, dl_attempt, title)
                q.put(f"403 차단 감지 — {f'{wait}초 뒤 ' if wait else ''}{nxt}으로 재시도")
                if wait:
                    for _ in range(wait):
                        if stop.is_set():
                            raise
                        time.sleep(1)
    except Exception as e:
        if stop.is_set():
            jobs[job_id]["status"] = "cancelled"
        else:
            log.warning("download failed: %s — %s", title, e)
            detail = _set_job_error(job_id, "download", f"영상 다운로드 실패: {e}")
            q.put(f"다운로드 오류: {detail}")
        q.put(None)
        return

    if stop.is_set() or not os.path.exists(audio_path):
        if stop.is_set():
            jobs[job_id]["status"] = "cancelled"
        else:
            _set_job_error(job_id, "download", "다운로드 결과 오디오 파일을 찾을 수 없습니다.")
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


# ── SQLite 인덱스 초기화 + 백그라운드 reindex ─────────────────────────
db.init()


def _bg_reindex():
    try:
        s = db.reindex_all()
        log.info("db reindex: %s", s)
    except Exception as e:
        log.error("db reindex error: %s", e)


# ── Remote-access gating ──────────────────────────────────────────────
# 로컬(=동일 머신)에서는 모든 라우트 허용. 외부(Tailscale/LAN)에서는
# 이력조회 관련 라우트만 허용하고, 그 외 경로는 이력 페이지로 리다이렉트한다.
#  - 모바일 UA → /m (모바일 이력)
#  - 데스크톱  → /  (index.html이 원격 이력 모드로 렌더; 전사 UI 숨김, 조회·읽음·삭제만)
_LOOPBACK = {"127.0.0.1", "::1"}
# 원격에서 허용되는 데이터 엔드포인트(이력 조회/요약/읽음/삭제). 페이지(/, /m)는 별도 처리.
_REMOTE_DATA_ALLOWED = {
    "/history", "/history/text", "/history/mark_read", "/history/item", "/summary/content",
    "/history/distill",   # 영상별 증류 포함/제외(원격에서도 조정 가능)
}
_PHONE_UA  = re.compile(r"iPhone|iPod|Windows Phone", re.I)
_TABLET_UA = re.compile(r"iPad|Tablet|PlayBook|Kindle|Silk", re.I)
_MOBI_UA   = re.compile(r"Mobi", re.I)


def _is_phone_ua(ua: str) -> bool:
    """폰만 모바일 UI(/m)로 보낸다. 태블릿은 화면이 넓어 PC UI가 더 맞다.

    Android는 폰·태블릿이 같은 토큰을 쓰므로 'Mobile' 유무로 가른다(구글 권고).
    iPadOS 13+ Safari는 기본이 데스크톱 모드라 UA에 iPad가 없고, 그 경우엔
    자연히 PC UI로 간다.
    """
    ua = ua or ""
    if _PHONE_UA.search(ua):
        return True
    if _TABLET_UA.search(ua):
        return False
    if re.search(r"Android", ua, re.I):
        return bool(re.search(r"Mobile", ua, re.I))
    return bool(_MOBI_UA.search(ua))


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
            or p.startswith("/channels")   # 채널 자동 모니터 조회/토글(모바일 원격 관리)
            or p.startswith("/m/")
            or p.startswith("/static/")
            or p.startswith("/sframe/")
            or p.startswith("/thumb/")):   # 썸네일 사본(비공개 전환 영상 폴백)
        return
    is_mobile = _is_phone_ua(request.headers.get("User-Agent", ""))
    # 이력 페이지: 디바이스에 맞는 쪽으로 정규화
    if p in ("/", "/m"):
        if p == "/" and is_mobile:
            return redirect("/m", 302)
        if p == "/m" and not is_mobile:
            return redirect("/", 302)
        return  # 디바이스에 맞는 페이지 → 통과
    # 그 외 모든 경로 → 403 문구 없이 이력 페이지로 이동
    return redirect("/m" if is_mobile else "/", 302)


@app.after_request
def _optimize_web_response(resp):
    # 렌더된 HTML 페이지(/, /m)는 캐시 금지 → 템플릿 변경이 새로고침만으로 반영(모바일 캐시 방지).
    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    # 정적 URL은 템플릿의 mtime 쿼리로 버전 관리한다. 변경 전까지 재검증 요청을 없앤다.
    elif request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    # Tailscale 구간에서 JSON/HTML/JS/CSS 전송량을 줄인다. 바이너리와 SSE는 제외한다.
    compressible = (
        content_type.startswith("text/")
        or content_type.startswith("application/json")
        or content_type.startswith("application/javascript")
    )
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    if (request.method != "HEAD" and 200 <= resp.status_code < 300
            and accepts_gzip and compressible
            and not resp.headers.get("Content-Encoding")
            and not content_type.startswith("text/event-stream")):
        resp.direct_passthrough = False
        raw = resp.get_data()
        if len(raw) >= 1024:
            resp.set_data(gzip.compress(raw, compresslevel=6))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(resp.get_data()))
            resp.headers.pop("ETag", None)
            resp.headers.pop("Content-MD5", None)
            vary = {v.strip() for v in resp.headers.get("Vary", "").split(",") if v.strip()}
            vary.add("Accept-Encoding")
            resp.headers["Vary"] = ", ".join(sorted(vary))
    return resp


# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # 원격 접속이면 전사 UI를 숨긴 이력 모드로 렌더(조회·읽음·삭제만 가능)
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
            "error_stage":    "",
            "error_message":  "",
            "last_log":       "",
        }

    target = run_file_job if source == "file" else run_job
    args   = (job_id, file_path, data) if source == "file" else (job_id, data)
    threading.Thread(target=_job_guard, args=(target,) + args, daemon=True).start()
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
        "error_stage": job.get("error_stage") or "",
        "error_message": job.get("error_message") or "",
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
    _translate_titles_async()      # 새 항목의 외국어 제목을 뒤이어 번역


# ── 제목 번역 ──────────────────────────────────────────────────────────
# 외국어 제목 영상은 목록·요약 팝업에서 무슨 내용인지 바로 안 읽힌다.
# 제목만 한국어로 옮겨 두고(원문은 그대로 병기), 화면에서 함께 보여준다.

TITLE_TR_MODEL   = os.environ.get("TITLE_TR_MODEL", "claude-sonnet-5")
TITLE_TR_BATCH   = 40            # 한 번 호출에 묶는 제목 수
TITLE_TR_TIMEOUT = 240

_TITLE_TR_PROMPT = """다음 유튜브 영상 제목들을 한국어로 번역한다.

규칙:
- 기업·제품·인물·기술 고유명사는 원문 표기를 그대로 둔다 (NVIDIA, ChatGPT, Sam Altman, S&P 500).
- 직역투를 피하고 한국어 제목으로 자연스럽게 읽히게 한다.
- 원문의 어조(질문형·감탄형 등)와 정보량을 유지한다. 내용을 더하거나 빼지 않는다.
- 이미 한국어인 제목은 그대로 둔다.

출력: 입력 순서와 같은 길이의 JSON 문자열 배열만. 설명·코드펜스 없이 배열만 출력한다.

입력:
"""


def _parse_title_json(out: str, n: int) -> list[str] | None:
    """모델 응답에서 JSON 배열만 건져낸다(코드펜스·군더더기 허용)."""
    s = (out or "").strip()
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    return [str(x).strip() for x in arr]


def _translate_titles(titles: list[str]) -> list[str] | None:
    """제목 묶음을 번역. 실패하면 None(다음 기회에 다시 시도)."""
    if not titles:
        return []
    body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    r = llm_gateway.run_command(
        [_resolve_claude_bin(), "-p", "--model", TITLE_TR_MODEL,
         _TITLE_TR_PROMPT + body],
        timeout=TITLE_TR_TIMEOUT)
    if r.returncode != 0:
        log.warning("title translate failed: rc=%s %s", r.returncode, (r.stderr or "")[:160])
        return None
    return _parse_title_json(r.stdout, len(titles))


def translate_pending_titles(limit: int = TITLE_TR_BATCH) -> dict:
    """번역이 비어 있는 항목을 한 묶음 처리한다. 배치 스크립트와 훅이 함께 쓴다."""
    db.mark_korean_titles()                       # 한국어 제목은 대상에서 확정 제외
    todo = db.titles_needing_translation(limit)
    if not todo:
        return {"done": 0, "remaining": 0}
    out = _translate_titles([t["title"] for t in todo])
    if out is None:
        return {"done": 0, "remaining": len(todo), "error": "translate failed"}
    n = 0
    for item, ko in zip(todo, out):
        if ko and ko != item["title"]:
            n += db.set_title_ko(item["yt_id"], ko)
        else:
            db.set_title_ko(item["yt_id"], "")     # 번역해도 같으면 병기 불필요
    log.info("title translated: %d건", n)
    return {"done": n, "remaining": len(db.titles_needing_translation(limit))}


_title_tr_running = threading.Lock()


def _translate_titles_async() -> None:
    """요약 저장 뒤 새 제목을 조용히 번역한다(중복 실행 방지, 실패해도 무시)."""
    if not _title_tr_running.acquire(blocking=False):
        return

    def run():
        try:
            translate_pending_titles()
        except Exception as e:                      # noqa: BLE001
            log.warning("title translate hook failed: %s", e)
        finally:
            _title_tr_running.release()

    threading.Thread(target=run, daemon=True).start()


# Claude Code CLI(에이전트)가 요약을 '파일로 저장'하려다 권한 거부되면 본문 앞에
# "파일 쓰기 권한이 없어..." 같은 메타문구를 붙이는 경우가 있다. 요약은 출력 형식상
# 항상 '# 제목' H1로 시작하므로, 첫 H1 앞의 군더더기는 저장 전에 잘라낸다(결정적 보장).
def _clean_summary(text: str) -> str:
    if not text:
        return text
    # 일부 CLI 응답은 메타 문구 끝에 줄바꿈 없이 H1을 바로 붙인다.
    # 단일 '#'만 허용해 '### 소제목'을 제목으로 오인하지 않는다.
    m = re.search(r"(?<!#)#[ \t]+\S", text)
    if m and m.start() > 0:
        return text[m.start():].lstrip()
    return text


# 요약 생성용 시스템 프롬프트: 출력 전용 강제(도구/파일/승인 언급 금지).
_SUMMARY_SYS = ("요청된 마크다운 요약 결과 본문만 그대로 출력한다. "
                "파일을 생성·저장하지 말고, 도구를 사용하거나 저장 여부·권한·승인을 "
                "언급하거나 묻지 말 것. 메타 코멘트 없이 결과만 출력한다.")


def _model_label(model_id: str) -> str:
    """모델 ID → 사람이 읽는 라벨. Claude는 접두어 없이 tier만(예: claude-opus-4-8 → Opus 4.8),
    Grok은 브랜드 유지(grok-4.5 → Grok 4.5, grok-composer-2.5-fast → Grok Composer 2.5)."""
    mid = (model_id or "").strip()
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)-(\d+)", mid, re.I)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}"   # 'Opus 4.8'
    low = mid.lower()
    if low.startswith("grok"):
        v = re.search(r"(\d+)\.(\d+)", mid)
        ver = f" {v.group(0)}" if v else ""
        return ("Grok Composer" if "composer" in low else "Grok") + ver
    if low.startswith("gpt"):
        v = re.search(r"(\d+(?:\.\d+)?)", mid)
        return "GPT" + (f" {v.group(1)}" if v else "")
    return mid or "?"


def _model_line(label: str, note: str = "") -> str:
    """요약에 심는 '요약 모델' 마커(HTML 주석). common.js가 추출해 메타 칩(YouTube 보기 옆)으로
    렌더한다. 렌더 안 되는 환경에선 주석이라 화면에 안 보임(graceful)."""
    tail = f" · {note}" if note else ""
    return f"<!--SUMMARY_MODEL:{label}{tail}-->\n\n"


def _compress_line(summary_body: str, transcript_chars: int) -> str:
    """요약/전사 압축률 마커. common.js가 '🗜 원문 대비 N%' 칩으로 렌더."""
    if not transcript_chars or not summary_body:
        return ""
    pct = max(1, round(len(summary_body) / transcript_chars * 100))
    return f"<!--SUMMARY_COMPRESS:{pct}-->\n\n"


def _summarize_with_gpt(prompt: str) -> tuple[str, str]:
    """Codex CLI의 GPT 모델로 단일턴 요약. (요약 텍스트, 오류사유)."""
    try:
        r = llm_gateway.run_codex_prompt(
            _SUMMARY_SYS + "\n\n" + prompt,
            model=GPT_MODEL,
            timeout=GPT_TIMEOUT,
        )
    except Exception as e:
        return "", f"gpt 실행 오류: {e}"
    if r.timed_out:
        return "", f"gpt 타임아웃({GPT_TIMEOUT}s)"
    if r.returncode != 0:
        return "", (r.stderr or r.stdout or f"gpt rc={r.returncode}").strip()[:200]
    out = _clean_summary(r.stdout or "")
    if not out.strip():
        return "", "gpt 빈 응답"
    return out, ""


def _summarize_with_grok(prompt: str) -> tuple[str, str]:
    """Claude 실패 시 폴백: Grok CLI 단일턴 요약. (요약 텍스트, 오류사유).

    긴 전사 프롬프트는 argv 대신 --prompt-file(임시파일)로 전달(ARG_MAX 회피).
    """
    if not (GROK_BIN and os.path.exists(GROK_BIN)):
        return "", "grok 실행파일 없음"
    tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        tf.write(prompt)
        tf.close()
        cmd = [GROK_BIN, "--prompt-file", tf.name]
        if GROK_MODEL:
            cmd += ["-m", GROK_MODEL]
        r = llm_gateway.run_command(cmd, timeout=GROK_TIMEOUT)
    except Exception as e:
        return "", f"grok 실행 오류: {e}"
    finally:
        try:
            os.remove(tf.name)
        except OSError:
            pass
    if r.timed_out:
        return "", f"grok 타임아웃({GROK_TIMEOUT}s)"
    if r.returncode != 0:
        return "", (r.stderr or f"grok rc={r.returncode}").strip()[:200]
    out = _clean_summary(r.stdout or "")
    if not out.strip():
        return "", "grok 빈 응답"
    return out, ""


def _summarize_ordered(prompt: str, save_path: str | None, model_order,
                       *, transcript_chars: int = 0):
    """지정 순서대로 Opus/GPT/Grok을 시도하는 자동모니터용 SSE 생성기."""
    order = llm_gateway.normalize_model_order(model_order)
    failures: list[str] = []
    client_has_output = False

    def reset_client():
        nonlocal client_has_output
        if client_has_output:
            client_has_output = False
            return "event: reset\ndata: \"\"\n\n"
        return ""

    def save_full(body: str, label: str) -> tuple[str, str]:
        cline = _compress_line(body, transcript_chars)
        full = _model_line(label) + cline + body
        if save_path:
            try:
                document_io.atomic_write_text(save_path, full)
                _reindex_summary(save_path)
            except Exception as e:
                return "", "저장 실패: " + str(e)
        return full, ""

    for key in order:
        if key == "opus":
            command = [
                _resolve_claude_bin(), "-p",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--model", CLAUDE_MODEL,
                "--allowedTools", "",
                "--append-system-prompt", _SUMMARY_SYS,
                "--verbose",
            ]
            chunks: list[str] = []
            final: str | None = None
            error_msg: str | None = None
            used_model: str | None = None
            marker_sent = False
            try:
                for process_event in llm_gateway.stream_command(
                    command, input_text=prompt, timeout=CLAUDE_TIMEOUT
                ):
                    if process_event.kind == "timeout":
                        detail = process_event.stderr.strip()[:200]
                        error_msg = f"Claude 타임아웃({CLAUDE_TIMEOUT}s)" + (f": {detail}" if detail else "")
                        continue
                    if process_event.kind == "complete":
                        if process_event.returncode != 0 and not error_msg:
                            error_msg = process_event.stderr.strip()[:200] or f"Claude rc={process_event.returncode}"
                        continue
                    if process_event.kind != "stdout":
                        continue
                    line = process_event.data.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_type = ev.get("type")
                    if event_type == "system" and ev.get("model") and not used_model:
                        used_model = ev.get("model")
                        marker_sent = True
                        client_has_output = True
                        yield f"data: {json.dumps(_model_line(_model_label(used_model)))}\n\n"
                    if event_type == "stream_event":
                        sub = ev.get("event") or {}
                        if sub.get("type") == "content_block_delta":
                            delta = sub.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = delta.get("text") or ""
                                if text:
                                    if not marker_sent:
                                        marker_sent = True
                                        client_has_output = True
                                        yield f"data: {json.dumps(_model_line(_model_label(CLAUDE_MODEL)))}\n\n"
                                    chunks.append(text)
                                    client_has_output = True
                                    yield f"data: {json.dumps(text)}\n\n"
                    elif event_type == "result":
                        if ev.get("is_error"):
                            error_msg = ev.get("result") or "Claude CLI 오류"
                        else:
                            final = ev.get("result") or "".join(chunks)
            except Exception as e:
                error_msg = str(e)

            body = _clean_summary(final if final is not None else "".join(chunks))
            if not error_msg and body:
                cline = _compress_line(body, transcript_chars)
                if cline:
                    yield f"data: {json.dumps(cline)}\n\n"
                _, save_err = save_full(body, _model_label(used_model or CLAUDE_MODEL))
                if save_err:
                    yield f"event: error\ndata: {json.dumps(save_err)}\n\n"
                    return
                yield "event: done\ndata: \n\n"
                return
            failures.append(f"Opus: {error_msg or '빈 응답'}")
            log.warning("summarize opus 실패 → 다음 모델: %s", failures[-1])
            reset = reset_client()
            if reset:
                yield reset
            continue

        if key == "gpt":
            body, err = _summarize_with_gpt(prompt)
            label = _model_label(GPT_MODEL or "gpt")
        else:
            body, err = _summarize_with_grok(prompt)
            # -m 없이 CLI 기본 모델로 돌았으면 실제 모델 id를 조회해 버전까지 남긴다.
            label = _model_label(GROK_MODEL or llm_gateway.resolve_grok_default_model() or "grok")
        if not body:
            failures.append(f"{key.upper()}: {err or '빈 응답'}")
            log.warning("summarize %s 실패 → 다음 모델: %s", key, failures[-1])
            continue
        full, save_err = save_full(body, label)
        if save_err:
            yield f"event: error\ndata: {json.dumps(save_err)}\n\n"
            return
        reset = reset_client()
        if reset:
            yield reset
        yield f"data: {json.dumps(full)}\n\n"
        yield "event: done\ndata: \n\n"
        return

    yield f"event: error\ndata: {json.dumps('모든 요약 모델 실패: ' + ' / '.join(failures))}\n\n"


def _summarize_with_claude(prompt: str, save_path: str | None, *, skip_claude: bool = False,
                           transcript_chars: int = 0):
    """Claude Code CLI로 요약 → SSE 청크 생성, 완료 시 save_path에 저장.
    Claude 실패 시 Grok CLI로 폴백(GROK_FALLBACK).
    skip_claude=True 이면 Claude 호출 없이 Grok만 시도(게이트가 Claude 불가로 판정한 경우).

    프롬프트는 argv가 아니라 stdin으로 전달한다(긴 전사로 인한 ARG_MAX 초과 회피).
    """
    def _yield_grok(err_prefix: str = ""):
        if not GROK_FALLBACK:
            yield f"event: error\ndata: {json.dumps((err_prefix or 'Claude 스킵') + ' / Grok 폴백 비활성')}\n\n"
            return
        log.warning("summarize → Grok CLI%s", " (skip_claude)" if skip_claude else " 폴백 시도")
        gtext, gerr = _summarize_with_grok(prompt)
        if gtext:
            # Claude 부분 출력이 이미 전송됐을 수 있으므로 클라이언트가 본문을 비우게 한다.
            yield "event: reset\ndata: \"\"\n\n"
            log.info("summarize grok 성공 (%d자)", len(gtext))
            grok_label = _model_label(GROK_MODEL or llm_gateway.resolve_grok_default_model() or "grok")
            full = (_model_line(grok_label)
                    + _compress_line(gtext, transcript_chars) + gtext)
            yield f"data: {json.dumps(full)}\n\n"
            if save_path:
                try:
                    document_io.atomic_write_text(save_path, full)
                except Exception as e:
                    yield f"event: error\ndata: {json.dumps('저장 실패: ' + str(e))}\n\n"
                    return
                _reindex_summary(save_path)
            yield "event: done\ndata: \n\n"
            return
        log.warning("summarize grok 실패: %s", gerr)
        msg = (err_prefix + " / " if err_prefix else "") + f"Grok 폴백 실패: {gerr}"
        yield f"event: error\ndata: {json.dumps(msg)}\n\n"

    if skip_claude:
        yield from _yield_grok("Claude 스킵(게이트 판정)")
        return

    command = [
        _resolve_claude_bin(), "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--model", CLAUDE_MODEL,
        "--allowedTools", "",
        "--append-system-prompt", _SUMMARY_SYS,
        "--verbose",
    ]
    chunks: list[str] = []
    final: str | None = None
    error_msg: str | None = None
    used_model: str | None = None
    try:
        for process_event in llm_gateway.stream_command(
            command, input_text=prompt, timeout=CLAUDE_TIMEOUT
        ):
            if process_event.kind == "timeout":
                detail = process_event.stderr.strip()[:200]
                error_msg = f"Claude 타임아웃({CLAUDE_TIMEOUT}s)" + (f": {detail}" if detail else "")
                continue
            if process_event.kind == "complete":
                if process_event.returncode != 0 and not error_msg:
                    error_msg = process_event.stderr.strip()[:200] or f"Claude rc={process_event.returncode}"
                continue
            if process_event.kind != "stdout":
                continue
            line = process_event.data.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = ev.get("type")
            if event_type == "system" and ev.get("model") and not used_model:
                used_model = ev.get("model")
                yield f"data: {json.dumps(_model_line(_model_label(used_model)))}\n\n"
            if event_type == "stream_event":
                sub = ev.get("event") or {}
                if sub.get("type") == "content_block_delta":
                    delta = sub.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            chunks.append(text)
                            yield f"data: {json.dumps(text)}\n\n"
            elif event_type == "result":
                if ev.get("is_error"):
                    error_msg = ev.get("result") or "Claude CLI 오류"
                else:
                    final = ev.get("result") or "".join(chunks)
    except Exception as e:
        error_msg = str(e)

    if error_msg:
        log.warning("summarize failed (claude): %s", error_msg)
        yield from _yield_grok(error_msg)
        return

    body = _clean_summary(final if final is not None else "".join(chunks))
    if not body:
        yield from _yield_grok("Claude 빈 응답")
        return
    cline = _compress_line(body, transcript_chars)
    if cline:
        yield f"data: {json.dumps(cline)}\n\n"
    full = _model_line(_model_label(used_model or CLAUDE_MODEL)) + cline + body
    if save_path:
        try:
            document_io.atomic_write_text(save_path, full)
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

    # 메타+본문 전체를 주입하되, 반복 오프닝이 확인된 Sequoia 영상은
    # 저장 원문을 보존한 채 본 인터뷰 시작 전 내용만 요약 입력에서 제외한다.
    with open(abs_path, encoding="utf-8", errors="replace") as f:
        transcript_blob = f.read()
    if not transcript_blob.strip():
        return _json({"error": "전사 결과가 없습니다."}, 400)
    transcript_meta, transcript_body = document_io.parse_markdown_text(transcript_blob)
    transcript_body, opening_cutoff = keyframe_report.trim_sequoia_opening(
        transcript_meta, transcript_body
    )
    if opening_cutoff is not None:
        transcript_blob = document_io.render_markdown(
            transcript_meta, transcript_body, key_order=_MD_META_ORDER
        )
        log.info(
            "Sequoia opening omitted from summary input: %s (before %s)",
            os.path.basename(abs_path), keyframe_report._hms(opening_cutoff),
        )

    prompt = (data.get("prompt") or DEFAULT_PROMPT).replace("{transcript}", transcript_blob)
    save_path = _summary_path_for_md(abs_path)
    skip_claude = bool(
        data.get("skip_claude")
        or str(data.get("mode") or "").lower() == "grok"
    )
    requested_order = data.get("models") or data.get("model_order")
    model_order = (llm_gateway.normalize_model_order(requested_order)
                   if requested_order is not None else None)
    log.info(
        "summarize start: %s%s",
        os.path.basename(abs_path),
        " (skip_claude→grok)" if skip_claude else "",
    )

    def guarded_summary():
        with _summarize_sem:
            if model_order is not None:
                yield from _summarize_ordered(
                    prompt, save_path, model_order,
                    transcript_chars=len(transcript_blob),
                )
            else:
                yield from _summarize_with_claude(
                    prompt, save_path, skip_claude=skip_claude,
                    transcript_chars=len(transcript_blob),
                )

    return Response(guarded_summary(), mimetype="text/event-stream",
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
    document_io.atomic_write_text(PROMPT_FILE, prompt)
    return _json({"ok": True})


# ── 채널 자동 모니터링(로컬 전용 — 원격은 before_request에서 차단) ──────
@app.route("/channels")
def channels_list():
    """모니터링 채널 목록 + 큐 요약(모달용)."""
    try:
        return _json({"channels": db.list_channels(), "queue": db.queue_counts(),
                      "model_orders": db.get_monitor_model_orders()})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/channels/model-orders", methods=["PATCH"])
def channel_model_orders():
    """자동모니터 요약/캡처의 Opus·GPT·Grok 폴백 순서를 저장한다."""
    data = request.get_json(force=True) or {}
    updates = {}
    expected = set(llm_gateway.MODEL_KEYS)
    for key in ("summary", "capture"):
        if key not in data:
            continue
        value = data[key]
        if (not isinstance(value, list) or len(value) != len(expected)
                or set(str(v).lower() for v in value) != expected):
            return _json({"error": f"{key} 순서는 opus, gpt, grok을 한 번씩 포함해야 합니다."}, 400)
        updates[key] = [str(v).lower() for v in value]
    if not updates:
        return _json({"error": "변경할 순서가 없습니다(summary/capture)."}, 400)
    try:
        saved = db.set_monitor_model_orders(**updates)
        return _json({"ok": True, "model_orders": saved})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/channels/<int:cid>", methods=["PATCH"])
def channel_toggle(cid: int):
    """채널 설정 변경 — 자동수집 ON/OFF(enabled), 지식증류 대상(distill).

    보낸 키만 반영한다(둘 중 하나만 바꿔도 다른 값이 덮이지 않게).
    """
    data = request.get_json(force=True) or {}
    out, touched = {"ok": True}, False
    if "enabled" in data:
        if not db.set_channel_enabled(cid, bool(data["enabled"])):
            return _json({"error": "채널을 찾을 수 없습니다."}, 404)
        out["enabled"] = bool(data["enabled"]); touched = True
    if "distill" in data:
        if not db.set_channel_distill(cid, bool(data["distill"])):
            return _json({"error": "채널을 찾을 수 없습니다."}, 404)
        out["distill"] = bool(data["distill"]); touched = True
    if not touched:
        return _json({"error": "변경할 항목이 없습니다(enabled/distill)."}, 400)
    return _json(out)


@app.route("/channels/distill-excluded")
def channels_distill_excluded():
    """증류 제외 채널명 목록 — hermes 증류 파이프라인이 조회해 문서 로딩 단계에서 거른다."""
    try:
        return _json({"names": db.distill_excluded_names()})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/distill/policy")
def distill_policy():
    """증류 정책 전체 — 채널 제외 목록 + 영상 단위 오버라이드(오버라이드가 우선)."""
    try:
        return _json({"excluded_channels": db.distill_excluded_names(),
                      "overrides": db.distill_overrides()})
    except Exception as e:
        return _json({"error": str(e)}, 500)


def _history_item_from_payload(data: dict):
    """웹용 item_id를 DB 항목으로 해석한다. 레거시 경로 요청이면 (None, None)."""
    if "item_id" not in data:
        return None, None
    item = db.get_history_item(data.get("item_id"))
    if not item:
        return None, _json({"error": "항목을 찾을 수 없습니다."}, 404)
    return item, None


@app.route("/history/distill", methods=["PATCH"])
def history_distill():
    """영상 단위 증류 설정 — distill: true(포함) / false(제외) / null(미설정=채널 따름)."""
    data = request.get_json(force=True) or {}
    item, err = _history_item_from_payload(data)
    if err:
        return err
    path = ((item.get("summary_path") or item["md_path"]) if item
            else (data.get("path") or "").strip())
    if not path:
        return _json({"error": "item_id 또는 path 필요"}, 400)
    raw = data.get("distill", "__missing__")
    if raw == "__missing__":
        return _json({"error": "distill 필요(true/false/null)"}, 400)
    value = None if raw is None else bool(raw)
    if not db.set_item_distill(path, value):
        return _json({"error": "항목을 찾을 수 없습니다."}, 404)
    return _json({"ok": True, **(db.get_item_distill(path) or {})})


@app.route("/history")
def get_history():
    q = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread_only") == "1"
    try:
        revision = db.history_revision()
        # revision 단축 응답은 전체 목록에만 적용한다. 검색/필터 조건이 달라지면
        # 같은 DB revision이어도 결과 집합은 다르므로 실제 쿼리를 수행해야 한다.
        if not q and not unread_only and request.args.get("revision") == revision:
            return _json({"revision": revision, "unchanged": True})
        items = (db.search(q, unread_only=unread_only)
                 if q else db.list_history_items(unread_only=unread_only))
        revision = db.history_revision()
    except Exception:
        items = []
        revision = ""
    return _json({"revision": revision, "items": items})


@app.route("/history/check")
def history_check():
    """영상 중복 확인: 같은 yt_id를 이미 처리한 이력이 있으면 날짜·제목 반환(추가 전 경고용)."""
    item = db.find_by_yt_id(request.args.get("yt_id") or "")
    if not item:
        return _json({"exists": False})
    return _json({"exists": True, "date": item["date"], "title": item["title"]})


@app.route("/history/mark_read", methods=["PATCH"])
def mark_read():
    data = request.get_json(force=True) or {}
    is_read  = bool(data.get("is_read", True))
    item, err = _history_item_from_payload(data)
    if err:
        return err
    if item:
        ok = db.mark_read_by_id(item["item_id"], is_read)
    else:
        txt_path = (data.get("txt_path") or "").strip()
        abs_path, err = _check_res_path(txt_path)
        if err:
            return err
        ok = db.mark_read(abs_path, is_read)
    if not ok:
        return _json({"error": "항목을 찾을 수 없습니다."}, 404)
    return _json({"ok": True, "is_read": is_read, "revision": db.history_revision()})


@app.route("/history/item", methods=["DELETE"])
def history_delete():
    data = request.get_json(force=True) or {}
    item, err = _history_item_from_payload(data)
    if err:
        return err
    if item:
        abs_path, err = _check_res_path(item["md_path"])
        if err:
            return err
        summary_path = item.get("summary_path")
    else:
        txt_path = (data.get("txt_path") or "").strip()
        abs_path, err = _check_res_path(txt_path)
        if err:
            return err
        # 레거시 경로 호출은 기존 파일 배치 규칙으로 요약 경로를 유추한다.
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
    # 키프레임 디렉토리({stem}.frames/)도 함께 정리(누수 방지)
    if summary_path:
        frames_dir = os.path.splitext(summary_path)[0] + ".frames"
        if os.path.isdir(frames_dir):
            try:
                shutil.rmtree(frames_dir)
                deleted.append(frames_dir)
            except OSError as e:
                errors.append(str(e))
    return _json({"ok": True, "deleted": deleted, "errors": errors,
                  "revision": db.history_revision()})


@app.route("/history/text", methods=["POST"])
def history_text():
    data = request.get_json(force=True) or {}
    item, err = _history_item_from_payload(data)
    if err:
        return err
    raw_path = item["md_path"] if item else (data.get("txt_path") or "")
    abs_path, err = _check_res_path(raw_path)
    if err:
        return err
    if not os.path.exists(abs_path):
        return _json({"error": "파일 없음"}, 404)
    try:
        _, transcript = _parse_md(abs_path)
        return _json({"text": transcript, "translation": _translation_payload(abs_path)})
    except Exception as e:
        return _json({"error": str(e)}, 500)


def _translation_payload(md_path: str) -> dict | None:
    """전사 전문 번역 상태 + 본문. 별도 작업(transcript_translator.py)의 결과물.

    아직 진행 중이어도 지금까지 번역된 부분은 보여준다(청크마다 저장되므로).
    """
    try:
        st = db.get_translation_for_path(md_path)
    except Exception:
        return None
    if not st:
        return None
    out = {"status": st.get("status"), "done": st.get("chunks_done") or 0,
           "total": st.get("chunks_total") or 0, "text": ""}
    p = st.get("out_path") or ""
    if p and os.path.isfile(p):
        try:
            raw = open(p, encoding="utf-8").read()
            # 머리부(프론트매터)와 마커 주석을 모두 걷어내고 본문만 넘긴다
            seps = [i for i, l in enumerate(raw.splitlines()) if l.strip() == "---"]
            if len(seps) >= 2:
                raw = "\n".join(raw.splitlines()[seps[1] + 1:])
            body = re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()
            # 제목은 모달 헤더에 이미 있으므로 본문 H1은 뺀다
            out["text"] = re.sub(r"\A#\s+.*?\n+", "", body, count=1).strip()
        except Exception:
            pass
    return out


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


@app.route("/summary/content", methods=["POST"])
def summary_content():
    data = request.get_json(force=True) or {}
    item, err = _history_item_from_payload(data)
    if err:
        return err
    path = (item.get("summary_path") if item else (data.get("path") or "").strip())
    if not path:
        return _json({"error": "요약 파일 없음"}, 404)
    abs_path = os.path.realpath(path)
    summary_real = os.path.realpath(SUMMARY_DIR)
    if not (abs_path.startswith(summary_real + os.sep) or abs_path == summary_real):
        return _json({"error": "접근 거부"}, 403)
    if not os.path.isfile(abs_path):
        return _json({"error": "파일 없음"}, 404)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        # 증류 설정을 함께 실어 뷰어가 별도 요청 없이 현재 상태를 표시한다.
        return _json({"content": content, "distill": db.get_item_distill(abs_path),
                      "title_ko": db.get_title_ko(abs_path)})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/thumb/<yt_id>")
def serve_thumbnail(yt_id: str):
    """썸네일 사본 서빙 — 목록의 유일한 소스.

    전사 시점에 받아 둔 사본만 쓴다. 브라우저가 i.ytimg.com 으로 나가지 않으므로
    VPN 경유 시 카드 20장분 외부 왕복이 사라진다. 사본 내용은 바뀌지 않으니
    캐시도 길게 준다.
    """
    if not re.fullmatch(r"[\w-]{5,20}", yt_id or ""):
        return _json({"error": "잘못된 id"}, 400)
    p = os.path.join(THUMB_DIR, f"{yt_id}.jpg")
    if not os.path.isfile(p):
        return _json({"error": "없음"}, 404)
    return send_file(p, mimetype="image/jpeg", max_age=31536000)


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

    kid = _kf_register(_parse_summary_title(summary_path))   # 상태 추적(대기→처리)
    try:
        with _keyframe_sem:   # 동시 키프레임 처리 직렬화(자원 경쟁 방지)
            _kf_set(kid, "processing")
            skip_claude = bool(
                data.get("skip_claude")
                or data.get("skip_claude_vision")
                or str(data.get("mode") or "").lower() == "grok"
            )
            requested_order = data.get("models") or data.get("model_order")
            model_order = (llm_gateway.normalize_model_order(requested_order)
                           if requested_order is not None else None)
            res = keyframe_report.generate_keyframes(
                summary_path, (data.get("url") or "").strip(), frames_dir, url_base,
                skip_claude=skip_claude,
                model_order=model_order,
            )
    except Exception as e:
        log.warning("keyframes error: %s", e)
        return _json({"ok": False, "reason": "error", "error": str(e)})
    finally:
        _kf_unregister(kid)

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


@app.route("/keyframes/status")
def keyframes_status():
    """백그라운드 키프레임 처리 상태(UI 배지용): 현재 처리 중 영상 + 대기 건수."""
    with _kf_lock:
        jobs_ = list(_kf_jobs.values())
    cur = next((j["title"] for j in jobs_ if j["state"] == "processing"), None)
    queued = sum(1 for j in jobs_ if j["state"] == "queued")
    return _json({"processing": cur, "queued": queued, "active": len(jobs_)})


if __name__ == "__main__":
    # 백그라운드 유지보수 스레드는 서버 실행 시에만 시작(import 부작용 제거 → 테스트에서 안전)
    threading.Thread(target=_cleanup_old_jobs, daemon=True).start()
    threading.Thread(target=_bg_reindex, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    log.info("starting server on %s:%s — claude=%s, model=%s",
             host, port, CLAUDE_BIN, CLAUDE_MODEL)
    app.run(debug=False, host=host, port=port, threaded=True)
