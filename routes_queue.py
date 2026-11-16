"""라우트: 처리 큐(/queue/*).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import db
from flask import request
from yt_web import app
import yt_core
import yt_queue

@app.route("/queue/preview")
def queue_preview():
    """URL의 메타(제목·채널)와 중복 여부 — 큐 팝업의 추가 폼이 사용."""
    url = (request.args.get("url") or "").strip()
    yt_id = yt_queue._extract_yt_id(url)
    if not yt_id:
        return yt_core._json({"error": "유튜브 URL이 아닙니다."}, 400)
    meta = yt_queue._oembed_meta(url)
    if not meta.get("title"):
        return yt_core._json({"error": "영상 정보를 가져올 수 없습니다(비공개·삭제 여부 확인)."}, 404)
    start_sec = yt_queue._extract_start_sec(url)
    dup = None
    if db.get_item_by_yt_id(yt_id):
        dup = "history"      # 시작 시각을 지정해도 이미 전사된 영상은 다시 받지 않는다
    else:
        st = db.queue_status_of(yt_id)
        if st in ("pending", "processing", "kf_retry", "deferred"):
            dup = "queue"
    # 같은 채널을 지난번에 어떻게 넣었는지(캡처·증류)를 함께 줘서 폼이 그대로 맞춰 뜬다.
    prefs = db.last_queue_prefs_for_channel(meta.get("channel") or "")
    return yt_core._json({"ok": True, "yt_id": yt_id, "title": meta["title"],
                  "channel": meta.get("channel") or "", "duplicate": dup,
                  "start_sec": start_sec, "prefs": prefs,
                  "start_label": yt_core._format_duration(start_sec) if start_sec else ""})


@app.route("/queue/items")
def queue_items():
    return yt_core._json(yt_queue._attach_queue_eta(db.queue_overview()))


@app.route("/queue/items", methods=["POST"])
def queue_add():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    yt_id = yt_queue._extract_yt_id(url)
    if not yt_id:
        return yt_core._json({"error": "유튜브 URL이 아닙니다."}, 400)
    try:
        start_sec = max(0, int(data.get("start_sec") or 0))
    except (TypeError, ValueError):
        start_sec = 0
    if not start_sec:
        start_sec = yt_queue._extract_start_sec(url)
    if db.get_item_by_yt_id(yt_id):
        return yt_core._json({"error": "이미 전사된 영상입니다.", "duplicate": "history"}, 409)
    meta = yt_queue._oembed_meta(url)
    title = (data.get("title") or "").strip() or meta.get("title") or url
    capture = data.get("capture")            # 영상 단위 캡처 지정(None=기본 포함)
    distill = data.get("distill")            # 증류 지정(None=채널/기본 설정 따름)
    # 지정이 없으면 같은 채널을 지난번에 넣은 설정을 그대로 따른다(폼을 거치지 않는
    # 원격·API 호출도 같은 결과가 되도록 서버에서 한 번 더 채운다).
    if capture is None or distill is None:
        prefs = db.last_queue_prefs_for_channel(meta.get("channel") or "")
        if capture is None:
            capture = prefs.get("capture")
        if distill is None:
            distill = prefs.get("distill")
    canonical = f"https://www.youtube.com/watch?v={yt_id}"
    if start_sec:
        canonical += f"&t={start_sec}"
    added = db.enqueue_video(yt_id, canonical, title, "manual",
                             capture=None if capture is None else bool(capture),
                             start_sec=start_sec,
                             distill=None if distill is None else bool(distill))
    if not added:
        # 이미 큐에 있음 — 종료 상태(done/failed/skipped)면 재처리 요청으로 보고 되살린다
        st = db.queue_status_of(yt_id)
        if st in ("pending", "processing", "kf_retry", "deferred"):
            return yt_core._json({"error": "이미 큐에 대기 중입니다.", "duplicate": "queue"}, 409)
        db.queue_requeue(yt_id, capture=None if capture is None else bool(capture),
                         distill=None if distill is None else bool(distill))
        db.set_queue_start_sec(yt_id, start_sec, url=canonical)
    started = yt_queue._kick_drain_if_idle()
    waiting = len(db.queue_overview()["waiting"])
    return yt_core._json({"ok": True, "yt_id": yt_id, "title": title, "start_sec": start_sec,
                  "waiting": waiting, "started": started})


@app.route("/queue/items/<int:qid>/move", methods=["POST"])
def queue_item_move(qid: int):
    d = (request.get_json(force=True) or {}).get("direction", "")
    if not db.queue_move(qid, d):
        return yt_core._json({"error": "이동 불가(대기 항목이 아니거나 끝)"}, 400)
    return yt_core._json({"ok": True})


@app.route("/queue/items/<int:qid>/requeue", methods=["POST"])
def queue_item_requeue(qid: int):
    """종료 상태(failed/done/skipped) 항목을 재시도 초기화 후 줄 맨 뒤로 복귀."""
    r = db.queue_row(qid)
    if not r:
        return yt_core._json({"error": "항목이 없습니다."}, 404)
    if r["status"] not in ("failed", "done", "skipped"):
        return yt_core._json({"error": "종료 상태 항목만 복귀할 수 있습니다."}, 400)
    if db.get_item_by_yt_id(r["yt_id"]):
        return yt_core._json({"error": "이미 전사된 영상입니다(이력 확인)."}, 409)
    db.queue_requeue(r["yt_id"])
    started = yt_queue._kick_drain_if_idle()
    return yt_core._json({"ok": True, "started": started})


@app.route("/queue/items/<int:qid>", methods=["PATCH"])
def queue_item_update(qid: int):
    """대기 항목의 영상 단위 설정 변경 — 캡처 포함 여부, 증류 지정."""
    data = request.get_json(force=True) or {}
    out = {}
    if "capture" in data:
        if not db.set_queue_capture(qid, bool(data["capture"])):
            return yt_core._json({"error": "변경 불가(대기 중 항목이 아님)"}, 400)
        out["capture"] = bool(data["capture"])
    if "distill" in data:
        if not db.set_queue_distill(qid, bool(data["distill"])):
            return yt_core._json({"error": "변경 불가(대기 중 항목이 아님)"}, 400)
        out["distill"] = bool(data["distill"])
    if not out:
        return yt_core._json({"error": "변경할 항목이 없습니다."}, 400)
    return yt_core._json({"ok": True, **out})


@app.route("/queue/items/<int:qid>", methods=["DELETE"])
def queue_item_cancel(qid: int):
    if not db.queue_cancel(qid):
        return yt_core._json({"error": "취소 불가(대기 중이 아님)"}, 400)
    return yt_core._json({"ok": True})
