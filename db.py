"""SQLite + FTS5 인덱스 — 전사/요약 md 파일에 대한 빠른 메타 쿼리/전문검색.

- 단일 진실 소스는 여전히 res/{date}/{stem}.md 와 res/summary/{date}/{stem}.md.
- 이 DB는 인덱스/캐시일 뿐이라 언제든 reindex_all()로 재구축 가능.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RES_DIR     = os.path.join(BASE_DIR, "res")
SUMMARY_DIR = os.path.join(RES_DIR, "summary")
DB_PATH     = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "index.db"))

_lock = threading.RLock()
_local = threading.local()


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    c = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")
    _local.c = c
    return c


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    md_path         TEXT PRIMARY KEY,
    summary_path    TEXT,
    date            TEXT NOT NULL,
    stem            TEXT NOT NULL,
    title           TEXT,
    uploader        TEXT,
    channel         TEXT,
    channel_url     TEXT,
    duration        REAL NOT NULL DEFAULT 0,
    upload_date     TEXT,
    webpage_url     TEXT,
    yt_id           TEXT,
    categories_json TEXT NOT NULL DEFAULT '[]',
    tags_json       TEXT NOT NULL DEFAULT '[]',
    source_file     TEXT,
    has_txt         INTEGER NOT NULL DEFAULT 0,
    transcript      TEXT,
    summary         TEXT,
    mtime_md        REAL NOT NULL,
    mtime_summary   REAL,
    indexed_at      REAL NOT NULL,
    is_read         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_date     ON items(date DESC);
CREATE INDEX IF NOT EXISTS idx_items_uploader ON items(uploader);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, uploader, transcript, summary,
    content='items', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, uploader, transcript, summary)
  VALUES (new.rowid, new.title, new.uploader, new.transcript, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, uploader, transcript, summary)
  VALUES('delete', old.rowid, old.title, old.uploader, old.transcript, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, uploader, transcript, summary)
  VALUES('delete', old.rowid, old.title, old.uploader, old.transcript, old.summary);
  INSERT INTO items_fts(rowid, title, uploader, transcript, summary)
  VALUES (new.rowid, new.title, new.uploader, new.transcript, new.summary);
END;
"""


def init() -> None:
    """앱 시작 시 1회 호출 — 스키마 생성/마이그레이션."""
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        # 마이그레이션: is_read 컬럼 (최초 1회)
        cols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        if "is_read" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE items SET is_read = 1 WHERE date <= '20260517'")


# ── Markdown 파서 (app._parse_md와 동일 동작; 모듈 독립성 위해 복제) ────

def _parse_yaml_front_matter(text: str) -> dict:
    result: dict = {}
    current_list_key = None
    for line in text.splitlines():
        if line.startswith("  - "):
            if current_list_key is not None:
                result[current_list_key].append(line[4:].strip())
        elif ": " in line:
            k, v = line.split(": ", 1)
            result[k.strip()] = v.strip()
            current_list_key = None
        elif line.endswith(":") and not line.startswith(" "):
            k = line[:-1].strip()
            result[k] = []
            current_list_key = k
        else:
            current_list_key = None
    return result


def parse_md(md_path: str) -> tuple[dict, str]:
    with open(md_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    if not content.startswith("---\n"):
        return {}, content.strip()
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content.strip()
    meta = _parse_yaml_front_matter(content[4:end])
    body = content[end + 5:].strip()
    if body.startswith("# "):
        nl = body.find("\n")
        body = body[nl + 1:].strip() if nl != -1 else ""
    return meta, body


# ── upsert / delete ────────────────────────────────────────────────────

def _summary_path_for(md_path: str) -> str | None:
    """md_path → 대응하는 summary md 경로 (있으면 절대경로, 없으면 None)."""
    try:
        rel = os.path.relpath(md_path, RES_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    sp = os.path.join(SUMMARY_DIR, rel)
    return sp if os.path.isfile(sp) else None


def upsert(md_path: str) -> bool:
    """전사 md 1건을 인덱싱. summary md가 있으면 함께. 파일 없으면 False."""
    md_path = os.path.realpath(md_path)
    if not os.path.isfile(md_path):
        return False

    try:
        meta, transcript = parse_md(md_path)
    except Exception:
        return False

    rel = os.path.relpath(md_path, RES_DIR)
    parts = rel.split(os.sep)
    if len(parts) < 2:
        return False
    date_dir = parts[0]
    stem = os.path.splitext(parts[-1])[0]

    sp = _summary_path_for(md_path)
    summary_text: str | None = None
    mtime_summary: float | None = None
    if sp:
        try:
            with open(sp, encoding="utf-8", errors="replace") as f:
                summary_text = f.read()
            mtime_summary = os.path.getmtime(sp)
        except Exception:
            sp = None

    try:
        mtime_md = os.path.getmtime(md_path)
    except OSError:
        return False

    with _lock:
        _conn().execute(
            """
            INSERT INTO items (
                md_path, summary_path, date, stem, title, uploader, channel, channel_url,
                duration, upload_date, webpage_url, yt_id,
                categories_json, tags_json, source_file,
                has_txt, transcript, summary, mtime_md, mtime_summary, indexed_at
            ) VALUES (?,?,?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?,?)
            ON CONFLICT(md_path) DO UPDATE SET
                summary_path=excluded.summary_path,
                date=excluded.date, stem=excluded.stem,
                title=excluded.title, uploader=excluded.uploader,
                channel=excluded.channel, channel_url=excluded.channel_url,
                duration=excluded.duration, upload_date=excluded.upload_date,
                webpage_url=excluded.webpage_url, yt_id=excluded.yt_id,
                categories_json=excluded.categories_json,
                tags_json=excluded.tags_json,
                source_file=excluded.source_file,
                has_txt=excluded.has_txt,
                transcript=excluded.transcript,
                summary=excluded.summary,
                mtime_md=excluded.mtime_md,
                mtime_summary=excluded.mtime_summary,
                indexed_at=excluded.indexed_at
                -- is_read 는 의도적으로 제외 (재인덱싱 시 읽음 상태 보존)
            """,
            (
                md_path,
                sp,
                date_dir,
                stem,
                meta.get("title") or "",
                meta.get("uploader") or "",
                meta.get("channel") or "",
                meta.get("channel_url") or "",
                float(meta.get("duration") or 0),
                meta.get("upload_date") or "",
                meta.get("webpage_url") or "",
                meta.get("id") or "",
                json.dumps(meta.get("categories") or [], ensure_ascii=False),
                json.dumps(meta.get("tags") or [], ensure_ascii=False),
                meta.get("source_file") or "",
                1 if transcript else 0,
                transcript,
                summary_text,
                mtime_md,
                mtime_summary,
                time.time(),
            ),
        )
    return True


def upsert_summary_only(md_path: str) -> bool:
    """전사 md의 summary 파일만 갱신됐을 때 호출. 본문 다시 안 읽음."""
    md_path = os.path.realpath(md_path)
    sp = _summary_path_for(md_path)
    summary_text: str | None = None
    mtime_summary: float | None = None
    if sp:
        try:
            with open(sp, encoding="utf-8", errors="replace") as f:
                summary_text = f.read()
            mtime_summary = os.path.getmtime(sp)
        except Exception:
            sp = None
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET summary_path = ?, summary = ?, mtime_summary = ?, indexed_at = ? "
            "WHERE md_path = ?",
            (sp, summary_text, mtime_summary, time.time(), md_path),
        )
        return cur.rowcount > 0


def delete(md_path: str) -> None:
    md_path = os.path.realpath(md_path)
    with _lock:
        _conn().execute("DELETE FROM items WHERE md_path = ?", (md_path,))


# ── 조회 ───────────────────────────────────────────────────────────────

def _row_to_item(r: sqlite3.Row) -> dict:
    return {
        "date":         r["date"],
        "stem":         r["stem"],
        "title":        r["title"] or r["stem"],
        "uploader":     r["uploader"] or r["channel"] or "—",
        "duration":     float(r["duration"] or 0),
        "webpage_url":  r["webpage_url"] or "",
        "categories":   json.loads(r["categories_json"] or "[]"),
        "tags":         json.loads(r["tags_json"] or "[]"),
        "channel_url":  r["channel_url"] or "",
        "has_txt":      bool(r["has_txt"]),
        "txt_path":     r["md_path"],
        "summary_path": r["summary_path"] if r["summary_path"] else None,
        "is_read":      bool(r["is_read"]),
    }


def list_items(unread_only: bool = False) -> list[dict]:
    where = "WHERE is_read = 0" if unread_only else ""
    rows = _conn().execute(
        f"SELECT * FROM items {where} ORDER BY date DESC, stem DESC"
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def search(q: str, limit: int = 500, unread_only: bool = False) -> list[dict]:
    """FTS5 매칭. q는 사용자 입력 그대로(공백 토큰 분리)."""
    q = (q or "").strip()
    if not q:
        return list_items(unread_only=unread_only)
    # 안전: 따옴표 등 escape (간단 처리: 따옴표 제거 후 OR 매칭이 아니라 모두 포함)
    safe = q.replace('"', " ").replace("'", " ")
    # 단어별 prefix 매칭으로 묶기
    terms = [t for t in safe.split() if t]
    if not terms:
        return list_items(unread_only=unread_only)
    expr = " ".join(f'"{t}"*' for t in terms)
    unread_clause = "AND i.is_read = 0" if unread_only else ""
    rows = _conn().execute(
        f"""
        SELECT i.* FROM items i
          JOIN items_fts f ON f.rowid = i.rowid
        WHERE items_fts MATCH ?
        {unread_clause}
        ORDER BY i.date DESC, i.stem DESC
        LIMIT ?
        """,
        (expr, limit),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def mark_read(md_path: str, is_read: bool = True) -> bool:
    """읽음/안읽음 토글. 해당 항목이 없으면 False."""
    md_path = os.path.realpath(md_path)
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET is_read = ? WHERE md_path = ?",
            (1 if is_read else 0, md_path),
        )
        return cur.rowcount > 0


def stats() -> dict:
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    has_summary = c.execute("SELECT COUNT(*) FROM items WHERE summary_path IS NOT NULL").fetchone()[0]
    return {"total": total, "with_summary": has_summary}


# ── 일괄 reindex ───────────────────────────────────────────────────────

def reindex_all(force: bool = False) -> dict:
    """res/*/*.md 전수 스캔. mtime 변경분만 upsert. 사라진 파일은 제거."""
    seen: set[str] = set()
    n_upserted = 0
    n_skipped = 0

    if os.path.isdir(RES_DIR):
        for date in sorted(os.listdir(RES_DIR), reverse=True):
            if date == "summary":
                continue
            dpath = os.path.join(RES_DIR, date)
            if not os.path.isdir(dpath):
                continue
            for fname in os.listdir(dpath):
                if not fname.endswith(".md"):
                    continue
                p = os.path.realpath(os.path.join(dpath, fname))
                seen.add(p)
                fs_md = os.path.getmtime(p)
                sp = os.path.join(SUMMARY_DIR, date, fname)
                fs_sum = os.path.getmtime(sp) if os.path.isfile(sp) else None

                if not force:
                    cur = _conn().execute(
                        "SELECT mtime_md, mtime_summary FROM items WHERE md_path = ?",
                        (p,),
                    ).fetchone()
                    if (cur and cur["mtime_md"] == fs_md
                            and (cur["mtime_summary"] or None) == fs_sum):
                        n_skipped += 1
                        continue
                if upsert(p):
                    n_upserted += 1

    # 사라진 파일 제거
    rows = _conn().execute("SELECT md_path FROM items").fetchall()
    n_removed = 0
    for r in rows:
        if r["md_path"] not in seen:
            delete(r["md_path"])
            n_removed += 1

    return {"upserted": n_upserted, "skipped": n_skipped, "removed": n_removed}
