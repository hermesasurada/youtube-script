"""6분 이하 영상은 한눈 요약만 만들지 않는다(2026-11-12 지시, 11-16 정정) — 본 요약·캡처는 평소대로."""
import importlib

import summary_policy
import yt_core
import yt_summary

EMPTY = "# 제목\n\n## 2. 한눈 요약\n\n## 3. 핵심 내용\n\n### 하나\n\n본문.\n"


def test_short_videos_skip_brief_only():
    assert summary_policy.skip_brief({"duration": 360})              # 정확히 6분도 생략('6분 이하')
    assert summary_policy.skip_brief({"duration": "125"})
    assert not summary_policy.skip_brief({"duration": 361})
    assert not summary_policy.skip_brief({"duration": 0})            # 길이 모름 → 한눈 요약 만든다
    assert not summary_policy.skip_brief({})
    assert not summary_policy.skip_brief({"duration": 200, "clip_start": 600})   # 구간 전사


def test_env_zero_turns_the_rule_off(monkeypatch):
    monkeypatch.setenv("YT_BRIEF_MIN_SECONDS", "0")
    mod = importlib.reload(summary_policy)
    try:
        assert not mod.skip_brief({"duration": 100})
    finally:
        monkeypatch.delenv("YT_BRIEF_MIN_SECONDS")
        importlib.reload(summary_policy)


def test_rebuild_brief_disabled_drops_heading_without_llm(monkeypatch):
    import yt_titles
    monkeypatch.setitem(yt_titles._TITLE_TR_PROVIDERS, "opus",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("한눈 요약 호출 금지")))
    out = yt_summary._rebuild_brief(EMPTY, "opus", "m", "t", False)
    assert "한눈 요약" not in out and "## 3. 핵심 내용" in out


def test_summarize_endpoint_summarizes_short_video_without_brief(tmp_path, monkeypatch):
    import app
    md = tmp_path / "short.md"
    md.write_text("---\ntitle: 짧은 영상\nduration: 240\n---\n\n본문 " * 3, encoding="utf-8")
    monkeypatch.setattr(yt_core, "_check_res_path", lambda p: (str(md), None))
    seen = {}
    monkeypatch.setattr(yt_summary, "_summarize_with_claude",
                        lambda *a, **k: seen.update(k) or iter(["event: done\ndata: \n\n"]))
    r = app.app.test_client().post("/summarize", json={"txt_path": "x"})
    r.get_data(); r.close()                 # 스트림을 끝까지 읽어야 요약 함수가 불린다(세마포어도 풀린다)
    assert r.status_code == 200 and seen["brief"] is False
    md.write_text("---\ntitle: 긴 영상\nduration: 900\n---\n\n본문 " * 3, encoding="utf-8")
    seen.clear()
    r = app.app.test_client().post("/summarize", json={"txt_path": "x"})
    r.get_data(); r.close()
    assert seen["brief"] is True


def test_monitor_summarizes_short_video(tmp_path, monkeypatch):
    import channel_monitor
    md = tmp_path / "short.md"
    md.write_text("---\ntitle: 짧은 영상\nduration: 300\n---\n\n본문", encoding="utf-8")
    calls = []

    def post(url, *a, **k):
        calls.append(url)
        raise RuntimeError("stop after first call")
    monkeypatch.setattr(channel_monitor.requests, "post", post)
    try:
        channel_monitor.process_video({"url": "u", "txt_path": str(md)}, "prompt")
    except Exception:
        pass
    assert calls and calls[0].endswith("/summarize")        # 짧은 영상도 요약을 부른다
