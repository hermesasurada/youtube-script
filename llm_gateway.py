"""Shared subprocess boundary for local LLM CLIs.

The gateway drains stdout and stderr concurrently, enforces a wall-clock timeout,
and guarantees child cleanup when a streaming client disconnects.
"""

from __future__ import annotations

import glob
import os
import queue
import shutil
import subprocess
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
