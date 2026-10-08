"""번역·재게시 영상 설명문에서 원본 YouTube 링크를 읽는다(검색 추정은 2026-10-08 제거)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


_ORIGINAL_LABEL_RE = re.compile(r"(?:원본(?:\s*영상)?|original(?:\s+video)?|source|출처)", re.I)
_YT_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s<>'\"]+|shorts/[\w-]+|live/[\w-]+)|"
    r"youtu\.be/[\w-]+)(?:[^\s<>'\"]*)?",
    re.I,
)


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


def explicit_original(description: str, current_video_id: str = "") -> dict[str, str] | None:
    """Read an explicitly labelled source link, or the sole external YouTube link."""
    current = youtube_id(current_video_id)
    labelled: list[tuple[str, str]] = []
    unlabelled: list[tuple[str, str]] = []
    for line in (description or "").splitlines():
        for match in _YT_URL_RE.finditer(line):
            url = match.group(0).rstrip(".,);]}")
            vid = youtube_id(url)
            if not vid or vid == current:
                continue
            pair = (vid, canonical_url(vid))
            (labelled if _ORIGINAL_LABEL_RE.search(line) else unlabelled).append(pair)
    pool = labelled or (unlabelled if len({vid for vid, _ in unlabelled}) == 1 else [])
    if not pool:
        return None
    vid, url = pool[0]
    return {"id": vid, "url": url, "title": "", "uploader": "", "method": "explicit",
            "confident": True}
