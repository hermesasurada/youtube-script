"""전사·요약 이력(items) 쿼리 — 색인(upsert·reindex_all), 이력 목록·검색·읽음·북마크, 제목 한국어 표기, 블로그 발행 기록, 섹션 메모, 영상별 증류, 전사 번역 진행 상태.

2026-11-16 리팩토링 4단계에서 db.py에서 옮겨 왔다(동작 변경 없음). 호출은 예전처럼 `db.이름`으로 한다 —
db.py가 이 모듈의 이름을 다시 내보낸다. 이 모듈 안에서도 db의 다른 이름은 `db.`를 붙여 부른다
(시험의 monkeypatch(db, …)·importlib.reload(db)가 예전처럼 먹게).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from document_io import parse_frontmatter, read_markdown
import db

def get_summary_notes(md_path: str) -> dict:
    """내용이 있는 메모만 돌려준다. 빈 body 행은 '지웠다'는 기록(아래)이라 건너뛴다."""
    return {r["section_key"]: r["body"] for r in db._conn().execute(
        "SELECT section_key, body FROM summary_notes WHERE md_path = ? AND body <> ''",
        (md_path,))}


def summary_notes_mtime(md_path: str) -> float:
    """이 영상 메모의 최종 수정시각(epoch). 메모가 없으면 0.

    메모는 블로그 본문에도 실리므로 '발행 이후 변경'을 따질 때 요약 파일 mtime과 함께
    본다. updated_at은 로컬 naive ISO 문자열이라 그대로 epoch로 바꾼다."""
    row = db._conn().execute(
        "SELECT MAX(updated_at) AS m FROM summary_notes WHERE md_path = ?", (md_path,)).fetchone()
    raw = (row["m"] if row else None) or ""
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def save_summary_note(md_path: str, section_key: str, body: str) -> None:
    """메모 저장. 빈 내용은 '삭제'지만 행은 body=''로 남긴다.

    행을 지워 버리면 최종 수정시각이 사라져, 메모를 넣고 발행한 뒤 지운 경우를
    '발행 이후 변경 없음'으로 잘못 판정한다(summary_notes_mtime). 빈 행은
    get_summary_notes가 걸러 화면·블로그에는 나오지 않는다."""
    with db._lock:
        if not body.strip():
            db._conn().execute(
                """INSERT INTO summary_notes VALUES (?, ?, '', ?)
                   ON CONFLICT(md_path, section_key) DO UPDATE SET
                   body = '', updated_at = excluded.updated_at""",
                (md_path, section_key, datetime.now().isoformat()))
        else:
            db._conn().execute("""INSERT INTO summary_notes VALUES (?, ?, ?, ?)
                ON CONFLICT(md_path, section_key) DO UPDATE SET
                body = excluded.body, updated_at = excluded.updated_at""",
                (md_path, section_key, body.strip(), datetime.now().isoformat()))


# ── 제목 번역 ──────────────────────────────────────────────────────────

def get_title_ko(path: str) -> str | None:
    """요약/전사 경로로 번역 제목 조회. 없거나 불필요면 None."""
    r = db._conn().execute(
        "SELECT title_ko FROM items WHERE md_path = ? OR summary_path = ? LIMIT 1",
        (path, path)).fetchone()
    if r is None:
        return None
    v = (r["title_ko"] or "").strip()
    return v or None


def clear_blog_publish(item_id: int) -> bool:
    """블로그 글을 지운 뒤 발행 기록을 비운다(다시 '블로그 발행' 상태로, 2026-10-19)."""
    with db._lock:
        cur = db._conn().execute(
            """UPDATE items SET blog_url = '', blog_post_id = '', blog_published_at = NULL,
                                blog_render_ver = '' WHERE rowid = ?""", (int(item_id),))
        return cur.rowcount > 0


def set_blog_publish(item_id: int, url: str, post_id: str, render_ver: str = "") -> bool:
    """블로그스팟 발행 결과 기록 — 같은 영상을 두 번 올리지 않는 근거.

    render_ver는 이 본문을 만든 렌더 방식의 버전이다. 나중에 표시 형식을 바꾸면
    저장된 버전이 달라져 '수정 필요'로 잡힌다."""
    with db._lock:
        cur = db._conn().execute(
            """UPDATE items SET blog_url = ?, blog_post_id = ?, blog_published_at = ?,
                                blog_render_ver = ? WHERE rowid = ?""",
            (url, post_id, db._now(), render_ver, int(item_id)))
        return cur.rowcount > 0


def set_title_ko(yt_id: str, value: str) -> bool:
    """번역 제목 저장. 빈 문자열은 '번역 불필요'로 기록해 재시도를 막는다."""
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET title_ko = ? WHERE yt_id = ?", (value, yt_id))
        return cur.rowcount > 0


def titles_needing_translation(limit: int = 500) -> list[dict]:
    """번역이 아직 없는 항목(제목에 한글이 없는 것만) → [{yt_id, title}]."""
    rows = db._conn().execute(
        """SELECT rowid, yt_id, title, channel_url FROM items
            WHERE title_ko IS NULL AND yt_id IS NOT NULL AND yt_id <> ''
              AND title IS NOT NULL AND title <> ''
            ORDER BY indexed_at DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        if db._HANGUL_RE.search(r["title"] or ""):
            continue                       # 한국어 제목은 번역 대상이 아니다
        # rowid는 번역 모델 라운드로빈 배정에만 쓴다(요약과 같은 규칙).
        out.append({"rowid": r["rowid"], "yt_id": r["yt_id"], "title": r["title"],
                    "channel_url": r["channel_url"]})
    return out


def mark_korean_titles() -> int:
    """한글이 든 제목은 번역 불필요로 확정(''), 매번 후보에 다시 오르지 않게 한다."""
    rows = db._conn().execute(
        "SELECT yt_id, title FROM items WHERE title_ko IS NULL AND yt_id IS NOT NULL"
    ).fetchall()
    ids = [r["yt_id"] for r in rows if db._HANGUL_RE.search(r["title"] or "")]
    if not ids:
        return 0
    with db._lock:
        db._conn().executemany("UPDATE items SET title_ko = '' WHERE yt_id = ?",
                            [(i,) for i in ids])
    return len(ids)


# ── 전사 전문 번역 (요약 파이프라인과 별개) ────────────────────────────

def set_translation_state(yt_id: str, md_path: str, out_path: str, status: str,
                          done: int, total: int, error: str = "") -> None:
    if not yt_id:
        return
    with db._lock:
        db._conn().execute(
            """INSERT INTO transcript_translation
                 (yt_id, md_path, out_path, status, chunks_done, chunks_total, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(yt_id) DO UPDATE SET
                 md_path=excluded.md_path, out_path=excluded.out_path,
                 status=excluded.status, chunks_done=excluded.chunks_done,
                 chunks_total=excluded.chunks_total, error=excluded.error,
                 updated_at=excluded.updated_at""",
            (yt_id, md_path, out_path, status, done, total, error or "", db._now()))


def get_translation_state(yt_id: str) -> dict | None:
    r = db._conn().execute("SELECT * FROM transcript_translation WHERE yt_id = ?",
                        (yt_id,)).fetchone()
    return dict(r) if r else None


def get_translation_for_path(path: str) -> dict | None:
    """전사(md_path) 또는 요약(summary_path) 경로로 번역 상태를 찾는다."""
    r = db._conn().execute(
        """SELECT t.* FROM transcript_translation t
             JOIN items i ON i.yt_id = t.yt_id
            WHERE i.md_path = ? OR i.summary_path = ? LIMIT 1""",
        (path, path)).fetchone()
    return dict(r) if r else None


def yt_id_for_md(md_path: str) -> str | None:
    r = db._conn().execute("SELECT yt_id FROM items WHERE md_path = ? LIMIT 1",
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
    return [dict(r) for r in db._conn().execute(sql, args).fetchall()]


def translation_status_counts() -> list[dict]:
    rows = db._conn().execute(
        "SELECT status, COUNT(*) n FROM transcript_translation GROUP BY status").fetchall()
    return [dict(r) for r in rows]


def get_translation_inflight() -> dict | None:
    r = db._conn().execute(
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
        rel = os.path.relpath(md_path, db.RES_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    sp = os.path.join(db.SUMMARY_DIR, rel)
    return sp if os.path.isfile(sp) else None


def upsert(md_path: str) -> bool:
    """전사 md 1건을 인덱싱. summary md가 있으면 함께. 파일 없으면 False."""
    md_path = os.path.realpath(md_path)
    if not os.path.isfile(md_path):
        return False

    try:
        meta, transcript = db.parse_md(md_path)
    except Exception:
        return False

    rel = os.path.relpath(md_path, db.RES_DIR)
    parts = rel.split(os.sep)
    if len(parts) < 2:
        return False
    date_dir = parts[0]
    stem = os.path.splitext(parts[-1])[0]

    sp = db._summary_path_for(md_path)
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

    with db._lock:
        db._conn().execute(
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
                -- is_read·bookmark 는 의도적으로 제외 (재인덱싱 시 상태 보존)
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
    sp = db._summary_path_for(md_path)
    summary_text: str | None = None
    mtime_summary: float | None = None
    if sp:
        try:
            with open(sp, encoding="utf-8", errors="replace") as f:
                summary_text = f.read()
            mtime_summary = os.path.getmtime(sp)
        except Exception:
            sp = None
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET summary_path = ?, summary = ?, mtime_summary = ?, indexed_at = ? "
            "WHERE md_path = ?",
            (sp, summary_text, mtime_summary, time.time(), md_path),
        )
        return cur.rowcount > 0


def delete(md_path: str) -> None:
    md_path = os.path.realpath(md_path)
    with db._lock:
        db._conn().execute("DELETE FROM items WHERE md_path = ?", (md_path,))


def has_summary_for_yt_id(yt_id: str) -> bool:
    """그 영상의 '요약까지' 끝난 이력이 있는지.

    find_by_yt_id는 전사 이력만 봐서, 요약이 타임아웃으로 끊겨 전사 파일만 남은
    반쪽 상태도 '이미 처리됨'으로 보인다. 자동 모니터가 그 둘을 구분하는 데 쓴다."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return False
    r = db._conn().execute(
        "SELECT 1 FROM items WHERE yt_id = ? AND summary IS NOT NULL "
        "AND trim(summary) <> '' LIMIT 1", (yt_id,)).fetchone()
    return bool(r)


def find_by_yt_id(yt_id: str) -> dict | None:
    """같은 영상(yt_id)을 이미 처리한 이력이 있으면 최신 1건 반환(중복 추가 경고용)."""
    yt_id = (yt_id or "").strip()
    if not yt_id:
        return None
    r = db._conn().execute(
        f"SELECT {db._ITEM_COLUMNS} FROM items "
        "WHERE yt_id = ? ORDER BY date DESC, stem DESC LIMIT 1",
        (yt_id,),
    ).fetchone()
    return db._row_to_item(r) if r else None


def set_item_distill(path: str, value: bool | None) -> bool:
    """영상 단위 증류 오버라이드. None=미설정(채널 설정을 따름), True=포함, False=제외.

    path는 전사(md_path) 또는 요약(summary_path) 어느 쪽이든 받는다(뷰어마다 키가 달라서).
    """
    v = None if value is None else (1 if value else 0)
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET distill = ? WHERE md_path = ? OR summary_path = ?",
            (v, path, path))
        return cur.rowcount > 0


def get_item_distill(path: str) -> dict | None:
    """영상의 증류 설정 조회 → {override, channel, registered, effective}.

    effective = 영상 오버라이드가 있으면 그것, 없으면 채널 설정.
    registered = 자동수집 채널로 등록돼 있는지. 미등록이면 따를 채널 설정이 없어
    channel 값은 기본값(포함)이며, UI가 그 차이를 구분해 표기한다.
    """
    r = db._conn().execute(
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
    rows = db._conn().execute(
        "SELECT yt_id, distill FROM items WHERE distill IS NOT NULL AND yt_id IS NOT NULL"
    ).fetchall()
    return {r["yt_id"]: bool(r["distill"]) for r in rows}


# ── 조회 ───────────────────────────────────────────────────────────────

_ITEM_COLUMNS = """
    date, upload_date, stem, title, uploader, channel, duration, webpage_url,
    categories_json, tags_json, channel_url, has_txt, md_path, summary_path, is_read
"""

_HISTORY_COLUMNS = """
    rowid AS item_id, date, upload_date, stem, title, title_ko, uploader, channel,
    duration, webpage_url, channel_url, has_txt,
    CASE WHEN summary_path IS NOT NULL THEN 1 ELSE 0 END AS has_summary,
    is_read, bookmark, distill
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
        # 번역 제목(없으면 빈 문자열) — 이력 카드가 번역 제목을 크게, 원제를 작게 보인다(2026-11-20)
        "title_ko":      (r["title_ko"] or "").strip(),
        "uploader":      r["uploader"] or r["channel"] or "—",
        "duration":      float(r["duration"] or 0),
        "webpage_url":   r["webpage_url"] or "",
        "channel_url":   r["channel_url"] or "",
        "has_txt":       bool(r["has_txt"]),
        "has_summary":   bool(r["has_summary"]),
        "is_read":       bool(r["is_read"]),
        "bookmark":      bool(r["bookmark"]),
    }


_UC_RE = re.compile(r"/channel/(UC[\w-]{10,})")


def _channel_titles() -> dict[str, str]:
    """등록 채널의 현행 이름 {channel_id: title} — 목록·채널 필터 표시용(2026-11-07 사용자 지시)."""
    return {r["channel_id"]: r["title"].strip()
            for r in db._conn().execute("SELECT channel_id, title FROM channels").fetchall()
            if r["channel_id"] and (r["title"] or "").strip()}


def _apply_channel_title(item: dict, titles: dict[str, str]) -> dict:
    """카드·채널 필터의 채널명을 등록 채널의 현행 이름(channels.title)으로 보인다.

    items.uploader는 전사 시점 yt-dlp 이름이라 언어·개명에 따라 다르다(EngineerTV ↔ 엔지니어TV).
    등록되지 않은 채널은 원래 이름 그대로. DB의 uploader는 바꾸지 않는다(블로그 라벨·증류 매칭이 쓴다).
    """
    m = db._UC_RE.search(item.get("channel_url") or "")
    title = titles.get(m.group(1)) if m else None
    if title:
        item["uploader"] = title
    return item


def _history_distill(r: sqlite3.Row, excluded: set) -> bool:
    """증류 대상 여부 — 증류 파이프라인(source_repository._doc_from_path)과 같은 규칙:
    영상 오버라이드(items.distill)가 있으면 그것, 없으면 증류 제외 채널명에 걸리지 않으면 포함."""
    if r["distill"] is not None:
        return bool(r["distill"])
    names = {(r["channel"] or "").strip(), (r["uploader"] or "").strip()} - {""}
    return not (names & excluded)


def list_items(unread_only: bool = False) -> list[dict]:
    where = "WHERE is_read = 0" if unread_only else ""
    rows = db._conn().execute(
        f"SELECT {db._ITEM_COLUMNS} FROM items {where} ORDER BY date DESC, stem DESC"
    ).fetchall()
    return [db._row_to_item(r) for r in rows]


def list_history_items(unread_only: bool = False) -> list[dict]:
    """웹 카드 목록. 큰 transcript/summary 및 내부 절대경로를 읽거나 전송하지 않는다."""
    where = "WHERE is_read = 0" if unread_only else ""
    rows = db._conn().execute(
        f"SELECT {db._HISTORY_COLUMNS} FROM items {where} ORDER BY date DESC, stem DESC"
    ).fetchall()
    excluded = set(db.distill_excluded_names())
    titles = db._channel_titles()
    out = []
    for r in rows:
        item = db._row_to_history_item(r)
        item["distill"] = db._history_distill(r, excluded)
        out.append(db._apply_channel_title(item, titles))
    return out


def history_revision() -> str:
    """목록 표시값이 달라질 때 바뀌는 저비용 revision 토큰."""
    r = db._conn().execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(MAX(indexed_at), 0) AS indexed,
                  COALESCE(SUM(rowid), 0) AS ids,
                  COALESCE(SUM(CASE WHEN is_read != 0 THEN rowid ELSE 0 END), 0) AS reads,
                  COALESCE(SUM(CASE WHEN bookmark != 0 THEN rowid ELSE 0 END), 0) AS marks,
                  COALESCE(SUM(CASE WHEN summary_path IS NOT NULL THEN rowid ELSE 0 END), 0) AS summaries,
                  COALESCE(SUM(CASE WHEN distill IS NOT NULL THEN rowid * (distill + 1) ELSE 0 END), 0) AS ds,
                  COALESCE(SUM(LENGTH(COALESCE(title_ko, ''))), 0) AS tko,
                  (SELECT COALESCE(SUM(CASE WHEN COALESCE(distill, 1) = 0 THEN id ELSE 0 END), 0)
                          || '.' || COALESCE(SUM(id * LENGTH(COALESCE(title, ''))), 0)
                     FROM channels) AS chds
             FROM items"""
    ).fetchone()
    # 증류 대상 필터(2026-11-07)가 쓰는 값 — 영상 오버라이드·채널 증류 설정이 바뀌어도 목록을 다시 받는다
    return (f'{r["n"]}:{float(r["indexed"]):.6f}:{r["ids"]}:{r["reads"]}'
            f':{r["marks"]}:{r["summaries"]}:{r["ds"]}:{r["chds"]}:{r["tko"]}')


def get_history_item(item_id: int) -> dict | None:
    """정수 ID를 상세 파일 경로로 해석한다. 경로는 목록 응답에 노출하지 않는다."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    if item_id <= 0:
        return None
    r = db._conn().execute(
        """SELECT rowid AS item_id, md_path, summary_path, title, has_txt, is_read,
                  yt_id, webpage_url, title_ko, uploader, blog_url, blog_post_id,
                  blog_published_at, blog_render_ver
             FROM items WHERE rowid = ?""",
        (item_id,),
    ).fetchone()
    return dict(r) if r else None


def search(q: str, limit: int = 500, unread_only: bool = False) -> list[dict]:
    """FTS5 매칭. q는 사용자 입력 그대로(공백 토큰 분리)."""
    q = (q or "").strip()
    if not q:
        return db.list_items(unread_only=unread_only)
    # 안전: 따옴표 등 escape (간단 처리: 따옴표 제거 후 OR 매칭이 아니라 모두 포함)
    safe = q.replace('"', " ").replace("'", " ")
    # 단어별 prefix 매칭으로 묶기
    terms = [t for t in safe.split() if t]
    if not terms:
        return db.list_items(unread_only=unread_only)
    expr = " ".join(f'"{t}"*' for t in terms)
    unread_clause = "AND i.is_read = 0" if unread_only else ""
    rows = db._conn().execute(
        f"""
        SELECT i.rowid AS item_id, i.date, i.upload_date, i.stem, i.title, i.title_ko,
               i.uploader, i.channel, i.duration, i.webpage_url, i.channel_url,
               i.has_txt,
               CASE WHEN i.summary_path IS NOT NULL THEN 1 ELSE 0 END AS has_summary,
               i.is_read, i.bookmark
          FROM items i
          JOIN items_fts f ON f.rowid = i.rowid
        WHERE items_fts MATCH ?
        {unread_clause}
        ORDER BY i.date DESC, i.stem DESC
        LIMIT ?
        """,
        (expr, limit),
    ).fetchall()
    titles = db._channel_titles()
    return [db._apply_channel_title(db._row_to_history_item(r), titles) for r in rows]


def mark_read(md_path: str, is_read: bool = True) -> bool:
    """읽음/안읽음 토글. 해당 항목이 없으면 False."""
    md_path = os.path.realpath(md_path)
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET is_read = ? WHERE md_path = ?",
            (1 if is_read else 0, md_path),
        )
        return cur.rowcount > 0


def set_bookmark_by_id(item_id: int, on: bool) -> bool:
    """영상 북마크 토글 — 웹 목록의 정수 ID 기준."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET bookmark = ? WHERE rowid = ?",
            (1 if on else 0, item_id))
        return cur.rowcount > 0


def mark_read_by_id(item_id: int, is_read: bool = True) -> bool:
    """웹 목록의 정수 ID로 읽음 상태를 갱신한다."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    with db._lock:
        cur = db._conn().execute(
            "UPDATE items SET is_read = ? WHERE rowid = ?",
            (1 if is_read else 0, item_id),
        )
        return cur.rowcount > 0


def stats() -> dict:
    c = db._conn()
    total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    has_summary = c.execute("SELECT COUNT(*) FROM items WHERE summary_path IS NOT NULL").fetchone()[0]
    return {"total": total, "with_summary": has_summary}


# ── 일괄 reindex ───────────────────────────────────────────────────────

def reindex_all(force: bool = False) -> dict:
    """res/*/*.md 전수 스캔. mtime 변경분만 upsert. 사라진 파일은 제거."""
    seen: set[str] = set()
    n_upserted = 0
    n_skipped = 0

    if os.path.isdir(db.RES_DIR):
        for date in sorted(os.listdir(db.RES_DIR), reverse=True):
            if date == "summary":
                continue
            dpath = os.path.join(db.RES_DIR, date)
            if not os.path.isdir(dpath):
                continue
            for fname in os.listdir(dpath):
                if not fname.endswith(".md"):
                    continue
                p = os.path.realpath(os.path.join(dpath, fname))
                seen.add(p)
                fs_md = os.path.getmtime(p)
                sp = os.path.join(db.SUMMARY_DIR, date, fname)
                fs_sum = os.path.getmtime(sp) if os.path.isfile(sp) else None

                if not force:
                    cur = db._conn().execute(
                        "SELECT mtime_md, mtime_summary FROM items WHERE md_path = ?",
                        (p,),
                    ).fetchone()
                    if (cur and cur["mtime_md"] == fs_md
                            and (cur["mtime_summary"] or None) == fs_sum):
                        n_skipped += 1
                        continue
                if db.upsert(p):
                    n_upserted += 1

    # 사라진 파일 제거
    rows = db._conn().execute("SELECT md_path FROM items").fetchall()
    n_removed = 0
    for r in rows:
        if r["md_path"] not in seen:
            db.delete(r["md_path"])
            n_removed += 1

    return {"upserted": n_upserted, "skipped": n_skipped, "removed": n_removed}
