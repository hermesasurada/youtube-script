import original_video


def test_original_link_reading_is_removed():
    """설명문에서 원본 링크를 읽는 기능은 2026-11-10 사용자 지시로 삭제했다 — 되살리지 말 것."""
    assert not hasattr(original_video, "explicit_original")


def test_youtube_id_helpers_remain():
    assert original_video.youtube_id("https://youtu.be/ABCDEFGHIJK?t=20") == "ABCDEFGHIJK"
    assert original_video.canonical_url("ABCDEFGHIJK") == "https://www.youtube.com/watch?v=ABCDEFGHIJK"
