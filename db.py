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
from datetime import datetime, timedelta
from typing import Any, Iterable

from document_io import parse_frontmatter, read_markdown

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

-- ── 채널 자동 모니터링(신규 영상 → 자동 전사/요약) ──────────────────
-- 감지(폴러)와 처리(드레이너)가 공유. items(전사 이력)와는 별개 인덱스라 reindex 무관.
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    handle        TEXT,
    channel_id    TEXT UNIQUE NOT NULL,   -- UC... (RSS 키)
    title         TEXT,
    url           TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    baseline_done INTEGER NOT NULL DEFAULT 0,  -- 최초 폴 시 현재 피드를 seen 처리(백필 방지)
    last_checked  TEXT,
    added_at      TEXT,
    min_duration  INTEGER   -- 채널별 최소 길이(초) 오버라이드. NULL=전역 기본(MONITOR_MIN_DURATION)
);

CREATE TABLE IF NOT EXISTS watch_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    yt_id       TEXT UNIQUE NOT NULL,
    url         TEXT,
    title       TEXT,
    channel_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed/skipped/kf_retry
    reason      TEXT,
    added_at    TEXT,
    updated_at  TEXT,
    txt_path    TEXT,                             -- 캡처 재시도용 전사 md 경로(요약 성공 시 기록)
    kf_attempts INTEGER NOT NULL DEFAULT 0,       -- 캡처 재시도 횟수(1회로 제한)
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error_kind    TEXT,
    claimed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_wq_status ON watch_queue(status);
"""


def init() -> None:
    """앱 시작 시 1회 호출 — 스키마 생성/마이그레이션.

    버전 게이팅은 SQLite 내장 `PRAGMA user_version`(DB 헤더의 정수) 사용 — 별도 테이블 불필요.
    향후 마이그레이션은 `if ver < N: ...; PRAGMA user_version = N` 형태로 추가한다.
    """
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver < 1:
            # v1: is_read 컬럼(레거시 DB 보강) + 과거분(≤20260517) 읽음 처리 — 최초 1회뿐
            cols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
            if "is_read" not in cols:
                c.execute("ALTER TABLE items ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
                c.execute("UPDATE items SET is_read = 1 WHERE date <= '20260517'")
            c.execute("PRAGMA user_version = 1")
        if ver < 2:
            # v2: 채널별 최소 길이 오버라이드 컬럼(기존 channels 테이블 보강)
            ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
            if "min_duration" not in ccols:
                c.execute("ALTER TABLE channels ADD COLUMN min_duration INTEGER")
            c.execute("PRAGMA user_version = 2")
        if ver < 3:
            # v3: 캡처 재시도용 컬럼(watch_queue 보강) — 요약 성공/캡처 실패 시 다음 주기 1회 재시도
            wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
            if "txt_path" not in wcols:
                c.execute("ALTER TABLE watch_queue ADD COLUMN txt_path TEXT")
            if "kf_attempts" not in wcols:
                c.execute("ALTER TABLE watch_queue ADD COLUMN kf_attempts INTEGER NOT NULL DEFAULT 0")
            c.execute("PRAGMA user_version = 3")
        if ver < 4:
            wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
            additions = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT",
                "error_kind": "TEXT",
                "claimed_at": "TEXT",
            }
            for column, declaration in additions.items():
                if column not in wcols:
                    c.execute(f"ALTER TABLE watch_queue ADD COLUMN {column} {declaration}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_wq_retry ON watch_queue(status, next_retry_at)")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 예정/진행 라이브와 메타 조회 오류는 확정 제외가 아니다. 과거 누락분도 재판정한다.
            c.execute(
                """UPDATE watch_queue
                      SET status = 'deferred', next_retry_at = ?, error_kind = 'metadata_transient',
                          updated_at = ?
                    WHERE status = 'skipped'
                      AND (reason LIKE 'live:%' OR reason LIKE 'meta_error:%')""",
                (now, now),
            )
            # 서버 재시작·다운로드 일시 오류로 실패한 과거 전사도 제한된 재시도 경로에 태운다.
            c.execute(
                """UPDATE watch_queue
                      SET status = 'deferred', next_retry_at = ?, error_kind = 'pipeline_transient',
                          attempt_count = CASE WHEN attempt_count < 1 THEN 1 ELSE attempt_count END,
                          updated_at = ?
                    WHERE status = 'failed' AND reason LIKE '전사%'""",
                (now, now),
            )
            c.execute("PRAGMA user_version = 4")


# ── Markdown 파서 (app._parse_md와 동일 동작; 모듈 독립성 위해 복제) ────

def _parse_yaml_front_matter(text: str) -> dict:
    return parse_frontmatter(text)


def parse_md(md_path: str) -> tuple[dict, str]:
    return read_markdown(md_path)


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


def find_by_yt_id(yt_id: str) -> dict | None:
    """같은 영상(yt_id)을 이미 처리한 이력이 있으면 최신 1건 반환(중복 추가 경고용)."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return None
    r = _conn().execute(
        "SELECT * FROM items WHERE yt_id = ? ORDER BY date DESC, stem DESC LIMIT 1",
        (yt_id,),
    ).fetchone()
    return _row_to_item(r) if r else None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _after(seconds: int | float | None) -> str:
    delay = max(0, float(seconds or 0))
    return (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")


# ── 채널 모니터링: channels ────────────────────────────────────────────
def list_channels() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM channels ORDER BY title COLLATE NOCASE, id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_channel(cid: int) -> dict | None:
    r = _conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    return dict(r) if r else None


def channel_name_by_cid(channel_id: str) -> str:
    """channel_id(UC…) 문자열 → 표시용 채널명. title > @handle > 원본 순. 없으면 ''."""
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return ""
    r = _conn().execute(
        "SELECT title, handle FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if not r:
        return ""
    return (r["title"] or "").strip() or (("@" + r["handle"]) if r["handle"] else "")


def add_channel(channel_id: str, handle: str = "", title: str = "",
                url: str = "", enabled: bool = True) -> int:
    """채널 등록(이미 있으면 메타만 보강). id 반환."""
    channel_id = (channel_id or "").strip()
    if not channel_id:
        raise ValueError("channel_id required")
    with _lock:
        c = _conn()
        c.execute(
            """INSERT INTO channels (handle, channel_id, title, url, enabled, added_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 handle=COALESCE(NULLIF(excluded.handle,''), handle),
                 title =COALESCE(NULLIF(excluded.title ,''), title),
                 url   =COALESCE(NULLIF(excluded.url   ,''), url)""",
            (handle, channel_id, title, url, 1 if enabled else 0, _now()),
        )
        r = c.execute("SELECT id FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
        return int(r["id"])


def set_channel_enabled(cid: int, enabled: bool) -> bool:
    with _lock:
        cur = _conn().execute(
            "UPDATE channels SET enabled = ? WHERE id = ?", (1 if enabled else 0, cid)
        )
        return cur.rowcount > 0


def set_channel_baseline(cid: int) -> None:
    with _lock:
        _conn().execute("UPDATE channels SET baseline_done = 1 WHERE id = ?", (cid,))


def set_channel_min_duration(cid: int, seconds: int | None) -> bool:
    """채널별 최소 길이(초) 오버라이드. None이면 전역 기본으로 되돌림. 0이면 길이 제한 없음."""
    with _lock:
        cur = _conn().execute(
            "UPDATE channels SET min_duration = ? WHERE id = ?", (seconds, cid))
        return cur.rowcount > 0


def mark_channel_checked(cid: int) -> None:
    with _lock:
        _conn().execute("UPDATE channels SET last_checked = ? WHERE id = ?", (_now(), cid))


# ── 채널 모니터링: watch_queue ─────────────────────────────────────────
def in_queue(yt_id: str) -> bool:
    r = _conn().execute("SELECT 1 FROM watch_queue WHERE yt_id = ?", (yt_id,)).fetchone()
    return r is not None


def enqueue_video(yt_id: str, url: str, title: str, channel_id: str,
                  status: str = "pending", reason: str = "", *,
                  retry_after_seconds: int | float | None = None,
                  error_kind: str = "") -> bool:
    """큐에 추가(yt_id UNIQUE라 중복이면 무시). 새로 넣었으면 True."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return False
    with _lock:
        now = _now()
        next_retry_at = _after(retry_after_seconds) if retry_after_seconds is not None else None
        cur = _conn().execute(
            """INSERT OR IGNORE INTO watch_queue
                 (yt_id, url, title, channel_id, status, reason, added_at, updated_at,
                  next_retry_at, error_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (yt_id, url, title, channel_id, status, reason, now, now,
             next_retry_at, error_kind),
        )
        return cur.rowcount > 0


def queue_next_pending() -> dict | None:
    r = _conn().execute(
        "SELECT * FROM watch_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


def queue_claim_next() -> dict | None:
    """Atomically claim the oldest pending item for the single pipeline worker."""
    with _lock:
        conn = _conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM watch_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = _now()
            cur = conn.execute(
                """UPDATE watch_queue
                      SET status = 'processing', claimed_at = ?, updated_at = ?,
                          attempt_count = attempt_count + 1, next_retry_at = NULL
                    WHERE id = ? AND status = 'pending'""",
                (now, now, row["id"]),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute("SELECT * FROM watch_queue WHERE id = ?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return dict(claimed)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def queue_set_status(qid: int, status: str, reason: str = "") -> None:
    with _lock:
        _conn().execute(
            """UPDATE watch_queue
                  SET status = ?, reason = ?, updated_at = ?,
                      claimed_at = CASE WHEN ? = 'processing' THEN claimed_at ELSE NULL END,
                      next_retry_at = CASE WHEN ? = 'deferred' THEN next_retry_at ELSE NULL END
                WHERE id = ?""",
            (status, reason, _now(), status, status, qid),
        )


def queue_activate(qid: int, *, reset_attempts: bool = False) -> None:
    """Move a revalidated deferred item to pending without losing pipeline attempts."""
    with _lock:
        _conn().execute(
            """UPDATE watch_queue SET status = 'pending', reason = '', error_kind = NULL,
                      next_retry_at = NULL, claimed_at = NULL, updated_at = ?,
                      attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END
                WHERE id = ?""",
            (_now(), 1 if reset_attempts else 0, qid),
        )


def queue_set_txt_path(qid: int, txt_path: str) -> None:
    with _lock:
        _conn().execute(
            "UPDATE watch_queue SET txt_path = ?, updated_at = ? WHERE id = ?",
            (txt_path, _now(), qid),
        )


def queue_defer(
    qid: int,
    reason: str,
    *,
    error_kind: str,
    retry_after_seconds: int | float,
    increment_attempt: bool = False,
    max_attempts: int | None = None,
) -> str:
    """Schedule a retry, or make the item terminal after its retry budget."""
    with _lock:
        conn = _conn()
        row = conn.execute("SELECT attempt_count FROM watch_queue WHERE id = ?", (qid,)).fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempt_count"] or 0) + (1 if increment_attempt else 0)
        if max_attempts is not None and attempts >= max_attempts:
            conn.execute(
                """UPDATE watch_queue SET status = 'failed', reason = ?, error_kind = ?,
                          attempt_count = ?, next_retry_at = NULL, claimed_at = NULL, updated_at = ?
                    WHERE id = ?""",
                (reason, error_kind, attempts, _now(), qid),
            )
            return "failed"
        conn.execute(
            """UPDATE watch_queue SET status = 'deferred', reason = ?, error_kind = ?,
                      attempt_count = ?, next_retry_at = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?""",
            (reason, error_kind, attempts, _after(retry_after_seconds), _now(), qid),
        )
        return "deferred"


def queue_due_deferred(limit: int = 100) -> list[dict]:
    rows = _conn().execute(
        """SELECT * FROM watch_queue
            WHERE status = 'deferred' AND COALESCE(next_retry_at, '') <= ?
            ORDER BY id LIMIT ?""",
        (_now(), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def queue_recover_stale(stale_seconds: int = 10800) -> int:
    cutoff = (datetime.now() - timedelta(seconds=stale_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    now = _now()
    with _lock:
        cur = _conn().execute(
            """UPDATE watch_queue
                  SET status = 'deferred', reason = '중단된 processing 복구',
                      error_kind = 'worker_interrupted', next_retry_at = ?, claimed_at = NULL,
                      updated_at = ?
                WHERE status = 'processing'
                  AND COALESCE(claimed_at, updated_at, '') < ?""",
            (now, now, cutoff),
        )
        return cur.rowcount


def queue_mark_kf_retry(qid: int, txt_path: str, reason: str = "") -> None:
    """요약은 완료됐으나 캡처만 실패 → 다음 주기 캡처 재시도 예약(전사경로 보관, 시도횟수 +1)."""
    with _lock:
        _conn().execute(
            "UPDATE watch_queue SET status = 'kf_retry', txt_path = ?, "
            "kf_attempts = kf_attempts + 1, reason = ?, updated_at = ?, claimed_at = NULL "
            "WHERE id = ?",
            (txt_path, reason, _now(), qid),
        )


def queue_kf_retry_list() -> list[dict]:
    """캡처 재시도 대기(status='kf_retry') 항목 전체(id 순). drain 시작 시 스냅샷 용도."""
    rows = _conn().execute(
        "SELECT * FROM watch_queue WHERE status = 'kf_retry' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def queue_counts() -> dict:
    rows = _conn().execute(
        "SELECT status, COUNT(*) n FROM watch_queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def close_conn() -> None:
    """현재 스레드의 SQLite 연결을 닫는다.

    Flask 개발 서버는 요청마다 새 스레드를 만들고, 각 스레드가 _conn()으로
    별도 연결(WAL이면 db/-wal/-shm = FD 3개)을 열어 threadlocal에 캐시한다.
    요청 종료 시 닫지 않으면 FD가 누적돼 'Too many open files'로 이어진다.
    """
    c = getattr(_local, "c", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.c = None


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
