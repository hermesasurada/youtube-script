#!/usr/bin/env python3
"""유튜브 채널 자동 모니터 — 신규 영상 감지 → 전사/요약/캡처 자동화.

감지(폴러)와 처리(드레이너)를 한 프로세스에서 수행하되, 파일 락으로 단일 인스턴스를
보장한다. 20분 간격 launchd/cron로 실행. 파이프라인은 상시 구동 중인 Flask 서버
(기본 127.0.0.1:5001)의 HTTP 엔드포인트를 그대로 호출해 재사용한다(브라우저와 동일 경로).

흐름:
  1) 활성 채널마다 RSS(feeds/videos.xml)로 최근 업로드 목록 수집
  2) 최초 폴이면 현재 피드를 baseline(seen)로 기록만 하고 처리 안 함(백필 방지)
  3) 이후엔 이력(items)·큐에 없는 신규만 메타 확인 → 필터(≤3분·라이브·Shorts 제외) → 큐 적재
  4) 큐 drain 게이트: Claude OK 또는 Grok 폴백 준비 시 진행
     → /start→/result→/summarize(Claude→Grok)→/keyframes(Claude 비전)
  5) 건별 완료/실패 알림(Telegram, 자격증명 있을 때)

사용:
  python channel_monitor.py            # 폴 + 드레인(기본)
  python channel_monitor.py --dry-run  # 감지/필터만 출력(DB·처리 변경 없음)
  python channel_monitor.py --poll-only # 감지·적재만(드레인 안 함)
  python channel_monitor.py --drain-only # 큐 처리만(감지 안 함)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import fcntl
import unicodedata
import xml.etree.ElementTree as ET

import requests

import db
import llm_gateway

try:
    import yt_dlp
except Exception:
    yt_dlp = None

# ── 설정 ───────────────────────────────────────────────────────────────
BASE       = os.environ.get("YTS_BASE", "http://127.0.0.1:5001")
MIN_DUR    = int(os.environ.get("MONITOR_MIN_DURATION", "180"))   # 3분 이하 제외(채널별 오버라이드 가능)
POLL_SEC   = int(os.environ.get("MONITOR_POLL_SEC", "6"))         # /result 폴링 간격
MAX_JOB_SEC = int(os.environ.get("MONITOR_MAX_JOB_SEC", "18000"))  # 건당 전사 상한(5h) — 4시간대 장편(Acquired 등) 수용
SUMM_TIMEOUT = int(os.environ.get("MONITOR_SUMM_TIMEOUT", "900"))
KF_TIMEOUT   = int(os.environ.get("MONITOR_KF_TIMEOUT", "1800"))
STALE_PROCESSING_SEC = int(os.environ.get("MONITOR_STALE_PROCESSING_SEC", "10800"))
# 403 차단은 저녁 시간대(15~23시)에 몰리는데, 3회 예산은 백오프(30분→1h→2h)가
# 전부 한 차단 구간 안에 떨어져 소진되는 일이 있었다. 5회면 마지막 시도가
# 첫 실패로부터 ~13시간 뒤라 다른 시간대에 걸린다.
MAX_PIPELINE_ATTEMPTS = int(os.environ.get("MONITOR_MAX_PIPELINE_ATTEMPTS", "5"))
# 파이프라인 실패 재시도 간격 — 30분 고정(사용자 지정).
# 예산 5회 × 30분 = 첫 실패로부터 2시간 안에 전부 소진되므로, 저녁 차단
# 구간(15~23시)을 통째로 건너뛰지는 못한다. 대신 회복이 빠른 일시 장애를
# 빨리 따라잡고, 구간을 넘겨야 하는 건은 실패 후 수동 재처리한다.
RETRY_BASE_SEC = int(os.environ.get("MONITOR_RETRY_BASE_SEC", "1800"))


def _retry_delay(attempt: int) -> int:
    """다음 재시도까지 대기 초 — 시도 횟수와 무관하게 일정하다."""
    return RETRY_BASE_SEC
LOCK_PATH  = os.environ.get("MONITOR_LOCK", os.path.expanduser("~/.hermes/youtube-monitor.lock"))
OUTBOX     = os.environ.get("MONITOR_OUTBOX", os.path.expanduser("~/.hermes/youtube-monitor.outbox"))
ENV_PATH   = os.path.expanduser("~/.hermes/youtube-monitor.env")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"


def _load_env(path: str) -> None:
    """~/.hermes/youtube-monitor.env(KEY=VALUE)를 os.environ에 로드(기존값 우선)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env(ENV_PATH)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg: str) -> None:
    print(msg, flush=True)


# ── 요약 경로 가용성 가드 ───────────────────────────────────────────────
# 서버(app.py) 요약: Claude primary → Grok 폴백(GROK_FALLBACK).
# 예전에는 Claude 헬스만 보고 큐 전체를 막아 Grok 폴백이 의미 없었음.
# 게이트는 "요약 가능한 경로가 하나라도 있으면 진행"으로 본다.
class ClaudeUnavailable(Exception):
    """사용량 한도/인증/장애 등 LLM 요약 경로 전부 실패 — 큐 정지 후 다음 주기 재시도 대상."""


# 사용량 한도·인증·서버장애 등 '재시도하면 나아질' systemic 신호 패턴
_SYSTEMIC = re.compile(
    # 주의: 403/500/503은 yt-dlp/ffmpeg의 HTTP 오류(봇차단 등)와 겹쳐 오판하므로 제외.
    r"usage limit|session limit|rate.?limit|too many requests|\b429\b|\b401\b|\b529\b"
    r"|authenticat|unauthor|credit balance|quota|exceeded|overloaded|api error"
    r"|사용 한도|세션 한도|한도 초과|인증",
    re.I,
)

# app.py 와 동일 기본값 (env로 맞춤)
GROK_FALLBACK = os.environ.get("GROK_FALLBACK", "1") != "0"


def _is_systemic(msg: str) -> bool:
    return bool(_SYSTEMIC.search(msg or ""))


def _claude_bin() -> str:
    return llm_gateway.resolve_claude_bin()


def _grok_bin() -> str:
    return llm_gateway.resolve_grok_bin()


def _gpt_bin() -> str:
    return llm_gateway.resolve_codex_bin()


def claude_healthy() -> tuple[bool, str]:
    """저가 프로브로 Claude CLI 가용성 확인. (정상?, 사유)."""
    try:
        r = llm_gateway.run_command(
            [_claude_bin(), "-p", "--model", "haiku", "reply with: ok"],
            timeout=90,
        )
    except Exception as e:
        return False, f"claude 실행 오류: {e}"
    if r.timed_out:
        return False, "헬스체크 타임아웃(90s)"
    out = r.stdout.strip()
    err = r.stderr.strip()
    # stdin 경고 등은 노이즈
    err = re.sub(r"Warning: no stdin data received.*?(?:\n|$)", "", err, flags=re.I).strip()
    blob = (out + " " + err).strip()
    if r.returncode != 0 or not out or _is_systemic(blob):
        return False, (blob or f"rc={r.returncode}")[:200]
    return True, ""


def grok_fallback_ready() -> tuple[bool, str]:
    """app.py 요약 Grok 폴백을 시도할 수 있는지(바이너리 존재 + 플래그). 실제 호출은 안 함."""
    if not GROK_FALLBACK:
        return False, "GROK_FALLBACK=0"
    b = _grok_bin()
    if not os.path.exists(b):
        return False, f"grok 없음 ({b})"
    return True, b


def summarizer_gate(model_order=None) -> tuple[bool, str, str]:
    """큐 drain 진입 게이트.

    Returns:
        (proceed, mode, detail)
        mode: "opus" | "gpt" | "grok" | "none"
    저장된 순서에서 실행 가능한 첫 모델을 고른다. 실제 요청에서는 그 모델부터
    남은 순서를 서버에 전달해 인증·한도 오류까지 순차 폴백한다.
    """
    explicit = model_order is not None
    default = llm_gateway.MODEL_KEYS if explicit else ("opus", "grok")
    order = llm_gateway.normalize_model_order(model_order or default, default=default)
    failures = []
    for key in order:
        if key == "opus":
            ok, detail = claude_healthy()
        elif key == "gpt":
            path = _gpt_bin()
            ok, detail = os.path.exists(path), path
        else:
            path = _grok_bin()
            ok = os.path.exists(path) and (explicit or GROK_FALLBACK)
            detail = path if ok else ("GROK_FALLBACK=0" if not explicit else f"grok 없음 ({path})")
        if ok:
            return True, key, ("; ".join(failures) if failures else "")
        failures.append(f"{key}: {detail}")
    return False, "none", "; ".join(failures)


def _notify_head(v: dict, title: str) -> str:
    """알림 상단 블록: 채널명이 있으면 '📺 채널명\\n제목', 없으면 제목만."""
    chan = db.channel_name_by_cid(v.get("channel_id"))
    return f"📺 {chan}\n{title}" if chan else title


def notify(text: str) -> None:
    """알림 적재 — launchd 로그(stdout) + 아웃박스(hermes 크론 shim이 --announce로 stdout 배달).

    무거운 폴/드레인은 launchd가, 알림 배달은 hermes 크론이 담당(중앙 텔레그램, 토큰 불필요).
    자격증명이 직접 설정돼 있으면(youtube-monitor.env) 텔레그램 직발송도 병행(옵션).
    """
    log(text)
    try:
        os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
        with open(OUTBOX, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(text.rstrip() + "\n\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        log(f"[notify] 아웃박스 기록 실패: {e}")
    if TG_TOKEN and TG_CHAT:   # 선택: 직접 발송(기본 경로는 hermes 크론)
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:
            log(f"[notify] telegram 실패: {e}")


def announce() -> int:
    """아웃박스에 쌓인 알림을 stdout으로 출력(→ hermes 크론이 텔레그램 배달) 후 비운다.

    hermes 크론 shim에서 `--announce`로 호출. 새 알림이 없으면 무음(아무것도 출력 안 함).
    launchd 드레이너의 append와 flock으로 경쟁 안전.
    """
    try:
        f = open(OUTBOX, "a+", encoding="utf-8")
    except OSError:
        return 0
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read().strip()
        f.seek(0)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
    if content:
        print(content, flush=True)   # 이 stdout만 텔레그램으로 배달됨
    return 1 if content else 0


# ── YouTube 감지 ───────────────────────────────────────────────────────
def resolve_channel_id(handle: str) -> str | None:
    """@handle → 채널ID(UC...). 페이지 '자신의' 채널을 canonical/og:url/externalId 순으로 추출.

    주의: 채널 페이지 HTML엔 추천·featured 채널의 channelId가 먼저 등장할 수 있어,
    단순 첫 channelId 매칭은 엉뚱한 채널을 잡는다. canonical 링크가 그 페이지의 채널이라 우선.
    """
    handle = "".join(c for c in handle.strip().lstrip("@")
                     if unicodedata.category(c) != "Cf")  # 방향성/포맷 제어문자 제거
    url = f"https://www.youtube.com/@{handle}"
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=20)
    r.raise_for_status()
    for pat in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
                r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
                r'"externalId":"(UC[0-9A-Za-z_-]{22})"'):
        m = re.search(pat, r.text)
        if m:
            return m.group(1)
    return None


def rss_recent(channel_id: str) -> list[dict]:
    """채널 RSS → 최근 업로드[{yt_id,title,url,published}] (최신순, ~15건)."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", default="", namespaces=ns)
        if not vid:
            continue
        out.append({
            "yt_id": vid,
            "title": e.findtext("a:title", default="", namespaces=ns),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": e.findtext("a:published", default="", namespaces=ns),
        })
    return out


def filter_verdict(url: str, min_dur: int = MIN_DUR) -> tuple[bool, str]:
    """메타 확인 후 (처리해도 되는가, 사유). yt-dlp로 길이/라이브 판별.

    min_dur: 이 채널의 최소 길이(초). 0이면 길이 제한 면제.
    """
    if "/shorts/" in url:
        return False, "shorts"
    if yt_dlp is None:
        return True, ""   # 메타 불가 시 통과(다운로드 단계서 재검)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return False, f"meta_error:{e}"[:80]
    live = info.get("live_status")
    if live in ("is_live", "is_upcoming"):
        return False, f"live:{live}"           # 진행/예정 라이브 제외(끝난 다시보기는 처리)
    dur = float(info.get("duration") or 0)
    if min_dur and dur and dur <= min_dur:
        return False, f"short:{int(dur)}s"      # 최소 길이 이하 제외(min_dur=0이면 면제)
    return True, ""


# 메타 조회 실패 분류 — 다시 물어봐도 답이 같은 '영구 사유'와 '일시 장애'를 가른다.
# (영구 사유에 재시도 예산을 쓰면 yt-dlp 호출만 낭비하고 큐가 failed로 오염된다.
#  실제로 멤버십 전용·삭제 영상 21건이 각 8회씩 재시도된 뒤 failed로 쌓인 적이 있다.)
_META_TRANSIENT_RE = re.compile(
    r"timed?\s?out|timeout|connection|network|temporar|"
    r"too many requests|http error (?:429|5\d\d)|"
    r"unable to (?:download|connect|open)|read error|remote end closed|"
    r"getaddrinfo|ssl|handshake",
    re.I,
)
_META_PERMANENT_RE = re.compile(
    r"available to this channel|members[- ]only|join this channel|"      # 멤버십 전용
    r"has been removed|removed by the uploader|video unavailable|"        # 삭제
    r"private video|is private|"                                         # 비공개
    r"account associated with this video has been terminated|"           # 계정 정지
    r"copyright|"
    r"sign in to confirm your age|age[- ]restricted|inappropriate|"      # 로그인 필요
    r"not available in your country|blocked it in your country|"         # 지역 차단
    r"does not exist|no longer available",
    re.I,
)


# 다운로드 단계에서야 드러나는 '아직 방송 전' 신호(수동 추가 건은 사전 메타 확인이 없다)
_LIVE_PENDING_RE = re.compile(
    r"Premieres in|live event will begin|This live event|is upcoming|Scheduled for", re.I)


def _defer_policy(reason: str) -> tuple[bool, int, int, str]:
    """Return retryable, delay seconds, max checks, normalized error kind."""
    if reason.startswith("live:is_upcoming"):
        return True, 3600, 48, "live_upcoming"
    if reason.startswith("live:is_live"):
        return True, 1800, 48, "live_active"
    if reason.startswith("meta_error:"):
        body = reason.split(":", 1)[1]
        # 일시 장애를 먼저 본다 — "temporarily unavailable"이 영구 패턴에 걸리지 않도록
        if _META_TRANSIENT_RE.search(body):
            return True, 1800, 6, "metadata_transient"
        if _META_PERMANENT_RE.search(body):
            return False, 0, 0, "metadata_permanent"
        return True, 1800, 3, "metadata_unknown"   # 미상은 짧게만 재확인(폭주 방지)
    return False, 0, 0, "filter_permanent"


def recheck_deferred(channels: list[dict], *, dry: bool = False) -> int:
    """Re-evaluate due live/metadata/pipeline retries and activate ready videos."""
    by_channel = {row["channel_id"]: row for row in channels}
    activated = 0
    for item in db.queue_due_deferred():
        channel = by_channel.get(item.get("channel_id"))
        if channel is not None and not channel.get("enabled"):
            # 채널을 끄면 신규 감지만 멈추는 게 아니라 대기분도 정리한다 — 조용히
            # 건너뛰면 항목이 영원히 deferred로 남는다(실례: NVIDIA Developer OFF 후
            # 라이브 예정 건이 6일간 잔류). 다시 처리하려면 큐 팝업에서 재등록하면 된다.
            log(f"[deferred] 채널 비활성 → 정리: {item['title'][:50]}")
            if not dry:
                db.queue_set_status(item["id"], "skipped", "채널 비활성(대기 중 정리)")
            continue
        if db.find_by_yt_id(item["yt_id"]):
            if not dry:
                db.queue_set_status(item["id"], "done", "이미 처리된 이력 확인")
            continue

        # 파이프라인 일시 실패는 메타가 정상인지 다시 확인한 뒤 pending으로 복귀한다.
        min_duration = (
            channel.get("min_duration")
            if channel and channel.get("min_duration") is not None
            else MIN_DUR
        )
        ok, reason = filter_verdict(item["url"], min_duration)
        if ok:
            log(f"[deferred] 재처리 가능 → pending: {item['title'][:50]}")
            if not dry:
                reset_attempts = str(item.get("error_kind") or "").startswith(("metadata", "live"))
                db.queue_activate(item["id"], reset_attempts=reset_attempts)
            activated += 1
            continue

        retryable, delay, max_attempts, error_kind = _defer_policy(reason)
        if retryable:
            # 메타 오류(403·네트워크 등)는 재시도할수록 간격을 벌린다. 라이브 재확인은
            # '방송이 끝났나' 주기 확인이라 백오프 없이 고정 간격을 유지한다.
            if error_kind.startswith("metadata"):
                delay = max(delay, _retry_delay(item.get("attempt_count")))
            log(f"[deferred] 재확인 대기 [{reason}]: {item['title'][:50]}")
            if not dry:
                db.queue_defer(
                    item["id"], reason, error_kind=error_kind,
                    retry_after_seconds=delay, increment_attempt=True,
                    max_attempts=max_attempts,
                )
        else:
            log(f"[deferred] 확정 제외 [{reason}]: {item['title'][:50]}")
            if not dry:
                db.queue_set_status(item["id"], "skipped", reason)
    return activated


# ── 폴링(감지 → 큐 적재) ───────────────────────────────────────────────
def poll_channels(dry: bool = False) -> int:
    """활성 채널 폴 → 신규 pending 적재. 새로 pending에 넣은 건수 반환."""
    channels = db.list_channels()
    queued = recheck_deferred(channels, dry=dry)
    for ch in channels:
        if not ch["enabled"]:
            continue
        cid, title = ch["channel_id"], ch["title"] or ch["handle"] or ch["channel_id"]
        try:
            vids = rss_recent(cid)
        except Exception as e:
            log(f"[poll] {title}: RSS 오류 {e}")
            continue

        # 최초 폴: 현재 피드를 baseline(seen)로만 기록 → 이후 신규만 처리
        if not ch["baseline_done"]:
            log(f"[poll] {title}: baseline {len(vids)}건 seen 처리(백필 방지)")
            if not dry:
                for v in vids:
                    if not db.find_by_yt_id(v["yt_id"]):
                        db.enqueue_video(v["yt_id"], v["url"], v["title"], cid,
                                         status="skipped", reason="baseline")
                db.set_channel_baseline(ch["id"])
                db.mark_channel_checked(ch["id"])
            continue

        new_for_ch = 0
        for v in vids:
            if db.find_by_yt_id(v["yt_id"]) or db.in_queue(v["yt_id"]):
                continue                          # 이미 전사됨 / 이미 큐에 있음
            cmin = ch["min_duration"] if ch["min_duration"] is not None else MIN_DUR
            ok, reason = filter_verdict(v["url"], cmin)
            if not ok:
                log(f"[poll] {title}: 제외 [{reason}] {v['title'][:40]}")
                if not dry:
                    retryable, delay, _, error_kind = _defer_policy(reason)
                    db.enqueue_video(
                        v["yt_id"], v["url"], v["title"], cid,
                        status="deferred" if retryable else "skipped",
                        reason=reason,
                        retry_after_seconds=delay if retryable else None,
                        error_kind=error_kind,
                    )
                continue
            log(f"[poll] {title}: 신규 → 큐 {v['title'][:40]}")
            if not dry:
                db.enqueue_video(v["yt_id"], v["url"], v["title"], cid, status="pending")
            queued += 1
            new_for_ch += 1
        if not dry:
            db.mark_channel_checked(ch["id"])
    return queued


# ── 처리(파이프라인 = 서버 HTTP 재사용) ────────────────────────────────
def _get_prompt() -> str:
    try:
        return requests.get(f"{BASE}/prompt", timeout=15).json().get("prompt", "")
    except Exception:
        return ""


def process_video(v: dict, prompt: str, *, skip_claude: bool = False,
                  summary_models=None, capture_models=None) -> dict:
    """전사→요약→캡처를 서버 엔드포인트로 순차 수행. 완료 요약 파일 경로 반환.

    summary_models/capture_models: 자동모니터 설정에서 읽은 작업별 폴백 순서.
    skip_claude는 구버전 호출 호환용이며 명시 순서가 있으면 사용하지 않는다.
    """
    url = v["url"]
    txt_path = v.get("txt_path")
    if not txt_path or not os.path.isfile(txt_path):
        r = requests.post(f"{BASE}/start", json={"source": "url", "url": url}, timeout=30).json()
        job_id = r.get("job_id")
        if not job_id:
            raise RuntimeError(r.get("error", "start 실패"))

        txt_path = None
        deadline = time.time() + MAX_JOB_SEC
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            res = requests.get(f"{BASE}/result/{job_id}", timeout=30).json()
            # 서버가 재시작되면 잡 상태가 소실돼 404("Not found")가 온다 — status가
            # 영영 안 나오므로 여기서 끊지 않으면 MAX_JOB_SEC(5h)까지 헛폴링한다.
            if res.get("error") == "Not found":
                raise RuntimeError("전사 잡 소실(서버 재시작) — 재시도 필요")
            st = res.get("status")
            if st == "done":
                txt_path = res.get("txt_path")
                break
            if st in ("error", "cancelled"):
                detail = (res.get("error_message") or "").strip()
                raise RuntimeError(f"전사 {st}" + (f": {detail[:160]}" if detail else ""))
        if not txt_path:
            raise RuntimeError("전사 시간초과")
        db.queue_set_txt_path(v["id"], txt_path)
    else:
        log(f"[resume] 기존 전사 재사용: {os.path.basename(txt_path)}")

    # 요약(SSE 스트림 소비 → 서버가 성공 시에만 디스크에 저장, 실패 시 event:error 방출)
    summary_err = None
    await_err = False
    sum_body = {"txt_path": txt_path, "prompt": prompt}
    if summary_models is not None:
        sum_body["models"] = llm_gateway.normalize_model_order(summary_models)
    elif skip_claude:
        sum_body["skip_claude"] = True
        sum_body["mode"] = "grok"
    with requests.post(f"{BASE}/summarize", json=sum_body,
                       stream=True, timeout=(30, SUMM_TIMEOUT)) as sr:
        sr.raise_for_status()
        for raw in sr.iter_lines(decode_unicode=True):
            if not raw:
                continue
            raw = raw.strip()
            if raw == "event: error":
                await_err = True
            elif await_err and raw.startswith("data:"):
                payload = raw[5:].strip()
                try:
                    summary_err = json.loads(payload) if payload else "요약 실패"
                except Exception:
                    summary_err = payload or "요약 실패"
                await_err = False
    if summary_err:
        # 모든 지정 모델이 소진됐거나 systemic 장애면 큐를 멈추고 다음 주기에 재시도한다.
        if "모든 요약 모델 실패" in str(summary_err) or _is_systemic(str(summary_err)):
            raise ClaudeUnavailable(f"요약: {summary_err}")
        raise RuntimeError(f"요약 실패: {summary_err}")

    # 캡처(동기) — 요약은 이미 저장됨. 실패해도 항목은 완료(요약이 본체), systemic 여부만 표시
    kf_note, kf_systemic, kf_permanent = "", False, False
    # 캡처 포함 여부: 영상 단위 지정 > 채널 설정 > 기본 포함(큐 밖 호출은 종전대로)
    if not db.capture_enabled_for(v):
        log("[kf] 캡처 제외 설정 — 건너뜀")
        return {"txt_path": txt_path, "kf_note": "", "kf_systemic": False,
                "kf_permanent": False, "kf_skipped": True}
    try:
        kf_body = {"txt_path": txt_path, "url": url}
        if capture_models is not None:
            kf_body["models"] = llm_gateway.normalize_model_order(capture_models)
        elif skip_claude:
            kf_body["skip_claude"] = True
            kf_body["mode"] = "grok"
        kf = requests.post(f"{BASE}/keyframes", json=kf_body, timeout=KF_TIMEOUT).json()
        if not kf.get("ok"):
            kf_note = str(kf.get("reason") or "실패")
            emsg = str(kf.get("error") or "")
            # 비전 호출 실패 + 한도 신호일 때만 systemic (다운로드 403 등과 구분)
            kf_systemic = kf_note == "vision_failed" and _is_systemic(emsg)
            # 멤버십 전용 전환·삭제·비공개는 다시 받아도 결과가 같다 → 재시도 대상에서 제외.
            # (수집 당시엔 공개였다가 나중에 멤버십으로 돌리는 채널이 있어 캡처만 실패한다)
            kf_permanent = bool(_META_PERMANENT_RE.search(emsg))
            if kf_permanent:
                log(f"[kf] 캡처 불가(영구 사유, 재시도 안 함): {emsg[:110]}")
            else:
                log(f"[kf] 캡처 스킵/실패: {kf_note} {emsg[:120]}")
    except Exception as e:
        kf_note = "오류"
        log(f"[kf] 캡처 오류(요약은 유지): {e}")
    return {"txt_path": txt_path, "kf_note": kf_note, "kf_systemic": kf_systemic,
            "kf_permanent": kf_permanent}


def process_capture_only(txt_path: str, url: str, *, capture_models=None) -> dict:
    """요약이 이미 끝난 항목의 캡처(키프레임)만 재생성. 전사·요약은 스킵.

    반환: /keyframes 원 응답 dict — {"ok":True,"n_frames":N} 또는 {"ok":False,"reason","error"}.
    """
    body = {"txt_path": txt_path, "url": url}
    if capture_models is not None:
        body["models"] = llm_gateway.normalize_model_order(capture_models)
    return requests.post(f"{BASE}/keyframes", json=body, timeout=KF_TIMEOUT).json()


def drain() -> None:
    """주기당 '본편 1건 + 캡처 재시도 1건'까지 처리한다.

    본편(pending: 전사→요약→캡처)과 캡처 재시도(kf_retry: 캡처만 재생성)는
    별도 큐로 서고, 각자 들어온 순서대로 한 건씩만 나온다. 캡처 재시도가
    본편 처리 순서를 차지하지 않으면서도, 캡처 역시 유튜브에서 영상을 새로
    받는 작업이라 주기당 1건으로 제한된다. deferred는 due가 되면 poll 단계에서
    pending으로 복귀한다.
    """
    orders = db.get_monitor_model_orders()
    capture_order = orders["capture"]
    _drain_main_one(orders, capture_order)
    _drain_kf_retry_one(capture_order)


def _drain_kf_retry_one(capture_order) -> None:
    """캡처 재시도 큐에서 1건 — 본편과 별도 슬롯(주기당 본편 1 + 캡처 1).

    캡처도 유튜브에서 저해상도 영상을 새로 받으므로 무제한으로 풀 수는 없다.
    캡처만 1회 재생성(전사·요약 스킵)하고 성공·실패 무관하게 종료한다.
    """
    v = db.queue_claim_kf_retry()
    if not v:
        return
    title = v["title"] or v["yt_id"]
    head = _notify_head(v, title)
    txt_path = v.get("txt_path")
    if not txt_path:
        db.queue_set_status(v["id"], "done", reason="캡처 재시도 불가(전사경로 없음)")
        return
    log(f"[kf-retry] 캡처 재시도: {title}")
    try:
        kf = process_capture_only(txt_path, v["url"], capture_models=capture_order)
        if kf.get("ok"):
            db.queue_set_status(v["id"], "done", reason="캡처 재시도 성공")
            notify(f"📸 캡처 재시도 성공\n{head}\n{v['url']}")
            log(f"[kf-retry] 성공: {title} (n_frames={kf.get('n_frames')})")
        else:
            rz = str(kf.get("reason") or "실패")
            emsg = str(kf.get("error") or "")
            # 재시도 사이에 멤버십 전용으로 바뀌었을 수 있다 — 사유를 구분해 알린다
            if _META_PERMANENT_RE.search(emsg):
                db.queue_set_status(v["id"], "done", reason="캡처 불가(멤버십/비공개 전환)")
                log(f"[kf-retry] 캡처 불가(영구 사유): {emsg[:110]}")
                notify(f"ℹ️ 캡처 불가 — 멤버십/비공개 전환된 영상\n{head}\n{v['url']}")
            else:
                db.queue_set_status(v["id"], "done", reason=f"캡처 재시도 실패(포기): {rz}")
                log(f"[kf-retry] 재시도 실패 → 캡처 포기: {rz} {emsg[:120]}")
                notify(f"⚠️ 캡처 재시도 실패(포기)\n{head}\n{rz}\n{v['url']}")
    except Exception as e:
        db.queue_set_status(v["id"], "done", reason=f"캡처 재시도 오류(포기): {str(e)[:80]}")
        log(f"[kf-retry] 재시도 오류 → 캡처 포기: {e}")
        notify(f"⚠️ 캡처 재시도 오류(포기)\n{head}\n{e}\n{v['url']}")
    return


def _drain_main_one(orders, capture_order) -> None:
    """본편 큐(pending)에서 1건 — 전사→요약→캡처 전체 파이프라인."""
    v = db.queue_claim_one()
    if not v:
        return
    title = v["title"] or v["yt_id"]
    head = _notify_head(v, title)
    summary_order = orders["summary"]
    proceed, mode, detail = summarizer_gate(summary_order)
    if not proceed:
        # 요약 경로가 없으면 시도 자체를 안 한 것 — 예산을 태우지 않고 되돌린다.
        db.queue_unclaim(v["id"])
        notify("⛔ 요약 경로 없음 — 자동 처리 보류(다음 주기 재시도)\n"
               f"사유: {detail}")
        return
    active_summary_order = summary_order[summary_order.index(mode):]
    if detail:
        notify(f"⚠️ 요약 1순위 사용 불가 — **{mode.upper()}부터 폴백 처리**\n{detail}")

    log(f"[drain] 처리 시작: {title} (mode={mode})")
    try:
        res = process_video(
            v, _get_prompt(),
            summary_models=active_summary_order,
            capture_models=capture_order,
        )
        kf_note = res.get("kf_note")
        # 캡처만 일시 실패(systemic·영구사유 아님) → 큐 뒤에 kf_retry로 다시 세운다
        retryable = (bool(kf_note) and not res.get("kf_systemic")
                     and not res.get("kf_permanent"))
        if retryable and res.get("txt_path"):
            db.queue_mark_kf_retry(v["id"], res["txt_path"], reason=f"캡처 실패: {kf_note}")
            notify(f"✅ 자동 요약 완료 — 캡처 실패, 다음 주기 재시도 예약\n{head}\n{v['url']}")
            log(f"[drain] 캡처 재시도 예약: {title} ({kf_note})")
        else:
            db.queue_set_status(v["id"], "done")
            suffix = "(캡처 실패)" if kf_note else ("(캡처 제외)" if res.get("kf_skipped") else "")
            notify(f"✅ 자동 요약 완료{suffix}\n{head}\n{v['url']}")
    except ClaudeUnavailable as e:
        # 지정된 요약 모델이 모두 실패한 경우(폴백까지 소진)
        db.queue_defer(
            v["id"], "요약 경로 재시도 대기", error_kind="llm_unavailable",
            retry_after_seconds=_retry_delay(v.get("attempt_count")),
            max_attempts=MAX_PIPELINE_ATTEMPTS,
        )
        notify(f"⛔ 요약 실패(모든 폴백 소진) — 다음 주기 재시도\n{head}\n{e}")
    except Exception as e:
        reason = str(e)[:160]
        # 라이브·프리미어 예정은 실패가 아니다 — 재시도 예산을 태우지 않고
        # 방송 시작을 기다리는 라이브 재확인 경로로 보낸다(사용자 알림도 구분).
        if _LIVE_PENDING_RE.search(reason):
            db.queue_defer(
                v["id"], reason, error_kind="live_upcoming",
                retry_after_seconds=3600, max_attempts=None,
            )
            db.queue_bump_attempt(v["id"], -1)   # claim이 올린 시도 횟수 되돌림
            notify(
                f"📡 라이브/프리미어 예정 영상 — 방송 후 자동 처리 예정\n"
                f"{head}\n{reason}\n{v['url']}"
            )
            return
        state = db.queue_defer(
            v["id"], reason, error_kind="pipeline_transient",
            retry_after_seconds=_retry_delay(v.get("attempt_count")),
            max_attempts=MAX_PIPELINE_ATTEMPTS,
        )
        notify(
            f"⚠️ 자동 처리 실패 — {'재시도 예약' if state == 'deferred' else '재시도 소진'}\n"
            f"{head}\n{reason}\n{v['url']}"
        )


# ── 단일 인스턴스 락 + 진입점 ──────────────────────────────────────────
def _acquire_lock():
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    fp.write(str(os.getpid()))
    fp.flush()
    return fp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="감지/필터만 출력(변경 없음)")
    ap.add_argument("--poll-only", action="store_true", help="감지·적재만")
    ap.add_argument("--drain-only", action="store_true", help="큐 처리만")
    ap.add_argument("--announce", action="store_true",
                    help="아웃박스의 알림만 stdout으로 배출(hermes 크론 shim 전용)")
    args = ap.parse_args()

    if args.announce:                 # hermes 크론 경로: 순수 알림 배출(DB/락 불필요)
        announce()
        return 0

    db.init()
    recovered = db.queue_recover_stale(STALE_PROCESSING_SEC)
    if recovered:
        log(f"[recover] 중단된 processing {recovered}건 → deferred")

    if args.dry_run:
        n = poll_channels(dry=True)
        log(f"[dry-run] 처리 대상(신규) {n}건 — 실제 적재/처리는 안 함")
        log(f"[dry-run] 현재 큐: {db.queue_counts()}")
        return 0

    lock = _acquire_lock()
    if lock is None:
        log("[lock] 이미 실행 중 — 종료(다음 주기에 이어서 처리)")
        return 0

    try:
        if not args.drain_only:
            q = poll_channels()
            log(f"[poll] 신규 적재 {q}건, 큐: {db.queue_counts()}")
        if not args.poll_only:
            drain()
            log(f"[drain] 완료, 큐: {db.queue_counts()}")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
