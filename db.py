"""SQLite + FTS5 인덱스 — 전사/요약 md 파일에 대한 빠른 메타 쿼리/전문검색.

- 단일 진실 소스는 여전히 res/{date}/{stem}.md 와 res/summary/{date}/{stem}.md.
- 이 DB는 인덱스/캐시일 뿐이라 언제든 reindex_all()로 재구축 가능.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from document_io import parse_frontmatter, read_markdown
import llm_gateway

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RES_DIR     = os.path.join(BASE_DIR, "res")
SUMMARY_DIR = os.path.join(RES_DIR, "summary")
DB_PATH     = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "index.db"))

_lock = threading.RLock()
_HANGUL_RE = re.compile(r"[가-힣]")   # 제목에 한글이 있으면 번역 대상이 아니다
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
CREATE INDEX IF NOT EXISTS idx_items_date_stem ON items(date DESC, stem DESC);

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

CREATE TRIGGER IF NOT EXISTS items_au
AFTER UPDATE OF title, uploader, transcript, summary ON items BEGIN
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

CREATE TABLE IF NOT EXISTS monitor_settings (
    id             INTEGER PRIMARY KEY CHECK(id = 1),
    summary_models TEXT NOT NULL DEFAULT '["opus","gpt","grok"]',
    capture_models TEXT NOT NULL DEFAULT '["opus","gpt","grok"]',
    updated_at     TEXT
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
        if ver < 5:
            # v5: 채널별 '지식증류 대상' 플래그. 증류(옵시디언 볼트)는 그동안
            # hermes 쪽 config의 채널명 하드코딩으로만 걸렀는데, 채널명이 바뀌면
            # 매칭이 깨지고 UI로 바꿀 수도 없었다. channel_id 기준으로 여기서 관리한다.
            ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
            if "distill" not in ccols:
                c.execute("ALTER TABLE channels ADD COLUMN distill INTEGER NOT NULL DEFAULT 1")
            # 기존 제외분(hermes config의 excluded_channels)을 옮겨온다.
            c.execute("UPDATE channels SET distill = 0 WHERE title IN ('집코노미')")
            c.execute("PRAGMA user_version = 5")
        if ver < 6:
            # v6: 영상 단위 증류 오버라이드. NULL=미설정(채널 설정을 따름),
            # 1=포함, 0=제외. 채널 설정보다 우선한다.
            icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
            if "distill" not in icols:
                c.execute("ALTER TABLE items ADD COLUMN distill INTEGER")   # NULL 허용 = 미설정
            c.execute("PRAGMA user_version = 6")
        if ver < 7:
            # v7: 자동모니터의 요약/캡처 LLM 폴백 순서를 독립 저장한다.
            c.execute(
                "INSERT OR IGNORE INTO monitor_settings "
                "(id, summary_models, capture_models, updated_at) VALUES (1, ?, ?, ?)",
                ('["opus","gpt","grok"]', '["opus","gpt","grok"]', _now()),
            )
            c.execute("PRAGMA user_version = 7")
        if ver < 8:
            # v8: 외국어 제목의 한국어 번역. NULL=아직 없음, ''=번역 불필요(한국어 제목).
            icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
            if "title_ko" not in icols:
                c.execute("ALTER TABLE items ADD COLUMN title_ko TEXT")
            c.execute("PRAGMA user_version = 8")
        if ver < 9:
            # v9: 조회 정렬 인덱스 + FTS와 무관한 읽음/증류 변경 시 재색인 방지.
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_date_stem "
                      "ON items(date DESC, stem DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_items_unread_date_stem "
                      "ON items(is_read, date DESC, stem DESC)")
            c.execute("DROP TRIGGER IF EXISTS items_au")
            c.executescript("""
                CREATE TRIGGER items_au
                AFTER UPDATE OF title, uploader, transcript, summary ON items BEGIN
                  INSERT INTO items_fts(items_fts, rowid, title, uploader, transcript, summary)
                  VALUES('delete', old.rowid, old.title, old.uploader, old.transcript, old.summary);
                  INSERT INTO items_fts(rowid, title, uploader, transcript, summary)
                  VALUES (new.rowid, new.title, new.uploader, new.transcript, new.summary);
                END;
            """)
            c.execute("PRAGMA user_version = 9")
        if ver < 10:
            # v10: 전사 전문 번역(요약과 무관한 별도 작업)의 진행 상태.
            # 청크 단위로 이어할 수 있게 done/total을 남긴다.
            c.execute("""
                CREATE TABLE IF NOT EXISTS transcript_translation (
                    yt_id        TEXT PRIMARY KEY,
                    md_path      TEXT,
                    out_path     TEXT,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    chunks_done  INTEGER NOT NULL DEFAULT 0,
                    chunks_total INTEGER NOT NULL DEFAULT 0,
                    error        TEXT,
                    updated_at   TEXT
                )""")
            c.execute("PRAGMA user_version = 10")
        if ver < 11:
            # v11: 큐 순서 조정용 position. 기본은 id와 동일(FIFO)이며, 사용자가
            # 순서를 바꾸면 position끼리 맞바꾼다. 인출은 position 오름차순.
            wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
            if "position" not in wcols:
                c.execute("ALTER TABLE watch_queue ADD COLUMN position REAL")
            c.execute("UPDATE watch_queue SET position = id WHERE position IS NULL")
            c.execute("PRAGMA user_version = 11")
        if ver < 12:
            # v12: 영상 캡처(키프레임) 포함 여부.
            # watch_queue.capture: 수동 추가 시 영상 단위 지정(NULL=채널 설정 따름)
            # channels.capture:   채널별 기본값(1=포함)
            wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
            if "capture" not in wcols:
                c.execute("ALTER TABLE watch_queue ADD COLUMN capture INTEGER")
            ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
            if "capture" not in ccols:
                c.execute("ALTER TABLE channels ADD COLUMN capture INTEGER NOT NULL DEFAULT 1")
            c.execute("PRAGMA user_version = 12")
        if ver < 13:
            # v13: 전사 시작 시각(초). NULL·0 = 처음부터.
            # 긴 팟캐스트에서 뒷부분만 필요할 때 앞을 통째로 건너뛴다.
            wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
            if "start_sec" not in wcols:
                c.execute("ALTER TABLE watch_queue ADD COLUMN start_sec INTEGER")
            c.execute("PRAGMA user_version = 13")


# ── 제목 번역 ──────────────────────────────────────────────────────────

def get_title_ko(path: str) -> str | None:
    """요약/전사 경로로 번역 제목 조회. 없거나 불필요면 None."""
    r = _conn().execute(
        "SELECT title_ko FROM items WHERE md_path = ? OR summary_path = ? LIMIT 1",
        (path, path)).fetchone()
    if r is None:
        return None
    v = (r["title_ko"] or "").strip()
    return v or None


def set_title_ko(yt_id: str, value: str) -> bool:
    """번역 제목 저장. 빈 문자열은 '번역 불필요'로 기록해 재시도를 막는다."""
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET title_ko = ? WHERE yt_id = ?", (value, yt_id))
        return cur.rowcount > 0


def titles_needing_translation(limit: int = 500) -> list[dict]:
    """번역이 아직 없는 항목(제목에 한글이 없는 것만) → [{yt_id, title}]."""
    rows = _conn().execute(
        """SELECT yt_id, title FROM items
            WHERE title_ko IS NULL AND yt_id IS NOT NULL AND yt_id <> ''
              AND title IS NOT NULL AND title <> ''
            ORDER BY indexed_at DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        if _HANGUL_RE.search(r["title"] or ""):
            continue                       # 한국어 제목은 번역 대상이 아니다
        out.append({"yt_id": r["yt_id"], "title": r["title"]})
    return out


def mark_korean_titles() -> int:
    """한글이 든 제목은 번역 불필요로 확정(''), 매번 후보에 다시 오르지 않게 한다."""
    rows = _conn().execute(
        "SELECT yt_id, title FROM items WHERE title_ko IS NULL AND yt_id IS NOT NULL"
    ).fetchall()
    ids = [r["yt_id"] for r in rows if _HANGUL_RE.search(r["title"] or "")]
    if not ids:
        return 0
    with _lock:
        _conn().executemany("UPDATE items SET title_ko = '' WHERE yt_id = ?",
                            [(i,) for i in ids])
    return len(ids)


# ── 전사 전문 번역 (요약 파이프라인과 별개) ────────────────────────────

def set_translation_state(yt_id: str, md_path: str, out_path: str, status: str,
                          done: int, total: int, error: str = "") -> None:
    if not yt_id:
        return
    with _lock:
        _conn().execute(
            """INSERT INTO transcript_translation
                 (yt_id, md_path, out_path, status, chunks_done, chunks_total, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(yt_id) DO UPDATE SET
                 md_path=excluded.md_path, out_path=excluded.out_path,
                 status=excluded.status, chunks_done=excluded.chunks_done,
                 chunks_total=excluded.chunks_total, error=excluded.error,
                 updated_at=excluded.updated_at""",
            (yt_id, md_path, out_path, status, done, total, error or "", _now()))


def get_translation_state(yt_id: str) -> dict | None:
    r = _conn().execute("SELECT * FROM transcript_translation WHERE yt_id = ?",
                        (yt_id,)).fetchone()
    return dict(r) if r else None


def get_translation_for_path(path: str) -> dict | None:
    """전사(md_path) 또는 요약(summary_path) 경로로 번역 상태를 찾는다."""
    r = _conn().execute(
        """SELECT t.* FROM transcript_translation t
             JOIN items i ON i.yt_id = t.yt_id
            WHERE i.md_path = ? OR i.summary_path = ? LIMIT 1""",
        (path, path)).fetchone()
    return dict(r) if r else None


def yt_id_for_md(md_path: str) -> str | None:
    r = _conn().execute("SELECT yt_id FROM items WHERE md_path = ? LIMIT 1",
                        (md_path,)).fetchone()
    return r["yt_id"] if r else None


def translation_candidates(limit: int = 40, since_ts: float | None = None) -> list[dict]:
    """최근 영상부터, 아직 번역이 끝나지 않은 전사 목록.

    실패분은 여기서 제외한다(무한 재시도 방지). 중간에 끊긴 processing 건은
    다시 후보에 올려 이어서 처리한다.
    since_ts를 주면 그보다 오래된 전사는 아예 후보에서 뺀다(자동 처리 하한).
    """
    # failed 중에서도 연결 계열(서버 일시 무응답)은 6시간 뒤 후보로 복귀시킨다.
    # 진짜 영구 오류(파싱 불가 등)는 계속 제외 — 무한 재시도를 막는다.
    sql = """SELECT i.yt_id, i.md_path, i.title, i.indexed_at
               FROM items i
               LEFT JOIN transcript_translation t ON t.yt_id = i.yt_id
              WHERE i.yt_id IS NOT NULL AND i.yt_id <> ''
                AND i.md_path IS NOT NULL AND i.md_path <> ''
                AND (t.status IS NULL OR t.status = 'processing'
                     OR (t.status = 'failed'
                         AND (t.error LIKE '%Connection%' OR t.error LIKE '%timeout%')
                         AND t.updated_at < datetime('now', '-6 hours')))"""
    args: list = []
    if since_ts is not None:
        sql += " AND i.indexed_at >= ?"
        args.append(since_ts)
    sql += " ORDER BY i.indexed_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def translation_status_counts() -> list[dict]:
    rows = _conn().execute(
        "SELECT status, COUNT(*) n FROM transcript_translation GROUP BY status").fetchall()
    return [dict(r) for r in rows]


def get_translation_inflight() -> dict | None:
    r = _conn().execute(
        "SELECT * FROM transcript_translation WHERE status='processing' "
        "ORDER BY updated_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


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
        f"SELECT {_ITEM_COLUMNS} FROM items "
        "WHERE yt_id = ? ORDER BY date DESC, stem DESC LIMIT 1",
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


def set_channel_distill(cid: int, enabled: bool) -> bool:
    """채널을 지식증류(옵시디언 볼트) 대상에 포함할지. 0이면 증류 파이프라인이 건너뛴다."""
    with _lock:
        cur = _conn().execute(
            "UPDATE channels SET distill = ? WHERE id = ?", (1 if enabled else 0, cid))
        return cur.rowcount > 0


def get_monitor_model_orders() -> dict[str, list[str]]:
    """자동모니터의 요약/캡처별 LLM 처리 순서."""
    row = _conn().execute(
        "SELECT summary_models, capture_models FROM monitor_settings WHERE id = 1"
    ).fetchone()
    if not row:
        defaults = list(llm_gateway.MODEL_KEYS)
        return {"summary": defaults.copy(), "capture": defaults.copy()}
    return {
        "summary": llm_gateway.normalize_model_order(row["summary_models"]),
        "capture": llm_gateway.normalize_model_order(row["capture_models"]),
    }


def set_monitor_model_orders(*, summary=None, capture=None) -> dict[str, list[str]]:
    """보낸 작업의 모델 순서만 갱신하고 정규화된 전체 설정을 반환한다."""
    current = get_monitor_model_orders()
    summary_order = llm_gateway.normalize_model_order(
        current["summary"] if summary is None else summary
    )
    capture_order = llm_gateway.normalize_model_order(
        current["capture"] if capture is None else capture
    )
    with _lock:
        _conn().execute(
            """INSERT INTO monitor_settings (id, summary_models, capture_models, updated_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 summary_models=excluded.summary_models,
                 capture_models=excluded.capture_models,
                 updated_at=excluded.updated_at""",
            (json.dumps(summary_order), json.dumps(capture_order), _now()),
        )
    return {"summary": summary_order, "capture": capture_order}


def set_item_distill(path: str, value: bool | None) -> bool:
    """영상 단위 증류 오버라이드. None=미설정(채널 설정을 따름), True=포함, False=제외.

    path는 전사(md_path) 또는 요약(summary_path) 어느 쪽이든 받는다(뷰어마다 키가 달라서).
    """
    v = None if value is None else (1 if value else 0)
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET distill = ? WHERE md_path = ? OR summary_path = ?",
            (v, path, path))
        return cur.rowcount > 0


def get_item_distill(path: str) -> dict | None:
    """영상의 증류 설정 조회 → {override, channel, registered, effective}.

    effective = 영상 오버라이드가 있으면 그것, 없으면 채널 설정.
    registered = 자동수집 채널로 등록돼 있는지. 미등록이면 따를 채널 설정이 없어
    channel 값은 기본값(포함)이며, UI가 그 차이를 구분해 표기한다.
    """
    r = _conn().execute(
        """SELECT i.distill AS ov, i.channel_url, COALESCE(c.distill, 1) AS ch,
                  c.channel_id AS cid
             FROM items i
             LEFT JOIN channels c ON i.channel_url LIKE '%' || c.channel_id
            WHERE i.md_path = ? OR i.summary_path = ? LIMIT 1""",
        (path, path)).fetchone()
    if r is None:
        return None
    override = None if r["ov"] is None else bool(r["ov"])
    channel = bool(r["ch"])
    return {"override": override, "channel": channel,
            "registered": r["cid"] is not None,
            "effective": channel if override is None else override}


def distill_overrides() -> dict[str, bool]:
    """영상 단위 오버라이드가 지정된 것만 {yt_id: 포함여부}. 증류 파이프라인이 채널 설정보다 우선 적용."""
    rows = _conn().execute(
        "SELECT yt_id, distill FROM items WHERE distill IS NOT NULL AND yt_id IS NOT NULL"
    ).fetchall()
    return {r["yt_id"]: bool(r["distill"]) for r in rows}


def distill_excluded_names() -> list[str]:
    """증류 제외 채널의 표시명 전부(등록명 + 실제 이력에 쓰인 업로더명).

    채널명은 시간이 지나며 바뀌어서(예: 'RLWRLD' → 'RLWRLD - Dexterity Physical AI')
    한 이름만으론 과거 노트를 못 거른다. channel_id로 묶어 이름 변형을 모두 모은다.
    증류 파이프라인(hermes)이 이 목록을 읽어 문서 로딩 단계에서 제외한다.
    """
    rows = _conn().execute(
        """SELECT c.title, i.channel, i.uploader
             FROM channels c
             LEFT JOIN items i ON i.channel_url LIKE '%' || c.channel_id
            WHERE COALESCE(c.distill, 1) = 0"""
    ).fetchall()
    names = set()
    for r in rows:
        names |= {v.strip() for v in (r["title"], r["channel"], r["uploader"]) if v and v.strip()}
    return sorted(names)


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
                  error_kind: str = "", capture: bool | None = None,
                  start_sec: int | None = None) -> bool:
    """큐에 추가(yt_id UNIQUE라 중복이면 무시). 새로 넣었으면 True."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return False
    with _lock:
        now = _now()
        next_retry_at = _after(retry_after_seconds) if retry_after_seconds is not None else None
        conn = _conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO watch_queue
                 (yt_id, url, title, channel_id, status, reason, added_at, updated_at,
                  next_retry_at, error_kind, capture, start_sec)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (yt_id, url, title, channel_id, status, reason, now, now,
             next_retry_at, error_kind,
             None if capture is None else (1 if capture else 0),
             start_sec or None),
        )
        if cur.rowcount > 0:                       # 새 행의 position = id (FIFO 기본)
            conn.execute("UPDATE watch_queue SET position = id "
                         "WHERE yt_id = ? AND position IS NULL", (yt_id,))
        return cur.rowcount > 0


def queue_claim_one() -> dict | None:
    """본편 파이프라인(전사→요약→캡처)의 인출구 — pending 중 가장 오래된 한 건.

    주기당 1건 처리 원칙의 핵심. 캡처 재시도(kf_retry)는 본편 순서를 차지하지
    않도록 별도 인출구(queue_claim_kf_retry)로 분리돼 있다 — 같은 주기에
    '본편 1건 + 캡처 1건'까지 처리된다.
    """
    with _lock:
        conn = _conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM watch_queue WHERE status = 'pending' "
                "ORDER BY COALESCE(position, id), id LIMIT 1").fetchone()
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
            claimed = conn.execute("SELECT * FROM watch_queue WHERE id = ?",
                                   (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return dict(claimed)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def queue_claim_kf_retry() -> dict | None:
    """캡처 재시도 전용 인출구 — kf_retry 중 가장 오래된 한 건.

    캡처도 유튜브에서 저해상도 영상을 새로 받으므로(전사 오디오는 삭제됨)
    무제한으로 풀 수는 없고, 주기당 1건씩 본편과 별도로 처리한다.
    """
    with _lock:
        conn = _conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM watch_queue WHERE status = 'kf_retry' "
                "ORDER BY COALESCE(position, id), id LIMIT 1").fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = _now()
            cur = conn.execute(
                "UPDATE watch_queue SET status = 'processing', claimed_at = ?, "
                "updated_at = ? WHERE id = ? AND status = 'kf_retry'",
                (now, now, row["id"]))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute("SELECT * FROM watch_queue WHERE id = ?",
                                   (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return dict(claimed)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_item_by_yt_id(yt_id: str) -> dict | None:
    r = _conn().execute("SELECT md_path, title FROM items WHERE yt_id = ? LIMIT 1",
                        (yt_id,)).fetchone()
    return dict(r) if r else None


def queue_status_of(yt_id: str) -> str | None:
    r = _conn().execute("SELECT status FROM watch_queue WHERE yt_id = ?",
                        (yt_id,)).fetchone()
    return r["status"] if r else None


def set_queue_start_sec(yt_id: str, start_sec: int | None) -> None:
    """되살린(requeue) 항목의 전사 시작 시각 갱신 — 0/None이면 처음부터."""
    with _lock:
        _conn().execute(
            "UPDATE watch_queue SET start_sec = ?, updated_at = ? WHERE yt_id = ?",
            (start_sec or None, _now(), yt_id),
        )


def queue_requeue(yt_id: str, capture: bool | None = None) -> None:
    """종료 상태(done/failed/skipped) 항목을 줄 맨 뒤 pending으로 되살린다(수동 재처리)."""
    with _lock:
        conn = _conn()
        if capture is not None:
            conn.execute("UPDATE watch_queue SET capture = ? WHERE yt_id = ?",
                         (1 if capture else 0, yt_id))
        conn.execute(
            """UPDATE watch_queue
                  SET status = 'pending', reason = '', error_kind = NULL,
                      attempt_count = 0, next_retry_at = NULL, claimed_at = NULL,
                      position = (SELECT COALESCE(MAX(COALESCE(position, id)), 0) + 1
                                    FROM watch_queue),
                      updated_at = ?
                WHERE yt_id = ? AND status IN ('done', 'failed', 'skipped')""",
            (_now(), yt_id))


def capture_enabled_for(v: dict) -> bool:
    """이 큐 항목의 캡처 포함 여부 — 영상 단위 지정이 있으면 그것, 없으면 채널 설정.

    미등록 채널(수동 추가 등)은 기본 포함. 증류의 override/channel 구조와 같다.
    """
    ov = v.get("capture")
    if ov is not None:
        return bool(ov)
    cid = v.get("channel_id")
    if cid:
        r = _conn().execute("SELECT capture FROM channels WHERE channel_id = ?",
                            (cid,)).fetchone()
        if r is not None:
            return bool(r["capture"])
    return True


def set_channel_capture(chan_id: int, enabled: bool) -> bool:
    with _lock:
        cur = _conn().execute("UPDATE channels SET capture = ? WHERE id = ?",
                              (1 if enabled else 0, chan_id))
        return cur.rowcount > 0


def queue_overview(recent: int = 10) -> dict:
    """큐 팝업용 스냅샷 — 대기·진행 전체(처리 순서대로) + 최근 종료 몇 건."""
    conn = _conn()
    waiting = [dict(r) for r in conn.execute(
        """SELECT q.*, c.title AS channel_name FROM watch_queue q
             LEFT JOIN channels c ON c.channel_id = q.channel_id
            WHERE q.status IN ('processing', 'pending', 'kf_retry', 'deferred')
            ORDER BY CASE q.status WHEN 'processing' THEN 0
                                   WHEN 'pending' THEN 1
                                   WHEN 'kf_retry' THEN 1 ELSE 2 END,
                     COALESCE(q.position, q.id), q.id""")]
    finished = [dict(r) for r in conn.execute(
        """SELECT q.*, c.title AS channel_name FROM watch_queue q
             LEFT JOIN channels c ON c.channel_id = q.channel_id
            WHERE q.status IN ('done', 'failed')
            ORDER BY q.updated_at DESC LIMIT ?""", (recent,))]
    return {"waiting": waiting, "recent": finished}


def queue_move(qid: int, direction: str) -> bool:
    """대기 항목을 한 칸 위/아래로 — 같은 큐(같은 status)의 이웃과 position 교환.

    본편(pending)과 캡처 재시도(kf_retry)는 별도 큐라 서로 순서를 넘나들지 않는다.
    """
    if direction not in ("up", "down"):
        return False
    with _lock:
        conn = _conn()
        me = conn.execute(
            "SELECT id, status, COALESCE(position, id) AS pos FROM watch_queue "
            "WHERE id = ? AND status IN ('pending', 'kf_retry')", (qid,)).fetchone()
        if me is None:
            return False
        cmp_, order = ("<", "DESC") if direction == "up" else (">", "ASC")
        other = conn.execute(
            f"""SELECT id, COALESCE(position, id) AS pos FROM watch_queue
                 WHERE status = ?
                   AND (COALESCE(position, id), id) {cmp_} (?, ?)
                 ORDER BY COALESCE(position, id) {order}, id {order} LIMIT 1""",
            (me["status"], me["pos"], me["id"])).fetchone()
        if other is None:
            return False                            # 이미 맨 위/맨 아래
        conn.execute("UPDATE watch_queue SET position = ? WHERE id = ?",
                     (other["pos"], me["id"]))
        conn.execute("UPDATE watch_queue SET position = ? WHERE id = ?",
                     (me["pos"], other["id"]))
        return True


def queue_bump_attempt(qid: int, delta: int) -> None:
    """attempt_count 보정 — 라이브 예정처럼 '시도로 치지 않을' 경우 claim의 +1을 되돌린다."""
    with _lock:
        _conn().execute(
            "UPDATE watch_queue SET attempt_count = MAX(0, attempt_count + ?) WHERE id = ?",
            (delta, qid))


def queue_row(qid: int) -> dict | None:
    r = _conn().execute("SELECT * FROM watch_queue WHERE id = ?", (qid,)).fetchone()
    return dict(r) if r else None


def set_queue_capture(qid: int, enabled: bool) -> bool:
    """대기 중(pending·kf_retry·deferred) 항목의 캡처 포함을 영상 단위로 지정."""
    with _lock:
        cur = _conn().execute(
            """UPDATE watch_queue SET capture = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'kf_retry', 'deferred')""",
            (1 if enabled else 0, _now(), qid))
        return cur.rowcount > 0


def queue_cancel(qid: int) -> bool:
    """대기 중(pending·kf_retry·deferred) 항목만 취소 표시. 진행 중은 못 건드린다."""
    with _lock:
        cur = _conn().execute(
            """UPDATE watch_queue
                  SET status = 'skipped', reason = '사용자 취소', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'kf_retry', 'deferred')""",
            (_now(), qid))
        return cur.rowcount > 0


def queue_unclaim(qid: int) -> None:
    """claim을 되돌린다 — 처리를 시작하지 못한 경우(요약 게이트 불가 등).

    claim_one이 올린 attempt_count도 함께 되돌려, 시도하지 않은 주기가
    재시도 예산을 갉아먹지 않게 한다.
    """
    with _lock:
        _conn().execute(
            """UPDATE watch_queue
                  SET status = 'pending', claimed_at = NULL, updated_at = ?,
                      attempt_count = MAX(0, attempt_count - 1)
                WHERE id = ? AND status = 'processing'""",
            (_now(), qid))


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


def queue_has_processing() -> bool:
    r = _conn().execute(
        "SELECT 1 FROM watch_queue WHERE status = 'processing' LIMIT 1"
    ).fetchone()
    return r is not None


def last_queue_activity_epoch() -> float | None:
    """가장 최근 실제 처리(claim/완료/실패/재시도 예약) 시각.

    pending 적재는 처리가 아니므로 제외한다. 빈 폴 주기와 구분해야 수동 추가 시
    유휴 슬롯을 바로 쓸 수 있다.
    """
    row = _conn().execute(
        """SELECT MAX(COALESCE(claimed_at, updated_at)) AS t
             FROM watch_queue
            WHERE status IN ('processing','done','failed','kf_retry','deferred')
               OR claimed_at IS NOT NULL"""
    ).fetchone()
    raw = row["t"] if row else None
    if not raw:
        return None
    try:
        return time.mktime(time.strptime(str(raw), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError, OSError):
        return None


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

_ITEM_COLUMNS = """
    date, upload_date, stem, title, uploader, channel, duration, webpage_url,
    categories_json, tags_json, channel_url, has_txt, md_path, summary_path, is_read
"""

_HISTORY_COLUMNS = """
    rowid AS item_id, date, upload_date, stem, title, uploader, channel,
    duration, webpage_url, channel_url, has_txt,
    CASE WHEN summary_path IS NOT NULL THEN 1 ELSE 0 END AS has_summary,
    is_read
"""

def _row_to_item(r: sqlite3.Row) -> dict:
    return {
        "date":         r["date"],           # 전사 처리일(res/{date}/ 기준)
        "upload_date":  r["upload_date"] or "",   # 영상 게시일(YYYYMMDD, 없으면 "")
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


def _row_to_history_item(r: sqlite3.Row) -> dict:
    """웹 목록용 최소 projection. 파일 경로와 본문/태그는 상세 요청에서만 조회한다."""
    return {
        "item_id":       int(r["item_id"]),
        "date":          r["date"],
        "upload_date":   r["upload_date"] or "",
        "stem":          r["stem"],
        "title":         r["title"] or r["stem"],
        "uploader":      r["uploader"] or r["channel"] or "—",
        "duration":      float(r["duration"] or 0),
        "webpage_url":   r["webpage_url"] or "",
        "channel_url":   r["channel_url"] or "",
        "has_txt":       bool(r["has_txt"]),
        "has_summary":   bool(r["has_summary"]),
        "is_read":       bool(r["is_read"]),
    }


def list_items(unread_only: bool = False) -> list[dict]:
    where = "WHERE is_read = 0" if unread_only else ""
    rows = _conn().execute(
        f"SELECT {_ITEM_COLUMNS} FROM items {where} ORDER BY date DESC, stem DESC"
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def list_history_items(unread_only: bool = False) -> list[dict]:
    """웹 카드 목록. 큰 transcript/summary 및 내부 절대경로를 읽거나 전송하지 않는다."""
    where = "WHERE is_read = 0" if unread_only else ""
    rows = _conn().execute(
        f"SELECT {_HISTORY_COLUMNS} FROM items {where} ORDER BY date DESC, stem DESC"
    ).fetchall()
    return [_row_to_history_item(r) for r in rows]


def history_revision() -> str:
    """목록 표시값이 달라질 때 바뀌는 저비용 revision 토큰."""
    r = _conn().execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(MAX(indexed_at), 0) AS indexed,
                  COALESCE(SUM(rowid), 0) AS ids,
                  COALESCE(SUM(CASE WHEN is_read != 0 THEN rowid ELSE 0 END), 0) AS reads,
                  COALESCE(SUM(CASE WHEN summary_path IS NOT NULL THEN rowid ELSE 0 END), 0) AS summaries
             FROM items"""
    ).fetchone()
    return f'{r["n"]}:{float(r["indexed"]):.6f}:{r["ids"]}:{r["reads"]}:{r["summaries"]}'


def get_history_item(item_id: int) -> dict | None:
    """정수 ID를 상세 파일 경로로 해석한다. 경로는 목록 응답에 노출하지 않는다."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    if item_id <= 0:
        return None
    r = _conn().execute(
        """SELECT rowid AS item_id, md_path, summary_path, title, has_txt
             FROM items WHERE rowid = ?""",
        (item_id,),
    ).fetchone()
    return dict(r) if r else None


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
        SELECT i.rowid AS item_id, i.date, i.upload_date, i.stem, i.title,
               i.uploader, i.channel, i.duration, i.webpage_url, i.channel_url,
               i.has_txt,
               CASE WHEN i.summary_path IS NOT NULL THEN 1 ELSE 0 END AS has_summary,
               i.is_read
          FROM items i
          JOIN items_fts f ON f.rowid = i.rowid
        WHERE items_fts MATCH ?
        {unread_clause}
        ORDER BY i.date DESC, i.stem DESC
        LIMIT ?
        """,
        (expr, limit),
    ).fetchall()
    return [_row_to_history_item(r) for r in rows]


def mark_read(md_path: str, is_read: bool = True) -> bool:
    """읽음/안읽음 토글. 해당 항목이 없으면 False."""
    md_path = os.path.realpath(md_path)
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET is_read = ? WHERE md_path = ?",
            (1 if is_read else 0, md_path),
        )
        return cur.rowcount > 0


def mark_read_by_id(item_id: int, is_read: bool = True) -> bool:
    """웹 목록의 정수 ID로 읽음 상태를 갱신한다."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    with _lock:
        cur = _conn().execute(
            "UPDATE items SET is_read = ? WHERE rowid = ?",
            (1 if is_read else 0, item_id),
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
