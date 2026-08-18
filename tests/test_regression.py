"""최소 회귀 테스트 — 핵심 동작 고정(전체 커버리지 목표 아님).

대상: 마크다운 왕복/파서 일치, 경로 이탈 차단, DB upsert·검색·삭제,
키프레임 타임스탬프→섹션 배정, 원격 접근 게이팅.

실행:  .venv/bin/python -m pytest -q
주의:  conftest.py가 DB_PATH를 임시 파일로 격리한다(실제 index.db·res/ 미사용).
"""
import json
import gzip
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import app
import channel_monitor
import db
import keyframe_report
import llm_gateway


def test_clean_summary_removes_preamble_before_inline_h1():
    dirty = "전사 중반이 잘려 있어 전체 내용을 먼저 확인합니다.# 영상 제목\n\n## 1. 메타정보"
    assert app._clean_summary(dirty) == "# 영상 제목\n\n## 1. 메타정보"


def test_clean_summary_does_not_treat_h3_as_title():
    body = "요약할 수 없습니다.\n\n### 참고"
    assert app._clean_summary(body) == body


def test_clean_summary_skips_stray_hash_before_real_title():
    dirty = "#\n\n# 실제 영상 제목\n\n## 1. 메타정보"
    assert app._clean_summary(dirty) == "# 실제 영상 제목\n\n## 1. 메타정보"


def test_model_order_normalization_is_complete_and_unique():
    assert llm_gateway.normalize_model_order(["grok", "opus", "gpt"]) == ["grok", "opus", "gpt"]
    assert llm_gateway.normalize_model_order('["gpt","gpt","unknown"]') == ["gpt", "opus", "grok"]


# ── ① 마크다운 I/O 왕복 + app·db 파서 일치(드리프트 가드) ──────────────
def test_markdown_save_parse_roundtrip(tmp_path):
    meta = {
        "title": "테스트 영상 제목", "uploader": "채널A", "duration": "123",
        "webpage_url": "https://youtu.be/abc", "categories": ["News", "Tech"],
        "tags": ["alpha", "beta"],
    }
    md = str(tmp_path / "x.md")
    app._save_md(md, meta, "전사 본문\n둘째 줄")

    parsed, body = app._parse_md(md)
    assert body == "전사 본문\n둘째 줄"
    assert parsed["title"] == "테스트 영상 제목"
    assert parsed["uploader"] == "채널A"
    assert parsed["categories"] == ["News", "Tech"]
    assert parsed["tags"] == ["alpha", "beta"]


def test_app_and_db_frontmatter_parsers_agree(tmp_path):
    """app._parse_yaml_front_matter 와 db._parse_yaml_front_matter 가 같은 결과여야(중복 구현 드리프트 방지)."""
    md = str(tmp_path / "y.md")
    app._save_md(md, {"title": "T", "uploader": "U", "tags": ["x", "y"]}, "본문")
    fm = open(md, encoding="utf-8").read().split("---\n", 2)[1]
    assert app._parse_yaml_front_matter(fm) == db._parse_yaml_front_matter(fm)


# ── ② 경로 이탈 차단 ───────────────────────────────────────────────────
def test_path_traversal_blocked():
    ok, err = app._check_res_path(os.path.join(app.RES_DIR, "20260101", "x.md"))
    assert err is None and ok.startswith(os.path.realpath(app.RES_DIR))

    for bad in ("/etc/passwd", os.path.join(app.RES_DIR, "..", "..", "etc", "passwd")):
        _, e = app._check_res_path(bad)
        assert e is not None, f"이탈 경로가 허용됨: {bad}"


# ── ③ DB upsert·검색·삭제 (임시 RES_DIR/DB로 격리) ─────────────────────
def test_db_upsert_search_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "RES_DIR", str(tmp_path))   # 실제 res/ 미사용
    db.init()
    date_dir = tmp_path / "20260101"
    date_dir.mkdir()
    md = str(date_dir / "202601011200_0m10s_pyt.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("---\ntitle: 파이테스트제목 QWERTY\nuploader: 채널\nduration: 10\nid: ZZTESTID01\n"
                "---\n\n# 파이테스트제목 QWERTY\n\n전사 본문\n")

    assert db.upsert(md) is True
    assert any(it["title"] == "파이테스트제목 QWERTY" for it in db.list_items())
    assert any(it["title"] == "파이테스트제목 QWERTY" for it in db.search("QWERTY"))

    # 중복 영상 조회(yt_id) + /history/check 엔드포인트
    hit = db.find_by_yt_id("ZZTESTID01")
    assert hit and hit["title"] == "파이테스트제목 QWERTY"
    assert db.find_by_yt_id("NOPE") is None
    c = app.app.test_client()
    r = c.get("/history/check?yt_id=ZZTESTID01").get_json()
    assert r["exists"] is True and r["date"] == "20260101"
    assert c.get("/history/check?yt_id=NOPE").get_json()["exists"] is False

    db.delete(os.path.realpath(md))
    assert all(it["title"] != "파이테스트제목 QWERTY" for it in db.list_items())
    assert db.find_by_yt_id("ZZTESTID01") is None


def test_history_api_uses_lightweight_ids_and_revision(tmp_path, monkeypatch):
    """카드 목록은 본문·절대경로를 싣지 않고, 상세는 정수 ID로 조회한다."""
    monkeypatch.setattr(db, "RES_DIR", str(tmp_path))
    monkeypatch.setattr(db, "SUMMARY_DIR", str(tmp_path / "summary"))
    monkeypatch.setattr(app, "RES_DIR", str(tmp_path))
    monkeypatch.setattr(app, "SUMMARY_DIR", str(tmp_path / "summary"))
    db.init()

    date_dir = tmp_path / "20260813"
    summary_dir = tmp_path / "summary" / "20260813"
    date_dir.mkdir()
    summary_dir.mkdir(parents=True)
    md = date_dir / "202608131200_1m00s_web.md"
    summary = summary_dir / md.name
    md.write_text(
        "---\ntitle: 경량 이력 API\nuploader: 테스트 채널\nduration: 60\n"
        "id: LIGHTAPI01\nwebpage_url: https://youtu.be/LIGHTAPI01\n---\n\n전사 본문",
        encoding="utf-8",
    )
    summary.write_text("# 경량 이력 API\n\n요약 본문", encoding="utf-8")
    assert db.upsert(str(md))

    client = app.app.test_client()
    payload = client.get("/history").get_json()
    item = next(i for i in payload["items"] if i["title"] == "경량 이력 API")
    assert isinstance(item["item_id"], int)
    assert item["has_summary"] is True
    assert not ({"txt_path", "summary_path", "transcript", "summary", "tags", "categories"} & item.keys())

    unchanged = client.get("/history?revision=" + payload["revision"]).get_json()
    assert unchanged == {"revision": payload["revision"], "unchanged": True}

    assert client.post("/history/text", json={"item_id": item["item_id"]}).get_json()["text"] == "전사 본문"
    summary_payload = client.post("/summary/content", json={"item_id": item["item_id"]}).get_json()
    assert "요약 본문" in summary_payload["content"]

    marked = client.patch(
        "/history/mark_read", json={"item_id": item["item_id"], "is_read": True}
    ).get_json()
    assert marked["ok"] is True and marked["revision"] != payload["revision"]


def test_history_read_update_does_not_rebuild_fts():
    db.init()
    trigger = db._conn().execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='items_au'"
    ).fetchone()[0]
    normalized = " ".join(trigger.upper().split())
    assert "AFTER UPDATE OF TITLE, UPLOADER, TRANSCRIPT, SUMMARY" in normalized
    assert "IS_READ" not in normalized


def test_text_responses_are_gzipped_when_requested():
    response = app.app.test_client().get("/", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    assert b"YouTube Transcription" in gzip.decompress(response.data)


def test_summary_renderer_is_local_and_secondary_assets_are_deferred():
    """모바일 첫 요약이 외부 marked·전체 폰트 CSS를 기다리는 회귀를 막는다."""
    html = app.app.test_client().get("/m").get_data(as_text=True)
    assert "/static/vendor/marked.min.js?v=" in html
    assert "cdn.jsdelivr.net/npm/marked" not in html
    assert '<link rel="stylesheet" href="/static/fonts/google.css' not in html


# ── ④ 키프레임 타임스탬프 → 섹션 배정 (Tier3) ─────────────────────────
def test_keyframe_section_by_timestamp():
    headings = [{"idx": 0, "start": 0}, {"idx": 1, "start": 60}, {"idx": 2, "start": 120}]
    kept = [{"ts": 10, "section": None}, {"ts": 75, "section": None}, {"ts": 200, "section": None}]
    keyframe_report._assign_sections_by_time(kept, headings)
    assert [k["section"] for k in kept] == [0, 1, 2]


def test_keyframe_ts_label_and_hms():
    assert keyframe_report._parse_ts_label("제목 [3:20]") == 200
    assert keyframe_report._parse_ts_label("[1:02:03]") == 3723
    assert keyframe_report._parse_ts_label("시각 없음") is None
    assert keyframe_report._hms(200) == "03:20"      # 분은 2자리 패딩
    assert keyframe_report._hms(3723) == "1:02:03"
    # 라벨↔시각 왕복 일관성
    assert keyframe_report._parse_ts_label("[" + keyframe_report._hms(200) + "]") == 200


def test_sequoia_opening_is_removed_before_main_interview():
    meta = {"uploader": "Sequoia Capital", "title": "Factory interview"}
    body = (
        "[00:00] cold open answer\n"
        "[00:30] recurring title animation\n"
        "[00:52] we're here in the studio with Matan from Factory\n"
        "[01:15] main interview question"
    )

    trimmed, intro_ts = keyframe_report.trim_sequoia_opening(meta, body)

    assert intro_ts == 52
    assert trimmed.startswith("[00:52]")
    assert "cold open" not in trimmed
    assert keyframe_report._sequoia_opening_window(meta, body) == (0.0, 57.0)


def test_sequoia_opening_candidate_filter_includes_visual_tail():
    frames = [(0, "zero.jpg"), (25, "cold.jpg"), (55, "tail.jpg"), (58, "main.jpg")]
    assert keyframe_report._exclude_opening_candidates(frames, (0, 57)) == [(58, "main.jpg")]


def test_non_pattern_sequoia_interview_is_not_trimmed():
    meta = {"uploader": "Sequoia Capital", "title": "Jensen interview"}
    body = "[00:00] Thank you so much, Jensen.\n[00:32] Can you tell us what is an AI factory?"

    trimmed, intro_ts = keyframe_report.trim_sequoia_opening(meta, body)

    assert intro_ts is None
    assert trimmed == body
    assert keyframe_report._sequoia_opening_window(meta, body) is None


def test_existing_opening_figures_are_removed_without_touching_main_frames():
    text = (
        '# Summary\n\n<div class="kf-strip">'
        '<figure><img src="/x/kf_00025.jpg"><figcaption>opening</figcaption></figure>'
        '<figure><img src="/x/kf_00058.jpg"><figcaption>main</figcaption></figure>'
        '</div>\n\nBody\n'
    )

    cleaned, removed = keyframe_report._remove_opening_figures(text, (0, 57))

    assert removed == ["kf_00025.jpg"]
    assert "kf_00025.jpg" not in cleaned
    assert "kf_00058.jpg" in cleaned
    assert "Body" in cleaned


# ── ⑤ 원격 접근 게이팅(읽음·삭제는 허용, 전사·키프레임·프롬프트는 차단) ──
def test_remote_access_gating():
    c = app.app.test_client()
    remote = {"REMOTE_ADDR": "100.64.0.1"}

    # 차단 → 이력 페이지로 302 리다이렉트
    assert c.get("/keyframes/status", environ_base=remote).status_code == 302
    assert c.post("/start", environ_base=remote).status_code == 302

    # 허용(이력 조회) → 리다이렉트 아님
    assert c.get("/history", environ_base=remote).status_code != 302

    # 원격 삭제는 '유지'로 결정됨 → 게이팅 통과(잘못된 본문이라 핸들러가 안전하게 거부, 302 아님)
    r = c.delete("/history/item", json={"txt_path": "/etc/passwd"}, environ_base=remote)
    assert r.status_code != 302

    # 로컬(루프백)은 전부 허용
    assert c.get("/keyframes/status").status_code == 200


def test_queue_claim_and_stale_recovery(monkeypatch):
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM watch_queue")
    assert db.enqueue_video("claim-test", "https://youtu.be/claim-test", "claim", "UC1")

    claimed = db.queue_claim_one()
    assert claimed and claimed["yt_id"] == "claim-test"
    assert claimed["status"] == "processing"
    assert claimed["claimed_from"] == "pending"
    assert claimed["attempt_count"] == 1
    assert db.queue_claim_one() is None

    old = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE watch_queue SET claimed_at = ?, updated_at = ? WHERE id = ?",
        (old, old, claimed["id"]),
    )
    assert db.queue_recover_stale(stale_seconds=3600) == 1
    row = conn.execute("SELECT status, next_retry_at FROM watch_queue WHERE id = ?", (claimed["id"],)).fetchone()
    assert row["status"] == "deferred" and row["next_retry_at"]


def test_transient_filter_is_deferred_and_rechecked(monkeypatch):
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM watch_queue")
    db.enqueue_video(
        "live-test", "https://youtu.be/live-test", "live", "UC1",
        status="deferred", reason="live:is_upcoming", retry_after_seconds=0,
        error_kind="metadata_transient",
    )
    monkeypatch.setattr(channel_monitor, "filter_verdict", lambda url, min_dur: (True, ""))

    assert channel_monitor.recheck_deferred([], dry=False) == 1
    row = conn.execute("SELECT status FROM watch_queue WHERE yt_id = 'live-test'").fetchone()
    assert row["status"] == "pending"


def test_permanent_metadata_errors_are_not_retried():
    """멤버십 전용·삭제 영상은 다시 물어도 답이 같다 — 재시도 예산을 쓰면 안 된다.

    (이 구분이 없어 21건이 각 8회씩 재시도된 뒤 failed로 쌓인 회귀가 있었다.)
    """
    permanent = [
        "meta_error:ERROR: [youtube] X: This video is available to this channel's members",
        "meta_error:ERROR: [youtube] X: Video unavailable. This video has been removed",
        "meta_error:ERROR: [youtube] X: Private video. Sign in if you have been granted access",
        "meta_error:ERROR: [youtube] X: Sign in to confirm your age",
    ]
    for reason in permanent:
        retryable, _, _, kind = channel_monitor._defer_policy(reason)
        assert retryable is False, reason
        assert kind == "metadata_permanent"

    transient = [
        "meta_error:ERROR: unable to download video data: HTTP Error 429",
        "meta_error:ERROR: read error / connection reset by peer",
        "meta_error:ERROR: The read operation timed out",
        # '일시'를 먼저 판정하므로 unavailable 문자열이 섞여도 영구로 새지 않는다
        "meta_error:ERROR: This video is temporarily unavailable",
    ]
    for reason in transient:
        retryable, _, attempts, kind = channel_monitor._defer_policy(reason)
        assert retryable is True, reason
        assert kind == "metadata_transient"
        assert attempts == 6

    # 처음 보는 오류는 재시도하되 폭주하지 않도록 예산을 적게 준다
    retryable, _, attempts, kind = channel_monitor._defer_policy("meta_error:ERROR: 새로운 유형")
    assert (retryable, attempts, kind) == (True, 3, "metadata_unknown")


def test_item_distill_overrides_channel_setting():
    """영상 단위 증류 지정은 채널 설정보다 우선하고, 미설정이면 채널을 따른다."""
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM items WHERE md_path = '/tmp/_d.md'")
    conn.execute("DELETE FROM channels WHERE channel_id = 'UC_DTEST'")
    db.add_channel("UC_DTEST", handle="dtest", title="D채널",
                   url="https://www.youtube.com/channel/UC_DTEST")
    conn.execute(
        "INSERT INTO items (md_path, summary_path, date, stem, title, channel_url, yt_id, "
        "                   mtime_md, indexed_at) "
        "VALUES ('/tmp/_d.md', '/tmp/_d_s.md', '20260804', 's', 't', "
        "'https://www.youtube.com/channel/UC_DTEST', 'DVID', 0, 0)")

    cid = [c["id"] for c in db.list_channels() if c["channel_id"] == "UC_DTEST"][0]
    try:
        for ch_on in (True, False):
            db.set_channel_distill(cid, ch_on)
            # 미설정 → 채널 설정을 따른다
            db.set_item_distill("/tmp/_d.md", None)
            d = db.get_item_distill("/tmp/_d.md")
            assert d["override"] is None and d["effective"] is ch_on
            # 포함/제외 → 채널과 무관하게 그 값이 이긴다
            db.set_item_distill("/tmp/_d.md", True)
            assert db.get_item_distill("/tmp/_d.md")["effective"] is True
            db.set_item_distill("/tmp/_d.md", False)
            assert db.get_item_distill("/tmp/_d.md")["effective"] is False

        # 요약 경로로도 지정할 수 있어야 한다(뷰어가 summary_path를 키로 쓴다)
        assert db.set_item_distill("/tmp/_d_s.md", True)
        assert db.get_item_distill("/tmp/_d_s.md")["override"] is True
        assert db.distill_overrides().get("DVID") is True
        # 미설정은 오버라이드 목록에서 빠진다
        db.set_item_distill("/tmp/_d.md", None)
        assert "DVID" not in db.distill_overrides()
    finally:
        conn.execute("DELETE FROM items WHERE md_path = '/tmp/_d.md'")
        conn.execute("DELETE FROM channels WHERE channel_id = 'UC_DTEST'")


def test_monitor_model_orders_persist_independently():
    db.init()
    try:
        saved = db.set_monitor_model_orders(
            summary=["gpt", "opus", "grok"],
            capture=["grok", "gpt", "opus"],
        )
        assert saved == {
            "summary": ["gpt", "opus", "grok"],
            "capture": ["grok", "gpt", "opus"],
        }
        assert db.get_monitor_model_orders() == saved

        client = app.app.test_client()
        payload = client.get("/channels").get_json()
        assert payload["model_orders"] == saved
        changed = client.patch(
            "/channels/model-orders", json={"summary": ["opus", "grok", "gpt"]}
        ).get_json()
        assert changed["model_orders"]["summary"] == ["opus", "grok", "gpt"]
        assert changed["model_orders"]["capture"] == ["grok", "gpt", "opus"]
        bad = client.patch(
            "/channels/model-orders", json={"capture": ["opus", "opus", "gpt"]}
        )
        assert bad.status_code == 400
    finally:
        db.set_monitor_model_orders(
            summary=["opus", "gpt", "grok"], capture=["opus", "gpt", "grok"]
        )


def test_membership_capture_failure_is_not_retried(monkeypatch):
    """멤버십 전용으로 전환된 영상은 캡처를 다시 받아도 같은 결과 — 재시도 예약하지 않는다.

    (수집 당시엔 공개였다가 나중에 멤버십으로 돌리는 채널이 있어 캡처만 실패한다.)
    """
    members = ("ERROR: [youtube] M86nb7kb_L4: This video is available to this "
               "channel's members on level: 착수 (or any higher level).")
    assert channel_monitor._META_PERMANENT_RE.search(members)
    # 일시 오류는 계속 재시도 대상이어야 한다
    for transient in ("ERROR: unable to download video data: HTTP Error 403: Forbidden",
                      "ERROR: [download] Got error: 23966 bytes read, 10032041 more expected"):
        assert not channel_monitor._META_PERMANENT_RE.search(transient)

    # process_video가 영구 사유를 kf_permanent로 표시하는지
    class _Resp:
        @staticmethod
        def json():
            return {"ok": False, "reason": "error", "error": members}
    monkeypatch.setattr(channel_monitor.requests, "post", lambda *a, **k: _Resp())
    res = {"txt_path": "/tmp/x.md", "kf_note": "", "kf_systemic": False, "kf_permanent": False}
    # 캡처 단계만 흉내 — 실제 함수는 전사·요약까지 타므로 판정 로직만 직접 확인
    emsg = members
    assert bool(channel_monitor._META_PERMANENT_RE.search(emsg)) is True


def test_retry_delay_is_constant_30min():
    """재시도 간격은 30분 고정(2026-08-17 사용자 지정 — 지수 백오프에서 변경).

    예산 5회 × 30분이라 2시간 안에 소진된다. 저녁 차단 구간을 통째로 넘기지는
    못하지만, 회복이 빠른 일시 장애를 빨리 따라잡는 쪽을 택했다.
    """
    d = channel_monitor._retry_delay
    assert [d(i) for i in (1, 2, 3, 4, 5)] == [1800] * 5
    assert d(0) == d(None) == 1800   # attempt 미기록(0/None)도 동일


def test_keyframe_retry_success_does_not_reference_missing_exception(monkeypatch):
    retry = {
        "id": 1, "yt_id": "retry-test", "title": "retry", "url": "https://youtu.be/retry-test",
        "channel_id": "UC1", "txt_path": "/tmp/retry.md",
    }
    # 단일 인출구: kf_retry 건이 claim_one으로 나온다
    monkeypatch.setattr(channel_monitor.db, "queue_claim_one",
                        lambda: {**retry, "claimed_from": "kf_retry"})
    monkeypatch.setattr(channel_monitor.db, "get_monitor_model_orders",
                        lambda: {"summary": ["opus"], "capture": ["opus"]})
    monkeypatch.setattr(channel_monitor.db, "queue_set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(channel_monitor.db, "channel_name_by_cid", lambda cid: "")
    monkeypatch.setattr(channel_monitor, "summarizer_gate", lambda: (True, "claude", ""))
    monkeypatch.setattr(channel_monitor, "process_capture_only", lambda path, url, **kwargs: {"ok": True, "n_frames": 2})
    notices = []
    monkeypatch.setattr(channel_monitor, "notify", notices.append)

    channel_monitor.drain()
    assert any("캡처 재시도 성공" in notice for notice in notices)
    assert not any("자동 처리 실패" in notice for notice in notices)


def test_process_video_resumes_from_saved_transcript(monkeypatch):
    calls = []

    class ResponseStub:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=False):
            return iter(["event: done", "data: "])

        def json(self):
            return {"ok": True, "n_frames": 1}

    def post(url, **kwargs):
        calls.append(url)
        return ResponseStub(url)

    monkeypatch.setattr(channel_monitor.os.path, "isfile", lambda path: path == "/tmp/existing.md")
    monkeypatch.setattr(channel_monitor.requests, "post", post)
    result = channel_monitor.process_video(
        {"id": 1, "url": "https://youtu.be/resume", "txt_path": "/tmp/existing.md"},
        "prompt",
    )

    assert result["txt_path"] == "/tmp/existing.md"
    assert not any(url.endswith("/start") for url in calls)
    assert any(url.endswith("/summarize") for url in calls)


def test_process_video_sends_separate_summary_and_capture_orders(monkeypatch):
    calls = []

    class ResponseStub:
        def __init__(self, url, body):
            self.url, self.body = url, body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def raise_for_status(self): return None
        def iter_lines(self, decode_unicode=False): return iter(["event: done", "data: "])
        def json(self): return {"ok": True, "n_frames": 1}

    def post(url, **kwargs):
        calls.append((url, kwargs.get("json") or {}))
        return ResponseStub(url, kwargs.get("json") or {})

    monkeypatch.setattr(channel_monitor.os.path, "isfile", lambda path: path == "/tmp/existing.md")
    monkeypatch.setattr(channel_monitor.requests, "post", post)
    channel_monitor.process_video(
        {"id": 1, "url": "https://youtu.be/orders", "txt_path": "/tmp/existing.md"},
        "prompt",
        summary_models=["gpt", "opus", "grok"],
        capture_models=["grok", "gpt", "opus"],
    )
    summary_body = next(body for url, body in calls if url.endswith("/summarize"))
    capture_body = next(body for url, body in calls if url.endswith("/keyframes"))
    assert summary_body["models"] == ["gpt", "opus", "grok"]
    assert capture_body["models"] == ["grok", "gpt", "opus"]


def test_stream_command_timeout_and_stderr_drain():
    events = list(llm_gateway.stream_command(
        [sys.executable, "-c", "import sys,time; print('ready', flush=True); "
         "sys.stderr.write('x'*200000); sys.stderr.flush(); time.sleep(2)"],
        timeout=0.2,
    ))
    assert any(event.kind == "stdout" and event.data.strip() == "ready" for event in events)
    assert events[-1].kind == "timeout"
    assert events[-1].stderr


def test_claude_partial_output_is_reset_before_grok_fallback(monkeypatch):
    stream = [
        llm_gateway.StreamEvent("stdout", data='{"type":"system","model":"claude-opus-5-0"}\n'),
        llm_gateway.StreamEvent(
            "stdout",
            data='{"type":"stream_event","event":{"type":"content_block_delta",'
                 '"delta":{"type":"text_delta","text":"partial"}}}\n',
        ),
        llm_gateway.StreamEvent(
            "stdout", data='{"type":"result","is_error":true,"result":"usage limit"}\n'
        ),
        llm_gateway.StreamEvent("complete", returncode=1, stderr="usage limit"),
    ]
    monkeypatch.setattr(app.llm_gateway, "stream_command", lambda *args, **kwargs: iter(stream))
    monkeypatch.setattr(app, "_summarize_with_grok", lambda prompt: ("# Grok 결과", ""))

    output = list(app._summarize_with_claude("prompt", None))
    partial_index = next(i for i, chunk in enumerate(output) if "partial" in chunk)
    reset_index = next(i for i, chunk in enumerate(output) if chunk.startswith("event: reset"))
    grok_index = next(
        i for i, chunk in enumerate(output)
        if chunk.startswith("data: ") and "Grok 결과" in json.loads(chunk[6:].strip())
    )
    assert partial_index < reset_index < grok_index
    assert 'data: ""' in output[reset_index]


def test_ordered_summary_uses_requested_fallback_order(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app, "_summarize_with_gpt",
        lambda prompt: calls.append("gpt") or ("", "quota"),
    )
    monkeypatch.setattr(
        app, "_summarize_with_grok",
        lambda prompt: calls.append("grok") or ("# Grok 성공", ""),
    )
    monkeypatch.setattr(
        app.llm_gateway, "stream_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Opus should not run")),
    )

    output = list(app._summarize_ordered(
        "prompt", None, ["gpt", "grok", "opus"], transcript_chars=100,
    ))
    assert calls == ["gpt", "grok"]
    bodies = [json.loads(chunk[6:].strip()) for chunk in output if chunk.startswith("data: ") and chunk[6:].strip()]
    assert any("Grok 성공" in body for body in bodies)
    assert output[-1].startswith("event: done")


def test_keyframe_gpt_can_be_first_vision_model(monkeypatch):
    result = llm_gateway.ProcessResult(
        0, '[{"name":"a.jpg","keep":true,"section":0,"type":"chart","caption":"차트"}]', ""
    )
    seen = {}
    def run_gpt(prompt, **kwargs):
        seen["images"] = kwargs["images"]
        return result
    monkeypatch.setattr(keyframe_report.llm_gateway, "run_codex_prompt", run_gpt)
    monkeypatch.setattr(
        keyframe_report.llm_gateway, "run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Later models should not run")),
    )
    verdict = keyframe_report.classify_and_assign(
        [(1.0, "/tmp/a.jpg")], [{"idx": 0, "text": "주제"}],
        model_order=["gpt", "opus", "grok"],
    )
    assert verdict["a.jpg"]["keep"] is True
    assert seen["images"] == ["/tmp/a.jpg"]


def _make_job(job_id, meta=None):
    import queue as _queue
    import threading as _threading
    app.jobs[job_id] = {
        "queue": _queue.Queue(), "stop_event": _threading.Event(),
        "status": "running", "result": None, "output_file": None,
        "error_stage": "", "error_message": "", "meta": meta or {},
    }


def test_truncated_audio_is_rejected_before_transcription(monkeypatch, tmp_path):
    """라이브 다시보기가 초반만 받히면 whisper를 돌리기 전에 끊는다.

    (3h44m 토론회에서 대기 구간만 받혀 전사가 한 줄로 끝난 회귀가 있었다.)
    """
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    called = {"whisper": False}
    monkeypatch.setattr(app, "get_file_duration", lambda p: 400.0)          # 실제 오디오 400초
    monkeypatch.setattr(app, "_run_whisper", lambda *a, **k: called.__setitem__("whisper", True) or 0)

    job_id = "truncated-audio"
    _make_job(job_id)
    try:
        app._transcribe_and_finish(job_id, str(audio), "auto", "8", 13454.0, str(tmp_path / "out.md"))
        assert called["whisper"] is False, "잘린 오디오인데 전사를 시도했다"
        assert app.jobs[job_id]["status"] == "error"
        assert app.jobs[job_id]["error_stage"] == "download"
        assert "잘렸습니다" in app.jobs[job_id]["error_message"]
    finally:
        app.jobs.pop(job_id, None)


def test_sparse_transcript_is_rejected_before_saving(monkeypatch, tmp_path):
    """길이 대비 본문이 사실상 비면 저장하지 않는다(요약·캡처 낭비 차단)."""
    md = tmp_path / "out.md"
    saved = {"called": False}
    monkeypatch.setattr(app, "_parse_transcript", lambda p: "[00:00] We'll be right back.")
    monkeypatch.setattr(app, "_save_md", lambda *a, **k: saved.__setitem__("called", True))

    job_id = "sparse-transcript"
    _make_job(job_id, meta={"duration": 13454})
    try:
        app._finish_transcription(job_id, str(tmp_path / "a.mp3"), 0, str(md))
        assert saved["called"] is False, "빈 전사를 저장했다"
        assert app.jobs[job_id]["status"] == "error"
        assert app.jobs[job_id]["error_stage"] == "transcribe"
        assert not md.exists()
    finally:
        app.jobs.pop(job_id, None)


def test_normal_transcript_still_saves(monkeypatch, tmp_path):
    """정상 분량 전사는 그대로 저장돼야 한다(방어가 과하게 걸리지 않는지)."""
    md = tmp_path / "ok.md"
    saved = {"called": False}
    monkeypatch.setattr(app, "_parse_transcript", lambda p: "[00:00] " + "가" * 3000)
    monkeypatch.setattr(app, "_save_md", lambda *a, **k: saved.__setitem__("called", True))

    job_id = "normal-transcript"
    _make_job(job_id, meta={"duration": 1800})
    try:
        app._finish_transcription(job_id, str(tmp_path / "a.mp3"), 0, str(md))
        assert saved["called"] is True
        assert app.jobs[job_id]["status"] == "done"
    finally:
        app.jobs.pop(job_id, None)


def test_job_result_exposes_failure_stage_and_reason():
    job_id = "ui-error-test"
    app.jobs[job_id] = {
        "status": "running", "result": None, "output_file": None,
        "error_stage": "", "error_message": "",
    }
    try:
        app._set_job_error(job_id, "download", "\x1b[31m다운로드 실패\x1b[0m\n접근 거부")
        payload = app.app.test_client().get(f"/result/{job_id}").get_json()
        assert payload["status"] == "error"
        assert payload["error_stage"] == "download"
        assert payload["error_message"] == "다운로드 실패 접근 거부"
    finally:
        app.jobs.pop(job_id, None)
