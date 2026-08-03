"""최소 회귀 테스트 — 핵심 동작 고정(전체 커버리지 목표 아님).

대상: 마크다운 왕복/파서 일치, 경로 이탈 차단, DB upsert·검색·삭제,
키프레임 타임스탬프→섹션 배정, 원격 접근 게이팅.

실행:  .venv/bin/python -m pytest -q
주의:  conftest.py가 DB_PATH를 임시 파일로 격리한다(실제 index.db·res/ 미사용).
"""
import json
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

    claimed = db.queue_claim_next()
    assert claimed and claimed["yt_id"] == "claim-test"
    assert claimed["status"] == "processing"
    assert claimed["attempt_count"] == 1
    assert db.queue_claim_next() is None

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


def test_retry_delay_backs_off_exponentially():
    """403·LLM 한도처럼 한동안 지속되는 장애는 간격을 벌려야 복구율이 높다."""
    d = channel_monitor._retry_delay
    assert d(1) == 1800          # 30분
    assert d(2) == 3600          # 1시간
    assert d(3) == 7200          # 2시간
    assert d(1) < d(2) < d(3)
    assert d(99) == channel_monitor.RETRY_MAX_SEC   # 상한에서 멈춘다
    assert d(0) == d(1)          # attempt 미기록(0/None)도 최소 간격 보장
    assert d(None) == d(1)


def test_keyframe_retry_success_does_not_reference_missing_exception(monkeypatch):
    retry = {
        "id": 1, "yt_id": "retry-test", "title": "retry", "url": "https://youtu.be/retry-test",
        "channel_id": "UC1", "txt_path": "/tmp/retry.md",
    }
    monkeypatch.setattr(channel_monitor.db, "queue_kf_retry_list", lambda: [retry])
    monkeypatch.setattr(channel_monitor.db, "queue_next_pending", lambda: None)
    monkeypatch.setattr(channel_monitor.db, "queue_claim_next", lambda: None)
    monkeypatch.setattr(channel_monitor.db, "queue_set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(channel_monitor.db, "channel_name_by_cid", lambda cid: "")
    monkeypatch.setattr(channel_monitor, "summarizer_gate", lambda: (True, "claude", ""))
    monkeypatch.setattr(channel_monitor, "process_capture_only", lambda path, url: {"ok": True, "n_frames": 2})
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
