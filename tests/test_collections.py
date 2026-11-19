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
    # 원격(폰)에서도 모음 페이지가 열리고, 서버가 이력 화면을 기기에 맞게 돌려보낼 때 질의 문자열은 그대로 넘긴다
    remote = {"REMOTE_ADDR": "100.64.0.9"}
    phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile"}
    assert client.get("/collection/fortune500-2026", environ_base=remote).status_code == 200
    r = client.get("/?open=5&from=fortune500-2026", environ_base=remote, headers=phone)
    assert r.status_code == 302 and r.headers["Location"].endswith("/m?open=5&from=fortune500-2026")


def test_collection_page_opens_summaries_in_page():
    """카드를 누르면 이력 화면 팝업이 아니라 모음 페이지 안(왼쪽 목록 + 오른쪽 요약)에서 연다(2026-11-19 사용자 지시)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    read = lambda p: open(os.path.join(root, p), encoding="utf-8").read()
    js = read("static/js/collection.js")
    assert 'href="?v=${it.item_id}"' in js and "history.pushState({colView: 1}" in js   # 카드 → 보기(뒤로가기로 카드 화면)
    for call in ("YS.apiSummaryContent", "YS.renderMarkdown", "YS.applyTitleTranslation", "YS.stripSummaryPopupChrome",
                 "YS.setupStickySummarySections", "YS.decoratePeople", "YS.attachSummaryNotes", "YS.apiMarkRead"):
        assert call in js                                                         # 요약 창과 같은 렌더·읽음 처리
    assert "i.ytimg.com/vi/${yt}/hqdefault.jpg" in js                             # 생중계 영상의 서버 썸네일은 방송 전 그림
    css = read("static/css/collection.css")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css and "aspect-ratio: 16 / 9" in css
    page = app.app.test_client().get("/collection/fortune500-2026").get_data(as_text=True)
    assert 'id="col-side"' in page and 'id="sum-panel-body"' in page and "/static/js/collection.js" in page
    # 이력 화면으로 넘겨 팝업을 띄우던 옛 배선은 지웠다
    for p in ("static/js/common.js", "static/js/index-boot.js", "static/js/index-summary.js", "static/js/mobile.js"):
        assert "collectionDeepLink" not in read(p) and "_collectionReturn" not in read(p)
    assert 'href="/collections"' in read("templates/index.html") and 'href="/collections"' in read("templates/mobile.html")
    assert 'id="refresh-btn"' not in read("templates/mobile.html")                # 폰 상단 새로고침 버튼은 삭제(당겨서 새로고침)



# ── 공개 사본(collection_publish, 2026-11-19) ──
import collection_publish as cp


def test_public_summary_page_is_cleaned():
    page = ('<!doctype html><html><head><meta charset="utf-8"><style>/* 작업 메모 */ .a{}</style></head><body>'
            '<main class="ys-export"><div class="sum-panel"><img src="data:image/png;base64,xx"></div>'
            '<footer class="ys-export-foot">원본 영상: x<br>요약 파일 만든 때: 2026. 11. 19.</footer></main></body></html>')
    out = cp._finish_summary_page(page, "Fortune 500 Innovation Forum 2026")
    assert '<meta name="robots" content="noindex">' in out                      # 검색 색인 끔
    assert '<a href="../index.html">← Fortune 500 Innovation Forum 2026</a>' in out
    assert "<img" not in out and "작업 메모" not in out and "만든 때" not in out
    assert "AI로 요약·번역한 글" in out


def test_public_build_links_and_private_bits(tmp_path, monkeypatch):
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM watch_queue")
    m = "Fortune 500 Innovation Forum 2026"
    sp = tmp_path / "a.sum.md"
    sp.write_text('# T\n<div class="kf-strip"><img src="/kf/1.jpg"></div>\n본문', encoding="utf-8")
    _add_item(conn, 911, "aaa", f"First | {m}", "20261116", is_read=1)
    conn.execute("UPDATE items SET summary_path=?, title_ko=? WHERE rowid=911", (str(sp), f"첫 세션 | {m}"))
    db.enqueue_video("ddd", "https://youtu.be/ddd", f"Waiting | {m}", "manual")
    monkeypatch.setattr(cv, "CACHE_DIR", str(tmp_path))
    assert "kf-strip" not in cp.render_payload("fortune500-2026")[0]["md"]       # 캡처 줄은 렌더 전에 뺀다
    monkeypatch.setattr(cp, "render_pages", lambda slug: {"aaa": {
        "html": '<html><head><meta charset="utf-8"></head><body><main class="ys-export">요약</main></body></html>',
        "title": "첫 세션"}})
    files = cp.build(["fortune500-2026"])
    assert set(files) == {"c/index.html", "c/fortune500-2026/index.html", "c/fortune500-2026/v/aaa.html"}
    page = files["c/fortune500-2026/index.html"].decode()
    assert 'href="v/aaa.html"' in page and "첫 세션" in page and "ddd" not in page   # 처리 전 영상은 안 싣는다
    assert "읽음" not in page and "/thumb/" not in page and "i.ytimg.com/vi/aaa/" in page
    assert 'href="fortune500-2026/index.html"' in files["c/index.html"].decode()

    sent = []
    monkeypatch.setattr("hermes_blogger.r2.put", lambda key, data, *a, **k: sent.append(key) or key)
    manifest = {}
    assert len(cp.upload(files, manifest)) == 3
    assert cp.upload(files, manifest) == []                                      # 안 바뀐 파일은 다시 안 올린다


def test_public_render_route_only_for_public_collections(monkeypatch):
    client = app.app.test_client()
    monkeypatch.setattr(cp, "public_slugs", lambda: [])
    assert client.get("/collection/fortune500-2026/public-render").status_code == 404


def test_schedule_overrides_upload_time_and_days_count_from_start(tmp_path, monkeypatch):
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM watch_queue")
    m = "TomorrowX Summit"
    db.enqueue_video("MtFNcfB4JEU", "https://youtu.be/x", f"Lisa Su on Nvidia | {m}", "manual")
    db.enqueue_video("paMsNoG_hi8", "https://youtu.be/y", f"Travis Kalanick | {m}", "manual")
    db.enqueue_video("newone", "https://youtu.be/z", f"Later Upload | {m} Day 2", "manual")
    monkeypatch.setattr(cv, "CACHE_DIR", str(tmp_path))
    # 새 영상은 업로드 시각만 안다: 2026-11-19 10:00 CST
    (tmp_path / "tomorrowx-2026.json").write_text(json.dumps({"newone": 1795104000}), encoding="utf-8")
    monkeypatch.setattr(cv, "_schedule_fill", lambda slug, ids: bool(ids))
    days = cv.items("tomorrowx-2026")["days"]
    assert [(d["label"], d["date"]) for d in days] == [("1일차", "2026-11-17"), ("2일차", "2026-11-18"), ("3일차", "2026-11-19")]
    lisa = days[0]["items"][0]
    assert lisa["time"] == "09:40" and lisa["time_src"] == ""                     # 일정표 무대 시각(현지)
    assert days[2]["items"][0]["time_src"] == "upload"                           # 일정표에 없으면 업로드 시각
    assert days[2]["items"][0]["short_title"] == "Later Upload"                  # 행사 이름 뒤 꼬리까지 뗀다
    # 시작일 기준이라 1일차 영상이 없어도 2일차는 2일차
    only_day2 = {"start": "2026-11-17"}
    assert cv.day_label(only_day2, "2026-11-18", 1) == "2일차"
