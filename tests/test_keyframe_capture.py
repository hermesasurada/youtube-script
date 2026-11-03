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
    pairs = kr.extract_candidates(str(video), str(tmp_path / "f"))
    uniform = [(ts, p) for ts, p in pairs if os.path.basename(p).startswith("iv_")]
    assert len(uniform) >= 10
    assert uniform[0][0] == pytest.approx(7.5, abs=0.2)       # 간격 15초의 절반 지점부터
    for ts, path in uniform:
        assert abs(_brightness(path) - ts) <= 2               # 적힌 시각 = 화면의 시각


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
