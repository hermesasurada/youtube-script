"""처리 큐 도우미 — 영상 ID·시작 시각 추출, oEmbed, 모니터 드레인 기동·ETA.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import db
import logging

log = logging.getLogger("yt-script")

# ── 채널 자동 모니터링(로컬 전용 — 원격은 before_request에서 차단) ──────
# ── 처리 큐 (단일 파이프라인 관리) ────────────────────────────────────
# 수동 전사 요청도 자동 모니터와 같은 watch_queue에 줄을 선다(유휴면 즉시, 이후 20분에 1건).

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


def _extract_yt_id(url: str) -> str | None:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


_T_PARAM_RE = re.compile(r"[?&#](?:t|start)=([0-9hms]+)", re.I)
_T_HMS_RE   = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$", re.I)


def _extract_start_sec(url: str) -> int:
    """공유 링크의 시작 시각(`?t=266`, `?t=4m26s`, `&start=90`) → 초. 없으면 0."""
    m = _T_PARAM_RE.search(url or "")
    if not m:
        return 0
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    hms = _T_HMS_RE.match(raw)
    if not hms or not any(hms.groups()):
        return 0
    h, mi, se = (int(g or 0) for g in hms.groups())
    return h * 3600 + mi * 60 + se


def _oembed_meta(url: str) -> dict:
    """oEmbed로 제목·채널명을 가볍게 조회(미디어 서버를 건드리지 않아 차단 위험 없음)."""
    try:
        with urllib.request.urlopen(
                "https://www.youtube.com/oembed?format=json&url="
                + urllib.parse.quote(url, safe=""), timeout=8) as r:
            d = json.loads(r.read())
            return {"title": (d.get("title") or "").strip(),
                    "channel": (d.get("author_name") or "").strip()}
    except Exception:
        return {}


_QUEUE_CYCLE_SEC = int(os.environ.get("QUEUE_CYCLE_SEC", "1200"))
_MONITOR_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_monitor.py")
_MONITOR_LOG_DIR = os.path.expanduser("~/Library/Logs/hermes")


def _queue_idle_enough(now: float | None = None) -> bool:
    """최근 주기 동안 실제 처리가 없었고, 지금 돌아가는 작업도 없으면 유휴."""
    if db.queue_has_processing():
        return False
    last = db.last_queue_activity_epoch()
    if last is None:
        return True
    return (now if now is not None else time.time()) - last >= _QUEUE_CYCLE_SEC


def _next_drain_epoch(now: float) -> float:
    """다음 본편/캡처 슬롯이 열릴 epoch초."""
    last = db.last_queue_activity_epoch()
    if db.queue_has_processing():
        return max(now, (last or now) + _QUEUE_CYCLE_SEC)
    if _queue_idle_enough(now):
        return now
    return (last or now) + _QUEUE_CYCLE_SEC


def _spawn_drain() -> bool:
    """launchd를 기다리지 않고 drain 한 사이클을 띄운다. 락이 잡혀 있으면 자식이 바로 종료."""
    try:
        os.makedirs(_MONITOR_LOG_DIR, exist_ok=True)
        out_path = os.path.join(_MONITOR_LOG_DIR, "youtube-monitor.out.log")
        err_path = os.path.join(_MONITOR_LOG_DIR, "youtube-monitor.err.log")
        with open(out_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
            # -u: 자식의 stdout을 버퍼링하지 않는다. 파일로 리다이렉트된 stdout은 블록
            # 버퍼링이라, 자식이 갑자기 죽으면 '[drain] 처리 시작' 같은 줄이 통째로
            # 사라져 큐가 왜 processing으로 남았는지 추적할 수 없었다(2026-09-03
            # GOOGLE IS BACK: 전사는 끝났는데 요약 호출 없이 고아가 됨, 로그 0줄).
            subprocess.Popen(
                [sys.executable, "-u", _MONITOR_PY, "--drain-only"],
                cwd=os.path.dirname(_MONITOR_PY),
                stdout=out,
                stderr=err,
                start_new_session=True,
                env=os.environ.copy(),
            )
        log.info("idle queue — drain started immediately")
        return True
    except Exception as e:
        log.warning("idle drain spawn failed: %s", e)
        return False


def _kick_drain_if_idle() -> bool:
    if not _queue_idle_enough():
        return False
    waiting = db.queue_overview().get("waiting") or []
    if not any(v.get("status") in ("pending", "kf_retry") for v in waiting):
        return False
    return _spawn_drain()


def _attach_queue_eta(overview: dict) -> dict:
    """대기 항목에 예정 시작 시각(eta, epoch초)을 붙인다.

    실제 처리가 주기 이상 없었으면 첫 슬롯은 지금이다(수동 추가 즉시 시작).
    본편(pending)과 캡처(kf_retry)는 슬롯이 분리돼 있어 각자 순번 × 주기로
    계산한다. deferred는 복귀 시점이 due에 달려 있어 예정을 확정할 수 없으므로
    붙이지 않는다(재시도 시각만 표시).
    """
    now = time.time()
    base = _next_drain_epoch(now)

    def _epoch(ts: str) -> float:
        try:
            return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return float("inf")

    waiting = overview.get("waiting", [])
    # 본편 슬롯 시뮬레이션: 주기마다 '그 시점까지 줄에 선 항목'(pending +
    # 복귀 시각이 지난 deferred) 중 position이 가장 앞선 것이 처리된다.
    # deferred는 due가 돼야 복귀하므로, 단순 순번 곱으로는 순서가 어긋난다
    # (재시도 간격과 처리 주기가 같은 박자라 자주 겹친다).
    main = [v for v in waiting if v.get("status") in ("pending", "deferred")]
    remain = list(main)
    t = base
    # ready가 없는 주기(예: 재시도 예약이 발화보다 몇 초 늦은 경우)는 배정 없이
    # 지나가므로, 항목 수가 아니라 '남은 항목이 있는 동안' 주기를 소비한다.
    for _ in range(len(main) * 4 + 8):
        if not remain:
            break
        ready = [v for v in remain
                 if v["status"] == "pending" or _epoch(v.get("next_retry_at") or "") <= t]
        if ready:
            pick = min(ready, key=lambda v: (v.get("position") or v["id"], v["id"]))
            pick["eta"] = t
            remain.remove(pick)
        t += _QUEUE_CYCLE_SEC
    n_kf = 0
    for v in waiting:
        if v.get("status") == "kf_retry":
            v["eta"] = base + n_kf * _QUEUE_CYCLE_SEC
            n_kf += 1
    return overview
