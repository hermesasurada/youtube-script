"""영상 모음(2026-11-19) — 제목으로 모으기·일자별 시간순·처리 전 영상·라우트·요약 바로 열기 배선."""
import json
import os

import app
import collections_view as cv
import db


def _add_item(conn, rowid, yt_id, title, upload_date, is_read=0):
    conn.execute(
        "INSERT INTO items (rowid, md_path, date, stem, title, yt_id, upload_date, duration, is_read,"
        " summary_path, mtime_md, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
        (rowid, f"/tmp/coll/{yt_id}.md", upload_date, yt_id, title, yt_id, upload_date, 600, is_read,
         f"/tmp/coll/{yt_id}.sum.md"))


def test_collection_groups_by_local_day_and_start_time(tmp_path, monkeypatch):
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM watch_queue")
    m = "Fortune 500 Innovation Forum 2026"
    _add_item(conn, 901, "aaa", f"Late Day One | {m}", "20261117")      # 게시일은 다음 날이지만 방송은 1일차
    _add_item(conn, 902, "bbb", f"Early Day One | {m}", "20261116", is_read=1)
    _add_item(conn, 903, "ccc", f"Day Two | {m}", "20261117")
    _add_item(conn, 904, "zzz", "다른 영상", "20261117")
    db.enqueue_video("ddd", "https://youtu.be/ddd", f"Waiting | {m}", "manual")
    monkeypatch.setattr(cv, "CACHE_DIR", str(tmp_path))
    # 디트로이트(UTC-5) 기준: aaa 11-16 17:00, bbb 11-16 09:00, ccc 11-17 10:00
    (tmp_path / "fortune500-2026.json").write_text(json.dumps(
        {"aaa": 1794866400, "bbb": 1794837600, "ccc": 1794927600}), encoding="utf-8")
    scheduled = []
    monkeypatch.setattr(cv, "_schedule_fill", lambda slug, ids: scheduled.extend(ids) or bool(ids))

    data = cv.items("fortune500-2026")
    days = data["days"]
    assert [d["date"] for d in days][:2] == ["2026-11-16", "2026-11-17"]
    assert [it["short_title"] for it in days[0]["items"]] == ["Early Day One", "Late Day One"]
    assert days[0]["items"][0]["time"] == "09:00" and days[0]["label"] == "1일차"
    assert all(it["yt_id"] != "zzz" for d in days for it in d["items"])          # 제목이 안 맞으면 빠진다
    waiting = [it for d in days for it in d["items"] if it["yt_id"] == "ddd"][0]
    assert waiting["state"] == "pending" and waiting["item_id"] is None          # 처리 전은 큐에서
    assert scheduled == ["ddd"]                                                  # 시각 모르는 것만 받으러 간다
    assert data["counts"] == {"total": 4, "done": 3, "read": 1, "waiting": 1}


def test_short_title_strips_event_name():
    m = "Fortune 500 Innovation Forum 2026"
    assert cv.short_title(f"GM’s Next Chapter | {m}", m) == "GM’s Next Chapter"
    assert cv.short_title("제목만", m) == "제목만"


def test_collection_routes_and_remote_access():
    client = app.app.test_client()
    r = client.get("/collections")                                               # 모음이 하나여도 고르는 화면을 거친다
    assert r.status_code == 200 and 'href="/collection/fortune500-2026"' in r.get_data(as_text=True)
    assert client.get("/collection/없는모음").status_code == 404
    assert client.get("/collection/없는모음/items").status_code == 404
    # 원격(폰)에서도 모음 페이지가 열리고, 이력 화면으로 돌려보낼 때 ?open=…은 그대로 넘긴다
    remote = {"REMOTE_ADDR": "100.64.0.9"}
    phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile"}
    assert client.get("/collection/fortune500-2026", environ_base=remote).status_code == 200
    r = client.get("/?open=5&from=fortune500-2026", environ_base=remote, headers=phone)
    assert r.status_code == 302 and r.headers["Location"].endswith("/m?open=5&from=fortune500-2026")


def test_deep_link_wiring_on_both_screens():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    read = lambda p: open(os.path.join(root, p), encoding="utf-8").read()
    common = read("static/js/common.js")
    assert "function collectionDeepLink()" in common and "collectionDeepLink," in common
    assert "YS.collectionDeepLink()" in read("static/js/index-boot.js")
    assert "if (_collectionReturn) { _collectionReturn = false; history.back(); }" in read("static/js/index-summary.js")
    mobile = read("static/js/mobile.js")
    assert "YS.collectionDeepLink()" in mobile and "!_modalPushed && !_collectionReturn" in mobile
    assert 'href="/collections"' in read("templates/index.html") and 'href="/collections"' in read("templates/mobile.html")
    assert 'id="refresh-btn"' not in read("templates/mobile.html")                # 폰 상단 새로고침 버튼은 삭제(당겨서 새로고침)
