"""요약 마크다운의 한글 AI 티를 저장 전에 걷어낸다.

im-not-ai(https://github.com/epoko77-ai/im-not-ai) quick-rules 중, 샘플 요약
두 편에서 효과가 확인된 결정적 규칙만 코드로 옮긴다. LLM 콜 없음.

적용:
  C-11  연결어미(-고/-며/-지만/…) 직후 쉼표
  I-3   문장 끝 '~다는 것이다' / '~이라는 것이다' / '~라는 것이다'
  A-16  '가 그것이다' 대명사 결말
  D-4   '전례 없던/없는' hype

youtube-script 장르 계약은 건드리지 않는다 — 한눈 요약 불릿, 소제목 타임스탬프,
핵심 볼드, 주장 주체('영상은 ~라고 본다'), 수치·직접 인용·HTML 키프레임.
"""
from __future__ import annotations

import os
import re

_CONNECTIVE_COMMA = re.compile(
    r"(고|며|지만|면서|아서|어서|으며|다며|이고|이며),"
)
_GEOTIDA = re.compile(r"(이라는 것이다|다는 것이다|라는 것이다)")
_GEOTIGEOT = re.compile(r"가 그것이다")
_HYPE_JEONRYE = re.compile(r"전례 없(던|는)")

# HTML 주석·키프레임·태그, 코드, URL. 직접 인용은 별도로 지킨다.
_PROTECT = re.compile(
    r"(<!--[\s\S]*?-->"
    r"|<div class=\"kf-strip\">[\s\S]*?</div>"
    r"|<[^>]+>"
    r"|```[\s\S]*?```"
    r"|`[^`]+`"
    r"|https?://[^\s)>\]]+"
    r"|“[^”]*”"
    r"|\"[^\"]*\""
    r")"
)

_HANGUL_RE = re.compile(r"[가-힣]")


def _enabled() -> bool:
    return os.environ.get("HUMANIZE_SUMMARY", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _stash_protected(text: str) -> tuple[str, list[str]]:
    held: list[str] = []

    def _hold(m: re.Match) -> str:
        held.append(m.group(0))
        return f"\x02H{len(held) - 1}\x02"

    return _PROTECT.sub(_hold, text), held


def _unstash(text: str, held: list[str]) -> str:
    if not held:
        return text
    return re.sub(r"\x02H(\d+)\x02", lambda m: held[int(m.group(1))], text)


def _drop_connective_comma(text: str) -> str:
    """C-11. '그리고,' '하지만,' 접속부사는 연결어미가 아니라서 쉼표를 둔다."""

    def _repl(m: re.Match) -> str:
        start = m.start()
        ending = m.group(1)
        if ending == "고" and text[max(0, start - 2):start] == "그리":
            return m.group(0)
        if ending == "지만" and start >= 1 and text[start - 1] == "하":
            prev = text[start - 2] if start >= 2 else ""
            if not ("가" <= prev <= "힣"):
                return m.group(0)
        return ending

    return _CONNECTIVE_COMMA.sub(_repl, text)


def _drop_geotida(text: str) -> str:
    """I-3. '생긴다는 것이다' → '생긴다', '지금이라는 것이다' → '지금이다'."""

    def _repl(m: re.Match) -> str:
        s = m.group(1)
        if s == "이라는 것이다":
            return "이다"
        return "다"

    text = _GEOTIDA.sub(_repl, text)
    # 볼드가 어미를 쪼갠 경우: '된다**는 것이다' → '된다**'
    return re.sub(r"다\*\*는 것이다", "다**", text)


def _drop_hype(text: str) -> str:
    return _HYPE_JEONRYE.sub(r"이전에는 없\1", text)


def humanize_summary(text: str) -> str:
    """요약 본문에 im-not-ai 결정적 규칙을 적용한다. 입력이 비었거나 끄면 그대로."""
    if not text or not _enabled() or not _HANGUL_RE.search(text):
        return text
    work, held = _stash_protected(text)
    work = _drop_connective_comma(work)
    work = _drop_geotida(work)
    work = _GEOTIGEOT.sub("다", work)
    work = _drop_hype(work)
    return _unstash(work, held)
