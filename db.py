"""SQLite + FTS5 인덱스 — 전사/요약 md 파일에 대한 빠른 메타 쿼리/전문검색.

- 단일 진실 소스는 여전히 res/{date}/{stem}.md 와 res/summary/{date}/{stem}.md.
- 이 DB는 인덱스/캐시일 뿐이라 언제든 reindex_all()로 재구축 가능.

2026-11-16 리팩토링 4단계: 연결·스키마·공용 도우미만 여기 두고, 쿼리는 영역별 모듈(db_items 이력,
db_channels 채널·모니터 설정, db_terms 각주 제외, db_queue 처리 큐), 마이그레이션은 db_migrations로 옮겼다.
호출은 예전처럼 `db.이름` — 이 파일 끝에서 다시 내보낸다.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RES_DIR     = os.path.join(BASE_DIR, "res")
SUMMARY_DIR = os.path.join(RES_DIR, "summary")
DB_PATH     = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "index.db"))

_lock = threading.RLock()

# 인명 확인 모델(판별·검색) 저장 칸 — DB v25. 빈 문자열 = name_resolver 기본값.
NAME_MODEL_KINDS = ("detect", "lookup")
NAME_MODEL_COLUMNS = tuple(f"name_{k}_{f}" for k in NAME_MODEL_KINDS for f in ("model", "effort"))

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
    summary_reasoning TEXT NOT NULL DEFAULT '{"opus":"default","gpt":"high","grok":"default"}',
    summary_cursor INTEGER NOT NULL DEFAULT 0,
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
    claimed_at    TEXT,
    last_fail_reason TEXT                         -- 직전 실패 사유(pending 복귀 시 reason은 지워지므로 별도 보관)
);
CREATE INDEX IF NOT EXISTS idx_wq_status ON watch_queue(status);
"""


def init() -> None:
    """앱 시작 시 1회 호출 — 스키마 생성/마이그레이션.

    버전 게이팅은 SQLite 내장 `PRAGMA user_version`(DB 헤더의 정수) 사용 — 별도 테이블 불필요.
    마이그레이션 본문은 db_migrations.MIGRATIONS(번호별 함수, 역사적 순서)에 있다.
    """
    import db_migrations
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        for number, migrate in db_migrations.MIGRATIONS:
            if ver < number:
                migrate(c)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _after(seconds: int | float | None) -> str:
    delay = max(0, float(seconds or 0))
    return (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")


def _json_list(raw) -> list:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _json_dict(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


# ── 영역별 쿼리 모듈(2026-11-16 리팩토링 4단계) — 예전처럼 db.이름으로 부르도록 다시 내보낸다 ──
from db_items import (  # noqa: E402,F401
    get_summary_notes,
    summary_notes_mtime,
    save_summary_note,
    get_title_ko,
    clear_blog_publish,
    set_blog_publish,
    set_title_ko,
    titles_needing_translation,
    mark_korean_titles,
    set_translation_state,
    get_translation_state,
    get_translation_for_path,
    yt_id_for_md,
    translation_candidates,
    translation_status_counts,
    get_translation_inflight,
    _parse_yaml_front_matter,
    parse_md,
    _summary_path_for,
    upsert,
    upsert_summary_only,
    delete,
    has_summary_for_yt_id,
    find_by_yt_id,
    set_item_distill,
    get_item_distill,
    distill_overrides,
    _ITEM_COLUMNS,
    _HISTORY_COLUMNS,
    _row_to_item,
    _row_to_history_item,
    _UC_RE,
    _channel_titles,
    _apply_channel_title,
    _history_distill,
    list_items,
    list_history_items,
    history_revision,
    get_history_item,
    search,
    mark_read,
    set_bookmark_by_id,
    mark_read_by_id,
    stats,
    reindex_all,
)
from db_channels import (  # noqa: E402,F401
    list_channels,
    get_channel,
    channel_name_by_cid,
    add_channel,
    set_channel_enabled,
    set_channel_baseline,
    set_channel_min_duration,
    set_channel_distill,
    _DEFAULT_SLOTS,
    _slots_from_legacy,
    get_monitor_summary_slots,
    set_monitor_summary_slots,
    get_title_model,
    set_title_model,
    get_name_models,
    set_name_model,
    reserve_monitor_summary_slots,
    distill_excluded_names,
    mark_channel_checked,
    set_channel_capture,
    channel_stats,
)
from db_terms import (  # noqa: E402,F401
    _TERM_HYPHENS_RE,
    normalize_term,
    list_term_exclusions,
    term_exclusion_times,
    sense_key,
    add_term_exclusion,
    remove_term_exclusion,
    _senses_of,
    term_exclusion_senses,
    set_term_exclusion_senses,
    term_sense_checks,
    set_term_sense_check,
)
from db_queue import (  # noqa: E402,F401
    in_queue,
    last_queue_prefs_for_channel,
    enqueue_video,
    queue_claim_one,
    queue_claim_kf_retry,
    get_item_by_yt_id,
    queue_status_of,
    set_queue_start_sec,
    queue_requeue,
    capture_enabled_for,
    queue_overview,
    queue_move,
    queue_bump_attempt,
    queue_row,
    set_queue_capture,
    set_queue_distill,
    queue_cancel,
    queue_unclaim,
    queue_set_status,
    queue_activate,
    queue_set_txt_path,
    queue_defer,
    queue_due_deferred,
    queue_recover_stale,
    queue_recover_interrupted,
    queue_mark_kf_retry,
    queue_defer_kf_retry,
    queue_has_processing,
    last_queue_activity_epoch,
    queue_counts,
)
