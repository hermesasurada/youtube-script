"""DB 스키마 마이그레이션 — `PRAGMA user_version` 번호별 함수 목록.

2026-11-16 리팩토링 4단계에서 db.init()의 `if ver < N:` 블록을 그대로 옮겼다(동작 변경 없음).
db.init()이 시작 시 읽은 user_version보다 큰 번호만 **아래 목록 순서대로** 돌린다. 순서는 역사적
그대로다 — v20이 v19보다 앞에 있다(새 DB는 v20 → v19 → v22 … 순으로 지나가 결과는 같다).
새 마이그레이션은 함수를 하나 만들고 MIGRATIONS 끝에 (번호, 함수)를 붙인다. 함수는 마지막에
`PRAGMA user_version = N`을 직접 실행한다.
"""
from __future__ import annotations

import json
from datetime import datetime

import db


def _v1(c) -> None:
    # v1: is_read 컬럼(레거시 DB 보강) + 과거분(≤20260517) 읽음 처리 — 최초 1회뿐
    cols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    if "is_read" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE items SET is_read = 1 WHERE date <= '20260517'")
    c.execute("PRAGMA user_version = 1")


def _v2(c) -> None:
    # v2: 채널별 최소 길이 오버라이드 컬럼(기존 channels 테이블 보강)
    ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
    if "min_duration" not in ccols:
        c.execute("ALTER TABLE channels ADD COLUMN min_duration INTEGER")
    c.execute("PRAGMA user_version = 2")


def _v3(c) -> None:
    # v3: 캡처 재시도용 컬럼(watch_queue 보강) — 요약 성공/캡처 실패 시 다음 주기 1회 재시도
    wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
    if "txt_path" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN txt_path TEXT")
    if "kf_attempts" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN kf_attempts INTEGER NOT NULL DEFAULT 0")
    c.execute("PRAGMA user_version = 3")


def _v4(c) -> None:
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


def _v5(c) -> None:
    # v5: 채널별 '지식증류 대상' 플래그. 증류(옵시디언 볼트)는 그동안
    # hermes 쪽 config의 채널명 하드코딩으로만 걸렀는데, 채널명이 바뀌면
    # 매칭이 깨지고 UI로 바꿀 수도 없었다. channel_id 기준으로 여기서 관리한다.
    ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
    if "distill" not in ccols:
        c.execute("ALTER TABLE channels ADD COLUMN distill INTEGER NOT NULL DEFAULT 1")
    # 기존 제외분(hermes config의 excluded_channels)을 옮겨온다.
    c.execute("UPDATE channels SET distill = 0 WHERE title IN ('집코노미')")
    c.execute("PRAGMA user_version = 5")


def _v6(c) -> None:
    # v6: 영상 단위 증류 오버라이드. NULL=미설정(채널 설정을 따름),
    # 1=포함, 0=제외. 채널 설정보다 우선한다.
    icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    if "distill" not in icols:
        c.execute("ALTER TABLE items ADD COLUMN distill INTEGER")   # NULL 허용 = 미설정
    c.execute("PRAGMA user_version = 6")


def _v7(c) -> None:
    # v7: 자동모니터의 요약/캡처 LLM 폴백 순서를 독립 저장한다.
    c.execute(
        "INSERT OR IGNORE INTO monitor_settings "
        "(id, summary_models, capture_models, updated_at) VALUES (1, ?, ?, ?)",
        ('["opus","gpt","grok"]', '["opus","gpt","grok"]', db._now()),
    )
    c.execute("PRAGMA user_version = 7")


def _v8(c) -> None:
    # v8: 외국어 제목의 한국어 번역. NULL=아직 없음, ''=번역 불필요(한국어 제목).
    icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    if "title_ko" not in icols:
        c.execute("ALTER TABLE items ADD COLUMN title_ko TEXT")
    c.execute("PRAGMA user_version = 8")


def _v9(c) -> None:
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


def _v10(c) -> None:
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


def _v11(c) -> None:
    # v11: 큐 순서 조정용 position. 기본은 id와 동일(FIFO)이며, 사용자가
    # 순서를 바꾸면 position끼리 맞바꾼다. 인출은 position 오름차순.
    wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
    if "position" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN position REAL")
    c.execute("UPDATE watch_queue SET position = id WHERE position IS NULL")
    c.execute("PRAGMA user_version = 11")


def _v12(c) -> None:
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


def _v13(c) -> None:
    # v13: 전사 시작 시각(초). NULL·0 = 처음부터.
    # 긴 팟캐스트에서 뒷부분만 필요할 때 앞을 통째로 건너뛴다.
    wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
    if "start_sec" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN start_sec INTEGER")
    c.execute("PRAGMA user_version = 13")


def _v14(c) -> None:
    # v14: 영상 북마크(즐겨찾기) + 큐 항목의 증류 지정.
    #  - items.bookmark: 0/1. 재색인 시 is_read처럼 보존된다(upsert SET에 없음).
    #  - watch_queue.distill: NULL=미지정(채널 설정 따름), 0/1=완료 시
    #    items.distill 오버라이드로 이관.
    icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    if "bookmark" not in icols:
        c.execute("ALTER TABLE items ADD COLUMN bookmark INTEGER NOT NULL DEFAULT 0")
    wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
    if "distill" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN distill INTEGER")
    c.execute("PRAGMA user_version = 14")


def _v15(c) -> None:
    # v15: 직전 실패 사유 보관. queue_activate가 pending 복귀 때 reason을
    # 비우므로, 같은 실패가 반복되는지 판정할 근거가 남지 않았다
    # (2026-09-01 멤버십 전용 영상이 매번 같은 전사 붕괴로 재시도됨).
    wcols = {r[1] for r in c.execute("PRAGMA table_info(watch_queue)").fetchall()}
    if "last_fail_reason" not in wcols:
        c.execute("ALTER TABLE watch_queue ADD COLUMN last_fail_reason TEXT")
    c.execute("PRAGMA user_version = 15")


def _v16(c) -> None:
    # v16: 블로그스팟 발행 기록(hermes_blogger). 중복 발행 방지 + 뷰어의 '블로그에서 보기'.
    icols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    for col in ("blog_url", "blog_post_id", "blog_published_at"):
        if col not in icols:
            c.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
    c.execute("PRAGMA user_version = 16")


def _v17(c) -> None:
    # v17: 요약 모델은 작업마다 순환한다. cursor는 '다음 작업'의 시작 모델이고,
    # reasoning은 모델별 추론 수준(default=각 CLI의 현재 기본 동작)이다.
    mcols = {r[1] for r in c.execute("PRAGMA table_info(monitor_settings)").fetchall()}
    if "summary_reasoning" not in mcols:
        c.execute(
            "ALTER TABLE monitor_settings ADD COLUMN summary_reasoning TEXT NOT NULL "
            "DEFAULT '{\"opus\":\"default\",\"gpt\":\"high\",\"grok\":\"default\"}'"
        )
    if "summary_cursor" not in mcols:
        c.execute(
            "ALTER TABLE monitor_settings ADD COLUMN summary_cursor INTEGER NOT NULL DEFAULT 0"
        )
    c.execute("PRAGMA user_version = 17")


def _v18(c) -> None:
    # v18: 각주 제외 용어(사용자 지정). 요약 뷰어의 ✕ 버튼으로 쌓이며 화면 렌더·
    # 블로그 발행·새 요약 프롬프트에 모두 적용된다. key는 비교용 정규화 형태.
    c.execute("""CREATE TABLE IF NOT EXISTS term_exclusions (
                key      TEXT PRIMARY KEY,
                term     TEXT NOT NULL,
                added_at TEXT NOT NULL
            )""")
    c.execute("PRAGMA user_version = 18")


def _v20(c) -> None:
    # 발행 당시의 블로그 본문 생성 방식(렌더 버전). 표시 형식을 바꾸면 이미 발행된
    # 글은 옛 형식이므로 '수정 필요'로 잡는 근거가 된다.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
    if "blog_render_ver" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN blog_render_ver TEXT")
    c.execute("PRAGMA user_version = 20")


def _v19(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS summary_notes (
                md_path TEXT NOT NULL,
                section_key TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (md_path, section_key)
            )""")
    c.execute("PRAGMA user_version = 19")


def _v22(c) -> None:
    # v22: 요약 슬롯별 구체 모델(Opus 별칭/5.5, GPT-6 Astra/Sol/Luna). 빈 값이면
    # 환경변수 기본값(CLAUDE_MODEL·GPT_MODEL)을 쓴다.
    mcols = {r[1] for r in c.execute("PRAGMA table_info(monitor_settings)").fetchall()}
    if "model_versions" not in mcols:
        c.execute("ALTER TABLE monitor_settings ADD COLUMN model_versions "
                  "TEXT NOT NULL DEFAULT '{}'")
    c.execute("PRAGMA user_version = 22")


def _v23(c) -> None:
    # v23: 요약 순번을 '슬롯 목록'으로(1~5개, 슬롯마다 구체 모델+추론 수준).
    # 기존 계열 순번·버전·추론 수준에서 그대로 옮겨 동작을 바꾸지 않는다.
    mcols = {r[1] for r in c.execute("PRAGMA table_info(monitor_settings)").fetchall()}
    if "summary_slots" not in mcols:
        c.execute("ALTER TABLE monitor_settings ADD COLUMN summary_slots "
                  "TEXT NOT NULL DEFAULT '[]'")
    row = c.execute("SELECT summary_models, summary_reasoning, model_versions "
                    "FROM monitor_settings WHERE id = 1").fetchone()
    if row:
        c.execute("UPDATE monitor_settings SET summary_slots = ? WHERE id = 1",
                  (json.dumps(db._slots_from_legacy(row)),))
    c.execute("PRAGMA user_version = 23")


def _v24(c) -> None:
    # v24: 제목 번역 모델을 설정 팝업에서 고른다(2026-09-29 사용자 지시).
    # 빈 값 = 환경변수 기본(TITLE_TR_MODEL·TITLE_TR_REASONING) — 동작을 바꾸지 않는다.
    mcols = {r[1] for r in c.execute("PRAGMA table_info(monitor_settings)").fetchall()}
    if "title_model" not in mcols:
        c.execute("ALTER TABLE monitor_settings ADD COLUMN title_model TEXT NOT NULL DEFAULT ''")
    if "title_effort" not in mcols:
        c.execute("ALTER TABLE monitor_settings ADD COLUMN title_effort TEXT NOT NULL DEFAULT ''")
    c.execute("PRAGMA user_version = 24")


def _v25(c) -> None:
    # v25: 인명 확인(판별·웹 검색) 모델을 설정 팝업에서 고른다(2026-10-08 사용자 지시).
    # 빈 값 = name_resolver 기본(Haiku 5.5) — 동작을 바꾸지 않는다.
    mcols = {r[1] for r in c.execute("PRAGMA table_info(monitor_settings)").fetchall()}
    for col in db.NAME_MODEL_COLUMNS:
        if col not in mcols:
            c.execute(f"ALTER TABLE monitor_settings ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    c.execute("PRAGMA user_version = 25")


def _v26(c) -> None:
    # v26: 각주 제외를 '뜻' 단위로(2026-10-30 사용자 지시). 같은 표기라도 뜻이 다르면
    # (하네스: AI 실행 환경 ↔ 차량 배선) 각주를 남긴다. senses = ✕를 누른 각주의 설명문
    # 목록(JSON), 빈 목록이면 예전처럼 표기만으로 뺀다. term_sense_checks = 다른 각주
    # 설명이 그 뜻과 같은지 로컬 LLM이 판정한 캐시(term_senses.py).
    tcols = {r[1] for r in c.execute("PRAGMA table_info(term_exclusions)").fetchall()}
    if "senses" not in tcols:
        c.execute("ALTER TABLE term_exclusions ADD COLUMN senses TEXT NOT NULL DEFAULT '[]'")
    c.execute("""CREATE TABLE IF NOT EXISTS term_sense_checks (
                key        TEXT NOT NULL,
                def_key    TEXT NOT NULL,
                same       INTEGER NOT NULL,
                definition TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                PRIMARY KEY (key, def_key)
            )""")
    c.execute("PRAGMA user_version = 26")


def _v27(c) -> None:
    # v27: 자동수집을 다시 켠 시각(UTC ISO). 꺼진 동안 올라온 영상은 처리하지 않고
    # 켠 뒤 올라온 영상만 처리한다(2026-11-01 사용자 지시). NULL = 끈 적 없음.
    ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()}
    if "resumed_at" not in ccols:
        c.execute("ALTER TABLE channels ADD COLUMN resumed_at TEXT")
    c.execute("PRAGMA user_version = 27")


MIGRATIONS = [
    (1, _v1),
    (2, _v2),
    (3, _v3),
    (4, _v4),
    (5, _v5),
    (6, _v6),
    (7, _v7),
    (8, _v8),
    (9, _v9),
    (10, _v10),
    (11, _v11),
    (12, _v12),
    (13, _v13),
    (14, _v14),
    (15, _v15),
    (16, _v16),
    (17, _v17),
    (18, _v18),
    (20, _v20),
    (19, _v19),
    (22, _v22),
    (23, _v23),
    (24, _v24),
    (25, _v25),
    (26, _v26),
    (27, _v27),
]
