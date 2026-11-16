"""캡처 대상 고르기 — 요약 소제목과 전사 타임스탬프에서 '화면을 봐야 하는 구간'을 찾고, 오프닝(광고·예고)을 뺀다.

영상 파일을 건드리지 않는다(글만 본다). 진입점은 `parse_summary`·`_visual_capture_targets`·`trim_sequoia_opening`
(2026-11-17 keyframe_report에서 분리).
"""
from __future__ import annotations

import os
import re
import document_io

import kf_base


_SEQUOIA_CHANNELS = {"sequoia capital"}
_TRANSCRIPT_TS_RE = re.compile(r"\[(\d{1,2}):\d{2}(?::\d{2})?\]")
_VISUAL_CUE_RE = re.compile(
    r"(?:"
    r"화면\s*(?:을|에|에서|으로)?\s*(?:보|나오|띄우|보여|공개)"
    r"|(?:여기|이걸|이것을|지금)\s*(?:보면|보시면)"
    r"|슬라이드|차트|그래프|도표|표(?:를|에서|가)"
    r"|인포그래픽|다이어그램|구조도|스크린샷|화면\s*공유"
    r"|사진|이미지|지도를|지도에서|데모|시연|실물"
    r"|\b(?:slide|chart|graph|table|diagram|screenshot|infographic|dashboard)\b"
    r"|\b(?:look at|as you can see|shown on (?:the )?screen|demo)\b"
    r")",
    re.I,
)
_SEQUOIA_INTERVIEW_INTRO_RE = re.compile(
    r"(?:\bwe(?:'re| are) here\b"
    r"|\bwelcome\b.{0,80}\b(?:to the show|to training data)\b"
    r"|\btoday we(?:'re| are) delighted to have\b"
    r"|\b(?:delighted|thrilled) to have you here\b"
    r"|\bi(?:'m| am) delighted to have\b)",
    re.I | re.S,
)


# ── 요약 파싱: 제목/URL/본문/소제목(##,###) ──────────────────────────

def parse_summary(md_path: str) -> dict:
    text = open(md_path, encoding="utf-8", errors="replace").read()
    title = (re.search(r"^#\s+(.+)$", text, re.M) or [None, os.path.basename(md_path)])
    title = title.group(1).strip() if hasattr(title, "group") else title[1]
    m = re.search(r"\|\s*URL\s*\|\s*(\S+)\s*\|", text)
    url = m.group(1).strip() if m else ""
    headings = []
    for mm in re.finditer(r"^(#{2,3})\s+(.+)$", text, re.M):
        htext = mm.group(2).strip()
        headings.append({"idx": len(headings), "level": len(mm.group(1)),
                         "text": htext, "start": _parse_ts_label(htext)})
    return {"title": title, "url": url, "markdown": text, "headings": headings}


def _parse_ts_label(text: str) -> float | None:
    """소제목의 [mm:ss] 또는 [h:mm:ss] 라벨(앞/뒤 무관) → 시작 초. 없으면 None."""
    m = re.search(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]", text)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    return (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))


def _paired_transcript_path(summary_path: str) -> str | None:
    marker = os.sep + "summary" + os.sep
    if marker not in summary_path:
        return None
    candidate = summary_path.replace(marker, os.sep, 1)
    return candidate if os.path.isfile(candidate) else None


def _timestamped_segments(body: str, *, max_seconds: int | None = 150) -> list[tuple[float, str]]:
    matches = list(_TRANSCRIPT_TS_RE.finditer(body))
    segments: list[tuple[float, str]] = []
    for idx, match in enumerate(matches):
        ts = _parse_ts_label(match.group(0))
        if ts is None or (max_seconds is not None and ts > max_seconds):
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        if segments and segments[-1][0] == ts:
            segments[-1] = (ts, segments[-1][1] + " " + body[match.end():end])
        else:
            segments.append((ts, body[match.end():end]))
    return segments


def _assignable_headings(headings: list[dict]) -> list[dict]:
    """캡처가 실제로 배치될 본문 소제목만 반환한다.

    기존 테스트/구형 문서처럼 level 정보가 없으면 전달받은 목록 전체를 사용한다.
    """
    content = [h for h in headings if h.get("level") == 3]
    return content or headings


def _section_for_timestamp(ts: float, headings: list[dict]) -> int | None:
    timed = sorted(
        ((h["idx"], h["start"]) for h in _assignable_headings(headings)
         if h.get("start") is not None),
        key=lambda x: x[1],
    )
    if not timed:
        return None
    section = timed[0][0]
    for idx, start in timed:
        if start <= ts:
            section = idx
        else:
            break
    return section


def _visual_capture_targets(body: str, headings: list[dict],
                            *, max_targets: int = kf_base.MAX_VISUAL_TARGETS) -> list[float]:
    """타임스탬프 전사에서 화면 자료를 직접 가리키는 시점을 고른다.

    소제목별 최고 단서 하나를 먼저 확보한 뒤 남는 예산에 다른 강한 단서를 채워,
    긴 영상 앞부분에 목표 시점이 몰리지 않도록 한다. 장면전환/균등 후보는 별도로
    계속 유지되므로 명시적 시각 단서가 없는 화면도 기존 방식으로 포착된다.
    """
    if max_targets <= 0:
        return []
    candidates: list[tuple[float, int]] = []
    for ts, text in _timestamped_segments(body, max_seconds=None):
        hits = _VISUAL_CUE_RE.findall(text)
        if hits:
            candidates.append((ts, len(hits)))
    if not candidates:
        return []

    timed_headings = [h for h in _assignable_headings(headings) if h.get("start") is not None]
    primary: list[tuple[float, int]] = []
    if timed_headings:
        best_by_section: dict[int, tuple[float, int]] = {}
        for item in candidates:
            section = _section_for_timestamp(item[0], headings)
            if section is None:
                continue
            previous = best_by_section.get(section)
            if previous is None or item[1] > previous[1]:
                best_by_section[section] = item
        primary = sorted(best_by_section.values())

    if len(primary) >= max_targets:
        step = len(primary) / max_targets
        selected = [primary[int(i * step)] for i in range(max_targets)]
    elif primary:
        selected = list(primary)
        selected_times = {ts for ts, _ in selected}
        extras = [
            item for item in sorted(candidates, key=lambda x: (-x[1], x[0]))
            if item[0] not in selected_times
        ]
        selected.extend(extras[:max_targets - len(selected)])
    else:
        ordered = sorted(candidates)
        if len(ordered) > max_targets:
            step = len(ordered) / max_targets
            selected = [ordered[int(i * step)] for i in range(max_targets)]
        else:
            selected = ordered
    selected.sort()
    return [ts for ts, _ in selected]


def _context_for_timestamp(ts: float, segments: list[tuple[float, str]],
                           *, window: float = 15, max_chars: int = 360) -> str:
    nearby = [(seg_ts, text) for seg_ts, text in segments if abs(seg_ts - ts) <= window]
    if not nearby and segments:
        nearby = [min(segments, key=lambda item: abs(item[0] - ts))]
    context = " ".join(
        f"[{kf_base._hms(seg_ts)}] {re.sub(r'\s+', ' ', text).strip()}" for seg_ts, text in nearby
    ).strip()
    return context[:max_chars]


def _sequoia_main_intro_time(meta: dict, body: str) -> float | None:
    """Locate the main-interview intro after Sequoia's cold open/title sequence."""
    channel = str(meta.get("uploader") or meta.get("channel") or "").strip().casefold()
    if channel not in _SEQUOIA_CHANNELS:
        return None
    segments = _timestamped_segments(body)
    for idx, (intro_ts, text) in enumerate(segments):
        if idx == 0 or not _SEQUOIA_INTERVIEW_INTRO_RE.search(text):
            continue
        return intro_ts
    return None


def _sequoia_opening_window(meta: dict, body: str) -> tuple[float, float] | None:
    """Return the full disposable opening window, including cold open and title animation."""
    intro_ts = _sequoia_main_intro_time(meta, body)
    if intro_ts is None:
        return None
    # The extra visual tail avoids retaining the final title-animation frames while
    # preserving the transcript from the exact main-interview introduction.
    return 0.0, intro_ts + 5.0


def trim_sequoia_opening(meta: dict, body: str) -> tuple[str, float | None]:
    """Remove Sequoia's disposable opening from LLM input; leave stored transcript intact."""
    intro_ts = _sequoia_main_intro_time(meta, body)
    if intro_ts is None:
        return body, None
    for match in _TRANSCRIPT_TS_RE.finditer(body):
        if _parse_ts_label(match.group(0)) == intro_ts:
            return body[match.start():].lstrip(), intro_ts
    return body, None


def _opening_exclusion_for_summary(summary_path: str) -> tuple[float, float] | None:
    transcript_path = _paired_transcript_path(summary_path)
    if not transcript_path:
        return None
    try:
        meta, body = document_io.read_markdown(transcript_path)
    except OSError:
        return None
    return _sequoia_opening_window(meta, body)


def _exclude_opening_candidates(
    frames: list[tuple[float, str]], window: tuple[float, float] | None
) -> list[tuple[float, str]]:
    if not window:
        return frames
    start, end = window
    return [(ts, path) for ts, path in frames if not start <= ts <= end]
