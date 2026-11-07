"""채널 /videos 페이지를 한국어 설정으로 읽는다(2026-11-07 사용자 지시).

쓰임 두 가지:
- RSS 대체: YouTube RSS 서버는 채널과 무관하게 404/500을 자주 낸다(같은 시각 구글 공식 채널도 실패).
- 한국어 표시: 제목·언어별 썸네일은 이 목록에만 실린다. 시청 페이지·yt-dlp·RSS는 원제와 기본 썸네일만 준다.
  한국어 제목에는 제작자가 단 것과 YouTube 자동 번역이 섞여 있고 구분되지 않는다. 최근 30개 영상만 보인다.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_CID_RE = re.compile(r"UC[\w-]{22}")
_AGO_RE = re.compile(r"(\d+)\s*(초|분|시간|일|주|개월|년)\s*전")
_UNIT = {"초": 1, "분": 60, "시간": 3600, "일": 86400, "주": 7 * 86400, "개월": 30 * 86400, "년": 365 * 86400}
_HANGUL_RE = re.compile(r"[가-힣]")


def channel_id_of(url: str) -> str:
    m = _CID_RE.search(url or "")
    return m.group(0) if m else ""


def _walk(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def _published(item: dict, now: datetime) -> str:
    """'6일 전' → 대략의 ISO 시각. 없으면 ""."""
    for part in _walk(item, "metadataParts"):
        for p in part or []:
            m = _AGO_RE.search(((p.get("text") or {}).get("content")) or "")
            if m:
                return (now - timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])).isoformat()
    return ""


def parse_videos(page: str, now: datetime | None = None) -> list[dict]:
    """채널 페이지 HTML → [{yt_id,title,url,published,thumb}] (최신순). thumb은 한국어 썸네일이 있을 때만."""
    i = page.find("ytInitialData")
    if i < 0:
        return []
    data, _ = json.JSONDecoder().raw_decode(page[page.find("{", i):])
    now = now or datetime.now(timezone.utc)
    out = []
    for v in _walk(data, "lockupViewModel"):
        vid = v.get("contentId") or ""
        meta = (v.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        title = (meta.get("title") or {}).get("content") or ""
        if not vid or not title:
            continue
        sources = next(_walk(v, "sources"), None) or [{}]
        thumb = sources[-1].get("url") or ""
        out.append({"yt_id": vid, "title": title, "url": f"https://www.youtube.com/watch?v={vid}",
                    "published": _published(meta, now),
                    "thumb": thumb if "/vi_lc/" in thumb and "_ko." in thumb else ""})
    return out


def channel_videos(channel_id: str, timeout: int = 20) -> list[dict]:
    if not _CID_RE.fullmatch(channel_id or ""):
        return []
    req = urllib.request.Request(f"https://www.youtube.com/channel/{channel_id}/videos",
                                 headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_videos(r.read().decode("utf-8", "replace"))


def korean_entries(channel_id: str, timeout: int = 20) -> dict[str, dict]:
    """{yt_id: 항목}. 실패하면 빈 사전 — 표시 보강이라 본 처리를 막으면 안 된다."""
    try:
        return {v["yt_id"]: v for v in channel_videos(channel_id, timeout)}
    except Exception:  # noqa: BLE001
        return {}


def korean_title(entry: dict | None, original: str = "") -> str:
    """한글이 있고 원제와 다른 제목만 돌려준다."""
    title = ((entry or {}).get("title") or "").strip()
    return title if _HANGUL_RE.search(title) and title != (original or "").strip() else ""


def until_known(videos: list[dict], is_known) -> list[dict]:
    """최신순 목록에서 이미 아는 영상 앞까지만. 아는 영상이 없으면 빈 목록.

    /videos는 RSS(15개)보다 길어(30개) 그대로 쓰면 수집 전 옛 영상이 새 영상으로 들어온다."""
    for n, v in enumerate(videos):
        if is_known(v["yt_id"]):
            return videos[:n]
    return []
