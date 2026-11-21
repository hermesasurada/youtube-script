"""라우트: 썸네일·요약 캡처 이미지·캡처 생성(/thumb, /sframe, /keyframes*).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import os
import re
import db
import keyframe_report
import original_video
from flask import request, send_file
from yt_web import app
import yt_core
import yt_models
import yt_summary
import logging

log = logging.getLogger("yt-script")

@app.route("/thumb/<yt_id>")
def serve_thumbnail(yt_id: str):
    """썸네일 사본 서빙 — 목록의 유일한 소스.

    전사 시점에 받아 둔 사본만 쓴다. 브라우저가 i.ytimg.com 으로 나가지 않으므로
    VPN 경유 시 카드 20장분 외부 왕복이 사라진다. 사본 내용은 바뀌지 않으니
    캐시도 길게 준다.
    """
    if not re.fullmatch(r"[\w-]{5,20}", yt_id or ""):
        return yt_core._json({"error": "잘못된 id"}, 400)
    p = os.path.join(yt_core.THUMB_DIR, f"{yt_id}.jpg")
    if not os.path.isfile(p):
        return yt_core._json({"error": "없음"}, 404)
    return send_file(p, mimetype="image/jpeg", max_age=31536000)


@app.route("/sframe/<path:rel>")
def serve_summary_frame(rel: str):
    """요약 키프레임 이미지 서빙 (res/summary 하위로 경로 검증)."""
    p = os.path.realpath(os.path.join(yt_core.SUMMARY_DIR, rel))
    base = os.path.realpath(yt_core.SUMMARY_DIR)
    if not p.startswith(base + os.sep):
        return "denied", 403
    if not os.path.isfile(p):
        return "not found", 404
    return send_file(p)


def _keyframe_source_url(md_path: str, requested: str) -> str:
    """캡처에 쓸 영상 URL. 전사 md의 webpage_url이 있으면 그것이 진실이다.

    번역·재게시 영상을 원본으로 갈아타 전사하면 큐가 넘겨주는 url은 여전히 수집본
    (비즈카페)이라, 그대로 받으면 원본 타임스탬프에 수집본 화면이 붙는다. 로컬 파일
    전사처럼 webpage_url이 없으면 요청값을 쓴다."""
    try:
        meta, _ = yt_core._parse_md(md_path)
    except Exception:
        return requested
    actual = str(meta.get("webpage_url") or "").strip()
    if actual and original_video.youtube_id(actual) and actual != requested:
        if requested:
            log.info("keyframes: url %s → md webpage_url %s", requested, actual)
        return actual
    return requested


@app.route("/keyframes", methods=["POST"])
def keyframes():
    """전사 md(txt_path) 기반으로 영상 키프레임을 추출·정렬해 요약에 합친다.

    요약이 먼저 있어야 함(섹션 정렬 대상). 영상 다운로드 실패 시 요약은 그대로 유지(폴백).
    """
    data = request.get_json(force=True) or {}
    abs_path, err = yt_core._check_res_path(data.get("txt_path") or "")
    if err:
        return err
    summary_path = yt_summary._summary_path_for_md(abs_path)
    if not summary_path or not os.path.isfile(summary_path):
        return yt_core._json({"ok": False, "reason": "no_summary", "error": "요약이 먼저 필요합니다."}, 400)

    rel = os.path.relpath(summary_path, yt_core.SUMMARY_DIR)        # {date}/{stem}.md
    date_dir = rel.split(os.sep)[0]
    stem = os.path.splitext(os.path.basename(summary_path))[0]
    frames_dir = os.path.join(yt_core.SUMMARY_DIR, date_dir, stem + ".frames")
    url_base = f"/sframe/{date_dir}/{stem}.frames"

    kid = yt_core._kf_register(yt_summary._parse_summary_title(summary_path))   # 상태 추적(대기→처리)
    try:
        with yt_core._keyframe_sem:   # 동시 키프레임 처리 직렬화(자원 경쟁 방지)
            yt_core._kf_set(kid, "processing")
            skip_claude = bool(
                data.get("skip_claude")
                or data.get("skip_claude_vision")
                or str(data.get("mode") or "").lower() == "grok"
            )
            # 순서를 명시하지 않으면 요약한 모델 → 요약 슬롯 순(수동 캡처·자동모니터 공통).
            requested_order = data.get("models") or data.get("model_order")
            if requested_order is not None:
                model_order = [str(m) for m in requested_order if str(m or "").strip()]
            else:
                model_order = None if skip_claude else yt_models._capture_order(summary_path)
            res = keyframe_report.generate_keyframes(
                summary_path, _keyframe_source_url(abs_path, (data.get("url") or "").strip()),
                frames_dir, url_base,
                skip_claude=skip_claude,
                model_order=model_order,
            )
    except Exception as e:
        log.warning("keyframes error: %s", e)
        return yt_core._json({"ok": False, "reason": "error", "error": str(e)})
    finally:
        yt_core._kf_unregister(kid)

    if res.get("ok") and res.get("n_frames"):
        try:
            db.upsert_summary_only(abs_path)   # 검색 인덱스 갱신
        except Exception:
            pass
        try:
            with open(summary_path, encoding="utf-8", errors="replace") as f:
                res["summary_md"] = f.read()   # 갱신된 요약(라이브 재렌더용)
        except Exception:
            pass
    log.info("keyframes: %s (%s)", os.path.basename(summary_path), res)
    return yt_core._json(res)


@app.route("/keyframes/status")
def keyframes_status():
    """백그라운드 키프레임 처리 상태(UI 배지용): 현재 처리 중 영상 + 대기 건수."""
    with yt_core._kf_lock:
        jobs_ = list(yt_core._kf_jobs.values())
    cur = next((j["title"] for j in jobs_ if j["state"] == "processing"), None)
    queued = sum(1 for j in jobs_ if j["state"] == "queued")
    return yt_core._json({"processing": cur, "queued": queued, "active": len(jobs_)})


# ── 요약 메타 칩의 제공사 로고(2026-11-21 사용자 지시 'llm 모델 카탈로그 기준') ─────────────────
# 모델 이름 → 제공사는 공용 llm_catalog.provider_mark(카탈로그 우선, 옛 모델은 계열). 로고는 공용 자원
# ~/projects/hermes-reader-fonts/providers/ 파일을 data URI로 돌려준다 — 화면이 HTML 파일로 내보내거나 공개 사본에
# 실어도 서버 없이 보이게.
_MARK_URI: dict[str, str] = {}
_MARK_TYPES = {".ico": "image/x-icon", ".png": "image/png", ".svg": "image/svg+xml"}


def _mark_data_uri(fname: str) -> str:
    if not fname:
        return ""
    if fname not in _MARK_URI:
        import base64
        from yt_web import READER_FONT_DIR
        path = os.path.join(READER_FONT_DIR, "providers", os.path.basename(fname))
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            _MARK_URI[fname] = f"data:{_MARK_TYPES.get(os.path.splitext(fname)[1], 'application/octet-stream')};base64,{data}"
        except OSError:
            _MARK_URI[fname] = ""
    return _MARK_URI[fname]


@app.get("/llm/model-marks")
def llm_model_marks():
    """?names=Opus 5.5|GPT 6.1 Sol → {이름: {provider, label, icon}}. 모르는 이름은 provider ''·icon ''."""
    import llm_gateway
    names = [n.strip() for n in (request.args.get("names") or "").split("|") if n.strip()][:40]
    out = {}
    for n in names:
        try:
            m = llm_gateway.llm_catalog.provider_mark(n)
        except Exception:  # noqa: BLE001 — 카탈로그 문제로 화면이 깨지면 안 된다
            m = {"provider": "", "label": "", "mark": ""}
        out[n] = {"provider": m["provider"], "label": m["label"], "icon": _mark_data_uri(m["mark"])}
    return yt_core._json(out)
