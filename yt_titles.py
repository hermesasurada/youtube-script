"""외국어 영상 제목의 한국어 표기(YouTube 한국어 제목 → 없으면 LLM 번역).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import os
import re
import threading
import db
import notation
import llm_gateway
import yt_listing
import yt_core
import yt_models
import logging

log = logging.getLogger("yt-script")

# ── 제목 번역 ──────────────────────────────────────────────────────────
# 외국어 제목 영상은 목록·요약 팝업에서 무슨 내용인지 바로 안 읽힌다.
# 제목만 한국어로 옮겨 두고(원문은 그대로 병기), 화면에서 함께 보여준다.

# 제목 번역 전용 모델 — GPT-6 Luna, 추론은 기본값(플래그 없음)(2026-09-23 사용자 지시).
# 예전에는 요약 슬롯을 돌며 Claude는 sonnet-5·추론 low였다. Luna가 실패한 묶음만
# 요약 슬롯 순번으로 넘긴다(각 슬롯 모델, 추론 기본값) — 다음 틱까지 미뤄지지 않게.
TITLE_TR_MODEL   = os.environ.get("TITLE_TR_MODEL", "gpt-6-luna")
TITLE_TR_BATCH   = 40            # 한 번 호출에 묶는 제목 수
TITLE_TR_TIMEOUT = 240
TITLE_TR_REASONING = os.environ.get("TITLE_TR_REASONING", "default")
# 설정 팝업에서 고른 값이 있으면 그것이 위 기본값을 대신한다(2026-09-29 사용자 지시).


def _title_tr_setting() -> tuple[str, str]:
    """제목 번역 1순위 (모델, 추론) — 저장값, 없으면 환경변수 기본값."""
    try:
        saved = db.get_title_model()
    except Exception:
        saved = {}
    return (saved.get("model") or TITLE_TR_MODEL,
            saved.get("effort") or TITLE_TR_REASONING)

_TITLE_TR_PROMPT = """다음 유튜브 영상 제목들을 한국어로 번역한다.

규칙:
- 고유명사는 원문 철자 그대로 둔다 — 기업·제품·모델·코드명·행사·매체·기관명(NVIDIA, Google, Tesla, Apple, YouTube, ChatGPT, Hugging Face, Jalapeño, GPT-5.6 Sol, Neutron, S&P 500, NASA)과 인명(Sam Altman, Jensen Huang, Mark Zuckerberg, Bill Ackman, Paul Graham, Lutnick). 음차하지 않는다(제이미 다이먼 ×, 마크 저커버그 ×, 엔비디아 ×, 구글 ×, 나사 ×, 로켓랩 ×, 허깅페이스 ×). 한글 표기가 흔한 회사·브랜드도 원문으로 쓴다. 원문 철자 뒤에 괄호로 한글을 덧붙이지도 않는다.
  한글 표기가 굳은 인명도 예외 없이 원문 철자로 쓴다(일론 머스크 × → Elon Musk). 지명은 널리 알려진 곳이면 한글(실리콘밸리, 캘리포니아, 뉴욕, 인도)로 쓰고 낯선 곳은 원문 철자로 둔다. 직함은 CEO·CFO처럼 원문 약어 그대로 둔다(최고경영자 ×). 같은 사람·회사는 어느 제목에서든 같은 표기를 쓴다(지명도 같다).
- 영문 이름 뒤 조사는 철자가 아니라 한국어로 읽은 끝소리에 맞춘다: Anthropic이·Anthropic과(Anthropic가 ×, Anthropic와 ×), Google은·Google로, OpenAI가·OpenAI와, NVIDIA는.
- Neocloud는 뉴클라우드·네오클라우드로 음차하지 않고 항상 원문 표기 Neocloud를 유지한다.
- 고유명사가 아닌 부분은 남김없이 한국어로 옮긴다. 제목 앞머리나 부제를 영어로 남겨 두지 않는다("Making Cities Awesome: …" → "도시를 멋지게 만들기: …", "Tariffs, DOGE & …" → "관세, DOGE, …"). 단 시리즈·프로그램명(The a16z Show, YC Paper Club, Lex Fridman Podcast, The Circuit, Sovereignty Bootcamp)은 그대로 둔다.
- 사전 뜻을 그대로 옮긴 딱딱한 직역을 피하고 한국어 영상 제목처럼 읽히게 쓴다. 자주 쓰는 표현은 이렇게 옮긴다: fireside chat → 대담(노변담화 ×), All-Hands → 전사 미팅, Explained → 해부 또는 총정리, Inside X → X 내부/들여다보기, JUST RECORDED → [방금 공개], Special Address → 특별 연설, Keynote → 기조연설, Full Interview → 인터뷰 전체, Recap → 정리, How to: X → X하는 법(앞머리 "How to:"는 지운다).
- 한자(發·對 등)와 옛 신문투를 쓰지 않는다. 숫자·날짜 표기는 원문 형식을 유지한다(9/2/2026). 달러 금액은 $와 축약 단위로 쓴다($9.25B·$500M은 그대로, $9.25 Billion → $9.25B, 92.5억 달러 ×).
- 원문의 어조(질문형·감탄형·대문자 강조)와 정보량을 유지하되, 더하거나 빼지 않는다. 의문형 제목은 "~인가/~일까"로 자연스럽게 옮기고, 설명형 제목은 명사구로 간결하게 끝낸다("Which Companies X Is Bullish On" → "X가 낙관하는 기업").
- 이미 한국어인 제목은 그대로 둔다.

출력: 입력 순서와 같은 길이의 JSON 문자열 배열만. 설명·코드펜스 없이 배열만 출력한다.

입력:
"""


def _preserve_title_terms(source: str, translated: str) -> str:
    """제목 번역 모델이 사용자 지정 원어 표기를 음차해도 결정적으로 복원한다."""
    # 흔한 한글 표기 회사·브랜드명 → 원문, 영문 이름 뒤 조사 교정(2026-09-26 A안).
    # 원래 한국어인 제목은 번역이 아니라 원문이므로 건드리지 않는다.
    if not re.search(r"[가-힣]", source or ""):
        translated = notation.normalize(translated or "")
    if re.search(r"\bneoclouds?\b", source or "", re.I):
        translated = re.sub(r"(?:뉴|네오)\s*클라우드(?:들)?", "Neocloud", translated or "")
    # fireside chat → '노변담화'는 사전 직역이라 제목에 어색하다(2026-09-11 지시). '대담'으로.
    if re.search(r"\bfireside\s+chat\b", source or "", re.I):
        translated = re.sub(r"노변\s*담화(?:\s*(?:참여|참가))?", "대담", translated or "")
    return translated


def _parse_title_json(out: str, n: int) -> list[str] | None:
    """모델 응답에서 JSON 배열만 건져낸다(코드펜스·군더더기 허용)."""
    s = (out or "").strip()
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    return [str(x).strip() for x in arr]


def _title_tr_order(first_id: int = 0) -> tuple[tuple[str, str], ...]:
    """제목 번역 순번 — (계열, 모델) 목록. 설정의 제목 번역 모델이 늘 먼저다.

    폴백은 요약 슬롯이다. 번역은 묶음으로 부르므로 묶음 첫 항목의 rowid로
    슬롯 순서를 돌린다 — 같은 묶음을 다시 시도해도 순서가 같다.
    """
    model = _title_tr_setting()[0]
    primary = (llm_gateway.model_family(model), model)
    try:
        slots = db.get_monitor_summary_slots()["slots"]
    except Exception:
        slots = [{"model": m} for m in (yt_core.CLAUDE_MODEL, yt_core.GPT_MODEL, "grok")]
    pairs = [(llm_gateway.model_family(x["model"]), x["model"]) for x in slots]
    offset = (max(1, int(first_id or 1)) - 1) % len(pairs)
    rest = [p for p in pairs[offset:] + pairs[:offset] if p != primary]
    return (primary, *dict.fromkeys(rest))


def _title_tr_runner(family: str):
    """제목 번역·한눈 요약용 실행기 — (prompt, label, model, effort, purpose) → (텍스트, 오류사유)."""
    def call(prompt: str, label: str, model: str | None = None, effort: str = "default",
             purpose: str = "title") -> tuple[str, str]:
        if family == "grok" and effort not in llm_gateway.llm_catalog.levels(model or "grok"):
            effort = "default"
        return yt_models._llm_text(family, prompt, model=model, effort=effort, purpose=purpose, title=label,
                         timeout=TITLE_TR_TIMEOUT)
    return call


_TITLE_TR_PROVIDERS = {key: _title_tr_runner(key)
                       for key in ("opus", "grok", "gpt", llm_gateway.GEMINI_FAMILY)}


def _translate_titles(titles: list[str], *, first_id: int = 0) -> list[str] | None:
    # YouTube 한국어 제목이 없는 외국어 제목에만 쓴다(2026-11-10 사용자 지시로 다시 사용).
    """제목 묶음을 번역. 실패하면 None(다음 기회에 다시 시도).

    설정의 제목 번역 모델(기본 GPT-6 Luna)이 그 추론 수준으로 먼저 옮기고, 실패하면
    요약 슬롯 순번으로 넘긴다(폴백은 추론 기본값).
    """
    if not titles:
        return []
    body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    prompt = _TITLE_TR_PROMPT + body
    label = f"제목 {len(titles)}건 번역"
    stdout = ""
    first_effort = _title_tr_setting()[1]
    for idx, (name, slot_model) in enumerate(_title_tr_order(first_id)):
        call = _TITLE_TR_PROVIDERS.get(name)
        if not call:
            continue
        stdout, err = call(prompt, label, slot_model, first_effort if idx == 0 else "default")
        if not err and stdout.strip():
            parsed = _parse_title_json(stdout, len(titles))
            if parsed is not None:
                return [_preserve_title_terms(source, translated)
                        for source, translated in zip(titles, parsed)]
            err = "형식 불일치"
        log.warning("title translate via %s failed: %s", name, err or "빈 응답")
    return None


def translate_pending_titles(limit: int = TITLE_TR_BATCH) -> dict:
    """번역이 비어 있는 항목을 한 묶음 처리한다. 배치 스크립트와 훅이 함께 쓴다.

    1순위는 YouTube가 한국 시청자에게 보여 주는 한국어 제목(2026-11-07 사용자 지시). 그것이
    없는 외국어 제목만 LLM으로 번역한다(2026-11-10 사용자 지시 — 블룸버그처럼 한국어 제목을
    달지 않는 채널이 원제로 남았다). LLM 번역이 실패하면 비워 두고 다음 기회에 다시 한다."""
    db.mark_korean_titles()                       # 한국어 제목은 대상에서 확정 제외
    todo = db.titles_needing_translation(limit)
    if not todo:
        return {"done": 0, "remaining": 0}
    n, listings, need_llm = 0, {}, []
    for item in todo:
        cid = yt_listing.channel_id_of(item.get("channel_url") or "")
        if cid and cid not in listings:
            listings[cid] = yt_listing.korean_entries(cid)
        entries = listings.get(cid) or {}
        if cid and not entries:
            continue                                # 목록을 못 읽었으면 다음 기회에 다시
        ko = yt_listing.korean_title(entries.get(item["yt_id"]), item["title"])
        if ko:
            n += db.set_title_ko(item["yt_id"], yt_core._nfc(ko))
        else:
            need_llm.append(item)
    llm = 0
    if need_llm:
        translated = _translate_titles([i["title"] for i in need_llm],
                                       first_id=int(need_llm[0].get("rowid") or 0))
        if translated is not None:
            for item, ko in zip(need_llm, translated):
                ko = yt_core._nfc((ko or "").strip())
                # 한글이 없거나 원제와 같으면 번역이 필요 없는 제목 — 원제만 쓴다.
                if not db._HANGUL_RE.search(ko) or ko == item["title"].strip():
                    ko = ""
                db.set_title_ko(item["yt_id"], ko)
                llm += 1 if ko else 0
    log.info("title from youtube: %d건, llm: %d건", n, llm)
    return {"done": n + llm, "remaining": len(db.titles_needing_translation(limit))}


_title_tr_running = threading.Lock()


def _translate_titles_async() -> None:
    """요약 저장 뒤 새 제목의 한국어 표기를 조용히 채운다(중복 실행 방지, 실패해도 무시)."""
    if not _title_tr_running.acquire(blocking=False):
        return

    def run():
        try:
            translate_pending_titles()
        except Exception as e:                      # noqa: BLE001
            log.warning("title translate hook failed: %s", e)
        finally:
            _title_tr_running.release()

    threading.Thread(target=run, daemon=True).start()
