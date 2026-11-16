"""번역 결과 검사 — 반복 붕괴(같은 줄 되풀이)와 한자·가나 누출을 글만 보고 잡아낸다.

LLM을 부르지 않는 순수 함수만 둔다: 붕괴 판정(`_looks_degenerate`)·되풀이 접기(`_collapse_repeats`),
누출 위치 찾기(`_leak_spans`·`han_leaks`), 교정본이 누출 낱말만 바꿨는지 대조(`_only_leaks_replaced`).
교정을 모델에 다시 시키는 일(`_repair_leaks`)은 transcript_translator에 있다
(2026-11-17 transcript_translator에서 분리).
"""
from __future__ import annotations

import re


_HANGUL_RE = re.compile(r"[가-힣]")
_TS_RE     = re.compile(r"^\[\d+:\d+(?::\d+)?\]")


REPEAT_MIN_LINE = 15      # 이보다 짧은 줄("네.", "맞아요.")은 원래 반복될 수 있다
REPEAT_LOOP = 5           # 같은 긴 줄이 이만큼 나오면 반복 루프


def _repeat_count(text: str) -> int:
    """같은 줄(앞뒤 공백·타임스탬프 제외, REPEAT_MIN_LINE자 이상)이 가장 많이 나온 횟수."""
    counts: dict[str, int] = {}
    for line in (text or "").splitlines():
        key = _TS_RE.sub("", line.strip()).strip()
        if len(key) >= REPEAT_MIN_LINE:
            counts[key] = counts.get(key, 0) + 1
    return max(counts.values(), default=0)


def _looks_degenerate(out: str, chunk: str, finish: str) -> bool:
    """같은 말을 max_tokens까지 반복하는 폭주를 걸러낸다.

    번역문은 원문과 분량이 비슷해야 하므로, 출력이 원문의 2.5배를 넘거나
    한 줄이 비정상적으로 길면 정상 번역이 아니다(실제로 'happened happened…'가
    5만 자 넘게 생성된 적이 있다). 분량 기준 아래에서도 같은 긴 줄이 되풀이되며
    뒷부분 내용과 타임스탬프를 잃는 루프가 있어(2026-09-26 A/B: 4천 자 청크에서
    '대규모로 컴퓨터를 구축해야 하고…' 164줄) 줄 반복도 본다. 원문 자체가 반복된
    경우(whisper 반복)는 번역이 그대로 따라간 것이라 폭주가 아니다.
    """
    if len(out) > len(chunk) * 2.5:
        return True
    if finish == "length" and len(out) > len(chunk) * 1.6:
        return True
    if _repeat_count(out) >= REPEAT_LOOP and _repeat_count(chunk) < 3:
        return True
    longest = max((len(l) for l in out.splitlines()), default=0)
    return longest > 3000


def _collapse_repeats(text: str) -> str:
    """연속으로 되풀이된 같은 긴 줄을 한 줄로 줄인다(재시도도 루프일 때의 마지막 정리)."""
    kept: list[str] = []
    prev = None
    for line in text.splitlines():
        key = _TS_RE.sub("", line.strip()).strip()
        if len(key) >= REPEAT_MIN_LINE and key == prev:
            continue
        kept.append(line)
        prev = key
    return "\n".join(kept)


# 번역문에 섞인 중국어·일본어 문자. 운영 번역본 조사(2026-09-26): 최근 60편 581청크 중
# 132청크에 '那里的'·'顺便说一下'·'铺设'처럼 모델이 중국어로 미끄러진 조각이 있었다.
_HAN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _leak_spans(text: str, source: str = "") -> list[tuple[int, int]]:
    """번역문에 새로 생긴 한자·가나 조각의 위치.

    정상으로 보는 것 — 원문에 이미 있던 조각(중국어 인명·지명 병기), 한글 뒤 괄호
    병기(반(反)), 한글 사이에 끼지 않은 한 글자(前 CEO·對중국). 두 글자 이상이거나
    한 글자라도 한글 사이에 끼면(민粹주의) 누출이다(app._script_leaks와 같은 기준).
    """
    spans: list[tuple[int, int]] = []
    for m in _HAN_RE.finditer(text or ""):
        run, a, b = m.group(0), m.start(), m.end()
        if run in (source or ""):
            continue
        before = text[a - 1] if a > 0 else ""
        before2 = text[a - 2] if a > 1 else ""
        after = text[b] if b < len(text) else ""
        if before == "(" and _HANGUL_RE.match(before2 or " "):
            continue
        if len(run) == 1 and not (_HANGUL_RE.match(before or " ") and _HANGUL_RE.match(after or " ")):
            continue
        spans.append((a, b))
    return spans


def han_leaks(text: str, source: str = "") -> list[str]:
    """번역문에 새로 생긴 한자·가나 조각(`_leak_spans` 기준)."""
    return [text[a:b] for a, b in _leak_spans(text, source)]


def _only_leaks_replaced(orig: str, new: str, spans: list[tuple[int, int]]) -> bool:
    """교정본이 감지한 한자 구간만 바꿨는지 — 그 밖의 글자는 한 글자도 달라지면 안 된다.

    교정 모델이 숫자·영문 이름·연도를 바꿔도(Boeing 2004 → Airbus 2024) 한자만 없으면
    통과하던 구멍을 막는다(Astra 검토, 2026-09-26). 누출 구간과 그 뒤에 붙은 한글 어미·괄호
    풀이, 경계의 띄어쓰기만 바뀔 수 있고, 대체어는 한자가 없어야 하며 원래 조각 길이에
    비례해야 한다. 대체어가 뜻을 제대로 옮겼는지(증가 → 감소)는 코드로 판정하지 못한다.
    """
    orig, new = orig.rstrip(), new.rstrip()
    if not spans:
        return orig == new
    # 한자 뒤에 붙은 한글 어미(噬菌체·关心的하는·精简된)는 대체어와 함께 다듬어질 수 있다.
    # 앞쪽은 넓히지 않는다 — 앞에 붙은 말은 대개 별개 낱말(10억 달러增加 → '달러')이라
    # 넓히면 단위·명사가 바뀌어도 통과한다. 예외는 가나 — 외래어 음차가 한글 음절과
    # 쪼개져 섞인다(세クター → 섹터). 숫자·영문·문장부호는 넓히지 않는다.
    grown: list[list[int]] = []                # [시작, 끝]
    for a, b in spans:
        if re.fullmatch(r"[\u3040-\u30ff]+", orig[a:b]):
            while a > 0 and _HANGUL_RE.match(orig[a - 1]):
                a -= 1
        while b < len(orig) and _HANGUL_RE.match(orig[b]):
            b += 1
        gloss = re.match(r"\([가-힣 ]+\)", orig[b:])       # 现在我们(지금 우리는)의 괄호 풀이
        if gloss:
            b += gloss.end()
        if grown and a <= grown[-1][1]:
            grown[-1][1] = max(b, grown[-1][1])
        else:
            grown.append([a, b])
    parts, pos = [], 0
    for a, b in grown:
        keep = orig[pos:a]
        parts.append(re.escape(keep.rstrip()) + r"\s*" if keep.strip() else r"\s*")
        limit = max(8, (b - a) * 3)
        parts.append(r"([^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{0,%d}?)" % limit)
        pos = b
    tail = orig[pos:]
    parts.append(r"\s*" + re.escape(tail.lstrip()) if tail.strip() else r"\s*")
    m = re.fullmatch("".join(parts), new, re.S)
    if m is None:
        return False
    # 대체어에는 누출 부분을 옮긴 한국어가 실제로 있어야 한다 — 구간을 지우기만 하면
    # (10억 달러增加했다 → 10억 달러.) 원문 밖은 그대로여도 뜻이 사라진다(Astra 재검토).
    # 그래서 대체어의 한글 수가 그 구간에 원래 붙어 있던 한글(괄호 풀이 제외)보다 많아야 한다.
    def hangul(t: str) -> int:
        return len(_HANGUL_RE.findall(t))
    # 대체어가 원래 구간에 없던 숫자·영문·문장부호를 새로 들여오면(增加했다 → 2배 증가했다,
    # 증가했다. 추가 문장) 사실이나 문장이 끼어든 것이라 버린다.
    def facts(t: str) -> set[str]:
        return set(re.findall(r"[0-9]+|[A-Za-z]+|[.?!。]", t))   # 문장부호: 문장 덧붙이기 차단
    def ok(g: str, a: int, b: int) -> bool:
        if not facts(g) <= facts(orig[a:b]):
            return False
        base = hangul(re.sub(r"\([가-힣 ]+\)", "", orig[a:b]))
        # 한 글자도 의미가 있을 수 있다(자산再평가). 잡음으로 추정해 삭제하지 않는다.
        return hangul(g) > base
    return all(ok(g, a, b) for g, (a, b) in zip(m.groups(), grown))
