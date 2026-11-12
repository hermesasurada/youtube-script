"""요약 생성 정책 — 짧은 영상은 요약하지 않는다(2026-11-12 사용자 지시 "yt 영상 6분 이하는 요약생성 금지").

서버 `/summarize`(화면 요약 버튼·채널 모니터·일괄 재요약 모두 이 경로)와 채널 모니터가 같은 규칙을 쓴다.
기준은 전사 md 프론트매터의 `duration`(원본 영상 길이). 구간 전사(`clip_start`가 있는 md)는 duration이
잘라 낸 구간 길이라 원본 길이를 알 수 없고, 긴 영상의 일부를 사용자가 골라 전사한 것이므로 막지 않는다.
길이를 모르면(0·누락) 막지 않는다. 이미 만들어진 요약은 건드리지 않는다.
비상 조정: 환경변수 `YT_SUMMARY_MIN_SECONDS`(초, 기본 360). 0이면 규칙을 끈다.
"""
from __future__ import annotations

import os

SUMMARY_MIN_SECONDS = int(os.environ.get("YT_SUMMARY_MIN_SECONDS", "360"))
SKIP_MARK = "요약 생략(짧은 영상)"   # 채널 모니터가 이 문구로 '건너뜀'을 알아본다


def _fmt(secs: float) -> str:
    m, s = divmod(int(round(secs)), 60)
    return f"{m}분 {s}초" if s else f"{m}분"


def summary_skip_reason(meta: dict | None) -> str | None:
    """요약하지 않을 영상이면 사유 문자열, 요약해도 되면 None."""
    if SUMMARY_MIN_SECONDS <= 0 or not isinstance(meta, dict):
        return None
    if meta.get("clip_start") not in (None, "", 0, "0"):
        return None
    try:
        dur = float(meta.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    if 0 < dur <= SUMMARY_MIN_SECONDS:
        return (f"{SKIP_MARK}: 영상 길이 {_fmt(dur)} — {_fmt(SUMMARY_MIN_SECONDS)} 이하 영상은 요약하지 않습니다")
    return None
