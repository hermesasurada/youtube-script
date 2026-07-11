#!/usr/bin/env python3
"""기존 요약 md에 SUMMARY_COMPRESS 마커 백필.

LLM 재호출 없음. 전사 md 글자수 대비 요약 본문 글자수 비율만 계산해
`<!--SUMMARY_COMPRESS:N-->` 를 주입하고 index.db summary 컬럼을 갱신.

사용:
  python3 backfill_compress.py           # 전량
  python3 backfill_compress.py --dry-run # 쓰기 없이 통계만
  python3 backfill_compress.py --force   # 이미 있는 마커도 재계산
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import db

_MODEL_RE = re.compile(r"<!--\s*SUMMARY_MODEL:[\s\S]*?-->\s*", re.M)
_COMPRESS_RE = re.compile(r"<!--\s*SUMMARY_COMPRESS:(\d+)\s*-->\s*", re.M)


def _strip_markers(text: str) -> str:
    t = _MODEL_RE.sub("", text)
    t = _COMPRESS_RE.sub("", t)
    return t.lstrip("\n")


def _compress_pct(summary_body: str, transcript_chars: int) -> int | None:
    if not transcript_chars or not summary_body:
        return None
    return max(1, round(len(summary_body) / transcript_chars * 100))


def _inject(text: str, pct: int) -> str:
    """기존 SUMMARY_MODEL 유지, COMPRESS를 그 직후(또는 파일 선두)에 둠."""
    # 기존 compress 제거 후 재삽입
    text = _COMPRESS_RE.sub("", text)
    compress_line = f"<!--SUMMARY_COMPRESS:{pct}-->\n\n"
    m = re.match(r"(<!--\s*SUMMARY_MODEL:[\s\S]*?-->)(\s*)", text)
    if m:
        rest = text[m.end() :].lstrip("\n")
        return m.group(1).rstrip() + "\n\n" + compress_line + rest
    return compress_line + text.lstrip("\n")


def process_pair(md_path: str, summary_path: str, *, force: bool, dry_run: bool) -> str:
    """ok:N | skip | missing_* | err:..."""
    if not os.path.isfile(summary_path):
        return "missing_summary"
    if not os.path.isfile(md_path):
        return "missing_transcript"

    try:
        with open(summary_path, encoding="utf-8", errors="replace") as f:
            summary = f.read()
        with open(md_path, encoding="utf-8", errors="replace") as f:
            transcript = f.read()
    except OSError as e:
        return f"err:{e}"

    existing = _COMPRESS_RE.search(summary)
    if existing and not force:
        return "skip"

    body = _strip_markers(summary)
    pct = _compress_pct(body, len(transcript))
    if pct is None:
        return "no_chars"

    new = _inject(summary, pct)
    if new == summary:
        return "unchanged"

    if dry_run:
        return f"would:{pct}"

    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        return f"err:{e}"

    try:
        db.upsert_summary_only(md_path)
    except Exception:
        pass
    return f"ok:{pct}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill SUMMARY_COMPRESS chips")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="재계산·덮어쓰기")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    db.init()
    items = db.list_items()
    pairs = [
        (it.get("txt_path"), it.get("summary_path"))
        for it in items
        if it.get("summary_path") and it.get("txt_path")
    ]

    stats: dict[str, int] = {}
    would_pcts: list[int] = []
    n = 0
    for md, sp in pairs:
        st = process_pair(md, sp, force=args.force, dry_run=args.dry_run)
        key = st.split(":")[0]
        stats[key] = stats.get(key, 0) + 1
        if key in ("ok", "would") and ":" in st:
            try:
                would_pcts.append(int(st.split(":")[1]))
            except ValueError:
                pass
        n += 1
        if args.limit and n >= args.limit:
            break

    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"backfill_compress [{mode}]: {n} pairs")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if would_pcts:
        would_pcts.sort()
        mid = would_pcts[len(would_pcts) // 2]
        print(
            f"  pct range: min={would_pcts[0]} median={mid} max={would_pcts[-1]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
