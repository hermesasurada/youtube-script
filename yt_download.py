"""yt-dlp 다운로드의 차단 대응 재시도 — 음성(전사)과 영상(캡처)이 같은 틀을 쓴다(2026-11-17).

시도는 3번이다: 1번 실패 → 곧바로 2번, 2번 실패 → 75초 쉬고 3번(`RETRY_WAIT`).
어느 시도에 대체 클라이언트(tv_simply·web_embedded)를 쓸지와, 어떤 오류를 '다시 해 볼 만한' 것으로
볼지는 호출처가 정한다 — 둘은 실측으로 따로 맞춘 값이라 여기서 합치지 않는다.

  음성(`yt_jobs.run_job`):   기본 → 기본(새 세션) → 대체.  재시도 조건 = 403(스트림 잘림 포함)
  영상(`keyframe_report`):   대체 → 기본 → 기본(새 세션).  재시도 조건 = 403·빈 파일·포맷 없음·SABR

IP 단위 차단(403)으로 웹 계열이 전부 막혀도 대체 클라이언트 둘은 통과한다(2026-08-18 실측).
"""
from __future__ import annotations

import re
import time
from typing import Callable

ALT_CLIENTS = ("tv_simply", "web_embedded")
RETRY_WAIT = {1: 0, 2: 75}           # 시도 번호 → 다음 시도까지 쉬는 초(3번째 실패는 포기)

# 영상(캡처)에서 세션·클라이언트를 바꾸면 풀릴 수 있는 실패. 비공개·멤버십·삭제 같은 영구 사유는
# 여기 없다 — 그건 호출 측이 _META_PERMANENT_RE로 본다.
_RETRYABLE_VIDEO_RE = re.compile(
    r"403|downloaded file is empty|Requested format is not available|missing a URL|SABR", re.I)


def is_403(msg: str) -> bool:
    """음성 다운로드의 재시도 조건 — 403(스트림이 중간에 잘린 변형도 403으로 올린다)."""
    return "403" in (msg or "")


def is_retryable_video_error(msg: str) -> bool:
    """세션·클라이언트를 바꾸면 풀릴 수 있는 다운로드 실패인가.

    403(IP 차단), 빈 파일·포맷 없음(SABR-only 실험 세션, 라이브 종료 직후 VOD 미처리).
    """
    return bool(_RETRYABLE_VIDEO_RE.search(msg or ""))


def next_label(attempt: int, alt_attempts) -> str:
    """재시도 알림에 쓰는 다음 시도 이름."""
    if attempt + 1 in alt_attempts:
        return "대체 클라이언트"
    return "기본 클라이언트" if attempt in alt_attempts else "새 세션"


def download_with_retry(url: str, opts: dict, *, alt_attempts, retryable: Callable[[str], bool],
                        after_download: Callable[[], None] | None = None,
                        on_retry: Callable[[int, int, str, Exception], None] | None = None,
                        should_stop: Callable[[], bool] | None = None) -> None:
    """`opts`로 `url`을 받는다. 다시 해 볼 만한 실패면 최대 두 번 더 시도하고, 아니면 그 예외를 그대로 올린다.

    alt_attempts    대체 클라이언트를 쓸 시도 번호(1~3)
    after_download  받은 뒤 검사 — 예외를 내면 그 시도는 실패로 친다(스트림 잘림 감지)
    on_retry        (시도 번호, 쉬는 초, 다음 시도 이름, 예외) — 알림·로그
    should_stop     취소 확인 — 참이면 재시도하지 않고, 쉬는 동안에도 1초마다 본다
    """
    import yt_dlp
    for attempt in (1, 2, 3):
        try:
            o = dict(opts)
            if attempt in alt_attempts:
                o["extractor_args"] = {"youtube": {"player_client": list(ALT_CLIENTS)}}
            with yt_dlp.YoutubeDL(o) as ydl:
                ydl.download([url])
            if after_download is not None:
                after_download()
            return
        except Exception as e:
            wait = RETRY_WAIT.get(attempt)
            if wait is None or not retryable(str(e)) or (should_stop is not None and should_stop()):
                raise
            if on_retry is not None:
                on_retry(attempt, wait, next_label(attempt, alt_attempts), e)
            if not wait:
                continue
            if should_stop is None:
                time.sleep(wait)
                continue
            for _ in range(wait):
                if should_stop():
                    raise
                time.sleep(1)
