"""한눈 요약을 본문(핵심 내용)에서 다시 뽑는다 — 소제목 하나당 불릿 하나(2026-11-02 사용자 지시).

요약 한 번에 위에서부터 쓰면 한눈 요약이 본문보다 먼저 나와 본문과 어긋날 수 있다. 그래서 요약을
받은 뒤 '## 3. 핵심 내용'만 근거로 모델을 한 번 더 불러 '## 2. 한눈 요약'을 바꿔 끼운다.
LLM 호출은 app이 한다(이 모듈은 프롬프트·검증·교체만). 무엇이든 어긋나면 None — 호출한 쪽은
첫 요약의 한눈 요약을 그대로 둔다(본업인 요약 저장을 막지 않는다).
"""
from __future__ import annotations

import re

BRIEF_HEAD_RE = re.compile(r"^##[ \t]+2\.[ \t]*한눈 요약[ \t]*$", re.M)
BODY_HEAD_RE = re.compile(r"^##[ \t]+3\.[ \t]*핵심 내용[ \t]*$", re.M)
NEXT_H2_RE = re.compile(r"^##[ \t]+\S", re.M)
SUBHEAD_RE = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.M)
_TS_RE = re.compile(r"\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*$")
_NOISE_RE = re.compile(r"<!--[\s\S]*?-->|<div class=\"(?:kf-strip|term-notes)\">[\s\S]*?</div>")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]+(.+?)[ \t]*$")

PROMPT = """아래는 영상 요약의 '핵심 내용' 본문이다. 이 본문만 근거로 '한눈 요약' 불릿을 쓴다.

규칙:
- 소제목(###) 하나당 불릿 하나. 아래 소제목 목록 순서대로 정확히 {n}개를 쓴다. 소제목을 합치거나 쪼개거나 건너뛰지 않는다.
- 불릿 하나는 그 섹션의 결론·주장을 한두 문장으로 압축한다. 소제목 문구를 되풀이하지 말고, 소제목만 읽어서는 알 수 없는 핵심 수치·근거·이유를 담는다.
- 본문에 없는 내용·평가를 보태지 않는다. 수치·고유명사 표기는 본문 그대로 쓴다.
- 각 불릿에서 가장 중요한 구절 하나만 **볼드**로 표시한다.
- 타임스탬프, 용어 각주 표식(\\*), "섹션 1:" 같은 머리표를 넣지 않는다.
- 문장은 '~다'로 끝낸다. 화자가 한 명이면 "그는"·"영상은"·"진행자는" 같은 전달 표현 없이 내용을 직접 쓴다. 여러 사람이 말하는 영상이면 발언자가 바뀌는 불릿 첫머리에서만 밝힌다.
- 출력은 `- `로 시작하는 불릿 {n}줄뿐이다. 머리말·맺음말·빈 설명·코드블록을 쓰지 않는다.

[소제목 목록]
{heads}

[핵심 내용]
{body}
"""


def _span(body: str):
    """(한눈 요약 내용 시작, 끝, 핵심 내용 본문) — 구조가 다르면 None."""
    brief = BRIEF_HEAD_RE.search(body or "")
    main = BODY_HEAD_RE.search(body or "")
    if not brief or not main or main.start() < brief.end():
        return None
    nxt = NEXT_H2_RE.search(body, brief.end())
    end = nxt.start() if nxt else len(body)
    after = NEXT_H2_RE.search(body, main.end())
    return brief.end(), end, body[main.end():after.start() if after else len(body)]


def headings(body: str) -> list[str]:
    """핵심 내용의 소제목(타임스탬프 뗀 것)."""
    span = _span(body)
    if not span:
        return []
    return [_TS_RE.sub("", h).strip() for h in SUBHEAD_RE.findall(span[2])]


def build_prompt(body: str) -> tuple[str, int] | None:
    """(프롬프트, 소제목 수). 한눈 요약·핵심 내용·소제목이 없으면 None."""
    span = _span(body)
    heads = headings(body)
    if not span or not heads:
        return None
    main = _NOISE_RE.sub("", span[2])
    main = re.sub(r"\n{3,}", "\n\n", main).strip()
    listing = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(heads))
    return PROMPT.format(n=len(heads), heads=listing, body=main), len(heads)


def parse(text: str, n: int) -> list[str] | None:
    """모델 출력 → 불릿 n개. 개수가 다르면 None."""
    items = []
    for line in (text or "").splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        item = _TS_RE.sub("", m.group(1)).replace("\\*", "").strip()
        if item:
            items.append(item)
    return items if len(items) == n else None


def replace(body: str, bullets: list[str]) -> str:
    """'## 2. 한눈 요약' 아래를 불릿으로 바꾼다(머리글과 다음 섹션은 그대로)."""
    span = _span(body)
    if not span or not bullets:
        return body
    start, end, _ = span
    return body[:start] + "\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n\n" + body[end:]
