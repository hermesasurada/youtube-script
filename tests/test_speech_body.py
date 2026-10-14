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
