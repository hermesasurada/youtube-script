"""말 없는 영상 거르기(2026-10-14, #1500 지식채널e): 음악·효과음 표시와 whisper 환각 정형 문장은 글자수에서 뺀다."""
import app


def test_music_markers_and_hallucination_lines_removed():
    t = ("[00:00] ♪♪♪\n[01:40] *Splash*\n[02:10] We'll be right back.\n[02:40] I'll see you next time.\n"
         "[03:10] I'm going to use the same method.\n[03:40] Thank you.\n[04:40] ♪ ♪\n[05:10] 시청해주셔서 감사합니다.")
    assert app._speech_body(t) == "I'm going to use the same method."


def test_real_speech_kept():
    t = "[00:00] Thank you for having me. Today we talk about HBM pricing.\n[00:30] 감사합니다 여러분, 오늘은 반도체 이야기입니다."
    body = app._speech_body(t)
    assert "HBM pricing" in body and "반도체" in body


def test_sparse_flags_music_only_video(monkeypatch):
    jid = "t-sparse"
    monkeypatch.setitem(app.jobs, jid, {"clip_duration": 311})
    t = "\n".join(["[00:00] ♪♪♪", "[02:10] We'll be right back.", "[03:40] Thank you.",
                   "[03:10] I'm going to use the same method to make the other side."])
    assert app._transcript_sparse(jid, t).startswith("전사 내용이 비어 있습니다")


def test_sparse_flags_silent_short_clip(monkeypatch):
    """1분 미만도 실제 말이 몇 낱말도 안 되면 음성 없는 영상(2026-11-01, Rocket Lab 37초 무음 클립)."""
    jid = "t-sparse-short"
    monkeypatch.setitem(app.jobs, jid, {"clip_duration": 37})
    for t in ("", "[00:00] ♪♪♪", "[00:05] Thank you.\n[00:20] We'll be right back."):
        detail = app._transcript_sparse(jid, t)
        assert detail.startswith("전사 내용이 비어 있습니다") and "음성 없는 영상" in detail
    # 짧아도 말이 있으면 통과
    assert app._transcript_sparse(jid, "[00:00] Neutron stage two is now on the pad at LC-3.") == ""
    assert app._transcript_sparse(jid, "[00:00] 뉴트론 2단이 발사대에 도착했습니다.") == ""
