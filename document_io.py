"""Canonical markdown document parsing and atomic file writes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from typing import Any


def parse_frontmatter(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.splitlines():
        if line.startswith("  - "):
            if current_list_key is not None:
                result[current_list_key].append(line[4:].strip())
        elif ": " in line:
            key, value = line.split(": ", 1)
            result[key.strip()] = value.strip()
            current_list_key = None
        elif line.endswith(":") and not line.startswith(" "):
            key = line[:-1].strip()
            result[key] = []
            current_list_key = key
        else:
            current_list_key = None
    return result


def parse_markdown_text(content: str, *, strip_title: bool = True) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content.strip()
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content.strip()
    meta = parse_frontmatter(content[4:end])
    body = content[end + 5 :].strip()
    if strip_title and body.startswith("# "):
        newline = body.find("\n")
        body = body[newline + 1 :].strip() if newline != -1 else ""
    return meta, body


def read_markdown(path: str, *, strip_title: bool = True) -> tuple[dict[str, Any], str]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_markdown_text(handle.read(), strip_title=strip_title)


def render_markdown(
    meta: Mapping[str, Any],
    body: str,
    *,
    key_order: Iterable[str] = (),
) -> str:
    lines = ["---"]
    written: set[str] = set()

    def append_value(key: str, value: Any) -> None:
        written.add(key)
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")

    for key in key_order:
        value = meta.get(key)
        if value not in (None, "", []):
            append_value(key, value)
    for key, value in meta.items():
        if key not in written and value not in (None, "", []):
            append_value(key, value)

    lines.extend(["---", ""])
    if meta.get("title"):
        lines.extend([f"# {meta['title']}", ""])
    lines.append(body)
    return "\n".join(lines)


def atomic_write_text(path: str, text: str) -> None:
    """Write beside the destination, fsync, then atomically replace it."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
