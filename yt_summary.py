"""요약 생성 — 문자 누출 게이트, 요약 다듬기, 한눈 요약 재구성, 모니터 슬롯 순차 요약·수동 Claude 요약, 각주 제외 주입.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import collections
import contextlib
import json
import os
import re
import db
import document_io
import humanize_korean
import notation
import term_marks
import brief_rebuild
import term_senses
import name_fix
import llm_gateway
import llm_runners
import yt_core
import yt_models
import yt_titles
import logging

log = logging.getLogger("yt-script")

def _summary_path_for_md(md_path: str) -> str | None:
    """전사 md 경로 → res/summary/{date}/{stem}.md 경로 산출(디렉토리 생성 포함)."""
    try:
        rel = os.path.relpath(md_path, yt_core.RES_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    p = os.path.join(yt_core.SUMMARY_DIR, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _md_from_summary_path(save_path: str) -> str | None:
    """summary md 경로에서 짝이 되는 전사 md 경로 역산."""
    try:
        rel = os.path.relpath(save_path, yt_core.SUMMARY_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return os.path.join(yt_core.RES_DIR, rel)


# 한국어 요약에 다른 문자체계가 섞여 들어온 흔적. LLM이 한국어를 쓰다 토큰 단위로
# 미끄러지면서 생기며, 드물지만(897건 중 3건) 조용히 남는다. 실측된 사례:
#   "…압도하기 전에 تمويل할 수 있느냐"(financing), "연조ічой과 달리"(연조직),
#   "민粹주의"(포퓰리즘)
# 자동 치환은 무엇으로 되돌릴지 알 수 없어 하지 않고 경고만 남긴다.
_ALIEN_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿ֐-׿ऀ-ॿ฀-๿Ѐ-ӿ԰-֏]+")
# 한자·가나는 한국어 글에서 병기로 정상 등장한다(반(反), 전년比). 한글 음절 사이에
# 끼어 단어를 깨는 경우만 혼입으로 본다.
_CJK_INLINE_RE = re.compile(r"(?<=[가-힣])[一-鿿぀-ゟ゠-ヿ]+(?=[가-힣])")


_HAN_RUN_RE = re.compile(r"[一-鿿]+")


# 한국어 문장 속 소문자 영어 단어(4자 이상). 고유명사는 대문자로 시작하고 약어는 전부 대문자라
# 여기 걸리지 않는다. 괄호 병기(증류(distillation))·HTML·각주·별표 각주어(harness\*)는 제외한다.
# 경계에 라틴 확장 문자(À-ɏ)를 넣어 Wärtsilä·Córdoba·Schrödinger가 'rtsil'·'rdoba'로 쪼개지지 않게 한다.
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-z\u00C0-\u024F0-9/.@_-])([a-z][a-z]{3,})(?![A-Za-z\u00C0-\u024F0-9/._-]|\\\*|\*)")
_LATIN_ALLOW = frozenset("""
open weight token bay attention chain thought context window harness root prompt agent
agents world model reward hacking peer review shell cold latent space rollout rollouts
auto hack diff demux full next with into
""".split())


def _stem(word: str) -> str:
    """crude English stem — 원문 대조에서 sycophantic/sycophant, democratized/democratize를 같게 본다."""
    w = word.lower()
    for suf in ("ically", "ization", "isation", "ations", "ation", "ingly", "ities", "ness", "ment",
                "ing", "ies", "ers", "ed", "es", "er", "ly", "al", "ic", "ize", "ise", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _latin_fragments(text: str, source: str = "") -> list[str]:
    """한글 문장에 남은 영어 조각(twoweeks 전, concuss된, parad 수, automat으로)을 찾는다.

    기준은 둘이다 — 소문자로 시작하는 4자 이상 영어 단어가 한글 본문 안에 있고, 그 단어(어간)가
    원문(source: 전사가 든 프롬프트)에도 제목에도 없다. 원문에 있는 단어(obesus, harness,
    sycophantic→sycophant)는 인용·병기일 수 있어 두고, 원문에 없는 단어는 모델이 옮기다 흘린
    조각이다. 영문 제목·메타 표·캡처 캡션·각주·괄호 병기·코드는 검사에서 뺀다.
    2026-09-16 Grok 요약 5건 검토: SemiAnalysis 편 한 건에 세 조각(twoweeks·Milestones·parad).
    기존 요약 1,112건 보정 결과는 _script_leaks 아래 테스트 참조.
    """
    body = text or ""
    body = re.sub(r"^# [^\n]*$", " ", body, flags=re.M)                                  # H1(영문 제목)
    body = re.sub(r"^## 1\. 메타정보.*?(?=^## )", " ", body, flags=re.S | re.M)          # 메타 표
    body = re.sub(r"<figure>.*?</figure>", " ", body, flags=re.S)                        # 캡처 캡션
    body = re.sub(r"<div class=\"term-notes\">.*?</div>", " ", body, flags=re.S)       # 각주 묶음
    body = re.sub(r"<p class=\"term-note\">.*?</p>", " ", body, flags=re.S)           # 낱개 각주
    body = re.sub(r"^\*\s+\*\*[^\n]*$", " ", body, flags=re.M)                       # 마크다운형 각주
    body = re.sub(r"<[^>]+>", " ", body)                                                 # 그 밖의 HTML
    body = re.sub(r"&[#a-zA-Z0-9]+;", " ", body)                                          # HTML 엔티티
    body = re.sub(r"`[^`\n]*`", " ", body)                                              # 코드 스팬
    body = re.sub(r"\([^()]*\)", " ", body)                                             # 괄호 병기
    body = re.sub(r"https?://\S+", " ", body)                                            # URL
    body = re.sub(r"\\\(.*?\\\)|\$[^$\n]+\$", " ", body)                                  # 수식
    src = (source or "") + " " + (re.search(r"^# ([^\n]*)$", text or "", re.M) or [None, ""])[1]
    src_stems = {_stem(w) for w in re.findall(r"[A-Za-z]{4,}", src)}
    # 요약 안에서 세 번 이상 쓰인 낱말은 흘린 조각이 아니라 의도한 표기다(sofic 군, tsserver).
    counts = collections.Counter(w.lower() for w in re.findall(r"[A-Za-z]{4,}", body))
    found: list[str] = []
    for m in _LATIN_WORD_RE.finditer(body):
        w = m.group(1)
        if w in _LATIN_ALLOW or _stem(w) in src_stems or counts[w] >= 3:
            continue
        before = body[max(0, m.start() - 2):m.start()]
        after = body[m.end():m.end() + 1]
        # 누출의 세 가지 모양: ① 앞에 공백 두 칸(단어를 흘린 자리: ' eventual 수주'), ② 한글 낱말 안에
        # 박힘(에피soode), ③ 한글 조사가 바로 붙음(concuss된·automat으로). 띄어 쓴 일반 영어 낱말
        # (dense attention, Mac mini)은 두어, 기술 용어를 억지로 옮기게 하지 않는다.
        dropped = before.endswith("  ")
        embedded = bool(re.match(r"[가-힣]", body[m.start() - 1:m.start()] or " "))
        attached = bool(re.match(r"[가-힣]", after or " ")) and len(w) >= 5
        if not (dropped or embedded or attached):
            continue
        window = body[max(0, m.start() - 40):m.end() + 40]
        if not re.search(r"[가-힣]", window):               # 영문 인용 블록은 제외
            continue
        found.append(f"{w!r} … {body[max(0, m.start() - 30):m.end() + 20].strip()!r}")
    return found


def _script_leaks(text: str, source: str = "") -> list[str]:
    """한국어 요약에 섞인 이질 문자(아랍·키릴 등)·'누출' 한자·영어 조각을 찾는다.

    source(전사가 든 프롬프트)를 주면 영어 조각 판정에 원문 대조를 쓴다(_latin_fragments).

    정상 한자 표기는 두 가지로 본다 — 한글 뒤 괄호 병기(반(反), 톈궁(天工, Tiangong),
    킨츠기(金継ぎ))와 한 글자 단독 사용(前 CEO, 對중국, 전년比, 마이크로범프 有). 두 글자
    이상의 한자가 병기 괄호 밖에 있거나(主张하는, lateral移動, 退役) 한 글자라도 한글 사이에
    끼어 단어를 깨면(민粹주의) 모델이 중국어로 미끄러진 것이다(2026-09-12 All-In 요약
    "主张하는" — Grok). 발견 목록을 문맥과 함께 돌려준다. 요약 1,061건 대상 보정:
    오탐 0, 실제 누출 2건 검출.
    """
    text = (text or "").replace("\\(", "(").replace("\\)", ")")
    found: list[str] = []
    for m in _ALIEN_SCRIPT_RE.finditer(text):
        found.append(f"{m.group(0)!r} … {text[max(0, m.start() - 30):m.end() + 20]!r}")
    for m in _HAN_RUN_RE.finditer(text):
        run, a, b = m.group(0), m.start(), m.end()
        before = text[a - 1] if a > 0 else ""
        after = text[b] if b < len(text) else ""
        before2 = text[a - 2] if a > 1 else ""
        hangul_before = bool(re.match(r"[가-힣]", before or " "))
        hangul_after = bool(re.match(r"[가-힣]", after or " "))
        # 한글(漢字…) 병기: 여는 괄호 바로 앞이 한글
        if before == "(" and re.match(r"[가-힣]", before2 or " "):
            continue
        # 한 글자는 한글 사이에 낀 경우만 누출
        if len(run) == 1 and not (hangul_before and hangul_after):
            continue
        found.append(f"{run!r} … {text[max(0, a - 30):b + 20]!r}")
    found.extend(_latin_fragments(text, source))
    return found


def _warn_script_mix(save_path: str, text: str) -> list[str]:
    """요약에 섞인 이질 문자를 로그로 경고한다(수정하지 않음). 발견 목록 반환."""
    found = _script_leaks(text)
    if found:
        log.warning("summary script mix: %s — %s",
                    os.path.basename(save_path), " | ".join(found[:4]))
    return found


_SCRIPT_RETRY_NOTE = (
    "\n\n[재출력 요청] 직전 응답에 한자·중국어 단어, 다른 문자체계, 또는 한국어 문장 속 영어 조각이 "
    "섞여 있었다({leaks}). 한글로만 쓰고(영문 고유명사·약어는 그대로), 한자는 '반(反)'처럼 한글 뒤 "
    "괄호 병기 외에는 쓰지 말 것. 영어 단어를 조사에 붙여 남기지 말 것(twoweeks 전 → 2주 전, "
    "concuss된 → 와닿은, Milestones로 → 이정표로). 예: 主张 → 주장. 같은 형식·같은 분량으로 다시 출력한다."
)


def _reindex_summary(save_path: str) -> None:
    try:
        with open(save_path, encoding="utf-8", errors="replace") as f:
            _warn_script_mix(save_path, f.read())
    except OSError:
        pass
    try:
        term_senses.schedule(save_path)   # 새 각주가 제외 용어와 표기만 같고 뜻이 다른지(뒤에서)
    except Exception:  # noqa: BLE001 — 부가 기능
        pass
    md = _md_from_summary_path(save_path)
    if md:
        try:
            db.upsert_summary_only(md)
        except Exception:
            pass
    yt_titles._translate_titles_async()      # 새 항목의 외국어 제목: YouTube 한국어 제목, 없으면 LLM 번역


def _rebuild_brief(body: str, family: str, model: str | None, title: str | None) -> str:
    """한눈 요약을 완성된 본문에서 뽑아 채운다 — 소제목 하나당 불릿 하나(2026-11-02 사용자 지시).

    첫 요약은 '## 2. 한눈 요약' 머리글만 비워 둔다(같은 날 B안 — 한눈 요약을 두 번 쓰던 중복 제거).
    요약을 낸 모델(계열·구체 모델, 추론 기본값)로 한 번 더 부르고, 실패·불릿 수 불일치·한자 누출이면
    한 번만 다시 시도한다. 그래도 안 되면 빈 머리글만 걷어 내고 요약은 그대로 저장한다 — 요약 저장을
    막지 않는다. 비상 스위치 YT_BRIEF_REBUILD=0(이때는 한눈 요약 없이 저장)."""
    if os.environ.get("YT_BRIEF_REBUILD", "1") == "0":
        return brief_rebuild.drop_empty(body)
    try:
        built = brief_rebuild.build_prompt(body)
        call = yt_titles._TITLE_TR_PROVIDERS.get(family)
        if not built or not call:
            return brief_rebuild.drop_empty(body)
        prompt, n = built
        for attempt in (1, 2):
            out, err = call(prompt, title or "한눈 요약", model, "default", purpose="brief")
            bullets = brief_rebuild.parse(out, n) if not err else None
            if not bullets:
                log.warning("한눈 요약 생성 실패 %d/2(%s): %s", attempt, family,
                            err or f"불릿 수 불일치(소제목 {n}개)")
                continue
            leaks = _script_leaks("\n".join(bullets), body)
            if leaks:
                log.warning("한눈 요약 생성 실패 %d/2(%s): 문자 누출 %s", attempt, family, leaks[0][:80])
                continue
            return _polish_summary(brief_rebuild.replace(body, bullets))
    except Exception as e:  # noqa: BLE001 — 한눈 요약 때문에 요약 저장이 깨지면 안 된다
        log.warning("한눈 요약 생성 실패(%s): %s", family, e)
    return brief_rebuild.drop_empty(body)


# Claude Code CLI(에이전트)가 요약을 '파일로 저장'하려다 권한 거부되면 본문 앞에
# "파일 쓰기 권한이 없어..." 같은 메타문구를 붙이는 경우가 있다. 요약은 출력 형식상
# 항상 '# 제목' H1로 시작하므로, 첫 H1 앞의 군더더기는 저장 전에 잘라낸다(결정적 보장).
_GFM_TABLE_BLOCK_RE = re.compile(r"(^|\n)((?:\|[^\n]*\n)+)")
_GFM_TABLE_DELIM_RE = re.compile(r"^\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")


def _ensure_gfm_tables(text: str) -> str:
    """헤더 다음 구분행이 없는 GFM 표를 고쳐, marked가 <p>로 붕괴시키지 않게 한다.

    유튜브 제목의 `|` 때문에 모델이 `\\|`는 넣으면서 `| --- | --- |` 는 빼먹는 경우가 있다.
    """
    if not text or "|" not in text:
        return text
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x01T{len(fences) - 1}\x01"

    text = re.sub(r"```[\s\S]*?```", _stash, text)

    def _insert_delim(m: re.Match) -> str:
        pre, block = m.group(1), m.group(2)
        trimmed = block.endswith("\n")
        lines = (block[:-1] if trimmed else block).split("\n")
        if len(lines) < 2 or _GFM_TABLE_DELIM_RE.match(lines[1]):
            return m.group(0)
        cols = max(0, lines[0].count("|") - 1)
        if cols < 2:
            return m.group(0)
        lines.insert(1, "|" + " --- |" * cols)
        out = "\n".join(lines) + ("\n" if trimmed else "")
        return pre + out

    text = _GFM_TABLE_BLOCK_RE.sub(_insert_delim, text)
    if fences:
        text = re.sub(r"\x01T(\d+)\x01", lambda m: fences[int(m.group(1))], text)
    return text


def _clean_summary(text: str) -> str:
    if not text:
        return text
    # 일부 CLI 응답은 메타 문구 끝에 줄바꿈 없이 H1을 바로 붙인다.
    # 단일 '#'만 허용해 '### 소제목'을 제목으로 오인하지 않는다.
    m = re.search(r"(?<!#)#[ \t]+\S", text)
    if m and m.start() > 0:
        text = text[m.start():].lstrip()
    return _ensure_gfm_tables(text)


def _prepare_summary_body(text: str) -> str:
    """저장·송출 직전: CLI 군더더기 제거 후 im-not-ai 결정적 윤문."""
    return _polish_summary(_clean_summary(text))


def _polish_summary(cleaned: str) -> str:
    """결정적 윤문 + 회사·브랜드명 원문 표기·영문 이름 뒤 조사 교정(notation)
    + 각주 표식과 각주 행의 짝 맞추기(term_marks)."""
    body = notation.normalize_summary(humanize_korean.humanize_summary(cleaned))
    try:
        return term_marks.fix(body)[0]
    except Exception:  # noqa: BLE001 — 교정 실패가 요약 저장을 막으면 안 된다
        return body


# 요약 생성용 시스템 프롬프트: 출력 전용 강제(도구/파일/승인 언급 금지).
_SUMMARY_SYS = ("요청된 마크다운 요약 결과 본문만 그대로 출력한다. "
                "파일을 생성·저장하지 말고, 도구를 사용하거나 저장 여부·권한·승인을 "
                "언급하거나 묻지 말 것. 메타 코멘트 없이 결과만 출력한다.")


def _summary_system_args() -> list:
    """요약 호출의 시스템 프롬프트 인자. 최소 구성이면 CLI 기본 프롬프트를 교체하고 그 뒤에
    _SUMMARY_SYS를 붙이며(도구·MCP도 끔), 최소 구성을 못 쓰면 예전처럼 기본 프롬프트 뒤에 덧붙인다."""
    return llm_gateway.claude_minimal_args(_SUMMARY_SYS) or ["--append-system-prompt", _SUMMARY_SYS]


def _model_line(label: str, note: str = "") -> str:
    """요약에 심는 '요약 모델' 마커(HTML 주석). common.js가 추출해 메타 칩(YouTube 보기 옆)으로
    렌더한다. 렌더 안 되는 환경에선 주석이라 화면에 안 보임(graceful)."""
    tail = f" · {note}" if note else ""
    return f"<!--SUMMARY_MODEL:{label}{tail}-->\n\n"


def _compress_line(summary_body: str, transcript_chars: int) -> str:
    """요약/전사 압축률 마커. common.js가 '🗜 원문 대비 N%' 칩으로 렌더."""
    if not transcript_chars or not summary_body:
        return ""
    pct = max(1, round(len(summary_body) / transcript_chars * 100))
    return f"<!--SUMMARY_COMPRESS:{pct}-->\n\n"


def _summarize_with_gpt(prompt: str, *, reasoning_effort: str = "default",
                        title: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Codex CLI의 GPT 모델로 단일턴 요약. (요약 텍스트, 오류사유). model은 슬롯의 구체 모델."""
    return yt_models._llm_text("gpt", _SUMMARY_SYS + "\n\n" + prompt, model=model, effort=reasoning_effort,
                     purpose="summary", title=title, timeout=yt_core.GPT_TIMEOUT,
                     postprocess=_prepare_summary_body)


def _summarize_with_gemini(prompt: str, *, reasoning_effort: str = "default",
                           title: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Gemini API(키)로 단일턴 요약(2026-11-16 사용자 지시). (요약 텍스트, 오류사유).
    GPT와 같은 프롬프트(_SUMMARY_SYS + 본 프롬프트). 실패·무료 한도(429)는 다음 슬롯이 받는다."""
    return yt_models._llm_text(llm_gateway.GEMINI_FAMILY, _SUMMARY_SYS + "\n\n" + prompt, model=model,
                     effort=reasoning_effort, purpose="summary", title=title, timeout=yt_core.GEMINI_TIMEOUT,
                     postprocess=_prepare_summary_body)


# Grok 전용 문체 보정. 2026-09-16 Opus·Grok 각 5건 비교에서 Grok만 반복한 결함을 겨냥한다 —
# 사전형 종결(보다·숏하다), 명사 나열 전보체, 주어 생략, 영어 조각(twoweeks 전), 표기 흔들림
# (harness/하니스, Coxon/Coxson). 정확도는 같았으므로 사실 규칙은 건드리지 않고 문장 마감만 잡는다.
# 본 프롬프트 앞에 붙여 먼저 읽히게 한다(전사가 뒤에 오므로 끝에 붙이면 묻힌다).
_GROK_STYLE_NOTE = """[문체 보정 — 아래 규칙은 본 지시문보다 우선한다]
- 모든 문장은 한국어 종결어미로 끝낸다(~다·~했다·~본다). '보다', '숏하다', '추적하다'처럼 사전형 동사나 명사로 문장을 끝내지 않는다.
- can·may·could를 그대로 '~할 수 있다'로 옮기지 않는다. 일반적 사실이면 '~한다', 경향이면 '~하기도 한다', 선택지면 '~하면 된다', 조건부면 '~하는 경우도 있다'로 옮긴다. 한 문단에 '~할 수 있다'가 세 번을 넘으면 다시 쓴다.
  (AI can help researchers → AI는 연구자를 돕는다 ○ / AI는 연구자를 도울 수 있다 ✗)
  다만 화자가 불확실성·유보를 말하는 자리(추정·전망·조건·한계)에서는 그 뉘앙스를 단정으로 바꾸지 않는다.
  (binding does not guarantee the effect → 결합한다고 효과가 보장되지는 않는다 ○)
- 주어가 같고 내용이 이어지면 문장을 쪼개지 말고 '~하고'·'~며'·'~거나'·'~지만'으로 잇는다. 같은 주어를 문장마다 다시 적는 글은 실패한 번역이다. 다만 한 문장에 절을 셋 이상 겹치지는 않는다.
  (고객은 모델 사용료를 낼 수 있다. 고객은 소프트웨어 접근 대가를 낼 수 있다. 고객은 컴퓨팅 비용을 낼 수 있다. ✗
   → 고객은 모델 사용료나 소프트웨어 이용료, 컴퓨팅 비용을 낸다. ○)
- 나열은 쉼표나 가운뎃점으로 하고 '과/와'는 마지막 항목 앞에만 쓴다. '과/와'를 연달아 잇지 않는다.
  (생물 모델과 에이전트 인터페이스와 실험 파트너 ✗ → 생물 모델, 에이전트 인터페이스, 실험 파트너 ○)
- 영어 단어를 한국어 문장 안에 조각으로 남기지 않는다. 일반명사·부사·형용사도 반드시 옮긴다(eventual 역할 ✗ → 최종적으로 맡을 역할 ○, hyperscaler ✗ → 하이퍼스케일러 ○, twoweeks 전 ✗ → 2주 전 ○). 영문 고유명사·약어만 원문 표기로 둔다.
- 영어 술어를 축자 번역하지 않고 한국어에서 그 뜻으로 쓰는 표현을 찾는다.
  (약에 대한 이해관계를 남긴다 ✗ → 그 약의 지분을 계속 갖는다 ○ / 그 작업을 수행한 대가로 여전히 받을 수 있다 ✗ → 그래도 수임료는 받는다 ○ / 녹기가 더 나쁘다 ✗ → 잘 녹지 않는다 ○)
- 영어의 동명사·부정사를 '~하는 일'로 기계 치환하지 않는다.
  (다음 단계는 효과를 시험하는 일이다 ✗ → 다음 단계는 효과를 시험하는 것이다 ○)
- 인터뷰·대담에서 발언자가 바뀌면 그 문장 첫머리에서 이름으로 밝힌다("Nadella는"). 같은 발언자가 이어지는 동안에는 반복하지 않는다. 화자가 한 명인 영상은 본 지시문 '원칙'대로 "진행자는"·"그는"·"영상은" 같은 주어를 세우지 않는다.
- 같은 인명·용어는 요약 전체에서 한 가지 표기만 쓴다. harness면 끝까지 harness, 하니스면 끝까지 하니스. 인명 철자는 전사에 나온 대로 한 번 정하고 바꾸지 않는다.
- 이 보정은 표현에만 해당한다. 사실·수치·화자 귀속 규칙은 본 지시문을 그대로 따른다.

"""


def _summarize_with_grok(prompt: str, *, reasoning_effort: str = "default",
                         title: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Grok CLI 단일턴 요약. (요약 텍스트, 오류사유). 프롬프트 맨 앞에 Grok 전용 문체 보정을 붙인다."""
    return yt_models._llm_text("grok", _GROK_STYLE_NOTE + prompt, model=model, effort=reasoning_effort,
                     purpose="summary", title=title, timeout=yt_core.GROK_TIMEOUT,
                     postprocess=_prepare_summary_body)


def _summary_attempts(model_order=None, reasoning_levels=None, slots=None) -> list[dict]:
    """시도할 순서 → [{family, model, effort}].

    slots(모니터 요약 슬롯, 구체 모델+추론)를 우선 쓰고, 없으면 옛 방식의 계열 순서
    (model_order)를 현재 버전 설정과 계열별 추론 수준으로 풀어 쓴다.
    """
    if slots:
        picked = llm_gateway.normalize_summary_slots(slots, [], preserve_unknown=True)
        # 같은 모델을 여러 슬롯에 두는 건 '시작 비중'을 높이려는 것이다(2026-09-27 사용자 지시).
        # 실패 폴백에서는 방금 실패한 모델을 다시 부르지 않게 첫 등장만 남긴다
        # (예: Sol·Opus·Sol → Sol 실패 시 Opus, 그다음 Sol을 또 부르지 않는다).
        seen: set[str] = set()
        out = []
        for s in picked:
            if s["model"] in seen:
                continue
            seen.add(s["model"])
            out.append({"family": llm_gateway.model_family(s["model"]), "model": s["model"],
                        "effort": s["effort"]})
        return out
    order = llm_gateway.normalize_model_order(model_order)
    reasoning = llm_gateway.normalize_reasoning_levels(reasoning_levels)
    versions = yt_models._model_versions()
    out = []
    for key in order:
        if key == llm_gateway.NONE_KEY:
            break
        out.append({"family": key, "model": versions.get(key, "grok"),
                    "effort": reasoning.get(key, "default")})
    return out


def _summarize_ordered(prompt: str, save_path: str | None, model_order=None,
                       *, transcript_chars: int = 0, reasoning_levels=None,
                       title: str | None = None, slots=None):
    """지정 순서대로 요약 모델을 시도하는 자동모니터용 SSE 생성기(슬롯 단위)."""
    attempts = _summary_attempts(model_order, reasoning_levels, slots)
    failures: list[str] = []
    client_has_output = False

    def reset_client():
        nonlocal client_has_output
        if client_has_output:
            client_has_output = False
            return "event: reset\ndata: \"\"\n\n"
        return ""

    def save_full(body: str, label: str) -> tuple[str, str]:
        cline = _compress_line(body, transcript_chars)
        full = _model_line(label) + cline + body
        if save_path:
            try:
                document_io.atomic_write_text(save_path, full)
                _reindex_summary(save_path)
            except Exception as e:
                return "", "저장 실패: " + str(e)
            name_fix.schedule(save_path, on_change=_reindex_summary)   # 인명 원문 표기·소개(뒤에서)
        return full, ""

    for attempt in attempts:
        key, slot_model, effort = attempt["family"], attempt["model"], attempt["effort"]
        if key == "opus":
            marker_sent = False
            stream = llm_runners.stream_claude(
                prompt, claude_bin=yt_core._resolve_claude_bin(), model=slot_model, effort=effort,
                title=title, timeout=yt_core.CLAUDE_TIMEOUT, system_args=_summary_system_args())
            with contextlib.closing(stream):
                try:
                    while True:
                        kind, value = next(stream)
                        if kind == "model":
                            marker_sent = True
                            client_has_output = True
                            yield f"data: {json.dumps(_model_line(yt_models._model_label(value)))}\n\n"
                            continue
                        if not marker_sent:
                            marker_sent = True
                            client_has_output = True
                            yield f"data: {json.dumps(_model_line(yt_models._model_label(slot_model)))}\n\n"
                        client_has_output = True
                        yield f"data: {json.dumps(value)}\n\n"
                except StopIteration as stop:
                    outcome = stop.value
            error_msg, used_model = outcome.error, outcome.used_model

            cleaned = _clean_summary(outcome.text)
            body = _polish_summary(cleaned)
            if not error_msg and body:
                body = _rebuild_brief(body, "opus", slot_model, title)
                label = yt_models._model_label(used_model or slot_model)
                if body != cleaned and client_has_output:
                    reset = reset_client()
                    if reset:
                        yield reset
                    full, save_err = save_full(body, label)
                    if save_err:
                        yield f"event: error\ndata: {json.dumps(save_err)}\n\n"
                        return
                    yield f"data: {json.dumps(full)}\n\n"
                else:
                    cline = _compress_line(body, transcript_chars)
                    if cline:
                        yield f"data: {json.dumps(cline)}\n\n"
                    _, save_err = save_full(body, label)
                    if save_err:
                        yield f"event: error\ndata: {json.dumps(save_err)}\n\n"
                        return
                yield "event: done\ndata: \n\n"
                return
            failures.append(f"{yt_models._model_label(slot_model)}: {error_msg or '빈 응답'}")
            log.warning("summarize opus 실패 → 다음 모델: %s", failures[-1])
            reset = reset_client()
            if reset:
                yield reset
            continue

        def _call(p: str):
            if key == "gpt":
                return _summarize_with_gpt(p, reasoning_effort=effort, title=title,
                                           model=slot_model)
            if key == llm_gateway.GEMINI_FAMILY:
                return _summarize_with_gemini(p, reasoning_effort=effort, title=title,
                                              model=slot_model)
            return _summarize_with_grok(p, reasoning_effort=effort, title=title, model=slot_model)
        body, err = _call(prompt)
        if key in ("gpt", llm_gateway.GEMINI_FAMILY):
            label = yt_models._model_label(slot_model)
        else:
            # -m 없이 CLI 기본 모델로 돌았으면 실제 모델 id를 조회해 버전까지 남긴다.
            label = yt_models._model_label(llm_gateway.grok_call_model(slot_model, yt_core.GROK_MODEL) or llm_gateway.resolve_grok_default_model() or "grok")
        # 한자·이질 문자 누출 게이트: 같은 모델에 한 번만 재요청하고, 그래도 섞이면 실패로
        # 보고 다음 모델로 넘긴다(2026-09-12 사용자 지적: All-In 요약의 主张).
        if body:
            leaks = _script_leaks(body, prompt)
            if leaks:
                log.warning("summarize %s: 문자 누출 %d건 — 같은 모델로 1회 재요청: %s",
                            key, len(leaks), leaks[0][:80])
                retry, rerr = _call(prompt + _SCRIPT_RETRY_NOTE.format(
                    leaks=", ".join(l.split(" … ")[0] for l in leaks[:3])))
                if retry and not _script_leaks(retry, prompt):
                    body = retry
                else:
                    body, err = "", f"한자·이질 문자 누출({len(leaks)}건) — 재요청 후에도 남음"
        if not body:
            failures.append(f"{label}: {err or '빈 응답'}")
            log.warning("summarize %s 실패 → 다음 모델: %s", key, failures[-1])
            continue
        body = _rebuild_brief(body, key, slot_model, title)
        full, save_err = save_full(body, label)
        if save_err:
            yield f"event: error\ndata: {json.dumps(save_err)}\n\n"
            return
        reset = reset_client()
        if reset:
            yield reset
        yield f"data: {json.dumps(full)}\n\n"
        yield "event: done\ndata: \n\n"
        return

    yield f"event: error\ndata: {json.dumps('모든 요약 모델 실패: ' + ' / '.join(failures))}\n\n"


def _summarize_with_claude(prompt: str, save_path: str | None, *, skip_claude: bool = False,
                           transcript_chars: int = 0, title: str | None = None):
    """Claude Code CLI로 요약 → SSE 청크 생성, 완료 시 save_path에 저장.
    Claude 실패 시 Grok CLI로 폴백(GROK_FALLBACK).
    skip_claude=True 이면 Claude 호출 없이 Grok만 시도(게이트가 Claude 불가로 판정한 경우).

    프롬프트는 argv가 아니라 stdin으로 전달한다(긴 전사로 인한 ARG_MAX 초과 회피).
    """
    def _yield_grok(err_prefix: str = ""):
        if not yt_core.GROK_FALLBACK:
            yield f"event: error\ndata: {json.dumps((err_prefix or 'Claude 스킵') + ' / Grok 폴백 비활성')}\n\n"
            return
        log.warning("summarize → Grok CLI%s", " (skip_claude)" if skip_claude else " 폴백 시도")
        gtext, gerr = _summarize_with_grok(prompt, title=title)
        if gtext:
            gtext = _rebuild_brief(gtext, "grok", yt_core.GROK_MODEL or None, title)
            # Claude 부분 출력이 이미 전송됐을 수 있으므로 클라이언트가 본문을 비우게 한다.
            yield "event: reset\ndata: \"\"\n\n"
            log.info("summarize grok 성공 (%d자)", len(gtext))
            grok_label = yt_models._model_label(yt_core.GROK_MODEL or llm_gateway.resolve_grok_default_model() or "grok")
            full = (_model_line(grok_label)
                    + _compress_line(gtext, transcript_chars) + gtext)
            yield f"data: {json.dumps(full)}\n\n"
            if save_path:
                try:
                    document_io.atomic_write_text(save_path, full)
                except Exception as e:
                    yield f"event: error\ndata: {json.dumps('저장 실패: ' + str(e))}\n\n"
                    return
                _reindex_summary(save_path)
            yield "event: done\ndata: \n\n"
            return
        log.warning("summarize grok 실패: %s", gerr)
        msg = (err_prefix + " / " if err_prefix else "") + f"Grok 폴백 실패: {gerr}"
        yield f"event: error\ndata: {json.dumps(msg)}\n\n"

    if skip_claude:
        yield from _yield_grok("Claude 스킵(게이트 판정)")
        return

    stream = llm_runners.stream_claude(
        prompt, claude_bin=yt_core._resolve_claude_bin(), model=yt_models._claude_model(), title=title,
        timeout=yt_core.CLAUDE_TIMEOUT, system_args=_summary_system_args())
    with contextlib.closing(stream):
        try:
            while True:
                kind, value = next(stream)
                if kind == "model":
                    yield f"data: {json.dumps(_model_line(yt_models._model_label(value)))}\n\n"
                else:
                    yield f"data: {json.dumps(value)}\n\n"
        except StopIteration as stop:
            outcome = stop.value
    error_msg, used_model = outcome.error, outcome.used_model

    if error_msg:
        log.warning("summarize failed (claude): %s", error_msg)
        yield from _yield_grok(error_msg)
        return

    cleaned = _clean_summary(outcome.text)
    body = _polish_summary(cleaned)
    if not body:
        yield from _yield_grok("Claude 빈 응답")
        return
    body = _rebuild_brief(body, "opus", yt_models._claude_model(), title)
    cline = _compress_line(body, transcript_chars)
    full = _model_line(yt_models._model_label(used_model or yt_models._claude_model())) + cline + body
    if body != cleaned and outcome.chunks:
        yield "event: reset\ndata: \"\"\n\n"
        yield f"data: {json.dumps(full)}\n\n"
    elif cline:
        yield f"data: {json.dumps(cline)}\n\n"
    if save_path:
        try:
            document_io.atomic_write_text(save_path, full)
        except Exception as e:
            yield f"event: error\ndata: {json.dumps('저장 실패: ' + str(e))}\n\n"
            return
        _reindex_summary(save_path)
        name_fix.schedule(save_path, on_change=_reindex_summary)
    yield "event: done\ndata: \n\n"


_SENSE_PROMPT_CHARS = 40     # 프롬프트에 싣는 뜻 길이 상한 — 뜻을 가르는 데는 첫머리면 충분하다


def _term_exclusions_for_prompt() -> list:
    """프롬프트용 제외 목록: 뜻이 기록된 용어는 (용어, [뜻…]), 아니면 용어 문자열."""
    out = []
    for info in db.term_exclusion_senses().values():
        out.append((info["term"], info["senses"]) if info["senses"] else info["term"])
    return out


def _inject_term_exclusions(prompt: str, terms: list) -> str:
    """사용자 지정 각주 제외 용어를 요약 프롬프트에 심는다.

    프롬프트 본문의 고정 제외 목록과 같은 층위로 읽히도록 전사 자리표시자가 있는
    줄 바로 앞에 넣는다(자리표시자가 없으면 끝에 붙인다). 항목은 용어 문자열(모든 뜻을
    뺀다) 또는 (용어, [뜻…]) — 뜻이 있으면 그 뜻으로 쓰일 때만 빼고, 같은 표기의 다른 뜻은
    평소 기준대로 각주를 단다(2026-10-30 사용자 지시: 하네스 AI 실행 환경 ↔ 차량 배선).
    """
    plain, scoped = [], []
    for t in terms or []:
        if isinstance(t, (tuple, list)):
            term = str(t[0]).strip()
            senses = [re.sub(r"\s+", " ", str(x)).strip() for x in (t[1] if len(t) > 1 else []) if str(x).strip()]
            if term and senses:
                scoped.append(term + "(" + " / ".join(x[:_SENSE_PROMPT_CHARS] for x in senses) + ")")
            elif term:
                plain.append(term)
        elif str(t).strip():
            plain.append(str(t).strip())
    if not plain and not scoped:
        return prompt
    line = ""
    if plain:
        line += ("- 사용자가 지정한 다음 용어도 설명 없이 이해되는 말로 보고 별표와 해설을 붙이지 않는다: "
                 + "·".join(plain) + "\n")
    if scoped:
        line += ("- 사용자가 지정한 다음 용어는 괄호 속 뜻으로 쓰일 때 설명 없이 이해되는 말로 보고 별표와 "
                 "해설을 붙이지 않는다. 같은 표기라도 다른 뜻으로 쓰이면 평소 기준대로 판단한다: "
                 + "; ".join(scoped) + "\n")
    line += "\n"
    idx = prompt.find("{transcript}")
    if idx < 0:
        return prompt.rstrip("\n") + "\n\n" + line.rstrip("\n")
    line_start = prompt.rfind("\n", 0, idx) + 1
    return prompt[:line_start] + line + prompt[line_start:]


def _term_exclusion_state() -> dict:
    """terms = 제외 용어 표시명, senses = 용어별 뜻(칩 툴팁), keep = 뜻이 달라 남길 각주 키
    ('용어 키\x01설명 키', common.js `_defKey`와 같은 규칙). 판정이 없는 각주는 예전처럼 뺀다."""
    try:
        excl = db.term_exclusion_senses()
        return {"terms": [v["term"] for v in excl.values()],
                "senses": {v["term"]: v["senses"] for v in excl.values() if v["senses"]},
                "keep": term_senses.kept_keys()}
    except Exception:  # noqa: BLE001 — 뜻 정보가 깨져도 표기 목록은 돌려준다
        return {"terms": db.list_term_exclusions()}


def _parse_summary_title(md_path: str) -> str:
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:]
    except Exception:
        pass
    return os.path.basename(md_path)[:-3]
