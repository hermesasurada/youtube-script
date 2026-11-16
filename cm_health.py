"""요약 모델 상태 점검 — Claude·Grok·GPT CLI가 지금 쓸 수 있는지 보고 처리를 시작할지 정한다(`summarizer_gate`).

한도·장애처럼 '전체가 막힌' 실패(`_is_systemic`)와 영상 하나의 실패를 가른다
(2026-11-17 channel_monitor에서 분리).
"""
from __future__ import annotations

import cm_base  # noqa: F401 — 알림용 환경 파일을 먼저 읽는다(아래 환경변수 기본값이 그 뒤에 잡히도록)
import os
import re
import llm_gateway


# ── 요약 경로 가용성 가드 ───────────────────────────────────────────────
# 서버(app.py) 요약: Claude primary → Grok 폴백(GROK_FALLBACK).
# 예전에는 Claude 헬스만 보고 큐 전체를 막아 Grok 폴백이 의미 없었음.
# 게이트는 "요약 가능한 경로가 하나라도 있으면 진행"으로 본다.
class ClaudeUnavailable(Exception):
    """사용량 한도/인증/장애 등 LLM 요약 경로 전부 실패 — 큐 정지 후 다음 주기 재시도 대상."""


# 사용량 한도·인증·서버장애 등 '재시도하면 나아질' systemic 신호 패턴
_SYSTEMIC = re.compile(
    # 주의: 403/500/503은 yt-dlp/ffmpeg의 HTTP 오류(봇차단 등)와 겹쳐 오판하므로 제외.
    r"usage limit|session limit|rate.?limit|too many requests|\b429\b|\b401\b|\b529\b"
    r"|authenticat|unauthor|credit balance|quota|exceeded|overloaded|api error"
    r"|사용 한도|세션 한도|한도 초과|인증",
    re.I,
)

# app.py 와 동일 기본값 (env로 맞춤)
GROK_FALLBACK = os.environ.get("GROK_FALLBACK", "1") != "0"


def _is_systemic(msg: str) -> bool:
    return bool(_SYSTEMIC.search(msg or ""))


def _claude_bin() -> str:
    return llm_gateway.resolve_claude_bin()


def _grok_bin() -> str:
    return llm_gateway.resolve_grok_bin()


def _gpt_bin() -> str:
    return llm_gateway.resolve_codex_bin()


def claude_healthy() -> tuple[bool, str]:
    """저가 프로브로 Claude CLI 가용성 확인. (정상?, 사유)."""
    try:
        r = llm_gateway.run_command(
            [_claude_bin(), "-p", "--model", "haiku", "reply with: ok"],
            timeout=90,
        )
    except Exception as e:
        return False, f"claude 실행 오류: {e}"
    if r.timed_out:
        return False, "헬스체크 타임아웃(90s)"
    out = r.stdout.strip()
    err = r.stderr.strip()
    # stdin 경고 등은 노이즈
    err = re.sub(r"Warning: no stdin data received.*?(?:\n|$)", "", err, flags=re.I).strip()
    blob = (out + " " + err).strip()
    if r.returncode != 0 or not out or _is_systemic(blob):
        return False, (blob or f"rc={r.returncode}")[:200]
    return True, ""


def grok_fallback_ready() -> tuple[bool, str]:
    """app.py 요약 Grok 폴백을 시도할 수 있는지(바이너리 존재 + 플래그). 실제 호출은 안 함."""
    if not GROK_FALLBACK:
        return False, "GROK_FALLBACK=0"
    b = _grok_bin()
    if not os.path.exists(b):
        return False, f"grok 없음 ({b})"
    return True, b


def summarizer_gate(model_order=None) -> tuple[bool, str, str]:
    """큐 drain 진입 게이트.

    Returns:
        (proceed, mode, detail)
        mode: "opus" | "gpt" | "grok" | "gemini" | "none"
    저장된 순서에서 실행 가능한 첫 모델을 고른다. 실제 요청에서는 그 모델부터
    남은 순서를 서버에 전달해 인증·한도 오류까지 순차 폴백한다.
    """
    explicit = model_order is not None
    default = llm_gateway.MODEL_KEYS if explicit else ("opus", "grok")
    order = llm_gateway.normalize_model_order(model_order or default, default=default)
    if explicit and isinstance(model_order, (list, tuple)):
        # Gemini(API 키) 계열은 옛 3계열 순번에 없어 normalize가 버린다 — 슬롯에서 온 순서를 앞에 두고
        # 옛 규칙의 나머지(기본 계열 보충)를 뒤에 잇는다. 옛 계열만 오면 결과는 예전과 같다.
        keep = llm_gateway.MODEL_KEYS + (llm_gateway.GEMINI_FAMILY,)
        asked = [k for k in dict.fromkeys(str(x).strip().lower() for x in model_order) if k in keep]
        if llm_gateway.GEMINI_FAMILY in asked:
            order = asked + [k for k in order if k not in asked]
    failures = []
    for key in order:
        if key == llm_gateway.NONE_KEY:
            break
        if key == "opus":
            ok, detail = claude_healthy()
        elif key == "gpt":
            path = _gpt_bin()
            ok, detail = os.path.exists(path), path
        elif key == "grok":
            path = _grok_bin()
            ok = os.path.exists(path) and (explicit or GROK_FALLBACK)
            detail = path if ok else ("GROK_FALLBACK=0" if not explicit else f"grok 없음 ({path})")
        elif key == llm_gateway.GEMINI_FAMILY:
            ok, detail = llm_gateway.gemini_ready()
        else:
            continue
        if ok:
            return True, key, ("; ".join(failures) if failures else "")
        failures.append(f"{key}: {detail}")
    return False, "none", "; ".join(failures)
