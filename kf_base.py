"""캡처 공통 — 설정값(환경변수), ffmpeg 경로, 시각 표기.

캡처 관련 설정은 모두 여기 있다. 다른 캡처 모듈은 `kf_base.이름`으로 읽으므로 테스트는 여기 값을 바꿔 끼운다
(2026-11-17 keyframe_report에서 분리).
"""
from __future__ import annotations

import os
import shutil


BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
FFMPEG      = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE     = shutil.which("ffprobe") or "ffprobe"
FFMPEG_DIR  = os.path.dirname(FFMPEG)
SCENE_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("SCENE_THRESHOLD", "0.3"))))  # ffmpeg 필터 삽입값 — [0,1] clamp
MAX_CANDIDATES  = int(os.environ.get("MAX_CANDIDATES", "40"))
MIN_GAP         = int(os.environ.get("MIN_GAP", "6"))   # 근접 중복 제거 간격(초)
MAX_VISUAL_TARGETS = int(os.environ.get("MAX_VISUAL_TARGETS", "12"))
TARGET_WINDOW   = float(os.environ.get("TARGET_WINDOW", "3"))       # 전사 시각단서 전후 탐색(초)
TARGET_INTERVAL = float(os.environ.get("TARGET_INTERVAL", "2"))     # 탐색 구간 프레임 간격(초)
MAX_FRAMES_PER_SECTION = int(os.environ.get("MAX_FRAMES_PER_SECTION", "4"))  # 2026-11-07 3→4(소제목 ÷4→÷6로 섹션이 줄어 영상당 캡처 상한 유지)
SCENE_SETTLE    = os.environ.get("SCENE_SETTLE", "1") != "0"  # 장면 시작 대신 장면 끝(완성된 화면) 프레임
HOLD_SETTLE     = os.environ.get("HOLD_SETTLE", "1") != "0"   # 채워지는 중인 장표를 완성될 때까지 기다린 프레임으로
HOLD_MAX_WAIT   = int(os.environ.get("HOLD_MAX_WAIT", "75"))  # 완성을 기다리는 최대 시간(초) — 전사 맥락과 너무 멀어지지 않게
# 기본 비전 모델(순서를 받지 못했을 때). 2026-11-09 사용자 승인으로 Opus → Sonnet 5.5 — 되돌리기는 VISION_MODEL=opus.
VISION_MODEL    = os.environ.get("VISION_MODEL", "claude-sonnet-5-5")
# 요약 모델 순서에 Claude Opus가 있으면 캡처는 이 모델로 바꿔 부른다(app._capture_order). 빈 값이면 바꾸지 않는다.
VISION_OPUS_TO  = os.environ.get("YT_VISION_OPUS_TO", "claude-sonnet-5-5").strip()
GPT_VISION_MODEL = os.environ.get("GPT_VISION_MODEL", os.environ.get("GPT_MODEL", "gpt-6-astra"))
# Claude 비전 3회 실패 시 Grok 폴백(요약 폴백과 동일 기조). grok CLI는 이미지 옵션이 없지만
# 프롬프트의 @경로를 읽어 비전이 동작한다 — 캡처를 통째로 잃는 것보다 낫다.
GROK_VISION_FALLBACK = os.environ.get("GROK_VISION_FALLBACK", "1") != "0"
# 빈 값이면 -m 을 붙이지 않아 grok CLI 기본 모델을 그대로 쓴다(CLI가 올라가면 자동으로 따라감).
GROK_VISION_MODEL    = os.environ.get("GROK_VISION_MODEL", os.environ.get("GROK_MODEL", ""))
VIDEO_MAXH      = int(os.environ.get("VIDEO_MAXH", "1080"))   # 다운로드 최대 높이(해상도)
FRAME_WIDTH     = int(os.environ.get("FRAME_WIDTH", "1708"))  # 프레임 가로폭(이전 854의 2배)
# subprocess 타임아웃(초) — 멈춘 ffmpeg/ffprobe/claude가 키프레임 세마포어를 영구 점유하는 것 방지
PROBE_TIMEOUT   = int(os.environ.get("PROBE_TIMEOUT", "30"))
FFMPEG_TIMEOUT  = int(os.environ.get("FFMPEG_TIMEOUT", "600"))
VISION_TIMEOUT  = int(os.environ.get("VISION_TIMEOUT", "300"))


def _hms(sec: float) -> str:
    s = int(sec); h, r = divmod(s, 3600); m, s = divmod(r, 60)
    return f"{h:01d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
