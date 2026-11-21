"""Flask 앱 객체와 웹 공통 처리 — 원격 접근 제한·응답 압축·DB 연결 정리, 공용 글꼴·정적 파일 버전. 라우트 모듈이 `from yt_web import app`으로 쓴다.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import gzip
import os
import re
import lan_guard  # 로컬·Tailscale 외 접속 차단(2026-10-30)
import db
from flask import Flask, redirect, request, send_from_directory
import yt_core

app      = Flask(__name__)
lan_guard.guard_flask(app)
app.config['TEMPLATES_AUTO_RELOAD'] = True
READER_FONT_DIR = os.path.expanduser("~/projects/hermes-reader-fonts")


@app.get("/reader-fonts/<path:filename>")
def reader_font_asset(filename):
    return send_from_directory(READER_FONT_DIR, filename, max_age=86400)


def _static_version(rel_path: str) -> str:
    """정적 자원 URL 버전. 파일이 바뀔 때만 브라우저 캐시 키가 바뀐다."""
    try:
        return format(os.stat(os.path.join(yt_core.BASE_DIR, "static", rel_path)).st_mtime_ns, "x")
    except OSError:
        return "0"


app.jinja_env.globals["static_v"] = _static_version


def _reader_font_version(rel_path: str) -> str:
    try:
        return format(os.stat(os.path.join(READER_FONT_DIR, rel_path)).st_mtime_ns, "x")
    except OSError:
        return "0"


app.jinja_env.globals["reader_font_v"] = _reader_font_version


# ── Remote-access gating ──────────────────────────────────────────────
# 로컬(=동일 머신)에서는 모든 라우트 허용. 외부(Tailscale/LAN)에서는
# 이력조회 관련 라우트만 허용하고, 그 외 경로는 이력 페이지로 리다이렉트한다.
#  - 모바일 UA → /m (모바일 이력)
#  - 데스크톱  → /  (index.html이 원격 이력 모드로 렌더; 전사 UI 숨김, 조회·읽음·삭제만)
_LOOPBACK = {"127.0.0.1", "::1"}
# 원격에서 허용되는 데이터 엔드포인트(이력 조회/요약/읽음/삭제). 페이지(/, /m)는 별도 처리.
_REMOTE_DATA_ALLOWED = {
    "/summary/note", "/summary/section",
    "/history", "/history/text", "/history/mark_read", "/history/item", "/summary/content",
    "/history/distill",   # 영상별 증류 포함/제외(원격에서도 조정 가능)
    "/history/bookmark",  # 영상 북마크 토글(원격에서도 가능)
    "/history/publish-blog",  # 블로그스팟 발행(원격에서도 — 폰에서 읽고 바로 올린다)
    "/history/delete-blog",   # 발행한 블로그 글 삭제(발행 메뉴, 원격에서도)
    "/llm/model-marks",       # 요약 메타 칩의 제공사 로고(2026-11-21) — 읽기 전용
    "/terms/excluded",        # 각주 제외 용어 — 요약 뷰어 ✕ 버튼(폰에서도 누른다)
    "/people/excluded",       # 인명 소개에서 제외 — 소개 창의 제외 버튼(폰에서도 누른다)
}
_PHONE_UA  = re.compile(r"iPhone|iPod|Windows Phone", re.I)
_TABLET_UA = re.compile(r"iPad|Tablet|PlayBook|Kindle|Silk", re.I)
_MOBI_UA   = re.compile(r"Mobi", re.I)


def _is_phone_ua(ua: str) -> bool:
    """폰만 모바일 UI(/m)로 보낸다. 태블릿은 화면이 넓어 PC UI가 더 맞다.

    Android는 폰·태블릿이 같은 토큰을 쓰므로 'Mobile' 유무로 가른다(구글 권고).
    iPadOS 13+ Safari는 기본이 데스크톱 모드라 UA에 iPad가 없고, 그 경우엔
    자연히 PC UI로 간다.
    """
    ua = ua or ""
    if _PHONE_UA.search(ua):
        return True
    if _TABLET_UA.search(ua):
        return False
    if re.search(r"Android", ua, re.I):
        return bool(re.search(r"Mobile", ua, re.I))
    return bool(_MOBI_UA.search(ua))


def _is_remote() -> bool:
    return request.remote_addr not in _LOOPBACK


@app.teardown_request
def _close_db_conn(exc):
    # 요청 처리 스레드의 SQLite 연결을 닫아 FD 누적 방지(개발서버 thread-per-request).
    try:
        db.close_conn()
    except Exception:
        pass


@app.before_request
def _restrict_remote_access():
    if request.remote_addr in _LOOPBACK:
        return
    p = request.path
    # 프롬프트는 원격에서 '읽기'만 허용(편집·저장은 로컬 전용)
    if p == "/prompt" and request.method == "GET":
        return
    # 데이터/정적 엔드포인트는 그대로 허용 (/sframe = 요약 키프레임 이미지)
    if (p in _REMOTE_DATA_ALLOWED
            or p.startswith("/channels")   # 채널 자동 모니터 조회/토글(모바일 원격 관리)
            or p.startswith("/queue/")     # 처리 큐 조회/추가/순서변경(원격 관리 허용)
            or p.startswith("/m/")
            or p == "/collections" or p.startswith("/collection/")   # 영상 모음(2026-11-19, 폰에서도 본다)
            or p.startswith("/static/")
            or p.startswith("/reader-fonts/")  # 공용 본문 글꼴 — 빠지면 폰에서 글꼴이 전부 대체 글꼴로 나온다(2026-10-30)
            or p.startswith("/sframe/")
            or p.startswith("/thumb/")):   # 썸네일 사본(비공개 전환 영상 폴백)
        return
    is_mobile = _is_phone_ua(request.headers.get("User-Agent", ""))
    # 이력 페이지: 디바이스에 맞는 쪽으로 정규화
    if p in ("/", "/m"):
        # 질의 문자열은 그대로 넘긴다 — 모음에서 '/?open=<id>&from=<모음>'으로 요약을 바로 연다(2026-11-19)
        qs = ("?" + request.query_string.decode("utf-8", "replace")) if request.query_string else ""
        if p == "/" and is_mobile:
            return redirect("/m" + qs, 302)
        if p == "/m" and not is_mobile:
            return redirect("/" + qs, 302)
        return  # 디바이스에 맞는 페이지 → 통과
    # 그 외 모든 경로 → 403 문구 없이 이력 페이지로 이동
    return redirect("/m" if is_mobile else "/", 302)


@app.after_request
def _optimize_web_response(resp):
    # 렌더된 HTML 페이지(/, /m)는 캐시 금지 → 템플릿 변경이 새로고침만으로 반영(모바일 캐시 방지).
    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    # 정적 URL은 템플릿의 mtime 쿼리로 버전 관리한다. 변경 전까지 재검증 요청을 없앤다.
    elif request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    # Tailscale 구간에서 JSON/HTML/JS/CSS 전송량을 줄인다. 바이너리와 SSE는 제외한다.
    compressible = (
        content_type.startswith("text/")
        or content_type.startswith("application/json")
        or content_type.startswith("application/javascript")
    )
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    if (request.method != "HEAD" and 200 <= resp.status_code < 300
            and accepts_gzip and compressible
            and not resp.headers.get("Content-Encoding")
            and not content_type.startswith("text/event-stream")):
        resp.direct_passthrough = False
        raw = resp.get_data()
        if len(raw) >= 1024:
            resp.set_data(gzip.compress(raw, compresslevel=6))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(resp.get_data()))
            resp.headers.pop("ETag", None)
            resp.headers.pop("Content-MD5", None)
            vary = {v.strip() for v in resp.headers.get("Vary", "").split(",") if v.strip()}
            vary.add("Accept-Encoding")
            resp.headers["Vary"] = ", ".join(sorted(vary))
    return resp
