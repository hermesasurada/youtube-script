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
    "엔비디아": "NVIDIA", "구글": "Google", "마이크로소프트": "Microsoft", "유튜브": "YouTube",
    "오픈AI": "OpenAI", "오픈에이아이": "OpenAI", "앤트로픽": "Anthropic",
    "앤스로픽": "Anthropic", "스페이스X": "SpaceX", "스페이스엑스": "SpaceX",
    "넷플릭스": "Netflix", "인텔": "Intel", "퀄컴": "Qualcomm",
    "브로드컴": "Broadcom", "팔란티어": "Palantir", "코어위브": "CoreWeave",
    "딥마인드": "DeepMind", "챗GPT": "ChatGPT", "챗지피티": "ChatGPT",
    "허깅페이스": "Hugging Face", "로켓랩": "Rocket Lab",
    "L3해리스": "L3Harris", "L3 해리스": "L3Harris", "엘쓰리해리스": "L3Harris",
    # 2026-11-28 보강(td 최근 30일 요약에서 한글로 남던 이름). 사람 성·일반어와 겹치는 이름
    # (번스타인·코히런트·퍼플렉시티·루시드·안두릴)은 넣지 않는다.
    "마이크론 테크놀로지": "Micron Technology", "마이크론테크놀로지": "Micron Technology",
    "소프트뱅크 그룹": "SoftBank Group", "소프트뱅크그룹": "SoftBank Group",
    "마벨 테크놀로지": "Marvell Technology", "키옥시아 홀딩스": "Kioxia Holdings",
    "마이크론": "Micron", "키옥시아": "Kioxia", "샌디스크": "SanDisk", "씨게이트": "Seagate",
    "웨스턴디지털": "Western Digital", "웨스턴 디지털": "Western Digital",
    "골드만삭스": "Goldman Sachs", "골드만 삭스": "Goldman Sachs",
    "모건스탠리": "Morgan Stanley", "모건 스탠리": "Morgan Stanley",
    "JP모건": "JPMorgan", "JP 모건": "JPMorgan", "제이피모건": "JPMorgan",
    "씨티그룹": "Citigroup", "씨티": "Citi", "노무라": "Nomura", "맥쿼리": "Macquarie",
    "웨드부시": "Wedbush", "블랙록": "BlackRock",
    "화웨이": "Huawei", "알리바바": "Alibaba", "텐센트": "Tencent", "바이두": "Baidu",
    "바이트댄스": "ByteDance", "샤오미": "Xiaomi", "폭스콘": "Foxconn", "소프트뱅크": "SoftBank",
    "닌텐도": "Nintendo", "딥시크": "DeepSeek", "미스트랄": "Mistral",
    "네비우스": "Nebius", "블룸에너지": "Bloom Energy", "블룸 에너지": "Bloom Energy",
    "마벨": "Marvell", "램리서치": "Lam Research", "램 리서치": "Lam Research",
    "루멘텀": "Lumentum", "버티브": "Vertiv", "시스코": "Cisco", "슈퍼마이크로": "Supermicro",
    "델 테크놀로지스": "Dell Technologies", "델테크놀로지스": "Dell Technologies",
    "크라우드스트라이크": "CrowdStrike", "지스케일러": "Zscaler",
    "세일즈포스": "Salesforce", "서비스나우": "ServiceNow", "어도비": "Adobe",
    "우버": "Uber", "웨이모": "Waymo", "코스트코": "Costco", "월마트": "Walmart",
    "코인베이스": "Coinbase", "로빈후드": "Robinhood", "보잉": "Boeing",
    "록히드마틴": "Lockheed Martin", "록히드 마틴": "Lockheed Martin",
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

# 회사명 말고도 흔히 쓰이는 이름(아마존 열대우림, 애플 파이, 블록체인 오라클, 자속밀도 단위
# 테슬라)은 조사가 붙어도 뜻이 갈려서(아마존은 열대우림이다) 자동 치환하지 않는다.
# 문맥을 보는 모델이 프롬프트 규칙으로 원문 표기를 맡는다(Astra 재검토, 2026-09-26).
AMBIGUOUS_NOT_REPLACED = ("아마존", "애플", "오라클", "테슬라")


def _brand_re(names, tail, extra_lookbehind=""):
    # 앞에 밑줄 하나(파일명 `a_엔비디아`)면 두고, 밑줄 볼드 `__엔비디아__`는 바꾼다.
    return re.compile(r"(?<![가-힣A-Za-z])(?<![^_]_)" + extra_lookbehind + "("
                      + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + ")" + tail)


_BRAND_RE = _brand_re(BRANDS_KO_TO_EN, _TAIL)
# '루멘텀(Lumentum)'을 바꾸면 'Lumentum(Lumentum)'이 된다 — 같은 이름을 되풀이한 괄호는 뗀다.
_DUP_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, set(BRANDS_KO_TO_EN.values())),
                                                        key=len, reverse=True)) + r") ?\(\1\)", re.I)

# 영문 이름 → 한국어 발음의 끝소리. 'none'=받침 없음, 'rieul'=ㄹ 받침, 'other'=그 밖의 받침.
FINAL_SOUND = {
    "L3Harris": "none", "Anthropic": "other", "Amazon": "other", "Palantir": "none", "OpenAI": "none",
    "NVIDIA": "none", "Nvidia": "none", "Tesla": "none", "Meta": "none",
    "Microsoft": "none", "YouTube": "none", "SpaceX": "none", "Netflix": "none",
    "xAI": "none", "Google": "rieul", "Apple": "rieul", "Intel": "rieul",
    "Oracle": "rieul", "Qualcomm": "other", "Broadcom": "other", "CoreWeave": "none",
    "DeepMind": "none", "ChatGPT": "none", "Samsung": "other", "Neocloud": "none",
    "Starship": "other", "Starlink": "none", "Blackwell": "rieul", "Rubin": "other",
    "Micron": "other", "Kioxia": "none", "SanDisk": "none", "Goldman Sachs": "none",
    "Morgan Stanley": "none", "JPMorgan": "other", "Citi": "none", "Nomura": "none",
    "Huawei": "none", "Alibaba": "none", "Tencent": "none", "SoftBank": "none",
    "Nebius": "none", "Marvell": "rieul", "Lumentum": "other", "Cisco": "none", "Dell": "rieul",
}

_PAIRS = (("이", "가"), ("은", "는"), ("을", "를"), ("과", "와"))
_PARTICLE_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(map(re.escape, FINAL_SOUND), key=len, reverse=True))
    + r")(\*\*|__)?(이|가|은|는|을|를|과|와|으로|로)(?=의(?![가-힣])|[^가-힣]|$)")

# 코드·태그·URL·직접 인용은 건드리지 않는다.
# 인용은 줄을 넘어도 보호하되, 짝 없는 따옴표가 글 전체를 삼키지 않게 길이를 제한한다.
# 마크다운 링크·이미지 주소와 경로·파일명(캡처 폴더 `…_테슬라.frames/`)도 보호한다.
_PROTECT_CORE = (r"<!--[\s\S]*?-->|```[\s\S]*?```|<[^>]+>|`[^`\n]+`|https?://[^\s)>\]]+"
                 r"|\]\([^)\n]*\)"
                 # 경로·파일명: 밑줄이나 슬래시가 있고 확장자로 끝나는 조각이 든 토큰.
                 # 확장자가 없는 `__볼드__`·`테슬라/엔비디아`는 보호하지 않는다.
                 r"|(?=[^\s()\[\]<>\"']*[_/])[^\s()\[\]<>\"']*\.[A-Za-z][A-Za-z0-9]{1,5}\b[^\s()\[\]<>\"']*"
                 r"|“[^”]{0,400}”|\"[^\"]{0,400}\"")
_PROTECT = re.compile("(" + _PROTECT_CORE + ")")
# 요약: 원문 제목(H1)과 메타정보 표 행도 원문 그대로 둔다.
_PROTECT_SUMMARY = re.compile(r"(^# [^\n]*$|^[ \t]*\|[^\n]*$|" + _PROTECT_CORE + ")", re.M)


def _fix_particle(m: re.Match) -> str:
    name, bold, particle = m.group(1), m.group(2) or "", m.group(3)
    final = FINAL_SOUND[name]
    if particle in ("으로", "로"):
        return name + bold + ("으로" if final == "other" else "로")
    for with_final, without_final in _PAIRS:
        if particle in (with_final, without_final):
            return name + bold + (without_final if final == "none" else with_final)
    return m.group(0)


def _apply(text: str, fn, protect: re.Pattern = _PROTECT) -> str:
    out, last = [], 0
    for m in protect.finditer(text):
        out.append(fn(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def _brands(s: str) -> str:
    return _DUP_RE.sub(r"\1", _BRAND_RE.sub(lambda m: BRANDS_KO_TO_EN[m.group(1)], s))


def _particles(s: str) -> str:
    return _PARTICLE_RE.sub(_fix_particle, s)


def english_brands(text: str) -> str:
    """흔한 한글 표기 회사·브랜드명을 원문으로."""
    return _apply(text, _brands) if text else text


def fix_particles(text: str) -> str:
    """끝소리를 아는 영문 이름 뒤 조사를 바로잡는다(Anthropic가 → Anthropic이)."""
    return _apply(text, _particles) if text else text


def normalize(text: str) -> str:
    return fix_particles(english_brands(text))


def normalize_summary(text: str) -> str:
    """요약 마크다운용 — 보호 구간(코드·인용·태그)을 글 전체 기준으로 잡고,
    원문 제목(H1)과 메타정보 표도 원문 그대로 둔다."""
    if not text:
        return text
    return _apply(text, lambda s: _particles(_brands(s)), _PROTECT_SUMMARY)
