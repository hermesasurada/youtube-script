"""라우트: 채널 모니터 설정(/channels*, /distill/policy).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import html as _htmlmod
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import db
import llm_gateway
from flask import Response, request, send_file
from yt_web import app
import yt_core
import yt_models
import yt_titles
import logging

log = logging.getLogger("yt-script")

@app.route("/channels")
def channels_list():
    """모니터링 채널 목록 + 큐 요약(모달용). 채널마다 자동 처리 실적(done·last_done)을 붙인다."""
    try:
        stats = db.channel_stats()
        chans = [{**c, **stats.get(c["channel_id"], {"done": 0, "last_done": None})}
                 for c in db.list_channels()]
        return yt_core._json({"channels": chans, "queue": db.queue_counts(),
                      **yt_models._monitor_model_payload()})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


# ── 채널 추가(2026-11-14 사용자 지시 "yt와 xw에 사용자 핸들로 채널을 추가할 수 있는 기능") ──
# @핸들·youtube.com/@핸들·youtube.com/channel/UC…·UC… 채널 ID를 받는다. 핸들은 채널 페이지에서 채널 ID를 찾고
# (channel_monitor.resolve_channel_id — 추천 채널 ID를 잘못 잡지 않게 canonical 링크 우선), 이름은 채널 RSS 제목.
# 새 채널은 자동 수집 켜짐으로 등록되고, 첫 확인 때 지금 피드의 영상은 '본 것'으로만 기록한다(백필 방지 — 모니터 기존 규칙).
_CHANNEL_ID_RE = re.compile(r"\b(UC[0-9A-Za-z_-]{22})\b")
_HANDLE_URL_RE = re.compile(r"youtube\.com/@([^/?#\s]+)", re.I)


def _parse_channel_input(raw: str) -> tuple[str, str]:
    """입력 → (채널 ID, 핸들). 둘 중 하나는 빈 문자열일 수 있다. 알아볼 수 없으면 ('', '')."""
    text = "".join(c for c in (raw or "").strip() if unicodedata.category(c) != "Cf")
    m = _CHANNEL_ID_RE.search(text)
    if m:
        return m.group(1), ""
    m = _HANDLE_URL_RE.search(text)
    handle = urllib.parse.unquote(m.group(1)) if m else text.lstrip("@")
    if not handle or "/" in handle or " " in handle or len(handle) > 100:
        return "", ""
    return "", handle


def _channel_feed_title(channel_id: str) -> str:
    """채널 RSS 맨 위 <title>(채널 이름). 못 읽으면 빈 문자열."""
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            xml = r.read().decode("utf-8", "replace")
        m = re.search(r"<title>([^<]+)</title>", xml)
        return _htmlmod.unescape(m.group(1)).strip() if m else ""
    except Exception:  # noqa: BLE001 — 이름은 없어도 등록은 된다
        return ""


@app.route("/channels", methods=["POST"])
def channels_add():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("handle") or data.get("channel") or "").strip()
    channel_id, handle = _parse_channel_input(raw)
    if not channel_id and not handle:
        return yt_core._json({"error": "@핸들, 채널 주소 또는 UC로 시작하는 채널 ID를 넣어 주세요."}, 400)
    if not channel_id:
        try:
            import channel_monitor
            channel_id = channel_monitor.resolve_channel_id(handle) or ""
        except Exception as e:  # noqa: BLE001
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 404:
                return yt_core._json({"error": f"@{handle} 채널을 찾을 수 없습니다."}, 404)
            return yt_core._json({"error": f"채널 확인 실패: {e}"}, 502)
        if not channel_id:
            return yt_core._json({"error": f"@{handle} 채널을 찾을 수 없습니다."}, 404)
    existing = next((c for c in db.list_channels() if c["channel_id"] == channel_id), None)
    if existing:
        return yt_core._json({"error": f"이미 등록된 채널입니다: {existing.get('title') or existing.get('handle') or channel_id}",
                      "channel": existing}, 409)
    title = _channel_feed_title(channel_id)
    if not title and not handle:
        return yt_core._json({"error": f"채널 {channel_id}의 피드를 읽을 수 없습니다(없는 채널일 수 있습니다)."}, 404)
    cid = db.add_channel(channel_id, handle=handle, title=title,
                         url=f"https://www.youtube.com/channel/{channel_id}", enabled=True)
    log.info("channel added: %s (%s, @%s)", title or channel_id, channel_id, handle)
    return yt_core._json({"ok": True, "channel": {**db.get_channel(cid), "done": 0, "last_done": None}})


# ── 채널 로고(설정-채널 탭) ── 채널 페이지 og:image(yt3) 를 작게 받아 res/channel_logos 에 둔다.
# 처음 요청 때 받고 30일마다 새로 받는다. 못 받으면 1시간 동안 다시 시도하지 않고 404 →
# 화면은 채널 이름 첫 글자로 대신한다.
CHANNEL_LOGO_DIR = os.path.join(yt_core.RES_DIR, "channel_logos")
_LOGO_MAX_AGE = 30 * 86400
_logo_fail: dict[str, float] = {}
_logo_lock = threading.Semaphore(4)   # 처음 열 때 수십 개가 한꺼번에 오므로 동시 4개까지만


def _fetch_channel_logo(channel_id: str, dest: str) -> bool:
    req = urllib.request.Request(
        f"https://www.youtube.com/channel/{channel_id}",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=12) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if not m or "yt3." not in m.group(1):
        return False
    img_url = re.sub(r"=s\d+-", "=s96-", _htmlmod.unescape(m.group(1)), count=1)
    with urllib.request.urlopen(urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"}),
                                timeout=12) as r:
        data = r.read()
    if len(data) < 200:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return True


@app.route("/channels/logo/<channel_id>")
def channel_logo(channel_id: str):
    if not re.fullmatch(r"UC[\w-]{20,24}", channel_id):
        return Response(status=404)
    dest = os.path.join(CHANNEL_LOGO_DIR, channel_id + ".jpg")
    fresh = os.path.exists(dest) and time.time() - os.path.getmtime(dest) < _LOGO_MAX_AGE
    if not fresh and time.time() - _logo_fail.get(channel_id, 0) > 3600:
        with _logo_lock:
            try:
                if not _fetch_channel_logo(channel_id, dest):
                    _logo_fail[channel_id] = time.time()
            except Exception as e:
                log.info("channel logo %s: %s", channel_id, e)
                _logo_fail[channel_id] = time.time()
    if not os.path.exists(dest):
        return Response(status=404)
    resp = send_file(dest, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.route("/channels/model-orders", methods=["PATCH"])
def channel_model_orders():
    """자동모니터의 요약 슬롯(1~5개, 모델+추론)을 저장한다. 캡처는 요약 모델을 따른다."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return yt_core._json({"error": "설정 형식이 잘못됐습니다."}, 400)
    slots = data.get("summary_slots")
    has_title = "title_model" in data or "title_effort" in data
    name_updates = {}
    for kind in db.NAME_MODEL_KINDS:
        mk, ek = f"name_{kind}_model", f"name_{kind}_effort"
        if mk in data or ek in data:
            name_updates[kind] = (mk, ek)
    if slots is None and not has_title and not name_updates:
        return yt_core._json({"error": "변경할 요약 슬롯이 없습니다."}, 400)
    if slots is not None and not llm_gateway.is_valid_summary_slots(slots, db.get_monitor_summary_slots()["slots"]):
        return yt_core._json({"error": f"요약 슬롯은 {llm_gateway.MIN_SUMMARY_SLOTS}~"
                               f"{llm_gateway.MAX_SUMMARY_SLOTS}개, 목록에 있는 모델이어야 합니다."}, 400)
    if has_title:
        cur_model, cur_effort = yt_titles._title_tr_setting()
        t_model = str(data.get("title_model") or cur_model).strip()
        t_effort = str(data.get("title_effort") or "default").strip().lower()
        unchanged = (t_model, t_effort) == (cur_model, cur_effort)
        entry = llm_gateway.llm_catalog.resolve(t_model) or {}
        # 제목 번역 러너는 Claude·GPT·Grok·Gemini(API 키) — 로컬 모델은 목록에 있어도 받지 않는다.
        if not unchanged and not (entry.get("provider") in ("claude", "codex", "grok", "gemini")
                                  and llm_gateway.llm_catalog.valid(t_model, t_effort, selectable=True)):
            return yt_core._json({"error": "제목 번역 모델·추론 조합이 목록에 없습니다."}, 400)
    name_values = {}
    if name_updates:
        saved_names = db.get_name_models()
        for kind, (mk, ek) in name_updates.items():
            cur = saved_names[kind]
            n_model = str((data.get(mk) if mk in data else cur["model"]) or "").strip()
            # 빈 모델 = 기본값(Haiku 5.5) — 추론도 비운다. 모델을 고르면 추론 기본은 'default'.
            n_effort = str(data.get(ek) or "default").strip().lower() if n_model else ""
            unchanged = (n_model, n_effort) == (cur["model"], cur["effort"])
            entry = (llm_gateway.llm_catalog.resolve(n_model) or {}) if n_model else {}
            # 웹 검색 도구가 있는 Claude·Codex CLI만 — Grok·로컬 모델은 목록에 있어도 받지 않는다.
            if n_model and not unchanged and not (
                    entry.get("provider") in yt_models._name_providers()
                    and llm_gateway.llm_catalog.valid(n_model, n_effort, selectable=True)):
                label = "판별" if kind == "detect" else "검색"
                return yt_core._json({"error": f"인명 {label} 모델·추론 조합이 목록에 없습니다(Claude·GPT만)."}, 400)
            name_values[kind] = (n_model, n_effort)
    try:
        for kind, (n_model, n_effort) in name_values.items():
            db.set_name_model(kind, n_model, n_effort)
        if has_title:
            db.set_title_model(t_model, t_effort)
        if slots is not None:
            db.set_monitor_summary_slots(slots)
        return yt_core._json({"ok": True, **yt_models._monitor_model_payload()})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


@app.route("/channels/<int:cid>", methods=["PATCH"])
def channel_toggle(cid: int):
    """채널 설정 변경 — 자동수집 ON/OFF(enabled), 지식증류 대상(distill).

    보낸 키만 반영한다(둘 중 하나만 바꿔도 다른 값이 덮이지 않게).
    """
    data = request.get_json(force=True) or {}
    out, touched = {"ok": True}, False
    if "enabled" in data:
        if not db.set_channel_enabled(cid, bool(data["enabled"])):
            return yt_core._json({"error": "채널을 찾을 수 없습니다."}, 404)
        out["enabled"] = bool(data["enabled"]); touched = True
    if "distill" in data:
        if not db.set_channel_distill(cid, bool(data["distill"])):
            return yt_core._json({"error": "채널을 찾을 수 없습니다."}, 404)
        out["distill"] = bool(data["distill"]); touched = True
    if "capture" in data:
        if not db.set_channel_capture(cid, bool(data["capture"])):
            return yt_core._json({"error": "채널을 찾을 수 없습니다."}, 404)
        out["capture"] = bool(data["capture"]); touched = True
    if not touched:
        return yt_core._json({"error": "변경할 항목이 없습니다(enabled/distill)."}, 400)
    return yt_core._json(out)


@app.route("/channels/distill-excluded")
def channels_distill_excluded():
    """증류 제외 채널명 목록 — hermes 증류 파이프라인이 조회해 문서 로딩 단계에서 거른다."""
    try:
        return yt_core._json({"names": db.distill_excluded_names()})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


@app.route("/distill/policy")
def distill_policy():
    """증류 정책 전체 — 채널 제외 목록 + 영상 단위 오버라이드(오버라이드가 우선)."""
    try:
        return yt_core._json({"excluded_channels": db.distill_excluded_names(),
                      "overrides": db.distill_overrides()})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)
