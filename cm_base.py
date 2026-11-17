"""채널 감시기 공통 — 설정값(환경변수), 알림용 환경 파일 읽기, 재시도 간격.

`~/.hermes/youtube-monitor.env`를 읽은 **뒤에** 텔레그램 토큰을 잡으므로, 감시기의 다른 모듈은 이 모듈을 먼저 불러온다.
다른 모듈은 `cm_base.이름`으로 읽는다(2026-11-17 channel_monitor에서 분리).
"""
from __future__ import annotations

import os


try:
    import yt_dlp
except Exception:
    yt_dlp = None

# ── 설정 ───────────────────────────────────────────────────────────────
BASE       = os.environ.get("YTS_BASE", "http://127.0.0.1:5001")
MIN_DUR    = int(os.environ.get("MONITOR_MIN_DURATION", "180"))   # 3분 이하 제외(채널별 오버라이드 가능)
POLL_SEC   = int(os.environ.get("MONITOR_POLL_SEC", "6"))         # /result 폴링 간격
MAX_JOB_SEC = int(os.environ.get("MONITOR_MAX_JOB_SEC", "18000"))  # 건당 전사 상한(5h) — 4시간대 장편(Acquired 등) 수용
SUMM_TIMEOUT = int(os.environ.get("MONITOR_SUMM_TIMEOUT", "1800"))  # 요약 재요청까지 고려해 30분 대기
KF_TIMEOUT   = int(os.environ.get("MONITOR_KF_TIMEOUT", "1800"))
# 403 차단은 저녁 시간대(15~23시)에 몰리는 경우가 있어 3회보다 넉넉한
# 5회 예산을 둔다. 짧은 일시 장애는 20분 간격으로 빠르게 따라잡는다.
MAX_PIPELINE_ATTEMPTS = int(os.environ.get("MONITOR_MAX_PIPELINE_ATTEMPTS", "5"))
# 파이프라인 실패 재시도 간격 — 20분 고정(처리 주기 10분과 별개로 유지).
# 예산 5회 × 20분이라 짧은 일시 장애는 빠르게 따라잡고, 저녁 차단
# 구간(15~23시)을 통째로 건너뛰지는 못한다. 대신 회복이 빠른 일시 장애를
# 빨리 따라잡고, 구간을 넘겨야 하는 건은 실패 후 수동 재처리한다.
RETRY_BASE_SEC = int(os.environ.get("MONITOR_RETRY_BASE_SEC", "1200"))


def _retry_delay(attempt: int) -> int:
    """다음 재시도까지 대기 초 — 시도 횟수와 무관하게 일정하다."""
    return RETRY_BASE_SEC
LOCK_PATH  = os.environ.get("MONITOR_LOCK", os.path.expanduser("~/.hermes/youtube-monitor.lock"))
OUTBOX     = os.environ.get("MONITOR_OUTBOX", os.path.expanduser("~/.hermes/youtube-monitor.outbox"))
ENV_PATH   = os.path.expanduser("~/.hermes/youtube-monitor.env")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"


def _load_env(path: str) -> None:
    """~/.hermes/youtube-monitor.env(KEY=VALUE)를 os.environ에 로드(기존값 우선)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env(ENV_PATH)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
