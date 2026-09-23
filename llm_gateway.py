"""Shared subprocess boundary for local LLM CLIs.

The gateway drains stdout and stderr concurrently, enforces a wall-clock timeout,
and guarantees child cleanup when a streaming client disconnects.
"""

from __future__ import annotations


# Shared metadata; service execution policy remains local.
import sys as _catalog_sys
from pathlib import Path as _CatalogPath
_catalog_sys.path.insert(0, str(_CatalogPath.home() / "projects/hermes-llm-log"))
import llm_catalog

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Sequence

import contextlib
import sys

# LLM 호출 이력(~/.hermes/data/llm_calls.db, hermes-llm-log). 모듈이 없거나 기록이 실패해도
# 요약·번역 본업은 그대로 돌아야 하므로 모든 진입점이 예외를 삼킨다.
try:
    _LLM_LOG_DIR = os.path.expanduser("~/projects/hermes-llm-log")
    if _LLM_LOG_DIR not in sys.path:
        sys.path.append(_LLM_LOG_DIR)
    import llm_log
except Exception:  # noqa: BLE001 — 모듈이 없어도 본업은 돈다
    llm_log = None

LLM_LOG_SERVICE = "yt"


def llm_track(provider: str, model: str | None = None, **fields):
    """`with llm_track(...) as call:` — call은 None일 수 있다(모듈 없음/진입 실패)."""
    if llm_log is None:
        return contextlib.nullcontext(None)
    try:
        return llm_log.track(LLM_LOG_SERVICE, provider, model or None, **fields)
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext(None)


def llm_begin(provider: str, model: str | None = None, **fields):
    """with 블록으로 감쌀 수 없는 제너레이터 경로용. `llm_finish`와 짝을 이룬다."""
    if llm_log is None:
        return None
    try:
        return llm_log.Call(LLM_LOG_SERVICE, provider, model or None, **fields)
    except Exception:  # noqa: BLE001
        return None


def llm_fill(call, *, usage=None, model=None, fail=None, status: str = "error", meta=None):
    """진행 중인 call에 usage/실제 모델/실패를 채운다. call이 None이거나 예외가 나도 조용히 넘어간다.

    별칭(opus)으로 요청했는데 실제 ID를 얻으면 model을 실제 ID로 바꾸고 별칭은 meta.alias에 남긴다."""
    if call is None:
        return
    try:
        requested = call.model
        if usage:
            call.usage(usage)
        if model:
            call.model = model
        if requested and call.model and call.model != requested:
            call.meta.setdefault("alias", requested)
        if meta:
            call.meta.update(meta)
        if fail:
            call.fail(str(fail)[:500], status=status)
    except Exception:  # noqa: BLE001
        pass


def llm_finish(call, **fill) -> None:
    llm_fill(call, **fill)
    if call is None:
        return
    try:
        call.finish()
    except Exception:  # noqa: BLE001
        pass


def unwrap_claude_json(stdout: str) -> tuple[str, dict]:
    """`claude -p --output-format json` stdout → (plain 출력과 동일한 텍스트, usage dict).

    plain 모드는 `<result>\\n`을 찍으므로 개행을 붙여 문자 그대로 맞춘다.
    봉투가 아니거나 파싱에 실패하면 원문 stdout을 그대로 돌려준다(빈 문자열 금지)."""
    raw = stdout or ""
    try:
        data = json.loads(raw.strip())
        if isinstance(data, dict) and data.get("type") == "result":
            text = data.get("result")
            if isinstance(text, str) and text:
                text = text + "\n"
            elif not isinstance(text, str):
                text = ""
            usage = {}
            if llm_log is not None:
                try:
                    usage = llm_log.usage_from_claude_json(data)
                except Exception:  # noqa: BLE001
                    usage = {}
            return text, usage
    except Exception:  # noqa: BLE001
        pass
    return raw, {}


def unwrap_grok_json(stdout: str) -> tuple[str, dict]:
    """`grok --output-format json` stdout → (plain 출력과 동일한 텍스트, usage dict).

    봉투는 {"text", "usage": {input_tokens, output_tokens, cache_read_input_tokens, ...},
    "modelUsage": {"<model>": {...}}} 로 Claude 봉투와 키가 같아 같은 파서를 쓴다.
    plain 모드는 `<text>\\n`을 찍으므로 개행을 붙인다. 파싱 실패면 원문 stdout 그대로."""
    raw = stdout or ""
    try:
        data = json.loads(raw.strip())
        if isinstance(data, dict) and "text" in data:
            text = data.get("text")
            text = (text + "\n") if isinstance(text, str) and text else (text if isinstance(text, str) else "")
            usage = {}
            if llm_log is not None:
                try:
                    usage = llm_log.usage_from_claude_json(data)
                except Exception:  # noqa: BLE001
                    usage = {}
            return text, usage
    except Exception:  # noqa: BLE001
        pass
    return raw, {}


def codex_usage(events: str) -> dict:
    """`codex exec --json` JSONL → usage dict(turn.completed.usage; cache_write까지)."""
    usage: dict = {}
    if llm_log is not None:
        try:
            usage = dict(llm_log.usage_from_codex_events(events or ""))
        except Exception:  # noqa: BLE001
            usage = {}
    try:
        for line in (events or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            ev = json.loads(line)
            u = ev.get("usage") if isinstance(ev, dict) else None
            if isinstance(u, dict) and u.get("cache_write_input_tokens") is not None:
                usage["cache_write_tokens"] = int(u["cache_write_input_tokens"])
    except Exception:  # noqa: BLE001
        pass
    return usage


def _codex_final_from_events(events: str) -> str:
    """--json 이벤트에서 마지막 agent_message 텍스트. 없으면 ''."""
    text = ""
    for line in (events or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
    return text


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    events: str = ""       # codex --json JSONL 원문(usage 추출용). 다른 경로는 빈 문자열.


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    data: str = ""
    returncode: int | None = None
    stderr: str = ""


def resolve_claude_bin() -> str:
    env = os.environ.get("CLAUDE_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("claude")
    if found:
        return found
    pattern = os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"
    )
    candidates = [path for path in glob.glob(pattern) if os.path.exists(path)]
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return env or "claude"


def resolve_grok_bin() -> str:
    env = os.environ.get("GROK_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("grok")
    if found:
        return found
    fallback = os.path.expanduser("~/.grok/bin/grok")
    return fallback if os.path.exists(fallback) else (env or "grok")


_grok_default_cache: dict = {"value": None, "ts": 0.0}
_GROK_DEFAULT_TTL = 6 * 3600      # CLI 기본 모델은 자주 안 바뀌므로 넉넉히 캐시


def resolve_grok_default_model() -> str | None:
    """`grok models`가 보고하는 기본 모델 id(예: 'grok-4.6'). 실패하면 None.

    -m 없이 호출할 때 어떤 모델이 실제로 쓰였는지 요약 마커에 남기려고 조회한다.
    CLI 호출이라 비싸서 캐시하고, 실패해도 라벨이 버전 없는 'Grok'이 될 뿐이다.
    """
    now = time.time()
    if _grok_default_cache["value"] and now - _grok_default_cache["ts"] < _GROK_DEFAULT_TTL:
        return _grok_default_cache["value"]
    try:
        r = run_command([resolve_grok_bin(), "models"], timeout=30)
        if r.returncode == 0:
            m = re.search(r"Default model:\s*(\S+)", r.stdout or "")
            if m:
                _grok_default_cache.update(value=m.group(1), ts=now)
                return m.group(1)
    except Exception:
        pass
    return _grok_default_cache["value"]      # 만료됐어도 옛 값이 없는 것보단 낫다


def resolve_codex_bin() -> str:
    """Resolve the Codex CLI used for the GPT model option."""
    env = os.environ.get("CODEX_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("codex")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/codex")
    return fallback if os.path.exists(fallback) else (env or "codex")


MODEL_KEYS = ("opus", "gpt", "grok")
NONE_KEY = "none"
SLOT_COUNT = len(MODEL_KEYS)
REASONING_LEVELS = tuple(llm_catalog.LEVEL_LABELS)
DEFAULT_SUMMARY_REASONING = {"opus": "default", "gpt": "high", "grok": "default"}

# 슬롯별로 고를 수 있는 구체 모델. "opus"는 Claude CLI의 최신 Opus 별칭이다
# (2026-09-23 현재 claude-opus-5-5로 풀린다). Grok은 CLI 기본 모델을 따라가므로 없다.
MODEL_VERSION_CHOICES = {
    "opus": llm_catalog.Choices("claude"),
    "gpt": llm_catalog.Choices("codex"),
}


# ── 요약 슬롯(2026-09-23) ──────────────────────────────────────────────
# 요약 순번은 계열 목록이 아니라 슬롯 목록이다. 슬롯 하나 = {"model": 구체 모델,
# "effort": 추론 수준}. 같은 계열이 여러 슬롯에 들어갈 수 있다(GPT Sol·Luna 등).
SUMMARY_MODEL_CHOICES = llm_catalog.ExecutorChoices(("claude", "codex"), aliases=("grok",))
MIN_SUMMARY_SLOTS = 1
MAX_SUMMARY_SLOTS = 5
CAPTURE_SLOTS = 3


def model_family(model: str) -> str:
    """구체 모델 → 계열 키(opus/gpt/grok). 옛 저장값의 계열 키도 그대로 받는다."""
    registered = llm_catalog.resolve(model)
    if registered and registered['provider'] in ('claude', 'codex', 'grok'):
        return {'claude': 'opus', 'codex': 'gpt', 'grok': 'grok'}[registered['provider']]
    m = str(model or "").strip().lower()
    if m.startswith("gpt"):
        return "gpt"
    if m.startswith("grok"):
        return "grok"
    return "opus"


def normalize_summary_slots(value, defaults: list[dict], preserve_unknown=False) -> list[dict]:
    """슬롯 목록 정규화: 알 수 없는 모델은 버리고, 추론 수준은 없으면 default.
    1개 미만이면 기본값, 5개를 넘으면 앞에서 자른다."""
    out: list[dict] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or "").strip()
        if model not in SUMMARY_MODEL_CHOICES and not (preserve_unknown and model and not model.startswith("-")):
            continue
        effort = str(raw.get("effort") or "default").strip().lower()
        out.append({"model": model,
                    "effort": effort if effort in REASONING_LEVELS else "default"})
    if len(out) < MIN_SUMMARY_SLOTS:
        return [dict(s) for s in defaults]
    return out[:MAX_SUMMARY_SLOTS]


def is_valid_summary_slots(value, existing=()) -> bool:
    if not isinstance(value, list) or not (MIN_SUMMARY_SLOTS <= len(value) <= MAX_SUMMARY_SLOTS):
        return False
    return all(isinstance(s, dict) and (s in existing or (s.get("model") in SUMMARY_MODEL_CHOICES
               and llm_catalog.valid(s.get("model"), str(s.get("effort") or "default").lower(), selectable=True)))
               for s in value)


def normalize_capture_models(value, legacy: dict[str, str], preserve_unknown=False) -> list[str]:
    """캡처 순차 폴백 3칸 — 구체 모델 또는 none. 옛 계열 키(gpt 등)는 legacy로 바꾼다.
    1순위는 비울 수 없고, none 뒤는 모두 none이다."""
    raw = value if isinstance(value, list) else []
    out: list[str] = []
    for item in raw[:CAPTURE_SLOTS]:
        key = str(item or "").strip()
        if key == NONE_KEY:
            out.append(NONE_KEY)
        elif key in SUMMARY_MODEL_CHOICES:
            out.append(key)
        elif key in legacy:
            out.append(legacy[key])
        elif preserve_unknown and key and not key.startswith("-"):
            out.append(key)
    while len(out) < CAPTURE_SLOTS:
        out.append(NONE_KEY)
    if out[0] == NONE_KEY:
        out[0] = legacy.get("opus", "opus")
    if NONE_KEY in out:
        cut = out.index(NONE_KEY)
        out = out[:cut] + [NONE_KEY] * (CAPTURE_SLOTS - cut)
    return out


def is_valid_capture_models(value, existing=()) -> bool:
    if not isinstance(value, list) or len(value) != CAPTURE_SLOTS:
        return False
    if value[0] == NONE_KEY:
        return False
    seen_none = False
    for item in value:
        if item == NONE_KEY:
            seen_none = True
        elif seen_none or (item not in existing and (item not in SUMMARY_MODEL_CHOICES or not llm_catalog.valid(item, capability='vision', selectable=True))):
            return False
    return True


def _parse_model_tokens(value) -> list[str]:
    raw = value
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            raw = decoded if isinstance(decoded, list) else raw.split(",")
        except (TypeError, ValueError):
            raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item or "").strip().lower() for item in raw]


def normalize_model_order(value, *, default: Sequence[str] = MODEL_KEYS) -> list[str]:
    """Return a duplicate-free model order of SLOT_COUNT entries.

    HTTP callers send JSON arrays, while persisted or environment-backed values may be
    JSON strings or comma-separated strings. Unknown values are ignored.

    ``none`` truncates the fallback chain: everything after the first none is none,
    and missing models are not filled in. Without none, omitted choices are appended
    from ``default`` (previous behaviour).
    """
    parsed = _parse_model_tokens(value)
    if NONE_KEY in parsed:
        order: list[str] = []
        for key in parsed:
            if key == NONE_KEY:
                break
            if key in MODEL_KEYS and key not in order:
                order.append(key)
        if not order:
            return list(default)
        while len(order) < SLOT_COUNT:
            order.append(NONE_KEY)
        return order[:SLOT_COUNT]
    order = []
    for key in parsed + [str(item).strip().lower() for item in default]:
        if key in MODEL_KEYS and key not in order:
            order.append(key)
    return order


def is_valid_monitor_order(value) -> bool:
    """1순위는 실제 모델, 없음은 접미사만. 중간 없음·중복 모델은 거부."""
    if not isinstance(value, list) or len(value) != SLOT_COUNT:
        return False
    keys = [str(v).lower() for v in value]
    if keys[0] not in MODEL_KEYS:
        return False
    seen_none = False
    seen: set[str] = set()
    for key in keys:
        if key == NONE_KEY:
            seen_none = True
            continue
        if seen_none or key not in MODEL_KEYS or key in seen:
            return False
        seen.add(key)
    return True


def normalize_round_robin_order(value) -> list[str]:
    """Return all summary models exactly once, preserving the requested cycle order."""
    parsed = _parse_model_tokens(value)
    order: list[str] = []
    for key in parsed + list(MODEL_KEYS):
        if key in MODEL_KEYS and key not in order:
            order.append(key)
    return order


def is_valid_round_robin_order(value) -> bool:
    """Summary round-robin always consists of all three models, once each."""
    if not isinstance(value, list) or len(value) != SLOT_COUNT:
        return False
    return set(str(key).lower() for key in value) == set(MODEL_KEYS)


def normalize_reasoning_levels(value) -> dict[str, str]:
    """Normalize per-model reasoning levels while retaining current defaults."""
    raw = value if isinstance(value, dict) else {}
    out = dict(DEFAULT_SUMMARY_REASONING)
    for key in MODEL_KEYS:
        level = str(raw.get(key, out[key])).strip().lower()
        if level in REASONING_LEVELS:
            out[key] = level
    return out


def is_valid_reasoning_levels(value) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return all(
        key in MODEL_KEYS and str(level).strip().lower() in REASONING_LEVELS
        for key, level in value.items()
    )


def run_codex_prompt(
    prompt: str,
    *,
    model: str,
    timeout: float,
    images: Sequence[str] = (),
    reasoning_effort: str = "default",
) -> ProcessResult:
    """Run one non-interactive GPT turn through the authenticated Codex CLI.

    ``--output-last-message`` separates the final answer from JSON/progress events and
    ``--ephemeral`` avoids leaving an automation thread behind. The model is sandboxed
    read-only; images are attached explicitly instead of asking the agent to open files.
    ``--json`` turns stdout into JSONL events (kept in ``events`` for token accounting);
    the answer still comes from the ``-o`` file, with the last agent_message as fallback.
    """
    codex = resolve_codex_bin()
    if not (codex and os.path.exists(codex)):
        return ProcessResult(127, "", f"codex 실행파일 없음 ({codex})")
    output = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    output.close()
    try:
        command = [
            codex, "exec", "--ephemeral", "--ignore-rules",
            "--skip-git-repo-check", "--sandbox", "read-only",
            "-C", tempfile.gettempdir(), "--output-last-message", output.name,
            "--json",
        ]
        if model:
            command += ["-m", model]
        effort = str(reasoning_effort or "default").lower()
        if effort != "default":
            command += ["-c", f"model_reasoning_effort={effort}"]
        for path in images:
            command += ["-i", path]
        command.append("-")
        result = run_command(command, input_text=prompt, timeout=timeout)
        final = ""
        try:
            with open(output.name, encoding="utf-8", errors="replace") as handle:
                final = handle.read()
        except OSError:
            pass
        if not final:
            # -o 파일이 비었으면 JSONL의 마지막 agent_message, 그것도 없으면 원문 stdout 그대로.
            try:
                final = _codex_final_from_events(result.stdout)
            except Exception:  # noqa: BLE001
                final = ""
        return ProcessResult(
            result.returncode,
            final or result.stdout,
            result.stderr,
            timed_out=result.timed_out,
            events=result.stdout,
        )
    finally:
        try:
            os.remove(output.name)
        except OSError:
            pass


def run_command(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float,
) -> ProcessResult:
    try:
        result = subprocess.run(
            list(args),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        return ProcessResult(result.returncode, result.stdout or "", result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ProcessResult(-1, stdout, stderr, timed_out=True)


def stream_command(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float,
) -> Iterator[StreamEvent]:
    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    output: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stderr_parts: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                output.put(("stdout", line))
        finally:
            output.put(("stdout_eof", None))

    def read_stderr() -> None:
        assert proc.stderr is not None
        try:
            # readline() would wait forever when a CLI writes a large message without a newline.
            # Read raw chunks so the child can never fill the stderr pipe and deadlock.
            while True:
                chunk = os.read(proc.stderr.fileno(), 8192)
                if not chunk:
                    break
                if sum(map(len, stderr_parts)) < 1_000_000:
                    stderr_parts.append(chunk.decode("utf-8", "replace"))
        finally:
            output.put(("stderr_eof", None))

    def write_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(input_text or "")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    if input_text is not None:
        threads.append(threading.Thread(target=write_stdin, daemon=True))
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    stdout_done = False
    stderr_done = False
    try:
        while not (stdout_done and stderr_done and proc.poll() is not None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield StreamEvent("timeout", stderr="".join(stderr_parts))
                return
            try:
                kind, data = output.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if kind == "stdout" and data is not None:
                yield StreamEvent("stdout", data=data)
            elif kind == "stdout_eof":
                stdout_done = True
            elif kind == "stderr_eof":
                stderr_done = True
        returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        yield StreamEvent("complete", returncode=returncode, stderr="".join(stderr_parts))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
