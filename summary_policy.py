"""한눈 요약 정책 — 6분 이하 영상은 한눈 요약(`## 2. 한눈 요약`)을 만들지 않는다.

2026-11-12 사용자 지시 "yt 영상 6분 이하는 요약생성 금지"는 **한눈 요약 생성 금지**라는 뜻이었다(2026-11-16 사용자 정정 —
"한눈요약을 안만든다고 한거지 전사요약 자체를 하지 않는다는게 아닌데"). 11-12~11-16 사이에는 요약 자체를 막아
짧은 영상이 전사만 남았다. 이제 짧은 영상도 본 요약·캡처는 평소대로 만들고, 본문을 다 쓴 뒤 따로 부르는
한눈 요약 재구성(`yt_summary._rebuild_brief`)만 건너뛰어 빈 머리글을 걷어 낸다.

기준은 전사 md 프론트매터의 `duration`(원본 영상 길이). 구간 전사(`clip_start`가 있는 md)는 duration이 잘라 낸
구간 길이라 원본 길이를 알 수 없어 규칙을 적용하지 않는다. 길이를 모르면(0·누락) 한눈 요약을 만든다.
비상 조정: 환경변수 `YT_BRIEF_MIN_SECONDS`(초, 기본 360). 0이면 규칙을 끈다(모든 영상에 한눈 요약).
"""
from __future__ import annotations

import os

BRIEF_MIN_SECONDS = int(os.environ.get("YT_BRIEF_MIN_SECONDS", "360"))


def skip_brief(meta: dict | None) -> bool:
    """한눈 요약을 만들지 않을 짧은 영상이면 True."""
    if BRIEF_MIN_SECONDS <= 0 or not isinstance(meta, dict):
        return False
    if meta.get("clip_start") not in (None, "", 0, "0"):
        return False
    try:
        dur = float(meta.get("duration") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < dur <= BRIEF_MIN_SECONDS
