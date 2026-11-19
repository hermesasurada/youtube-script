"""영상 모음 — 한 행사·시리즈 영상만 따로 모아 일자별·시간순으로 고르는 페이지(2026-11-19 사용자 지시
'포츈500 포럼건 영상들만 따로 모아서, 선택해서 볼 수 있는 페이지').

모음은 아래 `COLLECTIONS`에 한 줄씩 둔다. 영상은 제목에 `match` 문구가 들어간 것을 이력(items)과 처리 큐
(watch_queue, 아직 처리 전·실패)에서 모은다. 순서는 생중계 시작 시각(yt-dlp `release_timestamp`, 없으면
`timestamp`)을 행사 현지 시간대(`tz`)로 바꿔 일자별로 묶는다 — 이력에는 게시일(날짜)밖에 없어 같은 날
세션끼리 순서를 정할 수 없기 때문이다. 시각은 영상마다 한 번 받아 `res/collections/<slug>.json`에 둔다
(백그라운드, 한 번에 한 모음만). 시각을 아직 모르면 게시일로 그날 맨 뒤에 둔다.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import db
import yt_core

COLLECTIONS = {
    "fortune500-2026": {
        "title": "Fortune 500 Innovation Forum 2026",
        "subtitle": "디트로이트 · 2026년 11월 16~17일",
        "match": "Fortune 500 Innovation Forum 2026",
        "playlist": "https://www.youtube.com/playlist?list=PLSzQpLyHTG60",
        "tz": "America/Detroit",
        "public": True,       # 공개 사본(collection_publish.py)을 만들어 R2에 올린다(2026-11-19 사용자 지시)
        "start": "2026-11-16",
    },
    "tomorrowx-2026": {
        "title": "TomorrowX Summit 2026",
        "subtitle": "오스틴 Moody Center · 2026년 11월 17~18일",
        "match": "TomorrowX Summit",
        "playlist": "https://www.youtube.com/playlist?list=PLVfiEuj8EJp8",
        "tz": "America/Chicago",
        "start": "2026-11-17",
        "public": True,       # 공개 사본(2026-11-20 사용자 지시 "투모로x도 공개해줘")
        # 편집본이 행사 뒤 며칠에 걸쳐 올라와 업로드 시각으로는 일차가 틀린다 — 공식 일정표(tomorrowxsummit.com)의
        # 무대 시각(현지)을 적는다. 여기 없는 영상은 업로드 시각으로 두고 카드에 '업로드'라고 표시한다.
        "schedule": {
            "OmEfHDQ3B6c": "2026-11-17 08:30",   # 오프닝(Gavin Baker·Ron Biscardi·Antonio Gracias)
            "DXhcN9aaUFw": "2026-11-17 08:45",   # Brad Gerstner
            "_SFAGMYj1JE": "2026-11-17 09:10",   # David Solomon
            "MtFNcfB4JEU": "2026-11-17 09:40",   # Lisa Su
            "9RFkGYaa1nI": "2026-11-17 10:10",   # Matthew McConaughey × Guy Oseary
            "6r3D-CMJkuI": "2026-11-17 10:35",   # Stan Druckenmiller
            "mwqV1Gpgcyg": "2026-11-17 11:00",   # Michael Dell
            "V-Hqi1DNzbE": "2026-11-17 11:25",   # Dina Powell McCormick
            "Ed1V9Xz89Ic": "2026-11-17 12:20",   # Guillermo Rauch
            "UTueSKjzdto": "2026-11-17 13:10",   # Bret Johnsen
            "paMsNoG_hi8": "2026-11-18 13:40",   # Travis Kalanick
            "DY8clym2-jQ": "2026-11-18 17:30",   # Elon Musk(화상)
        },
    },
}

CACHE_DIR = os.path.join(yt_core.BASE_DIR, "res", "collections")
_fill_lock = threading.Lock()
_filling: set[str] = set()


def get(slug: str) -> dict | None:
    c = COLLECTIONS.get(slug)
    return dict(c, slug=slug) if c else None


def day_label(c: dict, day: str, index: int) -> str:
    """'N일차' — 행사 시작일(`start`)이 있으면 그날부터 센다(1일차 영상이 아직 없어도 2일차는 2일차)."""
    if not day:
        return "날짜 미상"
    start = c.get("start")
    if start:
        try:
            return f"{(date.fromisoformat(day) - date.fromisoformat(start)).days + 1}일차"
        except ValueError:
            pass
    return f"{index}일차"


def short_title(title: str, match: str) -> str:
    """'세션 제목 | 행사 이름[ 뒷말]' → '세션 제목'(행사 이름은 페이지 머리에 이미 있다)."""
    t = re.sub(r"\s*[|｜]\s*" + re.escape(match) + r"\b.*$", "", title or "").strip()
    return t or (title or "")


def _cache_path(slug: str) -> str:
    return os.path.join(CACHE_DIR, f"{slug}.json")


def _load_cache(slug: str) -> dict:
    try:
        with open(_cache_path(slug), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(slug: str, data: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _cache_path(slug) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, _cache_path(slug))


def _fetch_start_ts(yt_id: str) -> int | None:
    """생중계 시작 시각(없으면 게시 시각) — 실패하면 None(다음 기회에 다시)."""
    import yt_dlp
    opts = {"quiet": True, "skip_download": True, "no_warnings": True, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={yt_id}", download=False)
    ts = info.get("release_timestamp") or info.get("timestamp")
    return int(ts) if ts else None


def _fill_missing(slug: str, ids: list[str]) -> None:
    try:
        for yt_id in ids:
            try:
                ts = _fetch_start_ts(yt_id)
            except Exception:  # noqa: BLE001 — 비공개 전환·네트워크 오류는 게시일 순서로 남긴다
                ts = None
            if ts:
                with _fill_lock:
                    cache = _load_cache(slug)
                    cache[yt_id] = ts
                    _save_cache(slug, cache)
    finally:
        with _fill_lock:
            _filling.discard(slug)


def _schedule_fill(slug: str, ids: list[str]) -> bool:
    if not ids:
        return False
    with _fill_lock:
        if slug in _filling:
            return True
        _filling.add(slug)
    threading.Thread(target=_fill_missing, args=(slug, ids), daemon=True,
                     name=f"collection-ts-{slug}").start()
    return True


def _like(match: str) -> str:
    return "%" + match.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def items(slug: str, *, fill: bool = True) -> dict | None:
    """모음 화면 자료 — {collection, days:[{date,label,items:[…]}], counts, filling}. 없는 모음은 None."""
    c = get(slug)
    if not c:
        return None
    conn = db._conn()
    like = _like(c["match"])
    rows = conn.execute(
        "SELECT rowid AS item_id, yt_id, title, title_ko, duration, upload_date, is_read, bookmark,"
        " summary_path IS NOT NULL AS has_summary FROM items"
        " WHERE title LIKE ? ESCAPE '\\'", (like,)).fetchall()
    seen, out = set(), []
    for r in rows:
        if not r["yt_id"] or r["yt_id"] in seen:
            continue
        seen.add(r["yt_id"])
        out.append({"item_id": r["item_id"], "yt_id": r["yt_id"], "title": r["title"] or "",
                    "title_ko": (r["title_ko"] or "").strip(),
                    "duration": float(r["duration"] or 0), "upload_date": r["upload_date"] or "",
                    "is_read": bool(r["is_read"]), "bookmark": bool(r["bookmark"]),
                    "has_summary": bool(r["has_summary"]), "state": "done"})
    for q in conn.execute(
            "SELECT yt_id, title, status FROM watch_queue WHERE title LIKE ? ESCAPE '\\'"
            " AND status IN ('pending','processing','deferred','failed','kf_retry')", (like,)).fetchall():
        if q["yt_id"] in seen:
            continue
        seen.add(q["yt_id"])
        out.append({"item_id": None, "yt_id": q["yt_id"], "title": q["title"] or "", "title_ko": "", "duration": 0.0,
                    "upload_date": "", "is_read": False, "bookmark": False, "has_summary": False,
                    "state": q["status"]})

    cache = _load_cache(slug)
    tz = ZoneInfo(c.get("tz") or "UTC")
    schedule = c.get("schedule") or {}
    for it in out:
        it["short_title"] = short_title(it["title"], c["match"])
        ko = short_title(it["title_ko"], c["match"]) if it["title_ko"] else ""
        if ko and it["short_title"] != it["title"].strip():   # 원제에서 행사 이름을 뗐으면 번역 제목도 마지막 '| …'를 뗀다
            ko = re.sub(r"\s*[|｜][^|｜]*$", "", ko).strip() or ko
        it["short_ko"] = ko if ko != it["short_title"] else ""     # 번역 제목(원제와 같으면 비움)
        it["time_src"] = ""
        fixed = schedule.get(it["yt_id"])
        ts = cache.get(it["yt_id"])
        if fixed:                       # 공식 일정표의 무대 시각(현지)이 업로드 시각보다 우선
            local = datetime.strptime(fixed, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            it["day"], it["time"], it["ts"] = local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), int(local.timestamp())
        elif ts:
            local = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(tz)
            it["day"], it["time"], it["ts"] = local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), int(ts)
            if schedule:                # 일정표를 쓰는 모음에서 일정표에 없는 영상 — 무대 시각이 아니다
                it["time_src"] = "upload"
        else:
            d = it["upload_date"]
            it["day"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""
            it["time"], it["ts"] = "", None

    missing = [it["yt_id"] for it in out if it["ts"] is None and it["yt_id"] not in schedule]
    filling = _schedule_fill(slug, missing) if fill else False

    def key(it):
        return (it["day"] or "9999", it["ts"] is None, it["ts"] or 0, it["short_title"])
    out.sort(key=key)
    days: list[dict] = []
    for it in out:
        if not days or days[-1]["date"] != it["day"]:
            days.append({"date": it["day"], "items": []})
        days[-1]["items"].append(it)
    for i, d in enumerate(days, 1):
        d["label"] = day_label(c, d["date"], i)
    done = [it for it in out if it["state"] == "done"]
    return {
        "collection": {k: c[k] for k in ("slug", "title", "subtitle", "playlist", "tz") if k in c},
        "days": days,
        "counts": {"total": len(out), "done": len(done), "read": sum(1 for it in done if it["is_read"]),
                   "waiting": len(out) - len(done)},
        "filling": filling,
    }


def summaries() -> list[dict]:
    """모음 목록(모음 고르기 화면) — 이름·편수·표지 썸네일."""
    out = []
    for slug in COLLECTIONS:
        data = items(slug, fill=False) or {}
        first = next((it for d in data.get("days", []) for it in d["items"]), None)   # 맨 앞 영상 썸네일을 표지로
        out.append({"slug": slug, "title": COLLECTIONS[slug]["title"],
                    "subtitle": COLLECTIONS[slug].get("subtitle", ""), "counts": data.get("counts", {}),
                    "cover": first["yt_id"] if first else ""})
    return out
