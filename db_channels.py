"""채널 자동 모니터 설정 쿼리 — 등록 채널(수집·캡처·증류·최소 길이), 요약 슬롯·제목 번역·인명 확인 모델 설정, 증류 제외 채널, 채널별 처리 통계.

2026-11-16 리팩토링 4단계에서 db.py에서 옮겨 왔다(동작 변경 없음). 호출은 예전처럼 `db.이름`으로 한다 —
db.py가 이 모듈의 이름을 다시 내보낸다. 이 모듈 안에서도 db의 다른 이름은 `db.`를 붙여 부른다
(시험의 monkeypatch(db, …)·importlib.reload(db)가 예전처럼 먹게).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
import llm_gateway
import db

# ── 채널 모니터링: channels ────────────────────────────────────────────
def list_channels() -> list[dict]:
    rows = db._conn().execute(
        "SELECT * FROM channels ORDER BY title COLLATE NOCASE, id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_channel(cid: int) -> dict | None:
    r = db._conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    return dict(r) if r else None


def channel_name_by_cid(channel_id: str) -> str:
    """channel_id(UC…) 문자열 → 표시용 채널명. title > @handle > 원본 순. 없으면 ''."""
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return ""
    r = db._conn().execute(
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
    with db._lock:
        c = db._conn()
        c.execute(
            """INSERT INTO channels (handle, channel_id, title, url, enabled, added_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 handle=COALESCE(NULLIF(excluded.handle,''), handle),
                 title =COALESCE(NULLIF(excluded.title ,''), title),
                 url   =COALESCE(NULLIF(excluded.url   ,''), url)""",
            (handle, channel_id, title, url, 1 if enabled else 0, db._now()),
        )
        r = c.execute("SELECT id FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
        return int(r["id"])


def set_channel_enabled(cid: int, enabled: bool) -> bool:
    """자동수집 ON/OFF. 꺼져 있던 채널을 켜면 그 시각을 resumed_at에 남긴다 —
    폴러가 그보다 먼저 올라온 영상(꺼진 동안 쌓인 것)을 처리하지 않고 넘긴다."""
    with db._lock:
        conn = db._conn()
        if enabled:
            conn.execute(
                "UPDATE channels SET resumed_at = ? WHERE id = ? AND enabled = 0",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), cid))
        cur = conn.execute(
            "UPDATE channels SET enabled = ? WHERE id = ?", (1 if enabled else 0, cid)
        )
        return cur.rowcount > 0


def set_channel_baseline(cid: int) -> None:
    with db._lock:
        db._conn().execute("UPDATE channels SET baseline_done = 1 WHERE id = ?", (cid,))


def set_channel_min_duration(cid: int, seconds: int | None) -> bool:
    """채널별 최소 길이(초) 오버라이드. None이면 전역 기본으로 되돌림. 0이면 길이 제한 없음."""
    with db._lock:
        cur = db._conn().execute(
            "UPDATE channels SET min_duration = ? WHERE id = ?", (seconds, cid))
        return cur.rowcount > 0


def set_channel_distill(cid: int, enabled: bool) -> bool:
    """채널을 지식증류(옵시디언 볼트) 대상에 포함할지. 0이면 증류 파이프라인이 건너뛴다."""
    with db._lock:
        cur = db._conn().execute(
            "UPDATE channels SET distill = ? WHERE id = ?", (1 if enabled else 0, cid))
        return cur.rowcount > 0


_DEFAULT_SLOTS = [{"model": "opus", "effort": "default"},
                  {"model": "gpt-6-astra", "effort": "high"},
                  {"model": "grok", "effort": "default"}]


def _slots_from_legacy(row) -> list[dict]:
    """계열 순번(summary_models)·버전(model_versions)·추론(summary_reasoning) → 슬롯."""
    order = llm_gateway.normalize_round_robin_order(row["summary_models"])
    versions = db._json_dict(row["model_versions"])
    reasoning = llm_gateway.normalize_reasoning_levels(db._json_dict(row["summary_reasoning"]))
    pick = {"opus": versions.get("opus") or "opus",
            "gpt": versions.get("gpt") or "gpt-6-astra", "grok": "grok"}
    return llm_gateway.normalize_summary_slots(
        [{"model": pick[k], "effort": reasoning.get(k, "default")} for k in order],
        db._DEFAULT_SLOTS)


def get_monitor_summary_slots() -> dict:
    """요약 슬롯 목록과 다음 실행 슬롯 위치."""
    row = db._conn().execute(
        "SELECT summary_slots, summary_cursor FROM monitor_settings WHERE id = 1").fetchone()
    if not row:
        slots, cursor = [dict(x) for x in db._DEFAULT_SLOTS], 0
    else:
        slots = llm_gateway.normalize_summary_slots(db._json_list(row["summary_slots"]), db._DEFAULT_SLOTS, preserve_unknown=True)
        cursor = int(row["summary_cursor"] or 0) % len(slots)
    return {"slots": slots, "next_index": cursor}


def set_monitor_summary_slots(value) -> dict:
    slots = llm_gateway.normalize_summary_slots(value, db._DEFAULT_SLOTS, preserve_unknown=True)
    with db._lock:
        conn = db._conn()
        cur = conn.execute("SELECT summary_cursor FROM monitor_settings WHERE id = 1").fetchone()
        cursor = int(cur["summary_cursor"] or 0) % len(slots) if cur else 0
        if cur:
            conn.execute("UPDATE monitor_settings SET summary_slots = ?, summary_cursor = ?, "
                         "updated_at = ? WHERE id = 1", (json.dumps(slots), cursor, db._now()))
        else:
            order = json.dumps(list(llm_gateway.MODEL_KEYS))
            conn.execute("INSERT INTO monitor_settings (id, summary_models, capture_models, "
                         "summary_slots, updated_at) VALUES (1, ?, ?, ?, ?)",
                         (order, order, json.dumps(slots), db._now()))
    return db.get_monitor_summary_slots()


def get_title_model() -> dict:
    """저장된 제목 번역 모델·추론(없으면 빈 문자열 — 호출측이 환경변수 기본값을 쓴다)."""
    try:
        row = db._conn().execute(
            "SELECT title_model, title_effort FROM monitor_settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    return {"model": (row["title_model"] if row else "") or "",
            "effort": (row["title_effort"] if row else "") or ""}


def set_title_model(model: str, effort: str) -> dict:
    with db._lock:
        conn = db._conn()
        if conn.execute("SELECT 1 FROM monitor_settings WHERE id = 1").fetchone():
            conn.execute("UPDATE monitor_settings SET title_model = ?, title_effort = ?, updated_at = ? "
                         "WHERE id = 1", (model, effort, db._now()))
        else:
            order = json.dumps(list(llm_gateway.MODEL_KEYS))
            conn.execute("INSERT INTO monitor_settings (id, summary_models, capture_models, "
                         "summary_slots, title_model, title_effort, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                         (order, order, json.dumps([dict(x) for x in db._DEFAULT_SLOTS]), model, effort, db._now()))
    return db.get_title_model()


def get_name_models() -> dict:
    """저장된 인명 판별·검색 모델 {"detect": {"model", "effort"}, "lookup": {...}}(없으면 빈 값)."""
    try:
        row = db._conn().execute(
            f"SELECT {', '.join(db.NAME_MODEL_COLUMNS)} FROM monitor_settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    return {k: {"model": (row[f"name_{k}_model"] if row else "") or "",
                "effort": (row[f"name_{k}_effort"] if row else "") or ""}
            for k in db.NAME_MODEL_KINDS}


def set_name_model(kind: str, model: str, effort: str) -> dict:
    """인명 판별(detect)·검색(lookup) 중 한 칸을 저장한다. 빈 모델 = 기본값(추론도 비운다)."""
    if kind not in db.NAME_MODEL_KINDS:
        raise ValueError(f"unknown name model kind: {kind}")
    model = (model or "").strip()
    effort = (effort or "").strip() if model else ""
    mcol, ecol = f"name_{kind}_model", f"name_{kind}_effort"
    with db._lock:
        conn = db._conn()
        if conn.execute("SELECT 1 FROM monitor_settings WHERE id = 1").fetchone():
            conn.execute(f"UPDATE monitor_settings SET {mcol} = ?, {ecol} = ?, updated_at = ? "
                         "WHERE id = 1", (model, effort, db._now()))
        else:
            order = json.dumps(list(llm_gateway.MODEL_KEYS))
            conn.execute("INSERT INTO monitor_settings (id, summary_models, capture_models, "
                         f"summary_slots, {mcol}, {ecol}, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                         (order, order, json.dumps([dict(x) for x in db._DEFAULT_SLOTS]), model, effort, db._now()))
    return db.get_name_models()


def reserve_monitor_summary_slots() -> dict:
    """이번 작업의 시작 슬롯을 원자적으로 예약하고, 거기서부터 돈 슬롯 목록을 준다."""
    with db._lock:
        conn = db._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT summary_slots, summary_cursor FROM monitor_settings "
                               "WHERE id = 1").fetchone()
            slots = (llm_gateway.normalize_summary_slots(db._json_list(row["summary_slots"]),
                                                         db._DEFAULT_SLOTS, preserve_unknown=True)
                     if row else [dict(x) for x in db._DEFAULT_SLOTS])
            cursor = int(row["summary_cursor"] or 0) % len(slots) if row else 0
            next_cursor = (cursor + 1) % len(slots)
            if row:
                conn.execute("UPDATE monitor_settings SET summary_cursor = ?, updated_at = ? "
                             "WHERE id = 1", (next_cursor, db._now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    rotated = slots[cursor:] + slots[:cursor]
    return {"slots": rotated, "primary": rotated[0], "start_index": cursor,
            "next_index": next_cursor}


def distill_excluded_names() -> list[str]:
    """증류 제외 채널의 표시명 전부(등록명 + 실제 이력에 쓰인 업로더명).

    채널명은 시간이 지나며 바뀌어서(예: 'RLWRLD' → 'RLWRLD - Dexterity Physical AI')
    한 이름만으론 과거 노트를 못 거른다. channel_id로 묶어 이름 변형을 모두 모은다.
    증류 파이프라인(hermes)이 이 목록을 읽어 문서 로딩 단계에서 제외한다.
    """
    rows = db._conn().execute(
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
    with db._lock:
        db._conn().execute("UPDATE channels SET last_checked = ? WHERE id = ?", (db._now(), cid))


def set_channel_capture(chan_id: int, enabled: bool) -> bool:
    with db._lock:
        cur = db._conn().execute("UPDATE channels SET capture = ? WHERE id = ?",
                              (1 if enabled else 0, chan_id))
        return cur.rowcount > 0


def channel_stats() -> dict:
    """채널별 자동 처리 실적 {channel_id: {"done": 편수, "last_done": 마지막 완료 시각}}.
    설정-채널 탭에서 채널이 실제로 굴러가는지 한눈에 보이게 한다."""
    rows = db._conn().execute(
        "SELECT channel_id, COUNT(*) n, MAX(updated_at) last FROM watch_queue "
        "WHERE status = 'done' GROUP BY channel_id").fetchall()
    return {r["channel_id"]: {"done": r["n"], "last_done": r["last"]} for r in rows}
