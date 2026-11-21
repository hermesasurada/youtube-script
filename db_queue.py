"""처리 큐(watch_queue) 쿼리 — 등록·인출(claim)·재시도(defer)·순서 이동·상태 전환·복구.

2026-11-16 리팩토링 4단계에서 db.py에서 옮겨 왔다(동작 변경 없음). 호출은 예전처럼 `db.이름`으로 한다 —
db.py가 이 모듈의 이름을 다시 내보낸다. 이 모듈 안에서도 db의 다른 이름은 `db.`를 붙여 부른다
(시험의 monkeypatch(db, …)·importlib.reload(db)가 예전처럼 먹게).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
import db

# ── 채널 모니터링: watch_queue ─────────────────────────────────────────
def in_queue(yt_id: str) -> bool:
    r = db._conn().execute("SELECT 1 FROM watch_queue WHERE yt_id = ?", (yt_id,)).fetchone()
    return r is not None


def last_queue_prefs_for_channel(channel: str) -> dict:
    """같은 채널에서 마지막으로 쓴 캡처·증류 지정값(수동 큐 등록의 기본값).

    watch_queue에는 채널명이 없어 items(uploader/channel)로 이어 찾는다. 캡처와 증류는
    한쪽만 지정한 경우가 있어 각각 따로 가장 최근의 '명시값'을 고른다. 증류는 요약 화면에서
    나중에 바꿀 수 있으므로 items.distill(최종 상태)을 큐 값보다 앞에 둔다. 둘 다 없으면
    모니터 채널 기본 설정(channels)으로 내려간다. 값이 없으면 키를 넣지 않는다.
    """
    channel = (channel or "").strip()
    out: dict = {}
    if not channel:
        return out
    conn = db._conn()

    def latest(sql: str, *params):
        r = conn.execute(sql, params).fetchone()
        return None if r is None or r[0] is None else bool(r[0])

    cap = latest(
        """SELECT q.capture FROM watch_queue q JOIN items i ON i.yt_id = q.yt_id
           WHERE (i.uploader = ? OR i.channel = ?) AND q.capture IS NOT NULL
           ORDER BY q.id DESC LIMIT 1""", channel, channel)
    dis = latest(
        """SELECT i.distill FROM items i
           WHERE (i.uploader = ? OR i.channel = ?) AND i.distill IS NOT NULL
           ORDER BY i.date DESC, i.stem DESC LIMIT 1""", channel, channel)
    if dis is None:
        dis = latest(
            """SELECT q.distill FROM watch_queue q JOIN items i ON i.yt_id = q.yt_id
               WHERE (i.uploader = ? OR i.channel = ?) AND q.distill IS NOT NULL
               ORDER BY q.id DESC LIMIT 1""", channel, channel)
    source = "history"
    if cap is None or dis is None:                      # 모니터 채널이면 그 기본 설정으로
        row = conn.execute(
            """SELECT capture, distill FROM channels c
               WHERE c.title = ? OR EXISTS (SELECT 1 FROM items i
                     WHERE (i.uploader = ? OR i.channel = ?) AND i.channel_url LIKE '%' || c.channel_id)
               LIMIT 1""", (channel, channel, channel)).fetchone()   # 언어별 채널명이 달라 이력의 채널 주소로도 잇는다
        if row:
            if cap is None and row["capture"] is not None:
                cap, source = bool(row["capture"]), "channel"
            if dis is None and row["distill"] is not None:
                dis, source = bool(row["distill"]), "channel"
    if cap is not None:
        out["capture"] = cap
    if dis is not None:
        out["distill"] = dis
    if out:
        out["source"] = source
    return out


def enqueue_video(yt_id: str, url: str, title: str, channel_id: str,
                  status: str = "pending", reason: str = "", *,
                  retry_after_seconds: int | float | None = None,
                  error_kind: str = "", capture: bool | None = None,
                  start_sec: int | None = None,
                  distill: bool | None = None) -> bool:
    """큐에 추가(yt_id UNIQUE라 중복이면 무시). 새로 넣었으면 True."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return False
    with db._lock:
        now = db._now()
        next_retry_at = db._after(retry_after_seconds) if retry_after_seconds is not None else None
        conn = db._conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO watch_queue
                 (yt_id, url, title, channel_id, status, reason, added_at, updated_at,
                  next_retry_at, error_kind, capture, start_sec, distill)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (yt_id, url, title, channel_id, status, reason, now, now,
             next_retry_at, error_kind,
             None if capture is None else (1 if capture else 0),
             start_sec or None,
             None if distill is None else (1 if distill else 0)),
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
    with db._lock:
        conn = db._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM watch_queue WHERE status = 'pending' "
                "ORDER BY COALESCE(position, id), id LIMIT 1").fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = db._now()
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
    with db._lock:
        conn = db._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM watch_queue WHERE status = 'kf_retry' "
                "AND COALESCE(next_retry_at, '') <= ? "          # post_live 대기분은 due 전엔 안 꺼낸다
                "ORDER BY COALESCE(position, id), id LIMIT 1", (db._now(),)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = db._now()
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
    r = db._conn().execute(
        "SELECT md_path, summary_path, title FROM items "
        "WHERE yt_id = ? ORDER BY date DESC, stem DESC LIMIT 1",
        (yt_id,),
    ).fetchone()
    return dict(r) if r else None


def queue_status_of(yt_id: str) -> str | None:
    r = db._conn().execute("SELECT status FROM watch_queue WHERE yt_id = ?",
                        (yt_id,)).fetchone()
    return r["status"] if r else None


def set_queue_start_sec(yt_id: str, start_sec: int | None,
                        url: str | None = None) -> None:
    """되살린(requeue) 항목의 전사 시작 시각 갱신 — 0/None이면 처음부터.

    url도 함께 갱신한다(주어진 경우). start_sec 컬럼만 지우고 url에 이전
    &t=가 남으면 /start가 URL에서 시각을 다시 추출해 구간 전사가 재발한다.
    """
    with db._lock:
        if url:
            db._conn().execute(
                "UPDATE watch_queue SET start_sec = ?, url = ?, updated_at = ? WHERE yt_id = ?",
                (start_sec or None, url, db._now(), yt_id))
        else:
            db._conn().execute(
                "UPDATE watch_queue SET start_sec = ?, updated_at = ? WHERE yt_id = ?",
                (start_sec or None, db._now(), yt_id))


def queue_requeue(yt_id: str, capture: bool | None = None,
                  distill: bool | None = None) -> None:
    """종료 상태(done/failed/skipped) 항목을 줄 맨 뒤 pending으로 되살린다(수동 재처리)."""
    with db._lock:
        conn = db._conn()
        if capture is not None:
            conn.execute("UPDATE watch_queue SET capture = ? WHERE yt_id = ?",
                         (1 if capture else 0, yt_id))
        if distill is not None:
            conn.execute("UPDATE watch_queue SET distill = ? WHERE yt_id = ?",
                         (1 if distill else 0, yt_id))
        # txt_path·kf_attempts도 비운다. 남겨두면 process_video가 '[resume] 기존 전사
        # 재사용' 경로로 빠져 전사를 건너뛴다 — 전사본 자체를 다시 만들려고 되살린
        # 경우(모델 교체·손실 복구)에 의도와 정반대로 동작한다(2026-08-27 실측).
        conn.execute(
            """UPDATE watch_queue
                  SET status = 'pending', reason = '', error_kind = NULL,
                      attempt_count = 0, next_retry_at = NULL, claimed_at = NULL,
                      txt_path = NULL, kf_attempts = 0,
                      position = (SELECT COALESCE(MAX(COALESCE(position, id)), 0) + 1
                                    FROM watch_queue),
                      updated_at = ?
                WHERE yt_id = ? AND status IN ('done', 'failed', 'skipped')""",
            (db._now(), yt_id))


def capture_enabled_for(v: dict) -> bool:
    """이 큐 항목의 캡처 포함 여부 — 영상 단위 지정이 있으면 그것, 없으면 채널 설정.

    미등록 채널(수동 추가 등)은 기본 포함. 증류의 override/channel 구조와 같다.
    """
    ov = v.get("capture")
    if ov is not None:
        return bool(ov)
    cid = v.get("channel_id")
    if cid:
        r = db._conn().execute("SELECT capture FROM channels WHERE channel_id = ?",
                            (cid,)).fetchone()
        if r is not None:
            return bool(r["capture"])
    return True


def queue_overview(recent: int = 10) -> dict:
    """큐 팝업용 스냅샷 — 대기·진행 전체(처리 순서대로) + 최근 종료 몇 건."""
    conn = db._conn()
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
    for v in waiting:
        _annotate_flags(conn, v)
    excluded = set(db.distill_excluded_names())
    for v in finished:
        _annotate_flags(conn, v)
        _annotate_result(conn, v, excluded)
    return {"waiting": waiting, "recent": finished}


def _annotate_flags(conn, v: dict) -> None:
    """화면이 바로 읽는 실제 캡처·증류 여부와 근거(2026-11-21 사용자 지시 '큐에서 캡처/증류 여부를 알아보기 어려워').

    capture_on·distill_on = 영상 지정 > 채널 설정 > 기본(포함). *_src = 'video'|'channel'|'default'.
    """
    ch = None
    if v.get("channel_id"):
        ch = conn.execute("SELECT capture, distill FROM channels WHERE channel_id = ?",
                          (v["channel_id"],)).fetchone()
    for key in ("capture", "distill"):
        if v.get(key) is not None:
            v[key + "_on"], v[key + "_src"] = bool(v[key]), "video"
        elif ch is not None and ch[key] is not None:
            v[key + "_on"], v[key + "_src"] = bool(ch[key]), "channel"
        else:
            v[key + "_on"], v[key + "_src"] = True, "default"


def _annotate_result(conn, v: dict, excluded: set) -> None:
    """최근 처리 행: 끝난 시각, 실제로 만든 캡처 장수, 이력의 최종 증류 여부(이력 필터와 같은 규칙)."""
    v["finished_at"] = v.get("updated_at") or ""
    v["frames"] = None
    if v.get("status") != "done":
        return
    it = conn.execute(
        "SELECT summary_path, distill, channel, uploader FROM items WHERE yt_id = ? ORDER BY rowid DESC LIMIT 1",
        (v.get("yt_id"),)).fetchone()
    if it is None:
        return
    v["distill_on"] = db._history_distill(it, excluded)
    v["distill_src"] = "item" if it["distill"] is not None else "channel"
    sp = it["summary_path"] or ""
    frames_dir = sp[:-3] + ".frames" if sp.endswith(".md") else ""
    try:
        v["frames"] = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")]) if frames_dir else 0
    except OSError:
        v["frames"] = 0


def queue_move(qid: int, direction: str) -> bool:
    """대기 항목을 한 칸 위/아래로 — 같은 큐(같은 status)의 이웃과 position 교환.

    본편(pending)과 캡처 재시도(kf_retry)는 별도 큐라 서로 순서를 넘나들지 않는다.
    """
    if direction in ("top", "bottom"):
        return _queue_move_end(qid, direction)
    if direction not in ("up", "down"):
        return False
    with db._lock:
        conn = db._conn()
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


def _queue_move_end(qid: int, direction: str) -> bool:
    """대기 항목을 같은 큐(같은 status)의 맨 위/맨 아래로(2026-11-21 사용자 지시). 이미 끝이면 False."""
    with db._lock:
        conn = db._conn()
        me = conn.execute(
            "SELECT id, status, COALESCE(position, id) AS pos FROM watch_queue "
            "WHERE id = ? AND status IN ('pending', 'kf_retry')", (qid,)).fetchone()
        if me is None:
            return False
        agg = "MIN" if direction == "top" else "MAX"
        edge = conn.execute(
            f"SELECT {agg}(COALESCE(position, id)) FROM watch_queue WHERE status = ? AND id != ?",
            (me["status"], me["id"])).fetchone()[0]
        if edge is None:
            return False
        if (direction == "top" and me["pos"] < edge) or (direction == "bottom" and me["pos"] > edge):
            return False
        conn.execute("UPDATE watch_queue SET position = ? WHERE id = ?",
                     (edge - 1 if direction == "top" else edge + 1, me["id"]))
        return True


def queue_bump_attempt(qid: int, delta: int) -> None:
    """attempt_count 보정 — 라이브 예정처럼 '시도로 치지 않을' 경우 claim의 +1을 되돌린다."""
    with db._lock:
        db._conn().execute(
            "UPDATE watch_queue SET attempt_count = MAX(0, attempt_count + ?) WHERE id = ?",
            (delta, qid))


def queue_row(qid: int) -> dict | None:
    r = db._conn().execute("SELECT * FROM watch_queue WHERE id = ?", (qid,)).fetchone()
    return dict(r) if r else None


def set_queue_capture(qid: int, enabled: bool) -> bool:
    """대기 중(pending·kf_retry·deferred) 항목의 캡처 포함을 영상 단위로 지정."""
    with db._lock:
        cur = db._conn().execute(
            """UPDATE watch_queue SET capture = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'kf_retry', 'deferred')""",
            (1 if enabled else 0, db._now(), qid))
        return cur.rowcount > 0


def set_queue_distill(qid: int, enabled: bool) -> bool:
    """대기 중 항목의 증류 지정 — 완료 시 items.distill 오버라이드로 이관된다."""
    with db._lock:
        cur = db._conn().execute(
            """UPDATE watch_queue SET distill = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'kf_retry', 'deferred')""",
            (1 if enabled else 0, db._now(), qid))
        return cur.rowcount > 0


def queue_cancel(qid: int) -> bool:
    """대기 중(pending·kf_retry·deferred) 항목만 취소 표시. 진행 중은 못 건드린다."""
    with db._lock:
        cur = db._conn().execute(
            """UPDATE watch_queue
                  SET status = 'skipped', reason = '사용자 취소', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'kf_retry', 'deferred')""",
            (db._now(), qid))
        return cur.rowcount > 0


def queue_unclaim(qid: int) -> None:
    """claim을 되돌린다 — 처리를 시작하지 못한 경우(요약 게이트 불가 등).

    claim_one이 올린 attempt_count도 함께 되돌려, 시도하지 않은 주기가
    재시도 예산을 갉아먹지 않게 한다.
    """
    with db._lock:
        db._conn().execute(
            """UPDATE watch_queue
                  SET status = 'pending', claimed_at = NULL, updated_at = ?,
                      attempt_count = MAX(0, attempt_count - 1)
                WHERE id = ? AND status = 'processing'""",
            (db._now(), qid))


def queue_set_status(qid: int, status: str, reason: str = "") -> None:
    with db._lock:
        db._conn().execute(
            """UPDATE watch_queue
                  SET status = ?, reason = ?, updated_at = ?,
                      claimed_at = CASE WHEN ? = 'processing' THEN claimed_at ELSE NULL END,
                      next_retry_at = CASE WHEN ? = 'deferred' THEN next_retry_at ELSE NULL END
                WHERE id = ?""",
            (status, reason, db._now(), status, status, qid),
        )


def queue_activate(qid: int, *, reset_attempts: bool = False) -> None:
    """Move a revalidated deferred item to pending without losing pipeline attempts."""
    with db._lock:
        db._conn().execute(
            """UPDATE watch_queue SET status = 'pending', reason = '', error_kind = NULL,
                      next_retry_at = NULL, claimed_at = NULL, updated_at = ?,
                      attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END
                WHERE id = ?""",
            (db._now(), 1 if reset_attempts else 0, qid),
        )


def queue_set_txt_path(qid: int, txt_path: str) -> None:
    with db._lock:
        db._conn().execute(
            "UPDATE watch_queue SET txt_path = ?, updated_at = ? WHERE id = ?",
            (txt_path, db._now(), qid),
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
    with db._lock:
        conn = db._conn()
        row = conn.execute("SELECT attempt_count FROM watch_queue WHERE id = ?", (qid,)).fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempt_count"] or 0) + (1 if increment_attempt else 0)
        if max_attempts is not None and attempts >= max_attempts:
            conn.execute(
                """UPDATE watch_queue SET status = 'failed', reason = ?, error_kind = ?,
                          last_fail_reason = ?,
                          attempt_count = ?, next_retry_at = NULL, claimed_at = NULL, updated_at = ?
                    WHERE id = ?""",
                (reason, error_kind, reason, attempts, db._now(), qid),
            )
            return "failed"
        conn.execute(
            """UPDATE watch_queue SET status = 'deferred', reason = ?, error_kind = ?,
                      last_fail_reason = ?,
                      attempt_count = ?, next_retry_at = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?""",
            (reason, error_kind, reason, attempts, db._after(retry_after_seconds), db._now(), qid),
        )
        return "deferred"


def queue_due_deferred(limit: int = 100) -> list[dict]:
    rows = db._conn().execute(
        """SELECT * FROM watch_queue
            WHERE status = 'deferred' AND COALESCE(next_retry_at, '') <= ?
            ORDER BY id LIMIT ?""",
        (db._now(), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def queue_recover_stale(stale_seconds: int = 10800) -> int:
    cutoff = (datetime.now() - timedelta(seconds=stale_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    now = db._now()
    with db._lock:
        cur = db._conn().execute(
            """UPDATE watch_queue
                  SET status = 'deferred', reason = '중단된 processing 복구',
                      error_kind = 'worker_interrupted', next_retry_at = ?, claimed_at = NULL,
                      updated_at = ?
                WHERE status = 'processing'
                  AND COALESCE(claimed_at, updated_at, '') < ?""",
            (now, now, cutoff),
        )
        return cur.rowcount


def queue_recover_interrupted() -> int:
    """독점 모니터 락을 새로 얻은 프로세스가 고아 processing을 즉시 복구한다.

    watch_queue를 claim하는 주체는 단일 인스턴스 channel_monitor뿐이다. 따라서
    모니터 락을 획득한 직후 남아 있는 processing 행은 이전 프로세스/시스템 종료로
    끊긴 작업이다. pending으로 되돌리되 txt_path는 보존해 완료된 전사를 재사용한다.
    """
    now = db._now()
    with db._lock:
        cur = db._conn().execute(
            """UPDATE watch_queue
                  SET status = 'pending', reason = '중단된 작업 즉시 복구',
                      error_kind = 'worker_interrupted', next_retry_at = NULL,
                      claimed_at = NULL, updated_at = ?
                WHERE status = 'processing'""",
            (now,),
        )
        return cur.rowcount


def queue_mark_kf_retry(qid: int, txt_path: str, reason: str = "") -> None:
    """요약은 완료됐으나 캡처만 실패 → 다음 주기 캡처 재시도 예약(전사경로 보관, 시도횟수 +1)."""
    with db._lock:
        db._conn().execute(
            "UPDATE watch_queue SET status = 'kf_retry', txt_path = ?, "
            "kf_attempts = kf_attempts + 1, reason = ?, updated_at = ?, claimed_at = NULL "
            "WHERE id = ?",
            (txt_path, reason, db._now(), qid),
        )


def queue_defer_kf_retry(qid: int, retry_after_seconds: int | float, reason: str = "") -> None:
    """캡처 재시도를 나중으로 미룬다(시도 횟수는 세지 않는다).

    라이브 종료 직후(post_live)는 VOD 처리가 끝날 때까지 영상 포맷이 없거나 아주
    느리다 — 단 한 번뿐인 재시도를 그때 태우면 그대로 '포기'로 끝난다(2026-09-03).
    """
    with db._lock:
        db._conn().execute(
            "UPDATE watch_queue SET status = 'kf_retry', next_retry_at = ?, reason = ?, "
            "updated_at = ?, claimed_at = NULL WHERE id = ?",
            (db._after(retry_after_seconds), reason, db._now(), qid),
        )


def queue_has_processing() -> bool:
    r = db._conn().execute(
        "SELECT 1 FROM watch_queue WHERE status = 'processing' LIMIT 1"
    ).fetchone()
    return r is not None


def last_queue_activity_epoch() -> float | None:
    """가장 최근 실제 처리(claim/완료/실패/재시도 예약) 시각.

    pending 적재는 처리가 아니므로 제외한다. 빈 폴 주기와 구분해야 수동 추가 시
    유휴 슬롯을 바로 쓸 수 있다.
    """
    row = db._conn().execute(
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
    rows = db._conn().execute(
        "SELECT status, COUNT(*) n FROM watch_queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
