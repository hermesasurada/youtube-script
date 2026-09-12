import original_video


def test_translation_detection_is_content_based_not_channel_hardcoded():
    assert original_video.looks_like_translation(
        "희귀 인터뷰 전체번역",
        "임의 채널",
        "The Korean subtitles of the video were added for education.",
    )
    assert not original_video.looks_like_translation(
        "국내 기업 인터뷰",
        "임의 채널",
        "일반적인 영상 설명",
    )


def test_explicit_original_prefers_label_and_excludes_current_video():
    description = """채널: https://www.youtube.com/watch?v=CURRENT001
원본 영상: https://youtu.be/ORIGINAL01?t=20
참고 영상: https://www.youtube.com/watch?v=REFERENCE1
"""
    found = original_video.explicit_original(description, "CURRENT001")
    assert found == {
        "id": "ORIGINAL01",
        "url": "https://www.youtube.com/watch?v=ORIGINAL01",
        "title": "",
        "uploader": "",
        "method": "explicit",
    }


def test_candidate_selection_uses_identity_duration_and_channel_guards():
    candidates = [
        {"id": "REPOST00001", "title": "Doug Leone Sequoia Capital 번역", "uploader": "BZCF | 비즈까페", "duration": 3925},
        {"id": "WRONG000001", "title": "Doug Leone on Luck and Taking Risks", "uploader": "Stanford", "duration": 2989},
        {"id": "NR9NI51D7ek", "title": "How to Dominate for Decades | Doug Leone, Sequoia Capital", "uploader": "David Senra", "duration": 4938},
    ]
    found = original_video.select_candidate(
        "How to Dominate for Decades Doug Leone Sequoia Capital",
        candidates,
        current_video_id="REPOST00001",
        current_uploader="BZCF | 비즈까페",
        current_duration=3925,
    )
    assert found["id"] == "NR9NI51D7ek"
    assert found["uploader"] == "David Senra"


def test_candidate_selection_rejects_ambiguous_query():
    assert original_video.select_candidate(
        "business interview",
        [{"id": "SOMEVIDEO01", "title": "Business Interview", "duration": 1000}],
        current_duration=900,
    ) is None
