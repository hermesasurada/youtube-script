import json
from datetime import datetime, timezone

import app
import channel_monitor
import db
import yt_listing

CID = "UC" + "a" * 22


def _lockup(vid, title, ago="3시간 전", thumb="https://i.ytimg.com/vi/x/hq720.jpg?sqp=1"):
    return {"lockupViewModel": {
        "contentId": vid,
        "contentImage": {"thumbnailViewModel": {"image": {"sources": [{"url": "s"}, {"url": thumb}]}}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [
                {"text": {"content": "조회수 1만"}}, {"text": {"content": ago}}]}]}}}}}}


def _page(*items):
    return 'x var ytInitialData = ' + json.dumps({"a": {"contents": list(items)}}, ensure_ascii=False) + ';</script>'


def test_parse_videos_title_date_and_korean_thumb_only():
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    ko = "https://i.ytimg.com/vi_lc/b/hq720_ko.jpg?sqp=2"
    got = yt_listing.parse_videos(_page(_lockup("a", "가"), _lockup("b", "나", "2일 전", ko)), now)
    assert [v["yt_id"] for v in got] == ["a", "b"]
    assert got[0]["published"].startswith("2026-11-07T09") and got[0]["thumb"] == ""
    assert got[1]["published"].startswith("2026-11-05") and got[1]["thumb"] == ko
    assert yt_listing.parse_videos("no data") == []


def test_korean_title_needs_hangul_and_difference():
    assert yt_listing.korean_title({"title": "한글 제목"}, "Title") == "한글 제목"
    assert yt_listing.korean_title({"title": "Title"}, "Other") == ""
    assert yt_listing.korean_title({"title": "한글"}, "한글") == ""
    assert yt_listing.korean_title(None, "x") == ""


def test_until_known_never_backfills():
    vids = [{"yt_id": x} for x in "abc"]
    assert yt_listing.until_known(vids, lambda y: y == "c") == vids[:2]
    assert yt_listing.until_known(vids, lambda y: False) == []       # 기준을 못 찾으면 넣지 않는다
    assert yt_listing.until_known(vids, lambda y: y == "a") == []


def _setup_poll(monkeypatch, tmp_path, listing):
    monkeypatch.setenv("INDEX_DB", str(tmp_path / "p.db"))
    import importlib
    importlib.reload(db)
    db.init()
    cid = db.add_channel(CID, "h", "채널")
    db.set_channel_baseline(cid)
    db.enqueue_video("KNOWN", "u", "옛 영상", CID, status="skipped", reason="baseline")

    def boom(_):
        raise RuntimeError("404")
    monkeypatch.setattr(channel_monitor, "db", db)
    monkeypatch.setattr(channel_monitor, "rss_recent", boom)
    monkeypatch.setattr(channel_monitor.yt_listing, "channel_videos", listing)
    monkeypatch.setattr(channel_monitor, "filter_verdict", lambda url, m: (True, ""))
    monkeypatch.setattr(channel_monitor, "recheck_deferred", lambda chs, dry=False: 0)
    monkeypatch.setattr(db, "list_channels", lambda: [db.get_channel(cid)])


def _v(y):
    return {"yt_id": y, "title": y, "url": "u" + y, "published": "", "thumb": ""}


def test_poll_falls_back_to_channel_page_without_backfill(monkeypatch, tmp_path):
    _setup_poll(monkeypatch, tmp_path, lambda c: [_v("NEW"), _v("KNOWN"), _v("OLDER")])
    assert channel_monitor.poll_channels() == 1
    ids = {r["yt_id"]: r["status"] for r in db._conn().execute("SELECT yt_id, status FROM watch_queue")}
    assert ids.get("NEW") == "pending" and "OLDER" not in ids


def test_poll_survives_when_both_sources_fail(monkeypatch, tmp_path):
    def boom(_):
        raise RuntimeError("500")
    _setup_poll(monkeypatch, tmp_path, boom)
    assert channel_monitor.poll_channels() == 0


def test_pending_titles_use_youtube_korean_title_only(monkeypatch):
    url = "https://youtube.com/channel/" + CID
    other = "https://youtube.com/channel/UC" + "b" * 22
    todo = [{"rowid": 1, "yt_id": "A", "title": "Hello", "channel_url": url},
            {"rowid": 2, "yt_id": "B", "title": "World", "channel_url": url},
            {"rowid": 3, "yt_id": "C", "title": "Later", "channel_url": other},
            {"rowid": 4, "yt_id": "D", "title": "Manual", "channel_url": ""}]
    saved = {}
    monkeypatch.setattr(app.db, "mark_korean_titles", lambda: None)
    monkeypatch.setattr(app.db, "titles_needing_translation", lambda n: todo)
    monkeypatch.setattr(app.db, "set_title_ko", lambda y, t: saved.__setitem__(y, t) or 1)
    monkeypatch.setattr(app.yt_listing, "korean_entries",
                        lambda c: {"A": {"title": "안녕"}, "B": {"title": "World"}} if c == CID else {})

    def tr(*a, **k):
        raise AssertionError("LLM 번역을 부르면 안 된다")
    monkeypatch.setattr(app, "_translate_titles", tr)
    assert app.translate_pending_titles()["done"] == 1
    assert saved == {"A": "안녕", "B": "", "D": ""}          # C는 목록 실패 → 다음에 다시
