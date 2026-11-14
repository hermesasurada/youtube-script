"""채널 추가 API(2026-11-14 사용자 지시) — @핸들·주소·채널 ID로 등록, 중복·없는 채널 거절. 실제 네트워크 없이."""
import types

import app
import db

UC = "UC" + "A" * 22


def _client(monkeypatch, resolved=UC, title="테스트 채널"):
    fake = types.SimpleNamespace(resolve_channel_id=lambda h: resolved)
    monkeypatch.setitem(__import__("sys").modules, "channel_monitor", fake)
    monkeypatch.setattr(app, "_channel_feed_title", lambda cid: title)
    return app.app.test_client()


def _cleanup(cid):
    db._conn().execute("DELETE FROM channels WHERE channel_id = ?", (cid,))


def test_add_by_handle_then_duplicate_is_refused(monkeypatch):
    _cleanup(UC)
    c = _client(monkeypatch)
    r = c.post("/channels", json={"handle": "https://www.youtube.com/@TestCh/videos"})
    assert r.status_code == 200, r.get_json()
    ch = r.get_json()["channel"]
    assert ch["channel_id"] == UC and ch["handle"] == "TestCh" and ch["title"] == "테스트 채널"
    assert ch["enabled"] == 1 and ch["baseline_done"] == 0            # 첫 확인은 기준선만(백필 방지)
    r2 = c.post("/channels", json={"handle": "@TestCh"})
    assert r2.status_code == 409 and r2.get_json()["channel"]["channel_id"] == UC
    _cleanup(UC)


def test_add_by_channel_id_skips_handle_lookup(monkeypatch):
    uc = "UC" + "B" * 22
    _cleanup(uc)
    c = _client(monkeypatch, resolved=None, title="ID로 넣은 채널")
    r = c.post("/channels", json={"handle": f"youtube.com/channel/{uc}"})
    assert r.status_code == 200 and r.get_json()["channel"]["channel_id"] == uc
    _cleanup(uc)


def test_unknown_or_bad_input(monkeypatch):
    c = _client(monkeypatch, resolved=None)
    assert c.post("/channels", json={"handle": "@없는채널"}).status_code == 404
    assert c.post("/channels", json={"handle": ""}).status_code == 400
    assert c.post("/channels", json={"handle": "hello world"}).status_code == 400
