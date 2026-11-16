"""yt_download — 음성·영상 다운로드가 함께 쓰는 차단 대응 재시도(2026-11-17 통합)."""
import pytest
import yt_dlp

import yt_download

E403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def _fake_ydl(monkeypatch, script):
    """script: 시도마다 None(성공)·문자열(예외). 시도별 대체 클라이언트 사용 여부를 돌려준다."""
    used = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            used.append((self.opts.get("extractor_args") or {}).get("youtube", {}).get("player_client"))
            step = script[len(used) - 1] if len(used) <= len(script) else None
            if step:
                raise Exception(step)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return used


def _sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(yt_download.time, "sleep", seen.append)
    return seen


def test_audio_policy_default_default_alt(monkeypatch):
    used, sleeps, notes = _fake_ydl(monkeypatch, [E403, E403, None]), _sleeps(monkeypatch), []
    yt_download.download_with_retry("u", {"format": "bestaudio"}, alt_attempts=(3,), retryable=yt_download.is_403,
                                    on_retry=lambda n, wait, nxt, e: notes.append((n, wait, nxt)),
                                    should_stop=lambda: False)
    assert used == [None, None, ["tv_simply", "web_embedded"]]
    assert notes == [(1, 0, "새 세션"), (2, 75, "대체 클라이언트")]
    assert sleeps == [1] * 75                       # 취소를 1초마다 보려고 1초씩 쉰다


def test_video_policy_alt_default_default(monkeypatch):
    used, sleeps, notes = _fake_ydl(monkeypatch, ["The downloaded file is empty", "SABR", None]), _sleeps(monkeypatch), []
    yt_download.download_with_retry("u", {}, alt_attempts=(1,), retryable=yt_download.is_retryable_video_error,
                                    on_retry=lambda n, wait, nxt, e: notes.append((n, wait, nxt)))
    assert used == [["tv_simply", "web_embedded"], None, None]
    assert notes == [(1, 0, "기본 클라이언트"), (2, 75, "새 세션")]
    assert sleeps == [75]


def test_gives_up_after_third_and_on_non_retryable(monkeypatch):
    used = _fake_ydl(monkeypatch, [E403, E403, E403])
    _sleeps(monkeypatch)
    with pytest.raises(Exception, match="403"):
        yt_download.download_with_retry("u", {}, alt_attempts=(3,), retryable=yt_download.is_403)
    assert len(used) == 3
    used = _fake_ydl(monkeypatch, ["Private video. Sign in"])
    with pytest.raises(Exception, match="Private"):
        yt_download.download_with_retry("u", {}, alt_attempts=(1,), retryable=yt_download.is_retryable_video_error)
    assert len(used) == 1
    # 음성은 403만 다시 한다 — 빈 파일 오류는 바로 실패(영상과 다른 점)
    used = _fake_ydl(monkeypatch, ["The downloaded file is empty"])
    with pytest.raises(Exception):
        yt_download.download_with_retry("u", {}, alt_attempts=(3,), retryable=yt_download.is_403)
    assert len(used) == 1


def test_after_download_failure_counts_as_attempt_and_stop_cancels_wait(monkeypatch):
    used, sleeps = _fake_ydl(monkeypatch, [None, None]), _sleeps(monkeypatch)
    checks = iter([Exception("HTTP Error 403(스트림 잘림): 300/1000s"), None])

    def after():
        step = next(checks)
        if step:
            raise step

    yt_download.download_with_retry("u", {}, alt_attempts=(3,), retryable=yt_download.is_403, after_download=after)
    assert len(used) == 2 and sleeps == []

    used = _fake_ydl(monkeypatch, [E403, E403, None])
    stopped = {"n": 0}

    def should_stop():
        stopped["n"] += 1
        return stopped["n"] > 3                    # 쉬는 도중에 취소
    with pytest.raises(Exception, match="403"):
        yt_download.download_with_retry("u", {}, alt_attempts=(3,), retryable=yt_download.is_403, should_stop=should_stop)
    assert len(used) == 2                          # 3번째 시도는 하지 않는다


def test_opts_are_not_mutated(monkeypatch):
    _fake_ydl(monkeypatch, [None])
    opts = {"format": "x"}
    yt_download.download_with_retry("u", opts, alt_attempts=(1,), retryable=yt_download.is_403)
    assert opts == {"format": "x"}
