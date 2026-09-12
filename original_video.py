"""Translated/reposted YouTube video detection and original-candidate ranking."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


_TRANSLATION_RE = re.compile(
    r"(?:전체\s*번역|한글\s*번역|한국어\s*(?:번역|자막)|번역(?:본|했습니다|해드립니다)?|"
    r"korean\s+(?:translation|subtitles?))",
    re.I,
)
_ORIGINAL_LABEL_RE = re.compile(r"(?:원본(?:\s*영상)?|original(?:\s+video)?|source|출처)", re.I)
_YT_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s<>'\"]+|shorts/[\w-]+|live/[\w-]+)|"
    r"youtu\.be/[\w-]+)(?:[^\s<>'\"]*)?",
    re.I,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_STOPWORDS = {
    "a", "an", "and", "at", "by", "for", "from", "full", "in", "interview",
    "of", "on", "or", "the", "to", "video", "with",
}


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


def looks_like_translation(title: str = "", uploader: str = "", description: str = "") -> bool:
    """Detect Korean translation/repost videos without relying on one channel name."""
    haystack = "\n".join((title or "", uploader or "", description or ""))
    return bool(re.search(r"[가-힣]", haystack) and _TRANSLATION_RE.search(haystack))


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
    return {"id": vid, "url": url, "title": "", "uploader": "", "method": "explicit"}


def _tokens(value: str) -> set[str]:
    return {
        token.lower() for token in _TOKEN_RE.findall(value or "")
        if len(token) >= 2 and token.lower() not in _STOPWORDS
    }


def select_candidate(
    query: str,
    candidates: list[dict],
    *,
    current_video_id: str = "",
    current_uploader: str = "",
    current_duration: float = 0,
) -> dict[str, str] | None:
    """Choose a plausible original; ambiguous low-signal searches return None."""
    query_tokens = _tokens(query)
    if len(query_tokens) < 2:
        return None
    current_id = youtube_id(current_video_id)
    current_channel = (current_uploader or "").strip().casefold()
    ranked: list[tuple[float, int, dict]] = []
    for rank, candidate in enumerate(candidates or []):
        vid = youtube_id(str(candidate.get("id") or candidate.get("url") or ""))
        if not vid or vid == current_id:
            continue
        title = str(candidate.get("title") or "").strip()
        uploader = str(candidate.get("uploader") or candidate.get("channel") or "").strip()
        if current_channel and uploader.casefold() == current_channel:
            continue
        overlap = len(query_tokens & _tokens(title))
        if overlap < 2:
            continue
        score = min(overlap, 5) * 1.6 + max(0.0, 2.0 - rank * 0.25)
        duration = float(candidate.get("duration") or 0)
        if current_duration > 0 and duration > 0:
            ratio = duration / current_duration
            if 0.9 <= ratio <= 4.0:
                score += 3.0
            elif 0.65 <= ratio < 0.9:
                score += 1.0
            else:
                score -= 2.0
        if uploader:
            score += 0.5
        ranked.append((score, rank, candidate | {"id": vid, "title": title, "uploader": uploader}))
    if not ranked:
        return None
    score, _, best = max(ranked, key=lambda row: (row[0], -row[1]))
    if score < 6.0:
        return None
    return {
        "id": best["id"],
        "url": canonical_url(best["id"]),
        "title": best["title"],
        "uploader": best["uploader"],
        "method": "search",
    }
