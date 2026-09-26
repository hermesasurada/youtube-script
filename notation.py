"""회사·브랜드명 원문 표기와 영문 이름 뒤 조사를 결정적으로 맞춘다(2026-09-26 사용자 지시).

요약·제목 번역·전사 번역 모두 '회사·브랜드명은 원문 표기'(A안)로 통일했다. 프롬프트에
적어 둬도 모델이 '엔비디아'처럼 흔한 한글 표기로 돌아가는 일이 잦아(요약 66건 중
17건) 저장 직전에 한 번 더 바꾼다. 한글 표기와 원문의 끝소리가 같은 이름만 넣어서
뒤에 붙은 조사(엔비디아가 → NVIDIA가)는 그대로 맞는다. LLM 호출 없음.

영문 이름 뒤 조사는 모델이 철자만 보고 틀리게 붙이는 일이 있어(Anthropic가, Anthropic와)
발음 끝소리를 아는 이름만 고친다.
"""
from __future__ import annotations

import re

# 한글 표기 → 원문. 끝소리가 같아야 한다(조사를 건드리지 않으려고).
# '메타'는 메타데이터·메타 분석과 겹쳐서 넣지 않는다.
BRANDS_KO_TO_EN = {
    "엔비디아": "NVIDIA", "구글": "Google", "테슬라": "Tesla", "애플": "Apple",
    "아마존": "Amazon", "마이크로소프트": "Microsoft", "유튜브": "YouTube",
    "오픈AI": "OpenAI", "오픈에이아이": "OpenAI", "앤트로픽": "Anthropic",
    "앤스로픽": "Anthropic", "스페이스X": "SpaceX", "스페이스엑스": "SpaceX",
    "넷플릭스": "Netflix", "인텔": "Intel", "퀄컴": "Qualcomm", "오라클": "Oracle",
    "브로드컴": "Broadcom", "팔란티어": "Palantir", "코어위브": "CoreWeave",
    "딥마인드": "DeepMind", "챗GPT": "ChatGPT", "챗지피티": "ChatGPT",
    "허깅페이스": "Hugging Face", "로켓랩": "Rocket Lab",
}

# 한글 이름 뒤에 붙어도 되는 말(조사·서술격 등). 이 밖의 한글이 붙으면 다른 낱말의
# 일부(애플리케이션, 구글링, 아마존강)로 보고 건드리지 않는다.
_TAIL_WORDS = (
    "은", "는", "이", "가", "을", "를", "과", "와", "의", "도", "로", "으로", "에", "에서", "에게",
    "만", "까지", "부터", "보다", "처럼", "조차", "마저", "이나", "나", "이며", "며", "이고", "고",
    "이라는", "라는", "이란", "란", "이다", "다", "였다", "이었다", "과의", "와의", "에서도",
    "에서는", "에게는", "으로는", "로는", "과는", "와는", "이라고", "라고", "이랑", "랑", "하고",
    "측", "사", "식", "판", "제",
)
_TAIL = (r"(?=(?:" + "|".join(sorted(map(re.escape, _TAIL_WORDS), key=len, reverse=True))
         + r")?(?![가-힣]))")

_BRAND_RE = re.compile(
    r"(?<![가-힣A-Za-z])(" + "|".join(sorted(map(re.escape, BRANDS_KO_TO_EN), key=len, reverse=True))
    + r")" + _TAIL)

# 영문 이름 → 한국어 발음의 끝소리. 'none'=받침 없음, 'rieul'=ㄹ 받침, 'other'=그 밖의 받침.
FINAL_SOUND = {
    "Anthropic": "other", "Amazon": "other", "Palantir": "none", "OpenAI": "none",
    "NVIDIA": "none", "Nvidia": "none", "Tesla": "none", "Meta": "none",
    "Microsoft": "none", "YouTube": "none", "SpaceX": "none", "Netflix": "none",
    "xAI": "none", "Google": "rieul", "Apple": "rieul", "Intel": "rieul",
    "Oracle": "rieul", "Qualcomm": "other", "Broadcom": "other", "CoreWeave": "none",
    "DeepMind": "none", "ChatGPT": "none", "Samsung": "other", "Neocloud": "none",
    "Starship": "none", "Starlink": "none", "Blackwell": "rieul", "Rubin": "other",
}

_PAIRS = (("이", "가"), ("은", "는"), ("을", "를"), ("과", "와"))
_PARTICLE_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(map(re.escape, FINAL_SOUND), key=len, reverse=True))
    + r")(이|가|은|는|을|를|과|와|으로|로)(?=의(?![가-힣])|[^가-힣]|$)")

# 코드·태그·URL·직접 인용은 건드리지 않는다.
_PROTECT = re.compile(r"(<!--[\s\S]*?-->|<[^>]+>|```[\s\S]*?```|`[^`]+`|https?://[^\s)>\]]+"
                      r"|“[^”]*”|\"[^\"\n]*\")")


def _fix_particle(m: re.Match) -> str:
    name, particle = m.group(1), m.group(2)
    final = FINAL_SOUND[name]
    if particle in ("으로", "로"):
        return name + ("으로" if final == "other" else "로")
    for with_final, without_final in _PAIRS:
        if particle in (with_final, without_final):
            return name + (without_final if final == "none" else with_final)
    return m.group(0)


def _apply(text: str, fn) -> str:
    out, last = [], 0
    for m in _PROTECT.finditer(text):
        out.append(fn(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def english_brands(text: str) -> str:
    """흔한 한글 표기 회사·브랜드명을 원문으로."""
    if not text:
        return text
    return _apply(text, lambda s: _BRAND_RE.sub(lambda m: BRANDS_KO_TO_EN[m.group(1)], s))


def fix_particles(text: str) -> str:
    """끝소리를 아는 영문 이름 뒤 조사를 바로잡는다(Anthropic가 → Anthropic이)."""
    if not text:
        return text
    return _apply(text, lambda s: _PARTICLE_RE.sub(_fix_particle, s))


def normalize(text: str) -> str:
    return fix_particles(english_brands(text))


def normalize_summary(text: str) -> str:
    """요약 마크다운용 — 원문 제목(H1)과 메타정보 표는 원문 그대로 둔다."""
    if not text:
        return text
    return "\n".join(line if line.startswith("# ") or line.lstrip().startswith("|") else normalize(line)
                     for line in text.split("\n"))
