import original_video


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
        "confident": True,          # 설명문에 적힌 출처는 그대로 믿고 갈아탄다
    }
