"""youtube-script 서버 진입점 — launchd `com.yhandhs.youtube-script`가 `.venv/bin/python app.py`로 띄운다.

2026-11-16 리팩토링 2단계(사용자 지시 '1~4 순차 진행')로 기능별 모듈로 나눴다. 여기에는 로깅·FD 한도·DB 초기화·
서버 실행만 남는다. 모듈 지도:
  yt_web      Flask 앱·원격 접근 제한·응답 압축          yt_core     공용 설정·작업 상태·md 도구
  yt_transcribe whisper 전사·붕괴 판정                   yt_jobs     URL/파일 전사 작업·썸네일·오디오 보관
  yt_models   요약 모델 설정·라벨·_llm_text              yt_summary  요약 생성·다듬기·한눈 요약·각주 제외 주입
  yt_titles   제목 한국어 표기                           yt_queue    처리 큐 도우미       yt_blog  블로그 발행 도우미
  llm_runners 계열별 LLM 실행기                          routes_*    HTTP 라우트(main·summary·queue·channels·history·media·collections)
"""
import logging
import os
import threading

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import db
import yt_core
import yt_jobs
from yt_web import app
# 라우트 등록 — 가져오기만 하면 각 모듈의 @app.route가 app에 붙는다
import routes_main
import routes_summary
import routes_queue
import routes_channels
import routes_history
import routes_media
import routes_collections

ROUTE_MODULES = (routes_main, routes_summary, routes_queue, routes_channels, routes_history, routes_media,
                 routes_collections)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("yt-script")


def _raise_fd_limit():
    """launchd 기본 소프트 한도(256)는 너무 낮다 — 가능한 만큼 상향(FD 고갈 방지)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for target in (16384, 10240, 8192, 4096):
            cap = target if hard == resource.RLIM_INFINITY else min(target, hard)
            if cap <= soft:
                return
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
                log.info("FD soft limit raised: %d -> %d", soft, cap)
                return
            except (ValueError, OSError):
                continue
    except Exception as e:
        log.warning("could not raise FD limit: %s", e)


_raise_fd_limit()


# ── SQLite 인덱스 초기화 + 백그라운드 reindex ─────────────────────────
db.init()


def _bg_reindex():
    try:
        s = db.reindex_all()
        log.info("db reindex: %s", s)
    except Exception as e:
        log.error("db reindex error: %s", e)


if __name__ == "__main__":
    # 백그라운드 유지보수 스레드는 서버 실행 시에만 시작(import 부작용 제거 → 테스트에서 안전)
    threading.Thread(target=yt_jobs._cleanup_old_jobs, daemon=True).start()
    threading.Thread(target=_bg_reindex, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    log.info("starting server on %s:%s — claude=%s, model=%s",
             host, port, yt_core.CLAUDE_BIN, yt_core.CLAUDE_MODEL)
    app.run(debug=False, host=host, port=port, threaded=True)
