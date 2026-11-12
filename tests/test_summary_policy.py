"""6분 이하 영상은 요약하지 않는다(2026-11-12 사용자 지시) — summary_policy·/summarize·채널 모니터."""
import importlib

import summary_policy


def test_short_videos_are_skipped_and_long_or_clipped_are_not():
    assert summary_policy.summary_skip_reason({"duration": 360})          # 정확히 6분도 금지('6분 이하')
    assert summary_policy.summary_skip_reason({"duration": "125"})
    assert summary_policy.summary_skip_reason({"duration": 361}) is None
    assert summary_policy.summary_skip_reason({"duration": 0}) is None    # 길이 모름 → 막지 않음
    assert summary_policy.summary_skip_reason({}) is None
    assert summary_policy.summary_skip_reason({"duration": 200, "clip_start": 600}) is None   # 구간 전사
    assert summary_policy.SKIP_MARK in summary_policy.summary_skip_reason({"duration": 59})


def test_env_zero_turns_the_rule_off(monkeypatch):
    monkeypatch.setenv("YT_SUMMARY_MIN_SECONDS", "0")
    mod = importlib.reload(summary_policy)
    try:
        assert mod.summary_skip_reason({"duration": 100}) is None
    finally:
        monkeypatch.delenv("YT_SUMMARY_MIN_SECONDS")
        importlib.reload(summary_policy)


def test_summarize_endpoint_refuses_short_video(tmp_path, monkeypatch):
    import app
    md = tmp_path / "short.md"
    md.write_text("---\ntitle: 짧은 영상\nduration: 240\n---\n\n본문 " * 3, encoding="utf-8")
    monkeypatch.setattr(app, "_check_res_path", lambda p: (str(md), None))
    called = []
    monkeypatch.setattr(app, "_summarize_with_claude", lambda *a, **k: called.append(1) or iter(()))
    r = app.app.test_client().post("/summarize", json={"txt_path": "x"})
    assert r.status_code == 422
    assert r.get_json()["skipped"] == "short_video"
    assert not called


def test_monitor_skips_summary_and_capture_for_short_video(tmp_path, monkeypatch):
    import channel_monitor
    md = tmp_path / "short.md"
    md.write_text("---\ntitle: 짧은 영상\nduration: 300\n---\n\n본문", encoding="utf-8")
    monkeypatch.setattr(channel_monitor.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("요약·캡처를 부르면 안 된다")))
    res = channel_monitor.process_video({"url": "u", "txt_path": str(md)}, "prompt")
    assert res["summary_skipped"] and res["kf_skipped"]
