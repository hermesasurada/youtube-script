#!/usr/bin/env python3
"""외국어 영상 전사문을 요약 없이 통째로 한국어 번역한다.

기존 자동 처리(전사→요약→캡처)와 완전히 분리된 별도 작업이다.
- 대상: 전사 본문이 한국어가 아닌 영상 (최근 것부터)
- 엔진: 로컬 qwen3.8-27b (OpenAI 호환 API)
- 결과: res/translated/{date}/{stem}.md
- 진행 상태는 transcript_translation 테이블에 남겨, 중간에 끊겨도 청크 단위로 이어서 한다.

    python3 transcript_translator.py --once          # 대기 중 1건 처리(런처가 30분마다 호출)
    python3 transcript_translator.py --path <md>     # 특정 전사 지정 처리
    python3 transcript_translator.py --status        # 큐 현황
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import sys
import time

import db
import document_io
import notation

# LLM 호출 이력(hermes-llm-log). 모듈이 없거나 기록이 실패해도 번역 본업은 그대로 돈다.
try:
    _LLM_LOG_DIR = os.path.expanduser("~/projects/hermes-llm-log")
    if _LLM_LOG_DIR not in sys.path:
        sys.path.append(_LLM_LOG_DIR)
    import llm_log
except Exception:  # noqa: BLE001
    llm_log = None

LLM_LOG_SERVICE = "yt"


def _llm_track(provider: str, model: str | None = None, **fields):
    if llm_log is None:
        return contextlib.nullcontext(None)
    try:
        return llm_log.track(LLM_LOG_SERVICE, provider, model, **fields)
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext(None)


def _llm_host(base_url: str) -> str:
    try:
        if llm_log is not None:
            return llm_log.local_host_label(base_url)
        from urllib.parse import urlparse
        return urlparse(base_url).netloc or base_url
    except Exception:  # noqa: BLE001
        return base_url


def _llm_record_response(call, r) -> None:
    """openai SDK 응답 객체의 model/usage를 call에 옮긴다. 실패해도 조용히."""
    if call is None:
        return
    try:
        usage = getattr(r, "usage", None)
        call.tokens(input=getattr(usage, "prompt_tokens", None),
                    output=getattr(usage, "completion_tokens", None))
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            call.tokens(cache_read=cached)
        if getattr(r, "model", None):
            call.model = r.model
    except Exception:  # noqa: BLE001
        pass


BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
RES_DIR    = os.path.join(BASE_DIR, "res")
TRANS_DIR  = os.path.join(RES_DIR, "translated")

# 번역 백엔드 — 1차 DGX Spark(vLLM), 2차 로컬 oMLX.
# Spark가 청크당 1.4배 빠르지만 별도 머신이라 꺼져 있을 수 있어, 연결이 안 되면
# 맥 안의 oMLX로 넘어가 번역을 계속한다(둘 다 Qwen3.8 계열이라 결과가 비슷하다).
QWEN_BASE   = os.environ.get("QWEN_BASE_URL", "http://192.168.1.125:8000/v1")
QWEN_MODEL  = os.environ.get("QWEN_MODEL", "qwen3.8-27b")
OMLX_BASE   = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8080/v1")
OMLX_MODEL  = os.environ.get("OMLX_MODEL", "Qwen3.8-27B-Alis-MLX-6bit")
QWEN_TIMEOUT = int(os.environ.get("QWEN_TIMEOUT", "900"))

BACKENDS = [("spark", QWEN_BASE, QWEN_MODEL), ("omlx", OMLX_BASE, OMLX_MODEL)]
_active = 0          # 한 번 폴백하면 이 프로세스 동안 유지(죽은 서버를 매 청크 두드리지 않게)

# 청크가 크면 호출 오버헤드가 줄지만 출력이 길어져 중단 위험이 커진다.
# 실측(16 tok/s)상 4천자 내외가 한 번에 안정적으로 나오는 크기다.
CHUNK_CHARS   = int(os.environ.get("TRANSLATE_CHUNK_CHARS", "4000"))

# 자동 처리 하한(전사일). 이보다 과거 영상은 큐에 올리지 않는다 — 오래된 것까지
# 전부 훑으면 몇 주가 걸리는데 실효가 낮다. 필요하면 --path 로 개별 지정해 돌린다
# (수동 지정은 이 하한을 무시한다).
TRANSLATE_SINCE = os.environ.get("TRANSLATE_SINCE", "2026-08-01")
CTX_TAIL_CHARS = 240      # 직전 청크 꼬리를 문맥으로 물려 용어·화자를 잇는다
MAX_TOKENS    = 6144

_HANGUL_RE = re.compile(r"[가-힣]")
_TS_RE     = re.compile(r"^\[\d+:\d+(?::\d+)?\]")

# 요약 프롬프트(prompt.txt)의 표기 규칙을 번역용으로 옮긴 것 — 요약·제목 번역과 같은 기준.
# 핵심 차이: 여기서는 절대 요약·축약하지 않는다. prompt.txt 표기 규칙을 바꾸면 여기도 맞출 것.
SYSTEM_PROMPT = """유튜브 영상 전사문을 한국어로 번역한다.

**가장 중요: 요약하지 않는다.** 원문의 모든 문장을 빠짐없이 옮긴다. 임의로 줄이거나
합치거나 생략하지 말 것. 분량은 원문에 상응해야 한다.

표기 규칙:
- **회사·브랜드·제품·모델·기관명은 음차하지 말고 원문 표기를 그대로 쓴다** —
  NVIDIA, Google, Tesla, Apple, YouTube, Anthropic, ChatGPT, OpenAI, SpaceX, NASA, S&P 500처럼.
  한글 표기가 흔한 이름(엔비디아, 구글, 테슬라, 유튜브)도 원문으로 쓴다.
  한국 기업·기관은 한국어 표기를 쓴다(삼성전자, 금융위원회).
- Neocloud는 뉴클라우드·네오클라우드로 음차하지 않고 항상 Neocloud로 쓴다.
- **인물 이름도 원어를 그대로 쓴다** — "샘 올트먼"이 아니라 **Sam Altman**, "젠슨 황"이 아니라
  **Jensen Huang**. 예외는 한글 표기가 완전히 굳어진 일론 머스크뿐이다. 지명은 널리 알려진
  곳(실리콘밸리, 뉴욕, 캘리포니아)만 한글로 쓰고 낯선 곳은 원문 철자로 둔다.
- **이름이 애매하면 맥락으로 실제 대상을 특정한 뒤 표기한다.** 전사기는 발음이 비슷한 이름을
  자주 혼동한다. 전사 안의 영문 철자가 음차보다 우선이고(빈도가 아니라 철자가 근거),
  형제 제품의 작명 계열(Sol·Terra·Luna 계열의 "Soul" → Sol)과 회사–제품 관계(경쟁 LLM
  문맥의 "그록"은 xAI의 Grok, 칩 회사 Groq 아님)를 함께 본다. 그래도 확신이 없으면 전사에
  적힌 철자를 그대로 둔다(임의로 음역해 한글로 확정하지 말 것).
- 영문 이름 뒤 조사는 철자가 아니라 한국어로 읽은 끝소리에 맞춘다(Anthropic이, Google은, OpenAI가).
- 전문용어는 널리 쓰이는 한국어 용어가 있으면 그것을 쓰고, 없으면 원문을 유지한다.
  처음 나올 때만 괄호로 원문을 병기한다 — 추론(inference).
- **사전에 없는 한자 조어를 만들지 않는다** — 영어 용어를 `비-`·`무-`+한자어로 압축해 가짜
  전문용어를 지어내지 말 것(zero data retention → "데이터 비누지" ✗, "데이터 무보존(ZDR)" ○).
  뜻이 불분명한 조어가 되느니 원어를 그대로 두거나 뜻이 드러나는 구로 풀어 쓴다.
- **영어 관용구·업계 은어·약어는 음차하거나 직역하지 말고 뜻이 통하는 한국어로 풀어 쓴다.**
  원문 표현을 살릴 필요가 있으면 풀어 쓴 뒤 괄호에 병기한다.
  예: "5000억 달러 오버 또는 언더?"가 아니라 **"5000억 달러를 넘을까요, 못 넘을까요?"**,
  "종료 ARR"이 아니라 **"연말 기준 ARR(exit ARR)"**, "언더를 택했다"가 아니라
  **"넘지 못한다는 쪽에 걸었다"**. 한국어로 옮겼을 때 뜻이 통하지 않는 표현은 그대로 두지 않는다.
  다만 발화자의 어조·강조는 유지한다(요약이 아니라 번역이므로 문장을 없애지는 않는다).
- 연결어미(-고/-며/-지만/-면서) 바로 뒤에 쉼표를 찍지 않는다(`했고, 그래서` → `했고 그래서`).
- 숫자·단위·금액은 원문 값을 그대로 유지한다. 임의로 환산하지 않는다.

형식 규칙:
- `[mm:ss]` 형태의 타임스탬프는 원문에 있던 위치에 그대로 유지한다.
- 줄바꿈 구조를 원문과 동일하게 유지한다.
- 구어체의 말더듬·중복(`the the`, `you know`)은 자연스럽게 다듬되 내용은 보존한다.
- 번역문만 출력한다. 설명·머리말·코드펜스를 붙이지 않는다."""

# 앞 청크에서 쓴 영문 고유명사 — 긴 영상에서 같은 사람·회사 표기가 청크마다 흔들리지 않게
# 다음 청크에 목록으로 물려준다(직전 꼬리 240자만으로는 앞부분 표기를 모른다).
GLOSSARY_MAX = 40
# 영문 고유명사: 대문자가 하나 이상 든 3자 이상 낱말(NVIDIA·NASA·xAI·OpenAI·iPhone), 대문자로
# 시작하는 낱말이 이어지면 한 이름(David Friedberg). 가운뎃점·쉼표로 나열된 이름은 따로 센다.
_NAME_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9&'.-]*[A-Z])[A-Za-z][A-Za-z0-9&'.-]*[A-Za-z0-9]"
                      r"(?:[ \t]+[A-Z][A-Za-z0-9&'.-]*[A-Za-z0-9])*")
# 이름이 아닌 흔한 약어·단위는 목록 자리를 차지하지 않게 뺀다.
_GLOSSARY_SKIP = frozenset("""AI AGI API ASIC CEO CFO COO CTO CPU GPU TPU LLM IPO ETF EPS GDP CPI SaaS
    ARR KPI ROI USD KRW EV FSD HBM RAG MoE CoT URL PDF USB MW GW TW kWh MWh GWh AM PM OK TV PC IT
    Q1 Q2 Q3 Q4 FY YoY QoQ""".split())


def proper_noun_glossary(parts: list[str], limit: int = GLOSSARY_MAX) -> list[str]:
    """번역된 청크들에서 원문 표기로 남은 고유명사(대문자로 시작, 소문자 포함)를 자주 쓴 순으로."""
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for text in parts:
        for m in _NAME_RE.finditer(text or ""):
            words = m.group(0).strip(".'-").split()
            while words and words[0] in _GLOSSARY_SKIP:       # 'CEO David Friedberg' → 'David Friedberg'
                words.pop(0)
            name = " ".join(words)
            if (len(name) < 3 or name in _GLOSSARY_SKIP
                    or (len(name) > 12 and re.search(r"\d", name))):
                continue                                  # 너무 짧거나 흔한 약어·ID 같은 문자열
            counts[name] = counts.get(name, 0) + 1
            first.setdefault(name, len(first))
    ranked = sorted(counts, key=lambda n: (-counts[n], first[n]))
    return ranked[:limit]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _client(idx: int = 0):
    from openai import OpenAI
    _, base, _model = BACKENDS[idx]
    return OpenAI(base_url=base, api_key="none", timeout=QWEN_TIMEOUT)


def active_backend() -> str:
    return BACKENDS[_active][0]


def split_body(md_text: str) -> tuple[str, str]:
    """전사 md → (프론트매터 포함 머리부, 번역할 본문). 본문은 H1 다음부터."""
    lines = md_text.splitlines()
    seps = [i for i, l in enumerate(lines) if l.strip() == "---"]
    start = seps[1] + 1 if len(seps) >= 2 else 0
    # H1 제목 줄은 번역 대상에서 빼고 머리부로 넘긴다(제목은 title_ko가 따로 있다)
    for i in range(start, min(start + 5, len(lines))):
        if lines[i].startswith("# "):
            start = i + 1
            break
    return "\n".join(lines[:start]), "\n".join(lines[start:]).strip()


def is_foreign(body: str) -> bool:
    """전사 본문이 한국어가 아닌지. 앞부분 표본의 한글 비율로 판정한다."""
    sample = body[:4000]
    letters = [ch for ch in sample if ch.isalpha()]
    if not letters:
        return False
    han = sum(1 for ch in letters if _HANGUL_RE.match(ch))
    return (han / len(letters)) < 0.15


def chunk_body(body: str) -> list[str]:
    """타임스탬프 블록 경계로 자른다 — 문장이 청크 중간에서 끊기지 않게."""
    lines = body.splitlines()
    chunks, cur, size = [], [], 0
    for ln in lines:
        # 새 타임스탬프 블록이 시작될 때만 자를 수 있다
        if size >= CHUNK_CHARS and _TS_RE.match(ln.strip()) and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def _is_conn_error(msg: str) -> bool:
    m = msg.lower()
    return "connection" in m or "timeout" in m or "refused" in m or "unreachable" in m


def _call(user: str, temperature: float, rep_penalty: float,
          *, title: str | None = None, system: str = SYSTEM_PROMPT,
          purpose: str = "translate") -> tuple[str, str]:
    """활성 백엔드로 한 청크 번역. 연결이 안 되면 짧게 재시도한 뒤 다음 백엔드로 넘어간다.

    (한 번의 connection error로 영상 전체가 영구 실패 처리된 사례가 있었다)
    시도 한 번이 LLM 이력 한 행이다(연결 실패도 error 행으로 남는다).
    """
    global _active
    last = None
    for idx in range(_active, len(BACKENDS)):
        name, base, model = BACKENDS[idx]
        for attempt in range(2):
            try:
                with _llm_track("local", model, purpose=purpose, title=title,
                                reasoning="no-think", backend="http",
                                host=_llm_host(base)) as call:
                    r = _client(idx).chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                        max_tokens=MAX_TOKENS, temperature=temperature,
                        extra_body={"chat_template_kwargs": {"enable_thinking": False},
                                    "repetition_penalty": rep_penalty},
                    )
                    _llm_record_response(call, r)
                if idx != _active:
                    log(f"  ✓ {name} 백엔드로 전환")
                    _active = idx
                break
            except Exception as e:                   # noqa: BLE001
                last = e
                if not _is_conn_error(str(e)):
                    raise                            # 연결 외 오류는 폴백 대상이 아니다
                if attempt == 0:
                    log(f"  ↻ {name} 연결 실패 — 20초 뒤 재시도")
                    time.sleep(20)
        else:
            log(f"  ⤳ {name} 사용 불가 — 다음 백엔드 시도")
            continue
        break
    else:
        raise last
    out = (r.choices[0].message.content or "").strip()
    if out.startswith("```"):                     # 가끔 코드펜스로 감싼다
        out = re.sub(r"^```[a-z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out).strip()
    return out, (r.choices[0].finish_reason or "")


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


_LEAK_REPAIR_SYSTEM = """한국어 번역문 교정기다. 입력 줄들에는 중국어·일본어 문자가 잘못 섞여 있다.
각 줄에서 그 문자 부분만 뜻이 같은 자연스러운 한국어로 바꾸고, 나머지는 한 글자도 바꾸지 않는다
(타임스탬프·영문 고유명사·숫자·어순 그대로). 입력과 똑같이 `번호<TAB>문장` 형식으로,
같은 줄 수만 출력한다. 설명을 붙이지 않는다."""


def _repair_leaks(out: str, chunk: str, *, title: str | None = None) -> str:
    """한자가 섞인 줄만 골라 한 번 교정한다. 교정 결과가 깨끗하고 한자 구간만 바뀐 줄만 받는다.

    청크 전체 재번역보다 싸고, 멀쩡한 줄을 건드리지 않는다. 호출이 실패해도 번역은 그대로 둔다.
    """
    lines = out.splitlines()
    bad = [i for i, l in enumerate(lines) if han_leaks(l, chunk)]
    if not bad:
        return out
    tokens = sorted({t for i in bad for t in han_leaks(lines[i], chunk)})
    user = (f"섞인 문자: {', '.join(tokens)[:200]}\n\n"
            + "\n".join(f"{n + 1}\t{lines[i]}" for n, i in enumerate(bad)))
    try:
        fixed, _ = _call(user, 0.2, 1.0, title=f"{title} (한자 교정)" if title else None,
                         system=_LEAK_REPAIR_SYSTEM, purpose="repair")
    except Exception as e:                            # noqa: BLE001
        log(f"  ⚠ 한자 교정 호출 실패 — 원문 유지: {e}")
        return out
    got: dict[int, str] = {}
    for line in fixed.splitlines():
        m = re.match(r"^\s*(\d+)\t(.*)$", line)
        if m:
            got[int(m.group(1))] = m.group(2)
    n_ok = 0
    for n, i in enumerate(bad):
        new = got.get(n + 1)
        orig = lines[i]
        # 한자 구간 밖이 한 글자라도 달라지면(숫자·이름·타임스탬프 포함) 원래 줄을 둔다.
        if new and not han_leaks(new, chunk) and _only_leaks_replaced(orig, new, _leak_spans(orig, chunk)):
            lines[i] = new
            n_ok += 1
    log(f"  ✎ 한자 교정 {n_ok}/{len(bad)}줄 ({', '.join(tokens)[:60]})")
    return "\n".join(lines)


def translate_chunk(chunk: str, prev_tail: str = "", *, title: str | None = None,
                    glossary: list[str] | None = None) -> str:
    """한 청크 번역. 회사·브랜드명 원문 표기와 영문 이름 뒤 조사는 결정적으로 한 번 더 맞춘다."""
    return notation.normalize(_translate_chunk_raw(chunk, prev_tail, title=title, glossary=glossary))


def _translate_chunk_raw(chunk: str, prev_tail: str = "", *, title: str | None = None,
                         glossary: list[str] | None = None) -> str:
    user = chunk
    refs = []
    if glossary:
        refs.append("[앞 청크에서 쓴 고유명사 표기 — 같은 대상이면 이 철자를 따른다. "
                    "단 이번 원문에 더 분명한 철자가 나오면 원문을 우선한다]\n"
                    + ", ".join(glossary))
    if prev_tail:
        refs.append(f"[직전까지의 번역 끝부분 — 용어와 화자를 잇기 위한 참고. "
                    f"다시 번역하지 말 것]\n{prev_tail}")
    if refs:
        user = "\n\n".join(refs) + f"\n\n[여기부터 번역]\n{chunk}"
    out, finish = _call(user, 0.3, 1.05, title=title)
    if _looks_degenerate(out, chunk, finish):
        # 폭주는 샘플링 운에 좌우되므로, 온도를 낮추고 반복 페널티를 올려 한 번 더.
        log(f"  ↻ 폭주 감지({len(out):,}자, 줄 반복 {_repeat_count(out)}회, finish={finish}) — 재시도")
        out2, finish2 = _call(user, 0.15, 1.15, title=f"{title} (재시도)" if title else None)
        if not _looks_degenerate(out2, chunk, finish2):
            out = out2
        else:
            # 둘 다 비정상이면 반복이 적은 쪽, 같으면 짧은 쪽. 원문이 반복이 아니면 연속 반복 줄을 접는다.
            out = min((out, out2), key=lambda t: (_repeat_count(t), len(t)))
            log(f"  ⚠ 재시도도 비정상({len(out2):,}자) — 반복 적은 쪽 채택")
            if _repeat_count(chunk) < 3:
                out = _collapse_repeats(out)
    return _repair_leaks(out, chunk, title=title)


def out_path_for(md_path: str) -> str:
    """res/{date}/{stem}.md → res/translated/{date}/{stem}.md"""
    rel = os.path.relpath(os.path.abspath(md_path), RES_DIR)
    return os.path.join(TRANS_DIR, rel)


def _title_from_head(head: str, md_path: str) -> str:
    m = re.search(r"^#\s+(.+)$", head or "", re.M)
    return m.group(1).strip() if m else os.path.basename(md_path)


def translate_file(md_path: str, yt_id: str = "", *, title: str | None = None) -> dict:
    """전사 하나를 통째로 번역해 저장. 청크 단위로 이어쓰기 때문에 중단에 안전하다.

    title은 LLM 이력용 영상 제목(없으면 전사 H1, 그것도 없으면 파일명)."""
    md = open(md_path, encoding="utf-8").read()
    head, body = split_body(md)
    title = (title or "").strip() or _title_from_head(head, md_path)
    if not body.strip():
        return {"ok": False, "error": "본문 없음"}
    if not is_foreign(body):
        db.set_translation_state(yt_id, md_path, "", "skipped", 0, 0, "한국어 영상")
        return {"ok": True, "skipped": "한국어 영상"}

    chunks = chunk_body(body)
    dest = out_path_for(md_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 이어하기: 이미 끝난 청크 수만큼 건너뛴다
    st = db.get_translation_state(yt_id) if yt_id else None
    done_n = (st or {}).get("chunks_done") or 0
    parts: list[str] = []
    if done_n and os.path.isfile(dest):
        prev = open(dest, encoding="utf-8").read()
        marker = "\n\n<!--CHUNK-->\n\n"
        # 머리부(프론트매터·H1·마커)를 떼고 번역 청크만 복원한다. 이걸 빼먹으면
        # parts[0]에 머리부가 섞여 들어가고, _assemble이 머리부를 다시 붙여
        # 재개할 때마다 프론트매터가 한 벌씩 쌓인다.
        m = re.search(r"<!--TRANSLATED_BY:[^>]*-->[ \t]*\n?"
                      r"(?:<!--TRANSLATE_PARTIAL:[^>]*-->[ \t]*\n?)?", prev)
        body_prev = prev[m.end():] if m else prev
        parts = [p for p in body_prev.split(marker)]
        parts = parts[:done_n]                       # 신뢰할 수 있는 만큼만 유지
        parts = [p.strip() for p in parts if p.strip()]
        log(f"이어하기: {len(parts)}/{len(chunks)} 청크 완료 상태")
        done_n = len(parts)
    else:
        done_n = 0

    db.set_translation_state(yt_id, md_path, dest, "processing", done_n, len(chunks), "")
    t_all = time.time()
    for i in range(done_n, len(chunks)):
        tail = parts[-1][-CTX_TAIL_CHARS:] if parts else ""
        t0 = time.time()
        try:
            out = translate_chunk(chunks[i], tail, title=f"{title} [청크 {i + 1}/{len(chunks)}]",
                                  glossary=proper_noun_glossary(parts))
        except Exception as e:                        # noqa: BLE001
            db.set_translation_state(yt_id, md_path, dest, "failed", len(parts),
                                     len(chunks), str(e)[:300])
            log(f"청크 {i+1}/{len(chunks)} 실패: {e}")
            return {"ok": False, "error": str(e), "done": len(parts), "total": len(chunks)}
        parts.append(out)
        # 청크마다 저장 → 중단돼도 여기까지는 남는다
        document_io.atomic_write_text(
            dest, _assemble(head, parts, len(chunks)))
        db.set_translation_state(yt_id, md_path, dest, "processing", len(parts),
                                 len(chunks), "")
        log(f"청크 {i+1}/{len(chunks)} 완료 ({len(out):,}자, {time.time()-t0:.0f}s)")

    document_io.atomic_write_text(dest, _assemble(head, parts, len(chunks), final=True))
    db.set_translation_state(yt_id, md_path, dest, "done", len(parts), len(chunks), "")
    log(f"완료: {os.path.basename(dest)} ({time.time()-t_all:.0f}s)")
    return {"ok": True, "path": dest, "chunks": len(chunks)}


def _assemble(head: str, parts: list[str], total: int, final: bool = False) -> str:
    marker = "\n\n<!--CHUNK-->\n\n"
    body = marker.join(parts)
    note = "" if final else f"\n<!--TRANSLATE_PARTIAL:{len(parts)}/{total}-->\n"
    return f"{head}\n<!--TRANSLATED_BY:{QWEN_MODEL}-->{note}\n{body}\n"


def _since_ts() -> float | None:
    """TRANSLATE_SINCE(YYYY-MM-DD) → unix timestamp. 비우면 하한 없음."""
    s = (TRANSLATE_SINCE or "").strip()
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d"))
    except ValueError:
        log(f"TRANSLATE_SINCE 형식 오류({s!r}) — 하한 없이 진행")
        return None


def pick_next() -> dict | None:
    """최근 영상부터, 아직 번역 안 된 외국어 전사 1건(전사일 하한 적용)."""
    rows = db.translation_candidates(limit=40, since_ts=_since_ts())
    for r in rows:
        p = r["md_path"]
        if not p or not os.path.isfile(p):
            continue
        try:
            _, body = split_body(open(p, encoding="utf-8").read())
        except Exception:
            continue
        if not body.strip():
            continue
        if not is_foreign(body):
            db.set_translation_state(r["yt_id"], p, "", "skipped", 0, 0, "한국어 영상")
            continue
        return r
    return None


LOCK_PATH = os.path.join(RES_DIR, ".translate.lock")


def acquire_lock():
    """단일 인스턴스 보장 — 한 건 처리가 30분(런처 주기)을 넘겨도 겹치지 않게 한다.
    반환한 파일 객체를 살려 둬야 잠금이 유지된다."""
    os.makedirs(RES_DIR, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="대기 중 1건 처리")
    ap.add_argument("--path", help="특정 전사 md 경로 처리")
    ap.add_argument("--status", action="store_true", help="현황 출력")
    a = ap.parse_args()

    db.init()
    if a.status:
        for row in db.translation_status_counts():
            print(f"  {row['status']:<10} {row['n']}")
        cur = db.get_translation_inflight()
        if cur:
            print(f"  진행중: {cur['chunks_done']}/{cur['chunks_total']} — {cur['md_path']}")
        since = _since_ts()
        print(f"  자동 처리 하한: 전사일 {TRANSLATE_SINCE} 이후"
              if since else "  자동 처리 하한: 없음(전체)")
        print(f"  남은 후보: {len(db.translation_candidates(9999, since))}건"
              " (한국어 포함 — 실제 번역 대상은 이보다 적다)")
        return 0

    # 실제 번역을 돌리는 경로는 모두 같은 잠금을 쓴다. 한 건이 런처 주기(30분)를
    # 넘겨도 두 프로세스가 같은 파일에 동시에 쓰지 않게 하려는 것.
    if a.path or a.once:
        lock = acquire_lock()
        if lock is None:
            log("이미 처리 중(잠금 보유) — 종료")
            return 0

    if a.path:
        p = os.path.abspath(a.path)
        yt = db.yt_id_for_md(p) or ""
        log(f"지정 처리: {os.path.basename(p)}")
        r = translate_file(p, yt)
        log(str(r))
        return 0 if r.get("ok") else 1

    if a.once:
        nxt = pick_next()
        if not nxt:
            log("대상 없음")
            return 0
        log(f"대상: {nxt['title'][:60]}")
        r = translate_file(nxt["md_path"], nxt["yt_id"], title=nxt.get("title"))
        log(str(r))
        return 0 if r.get("ok") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
