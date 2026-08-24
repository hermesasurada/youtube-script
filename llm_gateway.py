"""Shared subprocess boundary for local LLM CLIs.

The gateway drains stdout and stderr concurrently, enforces a wall-clock timeout,
and guarantees child cleanup when a streaming client disconnects.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


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


MODEL_KEYS = ("opus", "gpt", "grok", "qwen")
NONE_KEY = "none"
SLOT_COUNT = len(MODEL_KEYS)

# ── 로컬 oMLX(Qwen) — OpenAI 호환 API. 토큰 비용 없이 맥에서 직접 돌린다.
OMLX_BASE    = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8080/v1")
OMLX_MODEL   = os.environ.get("OMLX_MODEL", "Qwen3.8-27B-Alis-MLX-6bit")
OMLX_TIMEOUT = int(os.environ.get("OMLX_TIMEOUT", "3600"))


def run_omlx_prompt(prompt: str, *, max_tokens: int = 8000,
                    temperature: float = 0.3) -> tuple[str, str]:
    """oMLX로 단일턴 생성. (본문, 오류사유) — 비전은 지원하지 않는다(요약 전용).

    사고 모드는 끈다(일반 작업에서 느려지는 주된 원인). 실패해도 예외를 던지지
    않고 사유를 돌려줘 상위 폴백 체인이 다음 모델로 넘어가게 한다.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return "", "openai 패키지 없음"
    try:
        cli = OpenAI(base_url=OMLX_BASE, api_key="none", timeout=OMLX_TIMEOUT)
        r = cli.chat.completions.create(
            model=OMLX_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except Exception as e:                       # noqa: BLE001 - 서버 다운·타임아웃 등
        return "", f"oMLX 오류: {str(e)[:200]}"
    body = (r.choices[0].message.content or "").strip()
    if not body:
        return "", "oMLX 빈 응답"
    fin = r.choices[0].finish_reason or ""
    if fin == "length":
        return body, ""      # 잘렸어도 본문은 살린다(상위에서 판단)
    return body, ""


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


def run_codex_prompt(
    prompt: str,
    *,
    model: str,
    timeout: float,
    images: Sequence[str] = (),
) -> ProcessResult:
    """Run one non-interactive GPT turn through the authenticated Codex CLI.

    ``--output-last-message`` separates the final answer from JSON/progress events and
    ``--ephemeral`` avoids leaving an automation thread behind. The model is sandboxed
    read-only; images are attached explicitly instead of asking the agent to open files.
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
        ]
        if model:
            command += ["-m", model]
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
        return ProcessResult(
            result.returncode,
            final or result.stdout,
            result.stderr,
            timed_out=result.timed_out,
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
