"""YouTube 주소 도우미. 원본 영상 찾기(검색 추정 2026-10-08, 설명문 링크 2026-11-10)는 모두 제거했다."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


def youtube_id(value: str | None) -> str:
    """Return a YouTube video id from a URL or bare id."""
    raw = (value or "").strip()
    if re.fullmatch(r"[\w-]{6,20}", raw):
        return raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0]
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return (parse_qs(parsed.query).get("v") or [""])[0]
        match = re.match(r"/(?:shorts|live|embed)/([\w-]+)", parsed.path)
        if match:
            return match.group(1)
    return ""


def canonical_url(video_id: str | None) -> str:
    vid = youtube_id(video_id)
    return f"https://www.youtube.com/watch?v={vid}" if vid else ""

