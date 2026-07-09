#!/usr/bin/env python3
"""유튜브 채널 자동 모니터 — 신규 영상 감지 → 전사/요약/캡처 자동화.

감지(폴러)와 처리(드레이너)를 한 프로세스에서 수행하되, 파일 락으로 단일 인스턴스를
보장한다. 20분 간격 launchd/cron로 실행. 파이프라인은 상시 구동 중인 Flask 서버
(기본 127.0.0.1:5001)의 HTTP 엔드포인트를 그대로 호출해 재사용한다(브라우저와 동일 경로).

흐름:
  1) 활성 채널마다 RSS(feeds/videos.xml)로 최근 업로드 목록 수집
  2) 최초 폴이면 현재 피드를 baseline(seen)로 기록만 하고 처리 안 함(백필 방지)
  3) 이후엔 이력(items)·큐에 없는 신규만 메타 확인 → 필터(≤5분·라이브·Shorts 제외) → 큐 적재
  4) 큐를 FIFO로 한 건씩 처리: /start→/result 폴링→/summarize(드레인)→/keyframes
  5) 건별 완료/실패 알림(Telegram, 자격증명 있을 때)

사용:
  python channel_monitor.py            # 폴 + 드레인(기본)
  python channel_monitor.py --dry-run  # 감지/필터만 출력(DB·처리 변경 없음)
  python channel_monitor.py --poll-only # 감지·적재만(드레인 안 함)
  python channel_monitor.py --drain-only # 큐 처리만(감지 안 함)
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import fcntl
import unicodedata
import xml.etree.ElementTree as ET

import requests

import db

try:
    import yt_dlp
except Exception:
    yt_dlp = None

# ── 설정 ───────────────────────────────────────────────────────────────
BASE       = os.environ.get("YTS_BASE", "http://127.0.0.1:5001")
MIN_DUR    = int(os.environ.get("MONITOR_MIN_DURATION", "300"))   # 5분 이하 제외
POLL_SEC   = int(os.environ.get("MONITOR_POLL_SEC", "6"))         # /result 폴링 간격
MAX_JOB_SEC = int(os.environ.get("MONITOR_MAX_JOB_SEC", "9000"))  # 건당 전사 상한(2.5h)
SUMM_TIMEOUT = int(os.environ.get("MONITOR_SUMM_TIMEOUT", "900"))
KF_TIMEOUT   = int(os.environ.get("MONITOR_KF_TIMEOUT", "1800"))
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


# ── Claude 가용성 가드 ─────────────────────────────────────────────────
class ClaudeUnavailable(Exception):
    """사용량 한도/인증/장애 등 Claude 자체 문제 — 큐 정지 후 다음 주기 재시도 대상."""


# 사용량 한도·인증·서버장애 등 '재시도하면 나아질' systemic 신호 패턴
_SYSTEMIC = re.compile(
    # 주의: 403/500/503은 yt-dlp/ffmpeg의 HTTP 오류(봇차단 등)와 겹쳐 오판하므로 제외.
    r"usage limit|session limit|rate.?limit|too many requests|\b429\b|\b401\b|\b529\b"
    r"|authenticat|unauthor|credit balance|quota|exceeded|overloaded|api error"
    r"|사용 한도|세션 한도|한도 초과|인증",
    re.I,
)


def _is_systemic(msg: str) -> bool:
    return bool(_SYSTEMIC.search(msg or ""))


def _claude_bin() -> str:
    """server(app.py)/keyframe와 동일한 claude CLI 경로 해석."""
    env = os.environ.get("CLAUDE_BIN")
    if env and os.path.exists(env):
        return env
    w = shutil.which("claude")
    if w:
        return w
    cands = sorted(_glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude")))
    return cands[-1] if cands else "claude"


def claude_healthy() -> tuple[bool, str]:
    """저가 프로브로 Claude CLI 가용성 확인. (정상?, 사유)."""
    try:
        r = subprocess.run([_claude_bin(), "-p", "--model", "haiku", "reply with: ok"],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "헬스체크 타임아웃(90s)"
    except Exception as e:
        return False, f"claude 실행 오류: {e}"
    out = (r.stdout or "").strip()
    blob = (out + " " + (r.stderr or "")).strip()
    if r.returncode != 0 or not out or _is_systemic(blob):
        return False, (blob or f"rc={r.returncode}")[:200]
    return True, ""


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


# ── 폴링(감지 → 큐 적재) ───────────────────────────────────────────────
def poll_channels(dry: bool = False) -> int:
    """활성 채널 폴 → 신규 pending 적재. 새로 pending에 넣은 건수 반환."""
    queued = 0
    for ch in db.list_channels():
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
                    db.enqueue_video(v["yt_id"], v["url"], v["title"], cid,
                                     status="skipped", reason=reason)
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


def process_video(v: dict, prompt: str) -> str:
    """전사→요약→캡처를 서버 엔드포인트로 순차 수행. 완료 요약 파일 경로 반환."""
    url = v["url"]
    r = requests.post(f"{BASE}/start", json={"source": "url", "url": url}, timeout=30).json()
    job_id = r.get("job_id")
    if not job_id:
        raise RuntimeError(r.get("error", "start 실패"))

    txt_path = None
    deadline = time.time() + MAX_JOB_SEC
    while time.time() < deadline:
        time.sleep(POLL_SEC)
        res = requests.get(f"{BASE}/result/{job_id}", timeout=30).json()
        st = res.get("status")
        if st == "done":
            txt_path = res.get("txt_path")
            break
        if st in ("error", "cancelled"):
            raise RuntimeError(f"전사 {st}")
    if not txt_path:
        raise RuntimeError("전사 시간초과")

    # 요약(SSE 스트림 소비 → 서버가 성공 시에만 디스크에 저장, 실패 시 event:error 방출)
    summary_err = None
    await_err = False
    with requests.post(f"{BASE}/summarize", json={"txt_path": txt_path, "prompt": prompt},
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
        # 요약 실패는 대부분 Claude 측(한도/장애/인증) → systemic이면 재시도 대상으로 올림
        if _is_systemic(str(summary_err)):
            raise ClaudeUnavailable(f"요약: {summary_err}")
        raise RuntimeError(f"요약 실패: {summary_err}")

    # 캡처(동기) — 요약은 이미 저장됨. 실패해도 항목은 완료(요약이 본체), systemic 여부만 표시
    kf_note, kf_systemic = "", False
    try:
        kf = requests.post(f"{BASE}/keyframes", json={"txt_path": txt_path, "url": url},
                           timeout=KF_TIMEOUT).json()
        if not kf.get("ok"):
            kf_note = str(kf.get("reason") or "실패")
            emsg = str(kf.get("error") or "")
            # 비전(Claude) 호출 실패일 때만 systemic 판정 — 영상 다운로드 403 등은 제외(오판 방지)
            kf_systemic = kf_note == "vision_failed" and _is_systemic(emsg)
            log(f"[kf] 캡처 스킵/실패: {kf_note} {emsg[:120]}")
    except Exception as e:
        kf_note = "오류"
        log(f"[kf] 캡처 오류(요약은 유지): {e}")
    return {"txt_path": txt_path, "kf_note": kf_note, "kf_systemic": kf_systemic}


def drain() -> None:
    """큐의 pending을 FIFO로 한 건씩 처리. Claude 사용불가면 큐를 정지하고 다음 주기 재시도."""
    if not db.queue_next_pending():
        return
    ok, reason = claude_healthy()          # 사전 헬스체크(작업 있을 때만 프로브)
    if not ok:
        notify(f"⛔ Claude 사용 불가 — 자동 요약 보류(다음 주기 재시도)\n사유: {reason}")
        return
    prompt = _get_prompt()
    while True:
        v = db.queue_next_pending()
        if not v:
            break
        db.queue_set_status(v["id"], "processing")
        title = v["title"] or v["yt_id"]
        log(f"[drain] 처리 시작: {title}")
        try:
            res = process_video(v, prompt)
            db.queue_set_status(v["id"], "done")
            suffix = "(캡처 실패)" if res.get("kf_note") else ""
            notify(f"✅ 자동 요약 완료{suffix}\n{title}\n{v['url']}")
            if res.get("kf_systemic"):       # 요약 후 Claude 한도 도달 → 다음 건 전사 낭비 방지
                notify("⛔ Claude 사용 한도 감지(캡처 단계) — 큐 정지, 다음 주기 재시도")
                break
        except ClaudeUnavailable as e:
            # Claude 측 문제 → 항목은 pending으로 되돌리고 큐 정지(다음 주기 재시도)
            db.queue_set_status(v["id"], "pending", reason="claude 재시도 대기")
            notify(f"⛔ Claude 사용 불가로 중단 — 큐 정지, 다음 주기 재시도\n{title}\n{e}")
            break
        except Exception as e:
            db.queue_set_status(v["id"], "failed", reason=str(e)[:120])
            notify(f"⚠️ 자동 처리 실패\n{title}\n{e}\n{v['url']}")


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
