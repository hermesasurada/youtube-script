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
import os
import re
import sys
import time
import fcntl
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


def notify(text: str) -> None:
    """완료/실패 알림 — stdout(로그/hermes-cron 전달) + Telegram(자격증명 있을 때)."""
    log(text)
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:
            log(f"[notify] telegram 실패: {e}")


# ── YouTube 감지 ───────────────────────────────────────────────────────
def resolve_channel_id(handle: str) -> str | None:
    """@handle → 채널ID(UC...). 채널 페이지 HTML에서 추출(최초 1회용)."""
    url = f"https://www.youtube.com/@{handle.lstrip('@')}"
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=20)
    r.raise_for_status()
    for pat in (r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{22})"',
                r'"browseId":"(UC[0-9A-Za-z_-]{22})"'):
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


def filter_verdict(url: str) -> tuple[bool, str]:
    """메타 확인 후 (처리해도 되는가, 사유). yt-dlp로 길이/라이브 판별."""
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
    if dur and dur <= MIN_DUR:
        return False, f"short:{int(dur)}s"      # 5분 이하 제외(Shorts 포함)
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
            ok, reason = filter_verdict(v["url"])
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

    # 요약(SSE 스트림을 끝까지 소비 → 서버가 디스크에 저장)
    with requests.post(f"{BASE}/summarize", json={"txt_path": txt_path, "prompt": prompt},
                       stream=True, timeout=(30, SUMM_TIMEOUT)) as sr:
        sr.raise_for_status()
        for _ in sr.iter_lines(decode_unicode=True):
            pass

    # 캡처(동기) — 실패해도 요약은 유지되므로 치명적 아님
    try:
        kf = requests.post(f"{BASE}/keyframes", json={"txt_path": txt_path, "url": url},
                           timeout=KF_TIMEOUT).json()
        if not kf.get("ok"):
            log(f"[kf] 캡처 스킵/실패: {kf.get('reason')}")
    except Exception as e:
        log(f"[kf] 캡처 오류(요약은 유지): {e}")
    return txt_path


def drain() -> None:
    """큐의 pending을 FIFO로 한 건씩 처리."""
    prompt = _get_prompt()
    while True:
        v = db.queue_next_pending()
        if not v:
            break
        db.queue_set_status(v["id"], "processing")
        title = v["title"] or v["yt_id"]
        log(f"[drain] 처리 시작: {title}")
        try:
            process_video(v, prompt)
            db.queue_set_status(v["id"], "done")
            notify(f"✅ 자동 요약 완료\n{title}\n{v['url']}")
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
    args = ap.parse_args()

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
