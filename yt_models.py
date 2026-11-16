"""요약 모델 설정·라벨·캡처 순서·인명 확인 모델, 그리고 계열별 기본 모델을 채워 llm_runners를 부르는 `_llm_text`.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import re
import db
import name_fix
import llm_gateway
import llm_runners
import yt_core
import yt_titles

def _llm_text(family: str, prompt: str, *, model: str | None, effort: str = "default",
              purpose: str, title: str | None, timeout: int, postprocess=None) -> tuple[str, str]:
    """계열 하나로 프롬프트 한 건 실행(llm_runners.run_text) — 계열별 기본 모델과 실행 파일을 여기서 채운다."""
    if family == "opus":
        model = model or _claude_model()
    elif family == "gpt":
        model = model or _gpt_model()
    elif family == "grok":
        model = llm_gateway.grok_call_model(model, yt_core.GROK_MODEL)
    elif family == llm_gateway.GEMINI_FAMILY:
        model = model or "gemini-4-hydrogen"
    return llm_runners.run_text(family, prompt, model=model or "", effort=effort, purpose=purpose,
                                title=title, timeout=timeout, claude_bin=yt_core._resolve_claude_bin(),
                                grok_bin=yt_core.GROK_BIN, postprocess=postprocess)


_CLAUDE_TIERS = ("opus", "sonnet", "haiku", "fable")
# 별칭(opus)만 알 때 쓰려고, 실제 응답으로 확인된 Claude 모델 ID를 tier별로 기억한다.
# 요약이 한 번 돌면 채워지고, 그 전에는 버전 없이 'Opus'로 표시한다.
_CLAUDE_SEEN: dict[str, str] = {}


def _model_versions() -> dict[str, str]:
    """계열별 대표 모델 — 요약 슬롯에서 그 계열이 처음 나오는 슬롯의 모델.
    슬롯에 없는 계열은 환경변수 기본값(CLAUDE_MODEL·GPT_MODEL). 수동 요약처럼
    모델 하나만 필요한 경로가 쓴다."""
    out = {"opus": yt_core.CLAUDE_MODEL, "gpt": yt_core.GPT_MODEL, "grok": "grok"}
    try:
        seen = set()
        for slot in db.get_monitor_summary_slots()["slots"]:
            fam = llm_gateway.model_family(slot["model"])
            if fam not in seen:
                seen.add(fam)
                out[fam] = slot["model"]
    except Exception:
        pass
    return out


def _claude_model() -> str:
    return _model_versions()["opus"]


def _gpt_model() -> str:
    return _model_versions()["gpt"]


def _model_label(model_id: str) -> str:
    """모델 ID → 사람이 읽는 라벨. Claude는 접두어 없이 등급+버전(2026-09-23 사용자 지시).

    claude-opus-5-5 → Opus 5.5, claude-opus-5 → Opus 5,
    claude-haiku-4-5-20251001 → Haiku 4.5(날짜 접미사 무시),
    grok-4.7 → Grok 4.7, grok-composer-2.5-fast → Grok Composer 2.5, gpt-6-astra → GPT 6 Astra.
    예전엔 세 조각 ID만 인식해 `claude-opus-5`가 원문 그대로 찍혔다.
    """
    mid = (model_id or "").strip()
    low = mid.lower()
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", low)
    if m:
        tier, major, minor = m.groups()
        _CLAUDE_SEEN[tier] = mid
        return f"{tier.capitalize()} {major}.{minor}" if minor else f"{tier.capitalize()} {major}"
    if low in _CLAUDE_TIERS:            # 별칭만 알 때 — 확인된 실제 모델이 있으면 그 버전으로
        seen = _CLAUDE_SEEN.get(low)
        return _model_label(seen) if seen else low.capitalize()
    if low.startswith("grok"):
        v = re.search(r"(\d+)\.(\d+)", mid)
        ver = f" {v.group(0)}" if v else ""
        return ("Grok Composer" if "composer" in low else "Grok") + ver
    if low.startswith("gemini"):          # gemini-4-hydrogen → Gemini 4 Hydrogen(2026-11-16)
        m = re.match(r"gemini-(\d+(?:\.\d+)?)((?:-[a-z0-9]+)*)$", low)
        if m:
            return "Gemini " + m.group(1) + "".join(" " + w.capitalize() for w in m.group(2).split("-") if w)
        return "Gemini"
    if low.startswith("gpt"):
        v = re.search(r"(\d+(?:\.\d+)?)", mid)
        tail = re.sub(r"^gpt-?\d+(?:\.\d+)?-?", "", low)   # 'astra' / 'sol' / ''
        return "GPT" + (f" {v.group(1)}" if v else "") + (f" {tail.title()}" if tail else "")
    return mid or "?"


def _monitor_version_options(capability="text") -> list[dict[str, str]]:
    """모델 선택기에 보일 구체 모델 목록(슬롯·값·표시명)."""
    selected = [slot["model"] for slot in db.get_monitor_summary_slots()["slots"]]
    opts = []
    for provider, family in (("claude", "opus"), ("codex", "gpt"), ("grok", "grok"),
                             ("gemini", llm_gateway.GEMINI_FAMILY)):
        if capability == "vision" and family == llm_gateway.GEMINI_FAMILY:
            continue                     # Gemini는 텍스트 전용 실행기(캡처 러너 없음)
        for item in llm_gateway.llm_catalog.options(provider, capability=capability,
                                                   selected=[m for m in selected if llm_gateway.model_family(m) == family]):
            opts.append(dict(item, slot=family))
    return opts


_SUMMARY_MODEL_RE = re.compile(r"<!--SUMMARY_MODEL:([^>]*?)(?: · [^>]*)?-->")


def _summary_model_of(summary_path: str | None, candidates: list[str]) -> str | None:
    """요약 파일의 '요약 모델' 마커(표시명)를 구체 모델 ID로 되돌린다. 모르면 None."""
    try:
        with open(summary_path or "", encoding="utf-8", errors="replace") as f:
            m = _SUMMARY_MODEL_RE.search(f.read(4096))
    except OSError:
        return None
    if not m:
        return None
    label = m.group(1).strip()
    pool = list(candidates) + [o["value"] for o in llm_gateway.llm_catalog.options(capability="vision")]
    return next((x for x in pool if _model_label(x) == label), None)


def _capture_order(summary_path: str | None) -> list[str]:
    """캡처 모델 순서 — 그 영상을 요약한 모델이 먼저, 실패하면 요약 슬롯 순서로 넘긴다.

    캡처 전용 설정은 두지 않는다(2026-09-23 사용자 지시: 요약 모델이 캡처까지 맡는다).
    이미지 입력이 안 되는 모델은 건너뛰고, 하나도 없으면 기본 비전 모델을 쓴다.
    """
    import keyframe_report as kr
    try:
        slots = [x["model"] for x in db.get_monitor_summary_slots()["slots"]]
    except Exception:
        slots = []
    first = _summary_model_of(summary_path, slots)
    order = list(dict.fromkeys(([first] if first else []) + slots))
    # 비전 티어 Opus → Sonnet(2026-11-09 사용자 승인, 포탈 LLM 이력 분석: Opus 캡처 건당 약 16만 토큰·7일 캐시 쓰기 7.3M).
    # 요약이 Opus여도 캡처는 kr.VISION_CLAUDE_MODEL(기본 Sonnet 5.5)로 한다. 되돌리기: YT_VISION_OPUS_TO="" 로 재시작.
    swap = kr.VISION_OPUS_TO
    if swap:
        order = list(dict.fromkeys(swap if ("opus" in m and m.startswith("claude")) else m for m in order))
    # Gemini는 이미지 러너가 없어 캡처 순서에서 뺀다(카탈로그상 비전 가능이어도, 2026-11-16).
    order = [m for m in order if llm_gateway.llm_catalog.valid(m, capability="vision")
             and llm_gateway.model_family(m) != llm_gateway.GEMINI_FAMILY]
    return order or [kr.VISION_MODEL]


def _monitor_model_payload() -> dict:
    """모델 선택기가 쓰는 전체 상태 — GET /channels와 PATCH 응답이 같은 모양을 준다."""
    summary = db.get_monitor_summary_slots()
    return {
        "summary_slots": summary["slots"],
        "summary_next_index": summary["next_index"],
        "model_options": _monitor_version_options(),
        "catalog_revision": llm_gateway.llm_catalog.read()["revision"],
        "reasoning_options": _monitor_reasoning_options(),
        "slot_limits": {"min": llm_gateway.MIN_SUMMARY_SLOTS, "max": llm_gateway.MAX_SUMMARY_SLOTS},
        "model_notes": _monitor_model_notes(),
        "title_model": yt_titles._title_tr_setting()[0],
        "title_effort": yt_titles._title_tr_setting()[1],
        **_name_model_payload(),
    }


# 인명 확인(name_fix → name_resolver) 판별·웹 검색 모델 — 설정 팝업 '인명 확인' 블록
# (2026-10-08 사용자 지시). 웹 검색 도구가 있는 Claude·Codex CLI만 고를 수 있다.
_NAME_FALLBACK_PROVIDERS = ("claude", "codex")


def _name_providers() -> tuple:
    return tuple(getattr(name_fix.name_resolver, "CHOICE_PROVIDERS", None) or _NAME_FALLBACK_PROVIDERS)


def _name_default_choice() -> dict:
    try:
        d = dict(name_fix.name_resolver.default_choice())
    except Exception:
        d = {"model": "claude-haiku-5-5", "effort": "default"}
    entry = llm_gateway.llm_catalog.resolve(d.get("model") or "") or {}
    d["label"] = entry.get("label") or _model_label(d.get("model") or "")
    return d


def _catalog_provider(model: str) -> str:
    """모델 ID → 카탈로그 제공사(claude/codex/grok). 카탈로그가 모르면 ID 접두어로."""
    try:
        entry = llm_gateway.llm_catalog.resolve(model) or {}
    except Exception:
        entry = {}
    if entry.get("provider"):
        return entry["provider"]
    m = (model or "").lower()
    return "grok" if m.startswith("grok") else "codex" if m.startswith(("gpt", "codex")) else "claude"


def _name_model_payload() -> dict:
    """저장값(빈 값 = 모듈 기본) + 선택지(Claude·Codex text 모델) + 기본값 표시."""
    saved = db.get_name_models()
    selected = [v["model"] for v in saved.values() if v["model"]]
    options = []
    for provider in _name_providers():
        # 저장된 모델은 자기 제공사 목록에만 '기존 선택'으로 보존한다 — 섞어 넘기면 GPT 목록에
        # 'claude-haiku-5-5 (미등록·기존 선택)'이 생기고 화면이 그쪽을 골랐다(2026-10-13).
        mine = [m for m in selected if _catalog_provider(m) == provider]
        options.extend(dict(o) for o in llm_gateway.llm_catalog.options(provider, capability="text",
                                                                        selected=mine))
    out = {"name_model_options": options, "name_model_default": _name_default_choice()}
    for kind, v in saved.items():
        out[f"name_{kind}_model"] = v["model"]
        out[f"name_{kind}_effort"] = v["effort"]
    return out


def _monitor_model_notes() -> list[dict[str, str]]:
    """모델 선택기 아래 '예외 규칙' — 순번과 다르게 도는 경로를 알려 준다(읽기 전용)."""
    return [
        {"rule": "캡처",
         "detail": "그 영상을 요약한 모델이 캡처도 맡고, 실패하면 요약 슬롯 순서로 넘긴다. "
                   "추론 수준은 쓰지 않는다 — 같은 프레임을 low·high로 판정해 보니 "
                   "유지·소제목 배정이 모두 같았고 시간만 늘었다(2026-09-23)."},
        {"rule": "Grok 문체 보정",
         "detail": "Grok 요약에만 한국어 문체 보정 지시를 앞에 붙인다."},
        {"rule": "문자 누출",
         "detail": "GPT·Grok 요약에 한자·영어 조각이 섞이면 같은 모델로 1회 다시 요청하고, "
                   "그래도 남으면 다음 순번으로 넘긴다."},
    ]


def _monitor_reasoning_options() -> list[dict[str, str]]:
    return llm_gateway.llm_catalog.reasoning_options()
