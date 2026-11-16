"""LLM 계열별 실행기 — 프롬프트 한 건을 실행해 (텍스트, 오류사유)를 돌려준다.

2026-11-16 리팩토링 1단계(사용자 지시 'yt 리팩토링 1~4 순차 진행'): 요약·제목 번역·한눈 요약이
계열마다 따로 갖고 있던 실행 함수(`_summarize_with_gpt/grok/gemini`, `_title_tr_with_claude/grok/gpt/gemini`,
`_gemini_text`)와 Claude stream-json 파싱 두 벌을 이 모듈 하나로 모았다. 호출 이력(llm_log) 기록 방식은 그대로다.

- `run_text(family, prompt, …)` — 단일턴 실행(계열 키 opus·gpt·grok·gemini). 어떤 오류도 예외로 내보내지 않는다.
  `postprocess`를 주면 결과를 그 함수로 다듬은 뒤 비었으면 '빈 응답' 실패로 기록한다(요약 경로).
- `stream_claude(prompt, …)` — Claude CLI stream-json 요약. ("model", 실제 모델 ID)·("text", 조각)을 내보내고
  끝나면 `StreamOutcome`을 돌려준다(`yield from`·StopIteration.value).

실행 파일·기본 모델은 호출측(app)이 넘긴다 — 시험에서 app.GROK_BIN 등을 바꿔 끼우는 경로를 그대로 살린다.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

import llm_gateway

_PROVIDER = {"opus": "claude", "gpt": "codex", "grok": "grok", llm_gateway.GEMINI_FAMILY: "gemini"}
_LABEL = {"opus": "claude", "gpt": "gpt", "grok": "grok", llm_gateway.GEMINI_FAMILY: "gemini"}


def _reasoning(effort: str) -> str | None:
    return None if effort == "default" else effort


def _finish_text(call, label: str, text: str, postprocess: Callable[[str], str] | None) -> tuple[str, str]:
    if postprocess is None:
        return text, ""
    out = postprocess(text or "")
    if not out.strip():
        llm_gateway.llm_fill(call, fail="빈 응답")
        return "", f"{label} 빈 응답"
    return out, ""


def _run_claude_json(prompt, *, model, effort, purpose, title, timeout, claude_bin, postprocess):
    command = [claude_bin, "-p", "--model", model, "--output-format", "json"] + llm_gateway.claude_minimal_args()
    if effort != "default":
        command += ["--effort", effort]
    command.append(prompt)
    with llm_gateway.llm_track("claude", model, purpose=purpose, title=title, backend="cli",
                               reasoning=_reasoning(effort)) as call:
        r = llm_gateway.run_command(command, timeout=timeout)
        stdout, usage = llm_gateway.unwrap_claude_json(r.stdout or "")
        llm_gateway.llm_fill(call, usage=usage)
        if r.returncode != 0:
            err = (r.stderr or stdout or f"rc={r.returncode}")[:200]
            llm_gateway.llm_fill(call, fail=err, status="timeout" if r.timed_out else "error")
            return "", err
        return _finish_text(call, "claude", stdout, postprocess)


def _run_codex(prompt, *, model, effort, purpose, title, timeout, postprocess):
    with llm_gateway.llm_track("codex", model, purpose=purpose, title=title, backend="cli",
                               reasoning=_reasoning(effort)) as call:
        try:
            r = llm_gateway.run_codex_prompt(prompt, model=model, timeout=timeout, reasoning_effort=effort)
        except Exception as e:  # noqa: BLE001
            llm_gateway.llm_fill(call, fail=e)
            return "", f"gpt 실행 오류: {e}"
        llm_gateway.llm_fill(call, usage=llm_gateway.codex_usage(getattr(r, "events", "")))
        if r.timed_out:
            llm_gateway.llm_fill(call, fail=f"타임아웃({timeout}s)", status="timeout")
            return "", f"gpt 타임아웃({timeout}s)"
        if r.returncode != 0:
            err = (r.stderr or r.stdout or f"gpt rc={r.returncode}").strip()[:200]
            llm_gateway.llm_fill(call, fail=err)
            return "", err
        return _finish_text(call, "gpt", r.stdout or "", postprocess)


def _run_grok(prompt, *, model, effort, purpose, title, timeout, grok_bin, postprocess):
    """긴 프롬프트는 argv 대신 --prompt-file(임시파일)로 넘긴다(ARG_MAX 회피). json 봉투로 usage를 남긴다.
    model이 비면 -m 없이 CLI 기본 모델로 돈다."""
    if not (grok_bin and os.path.exists(grok_bin)):
        return "", "grok 실행파일 없음"
    with llm_gateway.llm_track("grok", model or None, purpose=purpose, title=title, backend="cli",
                               reasoning=_reasoning(effort)) as call:
        tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        try:
            tf.write(prompt)
            tf.close()
            cmd = [grok_bin, "--prompt-file", tf.name, "--output-format", "json"]
            if model:
                cmd += ["-m", model]
            if effort != "default":
                cmd += ["--reasoning-effort", effort]
            r = llm_gateway.run_command(cmd, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            llm_gateway.llm_fill(call, fail=e)
            return "", f"grok 실행 오류: {e}"
        finally:
            try:
                os.remove(tf.name)
            except OSError:
                pass
        stdout, usage = llm_gateway.unwrap_grok_json(r.stdout or "")
        llm_gateway.llm_fill(call, usage=usage)
        if r.timed_out:
            llm_gateway.llm_fill(call, fail=f"타임아웃({timeout}s)", status="timeout")
            return "", f"grok 타임아웃({timeout}s)"
        if r.returncode != 0:
            err = (r.stderr or f"grok rc={r.returncode}").strip()[:200]
            llm_gateway.llm_fill(call, fail=err)
            return "", err
        return _finish_text(call, "grok", stdout, postprocess)


def _run_gemini(prompt, *, model, effort, purpose, title, timeout, postprocess):
    """Gemini API(키) 호출 1건(이력 1행). 429(무료 한도)·타임아웃도 오류사유로 돌려 다음 순번이 받게 한다."""
    if effort != "default" and effort not in llm_gateway.llm_catalog.levels(model):
        effort = "default"
    with llm_gateway.llm_track("gemini", model, purpose=purpose, title=title, backend="http",
                               reasoning=_reasoning(effort)) as call:
        if llm_gateway.gemini_api is None:
            llm_gateway.llm_fill(call, fail="gemini_api 모듈 없음")
            return "", "gemini_api 모듈 없음"
        try:
            text, usage = llm_gateway.gemini_api.generate(prompt, model, effort, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            llm_gateway.llm_fill(call, usage=getattr(e, "usage", None) or None)
            timed_out = isinstance(e, TimeoutError) or "timed out" in str(e)
            llm_gateway.llm_fill(call, fail=f"{type(e).__name__}: {e}",
                                 status="timeout" if timed_out else "error")
            return "", f"gemini {e}"
        llm_gateway.llm_fill(call, usage=usage)
        return _finish_text(call, "gemini", text or "", postprocess)


def run_text(family: str, prompt: str, *, model: str, effort: str = "default", purpose: str,
             title: str | None, timeout: int, claude_bin: str = "", grok_bin: str = "",
             postprocess: Callable[[str], str] | None = None) -> tuple[str, str]:
    """계열 하나로 프롬프트 한 건 실행 → (텍스트, 오류사유). model은 호출측이 기본값까지 풀어 넘긴다
    (grok만 빈 값 허용 — CLI 기본 모델)."""
    effort = str(effort or "default").lower()
    common = dict(model=model, effort=effort, purpose=purpose, title=title, timeout=timeout,
                  postprocess=postprocess)
    if family == "opus":
        return _run_claude_json(prompt, claude_bin=claude_bin, **common)
    if family == "gpt":
        return _run_codex(prompt, **common)
    if family == "grok":
        return _run_grok(prompt, grok_bin=grok_bin, **common)
    if family == llm_gateway.GEMINI_FAMILY:
        return _run_gemini(prompt, **common)
    return "", f"지원하지 않는 모델 계열: {family}"


# ── Claude stream-json ──────────────────────────────────────────────────────
@dataclass
class StreamOutcome:
    text: str = ""                 # result 이벤트 본문, 없으면 받은 조각을 이은 것
    error: str | None = None
    used_model: str | None = None  # system 이벤트가 알려 준 실제 모델 ID
    chunks: list[str] = field(default_factory=list)


def finish_claude_stream_log(call, result_event, used_model, error_msg, text) -> None:
    """stream-json 요약 호출 1건을 이력에 남긴다(제너레이터라 with 대신 begin/finish).

    result 이벤트의 usage/modelUsage에서 토큰·실제 모델을 뽑고, 오류·타임아웃·빈 응답·
    클라이언트 중단(result 없이 종료)을 status에 반영한다. 어떤 경우에도 예외를 내지 않는다."""
    if call is None:
        return
    try:
        usage = {}
        if llm_gateway.llm_log is not None and result_event:
            usage = llm_gateway.llm_log.usage_from_claude_json(result_event)
        fail, status = None, "error"
        if error_msg:
            fail = error_msg
            status = "timeout" if "타임아웃" in error_msg else "error"
        elif result_event is None:
            fail = "중단됨(result 이벤트 없음)"
        elif not (text or "").strip():
            fail = "빈 응답"
        llm_gateway.llm_finish(call, usage=usage, model=used_model, fail=fail, status=status)
    except Exception:  # noqa: BLE001
        pass


def stream_claude(prompt: str, *, claude_bin: str, model: str, effort: str = "default",
                  title: str | None = None, timeout: int, system_args: Sequence[str] = ()
                  ) -> Iterator[tuple[str, str]]:
    """Claude Code CLI 요약 스트림. 프롬프트는 stdin으로 넘긴다(긴 전사의 ARG_MAX 회피).

    내보내는 것: ("model", 실제 모델 ID) — system 이벤트에서 한 번, ("text", 조각) — 텍스트 델타마다.
    돌려주는 것: StreamOutcome. 소비측이 중간에 닫아도(클라이언트 중단) 이력은 finally에서 남는다."""
    command = [claude_bin, "-p", "--output-format", "stream-json", "--include-partial-messages",
               "--model", model, "--allowedTools", "", *system_args, "--verbose"]
    if effort != "default":
        command += ["--effort", effort]
    out = StreamOutcome()
    final: str | None = None
    result_event: dict | None = None
    call = llm_gateway.llm_begin("claude", model, purpose="summary", title=title, backend="cli",
                                 reasoning=_reasoning(effort))
    try:
        for pe in llm_gateway.stream_command(command, input_text=prompt, timeout=timeout):
            if pe.kind == "timeout":
                detail = pe.stderr.strip()[:200]
                out.error = f"Claude 타임아웃({timeout}s)" + (f": {detail}" if detail else "")
                continue
            if pe.kind == "complete":
                if pe.returncode != 0 and not out.error:
                    out.error = pe.stderr.strip()[:200] or f"Claude rc={pe.returncode}"
                continue
            if pe.kind != "stdout":
                continue
            line = pe.data.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = ev.get("type")
            if event_type == "system" and ev.get("model") and not out.used_model:
                out.used_model = ev.get("model")
                yield ("model", out.used_model)
            if event_type == "stream_event":
                sub = ev.get("event") or {}
                if sub.get("type") == "content_block_delta":
                    delta = sub.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            out.chunks.append(text)
                            yield ("text", text)
            elif event_type == "result":
                result_event = ev
                if ev.get("is_error"):
                    out.error = ev.get("result") or "Claude CLI 오류"
                else:
                    final = ev.get("result") or "".join(out.chunks)
    except Exception as e:  # noqa: BLE001
        out.error = str(e)
    finally:
        out.text = final if final is not None else "".join(out.chunks)
        finish_claude_stream_log(call, result_event, out.used_model, out.error, out.text)
    return out
