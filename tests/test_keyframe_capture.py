"""캡처 후보 추출 — 균등 프레임 시각, 같은 초 중복, 다운로드 순서."""
import os
import subprocess

import pytest

import keyframe_report as kr


def _gray_video(path, seconds=200):
    """밝기로 시각을 새긴 무손실 영상 — 뽑힌 프레임의 실제 시각을 복원할 수 있다."""
    subprocess.run([kr.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s=160x90:r=10:d={seconds},format=gray,geq=lum='floor(T)'",
                    "-pix_fmt", "gray", "-c:v", "ffv1", str(path)], check=True)


def _brightness(jpg):
    out = subprocess.run([kr.FFMPEG, "-v", "error", "-i", str(jpg), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return out[len(out) // 2]


@pytest.mark.skipif(not kr.shutil.which("ffmpeg"), reason="ffmpeg 없음")
def test_uniform_frames_carry_their_real_time(tmp_path, monkeypatch):
    video = tmp_path / "v.mkv"
    _gray_video(video)
    monkeypatch.setattr(kr, "FRAME_WIDTH", 160)
    monkeypatch.setattr(kr, "SCENE_SETTLE", False)
    monkeypatch.setattr(kr, "HOLD_SETTLE", False)
    pairs = kr.extract_candidates(str(video), str(tmp_path / "f"))
    uniform = [(ts, p) for ts, p in pairs if os.path.basename(p).startswith("iv_")]
    assert len(uniform) >= 10
    assert uniform[0][0] == pytest.approx(7.5, abs=0.2)       # 간격 15초의 절반 지점부터
    for ts, path in uniform:
        assert abs(_brightness(path) - ts) <= 2               # 적힌 시각 = 화면의 시각


@pytest.mark.skipif(not kr.shutil.which("ffmpeg"), reason="ffmpeg 없음")
def test_many_scenes_still_settle_to_scene_end(tmp_path, monkeypatch):
    """장면이 많아도(식 길이 한도) 후보로 뽑힌 장면은 끝 프레임으로 바뀐다."""
    video = tmp_path / "v.mkv"
    subprocess.run([kr.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x36:r=10:d=900,format=gray,geq=lum='255*mod(floor(T/3),2)'",
                    "-pix_fmt", "gray", "-c:v", "ffv1", str(video)], check=True)
    monkeypatch.setattr(kr, "FRAME_WIDTH", 64)
    pairs = kr.extract_candidates(str(video), str(tmp_path / "f"))
    scenes = [ts for ts, p in pairs if os.path.basename(p).startswith("scene_")]
    assert len(scenes) >= 20
    for ts in scenes:
        assert 2.1 <= ts % 3 <= 2.5                           # 다음 장면 0.7초 전(시작이면 0 근처)


def test_same_second_keeps_best_scored_frame():
    kept = [{"ts": 100.2, "file": "kf_00100.jpg", "_vision_score": 2.0},
            {"ts": 100.8, "file": "kf_00100.jpg", "_vision_score": 4.0},
            {"ts": 50.0, "file": "kf_00050.jpg"}]
    assert [(k["ts"]) for k in kr._dedupe_same_second(kept)] == [50.0, 100.8]


def test_same_second_tie_keeps_first_frame():
    kept = [{"ts": 100.2, "file": "kf_00100.jpg"}, {"ts": 100.8, "file": "kf_00100.jpg"}]
    assert [k["ts"] for k in kr._dedupe_same_second(kept)] == [100.2]


class _FakeYDL:
    calls = []
    outcomes = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        _FakeYDL.calls.append(self.opts.get("extractor_args"))
        out = _FakeYDL.outcomes.pop(0)
        if out:
            raise RuntimeError(out)
        open(self.opts["outtmpl"].replace("%(ext)s", "mp4"), "w").close()


@pytest.fixture
def fake_ydl(monkeypatch):
    import yt_dlp
    _FakeYDL.calls, _FakeYDL.outcomes = [], []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    sleeps = []
    monkeypatch.setattr(kr.time, "sleep", sleeps.append)
    return sleeps


def test_download_starts_with_alternate_client(fake_ydl, tmp_path):
    _FakeYDL.outcomes = [None]
    assert kr.download_video("u", str(tmp_path / "v")).endswith(".mp4")
    assert _FakeYDL.calls[0]["youtube"]["player_client"] == ["tv_simply", "web_embedded"]
    assert fake_ydl == []


def test_download_falls_back_to_default_client_then_waits(fake_ydl, tmp_path):
    _FakeYDL.outcomes = ["HTTP Error 403", "HTTP Error 403", None]
    assert kr.download_video("u", str(tmp_path / "v"))
    assert _FakeYDL.calls[1:] == [None, None]
    assert fake_ydl == [75]


def test_download_permanent_error_is_not_retried(fake_ydl, tmp_path):
    _FakeYDL.outcomes = ["Private video"]
    with pytest.raises(RuntimeError):
        kr.download_video("u", str(tmp_path / "v"))
    assert len(_FakeYDL.calls) == 1


def _slide(*boxes, bg=240):
    """배경 위에 어두운 사각형(열 범위, 행 범위)을 그린 160×90 프레임."""
    px = bytearray([bg]) * kr._HOLD_PIXELS
    for x0, x1, y0, y1 in boxes:
        for y in range(y0, y1):
            px[y * kr._HOLD_W + x0:y * kr._HOLD_W + x1] = bytes([30]) * (x1 - x0)
    return bytes(px)


TITLE, LEFT, RIGHT, OTHER = (10, 150, 5, 12), (10, 60, 25, 45), (90, 140, 25, 45), (30, 120, 60, 80)


def _timeline(*spans):
    """(장표, 초) 목록을 이어 붙인다."""
    return [frame for frame, seconds in spans for _ in range(seconds)]


def test_frame_moves_to_the_finished_slide():
    frames = _timeline((_slide(TITLE), 10), (_slide(TITLE, LEFT), 10),
                       (_slide(TITLE, LEFT, RIGHT), 10), (_slide(OTHER), 10))
    holds = kr._find_holds(frames)
    assert kr._settled_second(12, frames, holds) == 28        # 오른쪽 패널까지 채워진 뒤, 다음 장표 전
    assert kr._settled_second(2, frames, holds) == 28


def test_frame_stays_when_next_screen_is_a_new_slide():
    frames = _timeline((_slide(TITLE, LEFT), 20), (_slide(OTHER), 20))
    assert kr._settled_second(5, frames, kr._find_holds(frames)) is None


def test_moving_scene_does_not_jump_to_a_different_slide():
    """사람이 말하는 동안(정지 없음)의 프레임이 뒤에 나오는 다른 슬라이드로 바뀌면 안 된다."""
    moving = [_slide((10 + i, 40 + i, 20, 70)) for i in range(40)]
    frames = moving + _timeline((_slide(OTHER), 20))
    assert kr._settled_second(5, frames, kr._find_holds(frames)) is None


def test_finished_screen_held_for_a_long_time_does_not_move():
    frames = _timeline((_slide(TITLE, LEFT), 600))
    assert kr._settled_second(10, frames, kr._find_holds(frames)) is None


def test_wait_is_capped(monkeypatch):
    monkeypatch.setattr(kr, "HOLD_MAX_WAIT", 30)
    frames = _timeline((_slide(TITLE), 10), (_slide(TITLE, LEFT), 10), (_slide(TITLE, LEFT, RIGHT), 100))
    settled = kr._settled_second(0, frames, kr._find_holds(frames))
    assert settled == 30                                      # 40초 뒤 더 채워지는 것까지 기다리지 않는다


def test_constantly_moving_video_has_no_holds():
    frames = [bytes([(i * 37) % 256]) * kr._HOLD_PIXELS for i in range(30)]
    assert kr._find_holds(frames) == []


def test_analysis_failure_keeps_original_frames(tmp_path):
    pairs = [(1.0, "a.jpg"), (2.0, "b.jpg")]
    assert kr._settle_unfinished_frames(str(tmp_path / "없음.mp4"), str(tmp_path), pairs) == pairs
