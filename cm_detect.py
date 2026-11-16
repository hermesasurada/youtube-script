"""신규 영상 감지 — 채널 ID 확인, RSS 읽기, 광고·길이·라이브 걸러내기, 보류 영상 재확인, 큐 적재(`poll_channels`).

전사·요약은 하지 않는다(그건 channel_monitor.process_video). 메타 조회 실패의 영구/일시 구분(`_defer_policy`)도 여기 있다
(2026-11-17 channel_monitor에서 분리).
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata
import xml.etree.ElementTree as ET
import requests
import db
import yt_listing

import cm_base

def log(msg: str) -> None:
    print(msg, flush=True)


# ── YouTube 감지 ───────────────────────────────────────────────────────
def resolve_channel_id(handle: str) -> str | None:
    """@handle → 채널ID(UC...). 페이지 '자신의' 채널을 canonical/og:url/externalId 순으로 추출.

    주의: 채널 페이지 HTML엔 추천·featured 채널의 channelId가 먼저 등장할 수 있어,
    단순 첫 channelId 매칭은 엉뚱한 채널을 잡는다. canonical 링크가 그 페이지의 채널이라 우선.
    """
    handle = "".join(c for c in handle.strip().lstrip("@")
                     if unicodedata.category(c) != "Cf")  # 방향성/포맷 제어문자 제거
    url = f"https://www.youtube.com/@{handle}"
    r = requests.get(url, headers={"User-Agent": cm_base.UA, "Accept-Language": "en"}, timeout=20)
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
    r = requests.get(url, headers={"User-Agent": cm_base.UA}, timeout=20)
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


# 광고 영상(2026-10-24 사용자 지시, #1638 집코노미 '… | 흥청망청 [AD]'): 제목의 광고 표기 태그만 보고 건너뛴다.
# 대괄호·소괄호로 감싼 AD·광고·유료광고·Sponsored와 해시태그 #광고만 — 'ADR'·'AD램' 같은 낱말은 태그가 아니라 남는다.
_AD_TITLE_RE = re.compile(r"[\[(（【]\s*(?:AD|Ad|ad|광고|유료\s*광고|sponsored)\s*[\])）】]|#광고(?![가-힣])", re.I)


def is_ad_title(title: str) -> bool:
    return bool(_AD_TITLE_RE.search(title or ""))


def filter_verdict(url: str, min_dur: int = cm_base.MIN_DUR, *, allow_live: bool = False) -> tuple[bool, str]:
    """메타 확인 후 (처리해도 되는가, 사유). yt-dlp로 길이/라이브 판별.

    min_dur: 이 채널의 최소 길이(초). 0이면 길이 제한 면제.
    라이브는 자동 감지분이면 건너뛴다(2026-11-03 사용자 지시 — 예정·진행·끝난 다시보기 모두,
    사유 'live_skip:…'로 확정 제외). allow_live=True(수동 등록분)면 예전처럼 방송이 끝나기를 기다린다.
    프리미어(미리 올린 영상의 첫 공개)는 라이브가 아니다 — 공개 전엔 기다리고 공개 뒤 처리한다.
    """
    if "/shorts/" in url:
        return False, "shorts"
    if cm_base.yt_dlp is None:
        return True, ""   # 메타 불가 시 통과(다운로드 단계서 재검)
    try:
        with cm_base.yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # 방송 전 라이브는 live_status 대신 예외로 온다('This live event will begin in N hours')
        if not allow_live and _LIVE_EVENT_RE.search(str(e)):
            return False, f"live_skip:upcoming {e}"[:80]
        return False, f"meta_error:{e}"[:80]
    live = info.get("live_status")
    if not allow_live and live in ("is_live", "post_live", "was_live"):
        return False, f"live_skip:{live}"      # 라이브는 진행 중이든 끝났든 건너뛴다
    if live in ("is_live", "is_upcoming"):
        return False, f"live:{live}"           # 예정(프리미어일 수 있음)·수동 라이브는 끝나기를 기다린다
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
    r"does not exist|no longer available|"
    r"음성 없는 영상",                                                    # 두 언어 모두 백지(app._finish_transcription)
    re.I,
)


# 다운로드 단계에서야 드러나는 '아직 방송 전' 신호(수동 추가 건은 사전 메타 확인이 없다)
_LIVE_PENDING_RE = re.compile(
    r"Premieres in|live event will begin|This live event|is upcoming|Scheduled for", re.I)
# 그중 라이브 방송(프리미어가 아님)이 확실한 문구 — 자동 감지분은 건너뛴다(2026-11-03 사용자 지시)
_LIVE_EVENT_RE = re.compile(r"live event will begin|This live event", re.I)
# 서버(yt 앱)가 재시작돼 진행 중이던 작업이 사라진 신호. 영상 탓이 아니라 몇 번을
# 겪어도 문구가 같으므로 '동일 실패 반복' 판정에서 뺀다 — 재시도 예산(5회)은 그대로
# 쓴다(2026-09-23: 긴 재전사 도중 배포 재시작에 세 번 걸려 skipped로 굳었다).
_SERVER_RESTART_RE = re.compile(
    r"잡 소실\(서버 재시작\)|Connection aborted|RemoteDisconnected|Connection refused"
    r"|Max retries exceeded")


def _defer_policy(reason: str) -> tuple[bool, int, int, str]:
    """Return retryable, delay seconds, max checks, normalized error kind."""
    if reason.startswith("live_skip:"):
        return False, 0, 0, "live_skipped"     # 자동 감지 라이브는 확정 제외(2026-11-03 사용자 지시)
    if reason.startswith("live:is_upcoming"):
        return True, 3600, 48, "live_upcoming"
    if reason.startswith("live:is_live"):
        return True, 1800, 48, "live_active"
    if reason.startswith("meta_error:"):
        body = reason.split(":", 1)[1]
        # yt-dlp는 방송 전 라이브·프리미어를 live_status 대신 예외('This live event will begin in N hours')로
        # 알린다 — 메타 오류가 아니라 라이브 예정이다. 미상(3회)으로 두면 방송 전에 failed로 굳는다
        # (2026-11-02 Rocket Lab 'The Sea God Surveys' Launch: 30분 간격 3회 만에 failed).
        if _LIVE_PENDING_RE.search(body):
            return True, 3600, 48, "live_upcoming"
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
        # 수동 등록분은 이력이 있어도 취소하지 않는다 — 재처리(손실 복구·모델 교체)를
        # 노리고 넣은 것이라 '이미 있음'이 곧 스킵 사유가 될 수 없다. 자동 감지분만
        # 중복으로 정리한다(2026-08-27: 재처리 802초 손실 건이 이 경로로 취소됨).
        #
        # 단, 이력이 '전사만 있고 요약이 빈' 반쪽이면 중복이 아니라 미완성이다 —
        # 요약 SSE가 타임아웃(SUMM_TIMEOUT)으로 끊기면 전사 파일만 남는데, 이때
        # 중복으로 정리해 버리면 요약이 영영 생기지 않는다(2026-09-13 Qualcomm
        # 47분 영상: read timeout 900s → 다음 틱에서 '이미 처리된 이력 확인'으로 done).
        if item.get("channel_id") != "manual" and db.find_by_yt_id(item["yt_id"]):
            if db.has_summary_for_yt_id(item["yt_id"]):
                if not dry:
                    db.queue_set_status(item["id"], "done", "이미 처리된 이력 확인")
                continue
            log(f"[deferred] 이력에 요약이 없음(전사만) → 재처리: {item['title'][:50]}")

        # 파이프라인 일시 실패는 메타가 정상인지 다시 확인한 뒤 pending으로 복귀한다.
        min_duration = (
            channel.get("min_duration")
            if channel and channel.get("min_duration") is not None
            else cm_base.MIN_DUR
        )
        ok, reason = filter_verdict(item["url"], min_duration,
                                    allow_live=item.get("channel_id") == "manual")
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
                delay = max(delay, cm_base._retry_delay(item.get("attempt_count")))
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
def _parse_ts(stamp) -> datetime | None:
    """RSS published·channels.resumed_at(ISO, 시간대 포함) → aware datetime. 실패면 None."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def poll_channels(dry: bool = False) -> int:
    """활성 채널 폴 → 신규 pending 적재. 새로 pending에 넣은 건수 반환."""
    channels = db.list_channels()
    queued = recheck_deferred(channels, dry=dry)
    for ch in channels:
        if not ch["enabled"]:
            continue
        cid, title = ch["channel_id"], ch["title"] or ch["handle"] or ch["channel_id"]
        fallback = False
        try:
            vids = rss_recent(cid)
        except Exception as e:
            # YouTube RSS 서버는 채널과 무관하게 404/500을 자주 낸다 → 채널 페이지로 대신 읽는다
            try:
                vids, fallback = yt_listing.channel_videos(cid), True
                if not vids:
                    raise ValueError("빈 목록")
            except Exception as e2:
                log(f"[poll] {title}: 수집 실패 RSS {e} / 채널 페이지 {e2}")
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

        if fallback:
            vids = yt_listing.until_known(
                vids, lambda yid: bool(db.find_by_yt_id(yid) or db.in_queue(yid)))
        new_for_ch = 0
        resumed = _parse_ts(ch.get("resumed_at"))
        for v in vids:
            if db.find_by_yt_id(v["yt_id"]) or db.in_queue(v["yt_id"]):
                continue                          # 이미 전사됨 / 이미 큐에 있음
            # 자동수집을 껐다 다시 켰으면 켜기 전에 올라온 영상(꺼진 동안 쌓인 것)은
            # 처리하지 않는다(2026-11-01 사용자 지시). 필요하면 큐 팝업에서 직접 넣는다.
            pub = _parse_ts(v.get("published"))
            if resumed and pub and pub < resumed:
                log(f"[poll] {title}: 제외 [재개 전] {v['title'][:40]}")
                if not dry:
                    db.enqueue_video(v["yt_id"], v["url"], v["title"], cid,
                                     status="skipped", reason="수집 재개 전 영상")
                continue
            if is_ad_title(v["title"]):
                log(f"[poll] {title}: 제외 [ad] {v['title'][:40]}")
                if not dry:
                    db.enqueue_video(v["yt_id"], v["url"], v["title"], cid, status="skipped", reason="ad")
                continue
            cmin = ch["min_duration"] if ch["min_duration"] is not None else cm_base.MIN_DUR
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
