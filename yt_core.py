"""공용 설정(경로·실행 파일·모델 환경변수)·작업 상태(jobs)·동시 실행 세마포어·md 저장/파싱 같은 기본 도구.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import os

# .env는 다른 모듈(db·llm_gateway)이 환경변수를 읽기 전에 불러온다(예전 app.py 순서)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import queue
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
from datetime import datetime
import db
import document_io
import llm_gateway
from flask import Response

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
# 기본 모델은 large-v3 원본(비 turbo·비양자화). 2026-08-27 실측(6편·61분)에서
# turbo 계열은 어려운 오디오(다큐·다화자)에서 같은 문장을 수십 회 반복하는 환각으로
# 구간을 통째로 날렸다 — 손실 세그먼트 q5_0 48 / turbo 213 / large-v3 16.
# 양자화(q5_0)는 Metal에서 속도 이득이 없어(실측 동률) 정확도만 손해였다.
# large-v3는 2.5배 느리지만 실시간 8.7배라 큐 주기(20분/건)에 여유가 크다.
MODEL_PATH  = os.environ.get("MODEL_PATH",  os.path.join(WHISPER_DIR, "ggml-large-v3.bin"))

FFPROBE_EXE     = _resolve_binary("ffprobe")
FFMPEG_LOCATION = os.path.dirname(_resolve_binary("ffmpeg"))

AUDIO_DIR   = os.environ.get("AUDIO_DIR", BASE_DIR)
RES_DIR     = os.path.join(BASE_DIR, "res")
SUMMARY_DIR = os.path.join(RES_DIR, "summary")
# 썸네일 사본 — 전사 시점에 받아 둔다. 영상이 나중에 비공개·멤버십 전용으로 바뀌면
# i.ytimg.com 이미지도 함께 막혀 이력 카드가 빈칸이 되므로, 그때 이 사본으로 폴백한다.
THUMB_DIR   = os.path.join(RES_DIR, "thumbs")
AUDIO_EXT   = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma", ".mp4", ".webm"}

PROMPT_FILE         = os.path.join(BASE_DIR, "prompt.txt")
PROMPT_DEFAULT_FILE = os.path.join(BASE_DIR, "prompt_default.txt")   # '기본값으로 되돌리기'의 원본(repo 관리)

_FALLBACK_PROMPT = """\
YouTube 영상의 전사 텍스트를 구조화된 요약본으로 재작성해줘.
---
전사 텍스트:
{transcript}
"""


def _default_prompt() -> str:
    try:
        with open(PROMPT_DEFAULT_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _FALLBACK_PROMPT


DEFAULT_PROMPT = _default_prompt()


def _resolve_claude_bin() -> str:
    return llm_gateway.resolve_claude_bin()


CLAUDE_BIN   = _resolve_claude_bin()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "opus")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "900"))

# GPT 옵션은 로컬에 로그인된 Codex CLI를 단일턴 모델 호출기로 사용한다.
GPT_BIN       = llm_gateway.resolve_codex_bin()
GPT_MODEL     = os.environ.get("GPT_MODEL", "gpt-6-astra")
GPT_TIMEOUT   = int(os.environ.get("GPT_TIMEOUT", "900"))

# 요약 폴백: Claude 실패(한도/장애/오류) 시 Grok CLI로 재시도.
GROK_BIN      = llm_gateway.resolve_grok_bin()
# 빈 값이면 -m 을 붙이지 않아 grok CLI 기본 모델을 그대로 쓴다(CLI가 올라가면 자동으로 따라감).
GROK_MODEL    = os.environ.get("GROK_MODEL", "")
GROK_FALLBACK = os.environ.get("GROK_FALLBACK", "1") != "0"   # 폴백 활성(기본 ON)
GROK_TIMEOUT  = int(os.environ.get("GROK_TIMEOUT", "900"))
# Gemini(API 키) 요약 호출 제한(2026-11-16). 무료 티어 Flash 계열은 한 번에 1~2분 걸리기도 한다.
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "900"))

# 불완전 전사 방어 — 라이브 다시보기(was_live)는 프래그먼트가 많아 일부만 받히는 일이 있다.
# 실제 사례: 3h44m 토론회에서 대기 구간만 받혀 전사가 "We'll be right back." 한 줄로 끝났는데도
# 정상 처리로 간주돼 요약·캡처까지 진행됐다. 두 지점에서 거른다.
#  A) 다운로드된 오디오 길이가 원본 duration 대비 이 비율 미만이면 전사 자체를 하지 않는다.
#  B) 전사 본문이 분당 이 글자수 미만이면(사실상 무음/공백) 실패로 내려 요약·캡처를 막는다.
MIN_AUDIO_RATIO   = float(os.environ.get("MIN_AUDIO_RATIO", "0.5"))
MIN_CHARS_PER_MIN = float(os.environ.get("MIN_CHARS_PER_MIN", "40"))
MIN_SHORT_CHARS   = int(os.environ.get("MIN_SHORT_CHARS", "10"))   # 1분 미만 영상: 실제 말이 이보다 짧으면 무음
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
# 전사가 끝난 오디오는 바로 지우지 않고 보관함에 RETENTION_DAYS일 둔다(2026-10-31 사용자 지시 — 전사 재검토·
# whisper 설정 A/B를 다시 받지 않고 하려고. 128kbps mp3 시간당 약 55MB, 월 150~180시간 → 늘 25~30GB).
# 보관함은 res/audio_archive/YYYYMMDD/ — /files 목록은 res 아래 한 단계만 훑으므로 보관 파일이 섞이지 않는다.
# AUDIO_RETENTION_DAYS=0이면 예전처럼 바로 지운다. 실패한 잡의 오디오는 재시도가 새로 받으므로 계속 지운다.
AUDIO_ARCHIVE_DIR    = os.environ.get("AUDIO_ARCHIVE_DIR", os.path.join(RES_DIR, "audio_archive"))
AUDIO_RETENTION_DAYS = int(os.environ.get("AUDIO_RETENTION_DAYS", "90"))

# view_count, like_count 제외
_META_KEYS = [
    "id", "title", "uploader", "channel", "channel_url",
    "duration", "upload_date", "webpage_url",
    "categories", "tags",
]

# md 프론트매터 출력 순서
_MD_META_ORDER = [
    "title", "uploader", "channel", "channel_url",
    "duration", "clip_start", "upload_date", "webpage_url", "id",
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


def _history_item_from_payload(data: dict):
    """웹용 item_id를 DB 항목으로 해석한다. 레거시 경로 요청이면 (None, None)."""
    if "item_id" not in data:
        return None, None
    item = db.get_history_item(data.get("item_id"))
    if not item:
        return None, _json({"error": "항목을 찾을 수 없습니다."}, 404)
    return item, None
