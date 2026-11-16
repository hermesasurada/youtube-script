#!/usr/bin/env python3
"""유튜브 채널 자동 모니터 — 신규 영상 감지 → 전사/요약/캡처 자동화.

감지(폴러)와 처리(드레이너)를 한 프로세스에서 수행하되, 파일 락으로 단일 인스턴스를
보장한다. 20분 간격 launchd/cron로 실행. 파이프라인은 상시 구동 중인 Flask 서버
(기본 127.0.0.1:5001)의 HTTP 엔드포인트를 그대로 호출해 재사용한다(브라우저와 동일 경로).

흐름:
  1) 활성 채널마다 RSS(feeds/videos.xml)로 최근 업로드 목록 수집
  2) 최초 폴이면 현재 피드를 baseline(seen)로 기록만 하고 처리 안 함(백필 방지)
  3) 이후엔 이력(items)·큐에 없는 신규만 메타 확인 → 필터(≤3분·라이브·Shorts 제외) → 큐 적재
  4) 큐 drain 게이트: Claude OK 또는 Grok 폴백 준비 시 진행
     → /start→/result→/summarize(Claude→Grok)→/keyframes(Claude 비전)
  5) 건별 완료/실패 알림(Telegram, 자격증명 있을 때)

사용:
  python channel_monitor.py            # 폴 + 드레인(기본)
  python channel_monitor.py --dry-run  # 감지/필터만 출력(DB·처리 변경 없음)
  python channel_monitor.py --poll-only # 감지·적재만(드레인 안 함)
  python channel_monitor.py --drain-only # 큐 처리만(감지 안 함)
"""
from __future__ import annotations

from datetime import datetime
import argparse
import json
import os
import sys
import time
import fcntl

import requests

import db
import llm_gateway

import cm_base
import cm_detect
import cm_health
import cm_notify


def log(msg: str) -> None:
    print(msg, flush=True)


# ── 처리(파이프라인 = 서버 HTTP 재사용) ────────────────────────────────
def _get_prompt() -> str:
    try:
        return requests.get(f"{cm_base.BASE}/prompt", timeout=15).json().get("prompt", "")
    except Exception:
        return ""


def process_video(v: dict, prompt: str, *, skip_claude: bool = False,
                  summary_models=None, capture_models=None, summary_reasoning=None,
                  summary_slots=None) -> dict:
    """전사→요약→캡처를 서버 엔드포인트로 순차 수행. 완료 요약 파일 경로 반환.

    summary_slots: 이번 작업의 시작 슬롯부터 돈 요약 슬롯 목록(모델+추론). 있으면 우선.
    summary_models/summary_reasoning: 옛 계열 순서 호출 호환. capture_models를 주지 않으면
    서버가 '요약한 모델 → 요약 슬롯' 순으로 캡처한다(캡처 전용 설정은 없다).
    skip_claude는 구버전 호출 호환용이며 명시 순서가 있으면 사용하지 않는다.
    """
    url = v["url"]
    txt_path = v.get("txt_path")
    # 전사는 디스크와 items에 저장됐지만 monitor가 txt_path를 큐에 기록하기 전에
    # 재시작됐을 수 있다. 같은 영상의 완성 전사본을 찾아 불필요한 재다운로드와
    # Whisper 재실행을 피한다.
    if (not txt_path or not os.path.isfile(txt_path)) and v.get("yt_id"):
        saved = db.get_item_by_yt_id(v["yt_id"])
        saved_path = (saved or {}).get("md_path")
        if saved_path and os.path.isfile(saved_path):
            txt_path = saved_path
            db.queue_set_txt_path(v["id"], txt_path)
            log(f"[resume] DB에서 완성 전사 복구: {os.path.basename(txt_path)}")
    if not txt_path or not os.path.isfile(txt_path):
        payload = {"source": "url", "url": url}
        if v.get("start_sec"):
            payload["start_sec"] = int(v["start_sec"])   # 구간 전사(시작 시각 지정)
        r = requests.post(f"{cm_base.BASE}/start", json=payload, timeout=30).json()
        job_id = r.get("job_id")
        if not job_id:
            raise RuntimeError(r.get("error", "start 실패"))

        txt_path = None
        deadline = time.time() + cm_base.MAX_JOB_SEC
        while time.time() < deadline:
            time.sleep(cm_base.POLL_SEC)
            res = requests.get(f"{cm_base.BASE}/result/{job_id}", timeout=30).json()
            # 서버가 재시작되면 잡 상태가 소실돼 404("Not found")가 온다 — status가
            # 영영 안 나오므로 여기서 끊지 않으면 MAX_JOB_SEC(5h)까지 헛폴링한다.
            if res.get("error") == "Not found":
                raise RuntimeError("전사 잡 소실(서버 재시작) — 재시도 필요")
            st = res.get("status")
            if st == "done":
                txt_path = res.get("txt_path")
                break
            if st in ("error", "cancelled"):
                detail = (res.get("error_message") or "").strip()
                raise RuntimeError(f"전사 {st}" + (f": {detail[:160]}" if detail else ""))
        if not txt_path:
            raise RuntimeError("전사 시간초과")
        db.queue_set_txt_path(v["id"], txt_path)
    else:
        log(f"[resume] 기존 전사 재사용: {os.path.basename(txt_path)}")

    # 요약(SSE 스트림 소비 → 서버가 성공 시에만 디스크에 저장, 실패 시 event:error 방출)
    summary_err = None
    await_err = False
    sum_body = {"txt_path": txt_path, "prompt": prompt}
    if summary_slots is not None:
        sum_body["slots"] = summary_slots
    elif summary_models is not None:
        sum_body["models"] = llm_gateway.normalize_model_order(summary_models)
        if summary_reasoning is not None:
            sum_body["reasoning_levels"] = llm_gateway.normalize_reasoning_levels(
                summary_reasoning
            )
    elif skip_claude:
        sum_body["skip_claude"] = True
        sum_body["mode"] = "grok"
    with requests.post(f"{cm_base.BASE}/summarize", json=sum_body,
                       stream=True, timeout=(30, cm_base.SUMM_TIMEOUT)) as sr:
        sr.raise_for_status()
        for raw in sr.iter_lines(decode_unicode=True):
            if not raw:
                continue
            raw = raw.strip()
            if raw == "event: error":
                await_err = True
            elif await_err and raw.startswith("data:"):
                payload = raw[5:].strip()
                try:
                    summary_err = json.loads(payload) if payload else "요약 실패"
                except Exception:
                    summary_err = payload or "요약 실패"
                await_err = False
    if summary_err:
        # 모든 지정 모델이 소진됐거나 systemic 장애면 큐를 멈추고 다음 주기에 재시도한다.
        if "모든 요약 모델 실패" in str(summary_err) or cm_health._is_systemic(str(summary_err)):
            raise cm_health.ClaudeUnavailable(f"요약: {summary_err}")
        raise RuntimeError(f"요약 실패: {summary_err}")

    # 캡처(동기) — 요약은 이미 저장됨. 실패해도 항목은 완료(요약이 본체), systemic 여부만 표시
    kf_note, kf_systemic, kf_permanent = "", False, False
    # 캡처 포함 여부: 영상 단위 지정 > 채널 설정 > 기본 포함(큐 밖 호출은 종전대로)
    if not db.capture_enabled_for(v):
        log("[kf] 캡처 제외 설정 — 건너뜀")
        return {"txt_path": txt_path, "kf_note": "", "kf_systemic": False,
                "kf_permanent": False, "kf_skipped": True}
    try:
        kf_body = {"txt_path": txt_path, "url": url}
        if capture_models is not None:
            kf_body["models"] = list(capture_models)       # 구체 모델 목록(서버가 정규화)
        elif skip_claude:
            kf_body["skip_claude"] = True
            kf_body["mode"] = "grok"
        kf = requests.post(f"{cm_base.BASE}/keyframes", json=kf_body, timeout=cm_base.KF_TIMEOUT).json()
        if not kf.get("ok"):
            kf_note = str(kf.get("reason") or "실패")
            emsg = str(kf.get("error") or "")
            # 비전 호출 실패 + 한도 신호일 때만 systemic (다운로드 403 등과 구분)
            kf_systemic = kf_note == "vision_failed" and cm_health._is_systemic(emsg)
            # 멤버십 전용 전환·삭제·비공개는 다시 받아도 결과가 같다 → 재시도 대상에서 제외.
            # (수집 당시엔 공개였다가 나중에 멤버십으로 돌리는 채널이 있어 캡처만 실패한다)
            kf_permanent = bool(cm_detect._META_PERMANENT_RE.search(emsg))
            if kf_permanent:
                log(f"[kf] 캡처 불가(영구 사유, 재시도 안 함): {emsg[:110]}")
            else:
                log(f"[kf] 캡처 스킵/실패: {kf_note} {emsg[:120]}")
    except Exception as e:
        kf_note = "오류"
        log(f"[kf] 캡처 오류(요약은 유지): {e}")
    return {"txt_path": txt_path, "kf_note": kf_note, "kf_systemic": kf_systemic,
            "kf_permanent": kf_permanent}


def process_capture_only(txt_path: str, url: str, *, capture_models=None) -> dict:
    """요약이 이미 끝난 항목의 캡처(키프레임)만 재생성. 전사·요약은 스킵.

    반환: /keyframes 원 응답 dict — {"ok":True,"n_frames":N} 또는 {"ok":False,"reason","error"}.
    """
    body = {"txt_path": txt_path, "url": url}
    if capture_models is not None:
        body["models"] = list(capture_models)
    return requests.post(f"{cm_base.BASE}/keyframes", json=body, timeout=cm_base.KF_TIMEOUT).json()


def drain() -> None:
    """주기당 '본편 1건 + 캡처 재시도 1건'까지 처리한다.

    본편(pending: 전사→요약→캡처)과 캡처 재시도(kf_retry: 캡처만 재생성)는
    별도 큐로 서고, 각자 들어온 순서대로 한 건씩만 나온다. 캡처 재시도가
    본편 처리 순서를 차지하지 않으면서도, 캡처 역시 유튜브에서 영상을 새로
    받는 작업이라 주기당 1건으로 제한된다. deferred는 due가 되면 poll 단계에서
    pending으로 복귀한다.
    """
    _drain_main_one()
    _drain_kf_retry_one()


def _live_status(url: str) -> str:
    """yt-dlp live_status(is_live / post_live / was_live / ""). 조회 실패면 ""."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as y:
            return str(y.extract_info(url, download=False).get("live_status") or "")
    except Exception:
        return ""


def _age_hours(stamp: str | None) -> float:
    try:
        return (datetime.now() - datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except Exception:
        return 0.0


_KF_POST_LIVE_WAIT_S = 3600      # VOD 처리 대기 간격
_KF_POST_LIVE_MAX_H  = 48        # 이 시간이 지나도 post_live면 그냥 시도한다


def _drain_kf_retry_one() -> None:
    """캡처 재시도 큐에서 1건 — 본편과 별도 슬롯(주기당 본편 1 + 캡처 1).

    캡처도 유튜브에서 저해상도 영상을 새로 받으므로 무제한으로 풀 수는 없다.
    캡처만 1회 재생성(전사·요약 스킵)하고 성공·실패 무관하게 종료한다.
    """
    v = db.queue_claim_kf_retry()
    if not v:
        return
    title = v["title"] or v["yt_id"]
    head = cm_notify._notify_head(v, title)
    txt_path = v.get("txt_path")
    if not txt_path:
        db.queue_set_status(v["id"], "done", reason="캡처 재시도 불가(전사경로 없음)")
        return
    # 라이브 종료 직후(post_live)는 VOD 처리가 끝날 때까지 영상 포맷이 비거나 DASH
    # 세그먼트만 아주 느리게 내려온다(2026-09-03 집코노미 라이브: 종료 7분 뒤 캡처가
    # '빈 파일'로 실패, 40분 뒤에도 360p 10MB에 4분). 단 한 번뿐인 재시도를 지금
    # 태우지 말고 한 시간 뒤로 미룬다. 48시간이 지나도 그대로면 그냥 시도한다.
    if _live_status(v["url"]) == "post_live" and _age_hours(v.get("added_at")) < _KF_POST_LIVE_MAX_H:
        db.queue_defer_kf_retry(v["id"], _KF_POST_LIVE_WAIT_S, "VOD 처리 대기(라이브 종료 직후)")
        log(f"[kf-retry] post_live → {_KF_POST_LIVE_WAIT_S // 60}분 뒤 재시도: {title}")
        return
    log(f"[kf-retry] 캡처 재시도: {title}")
    try:
        kf = process_capture_only(txt_path, v["url"])
        if kf.get("ok"):
            db.queue_set_status(v["id"], "done", reason="캡처 재시도 성공")
            cm_notify.notify(f"📸 캡처 재시도 성공\n{head}\n{v['url']}")
            log(f"[kf-retry] 성공: {title} (n_frames={kf.get('n_frames')})")
        else:
            rz = str(kf.get("reason") or "실패")
            emsg = str(kf.get("error") or "")
            # 재시도 사이에 멤버십 전용으로 바뀌었을 수 있다 — 사유를 구분해 알린다
            if cm_detect._META_PERMANENT_RE.search(emsg):
                db.queue_set_status(v["id"], "done", reason="캡처 불가(멤버십/비공개 전환)")
                log(f"[kf-retry] 캡처 불가(영구 사유): {emsg[:110]}")
                cm_notify.notify(f"ℹ️ 캡처 불가 — 멤버십/비공개 전환된 영상\n{head}\n{v['url']}")
            else:
                db.queue_set_status(v["id"], "done", reason=f"캡처 재시도 실패(포기): {rz}")
                log(f"[kf-retry] 재시도 실패 → 캡처 포기: {rz} {emsg[:120]}")
                cm_notify.notify(f"⚠️ 캡처 재시도 실패(포기)\n{head}\n{rz}\n{v['url']}")
    except Exception as e:
        db.queue_set_status(v["id"], "done", reason=f"캡처 재시도 오류(포기): {str(e)[:80]}")
        log(f"[kf-retry] 재시도 오류 → 캡처 포기: {e}")
        cm_notify.notify(f"⚠️ 캡처 재시도 오류(포기)\n{head}\n{e}\n{v['url']}")
    return


def _drain_main_one() -> None:
    """본편 큐(pending)에서 1건 — 전사→요약→캡처 전체 파이프라인."""
    v = db.queue_claim_one()
    if not v:
        return
    title = v["title"] or v["yt_id"]
    head = cm_notify._notify_head(v, title)
    summary_round = db.reserve_monitor_summary_slots()
    slots = summary_round["slots"]
    primary = slots[0]["model"]
    # 게이트는 계열 단위(Claude 인증·CLI 존재)라, 슬롯 계열 순서로 물어 처음 쓸 수 있는
    # 계열을 받고 그 계열의 첫 슬롯부터 넘긴다(앞의 쓸 수 없는 슬롯은 건너뛴다).
    families = []
    for slot in slots:
        fam = llm_gateway.model_family(slot["model"])
        if fam not in families:
            families.append(fam)
    proceed, mode, detail = cm_health.summarizer_gate(families)
    if not proceed:
        # 요약 경로가 없으면 시도 자체를 안 한 것 — 예산을 태우지 않고 되돌린다.
        db.queue_unclaim(v["id"])
        cm_notify.notify("⛔ 요약 경로 없음 — 자동 처리 보류(다음 주기 재시도)\n"
               f"사유: {detail}")
        return
    start = next(i for i, x in enumerate(slots) if llm_gateway.model_family(x["model"]) == mode)
    active_slots = slots[start:]
    if detail:
        cm_notify.notify(f"⚠️ 이번 요약 순번 {primary} 사용 불가 — "
               f"{active_slots[0]['model']}부터 순환 폴백\n{detail}")

    log(f"[drain] 처리 시작: {title} (round={primary}, start={active_slots[0]['model']})")
    try:
        res = process_video(
            v, _get_prompt(),
            summary_slots=active_slots,
        )
        kf_note = res.get("kf_note")
        # 큐에서 증류를 지정했으면(NULL 아님) 완성된 이력에 영상 단위 오버라이드로 이관.
        # 캡처 재시도로 빠져도 전사·요약은 끝난 상태라 여기서 한 번에 처리한다.
        if v.get("distill") is not None and res.get("txt_path"):
            try:
                db.set_item_distill(res["txt_path"], bool(v["distill"]))
                log(f"[drain] 증류 지정 이관: {'포함' if v['distill'] else '제외'} — {title}")
            except Exception as e:
                log(f"[drain] 증류 지정 이관 실패(무시): {e}")
        # 캡처만 일시 실패(systemic·영구사유 아님) → 큐 뒤에 kf_retry로 다시 세운다
        retryable = (bool(kf_note) and not res.get("kf_systemic")
                     and not res.get("kf_permanent"))
        if retryable and res.get("txt_path"):
            db.queue_mark_kf_retry(v["id"], res["txt_path"], reason=f"캡처 실패: {kf_note}")
            cm_notify.notify(f"✅ 자동 요약 완료 — 캡처 실패, 다음 주기 재시도 예약\n{head}\n{v['url']}")
            log(f"[drain] 캡처 재시도 예약: {title} ({kf_note})")
        else:
            db.queue_set_status(v["id"], "done")
            suffix = "(캡처 실패)" if kf_note else ("(캡처 제외)" if res.get("kf_skipped") else "")
            cm_notify.notify(f"✅ 자동 요약 완료{suffix}\n{head}\n{v['url']}")
    except cm_health.ClaudeUnavailable as e:
        # 지정된 요약 모델이 모두 실패한 경우(폴백까지 소진)
        db.queue_defer(
            v["id"], "요약 경로 재시도 대기", error_kind="llm_unavailable",
            retry_after_seconds=cm_base._retry_delay(v.get("attempt_count")),
            max_attempts=cm_base.MAX_PIPELINE_ATTEMPTS,
        )
        cm_notify.notify(f"⛔ 요약 실패(모든 폴백 소진) — 다음 주기 재시도\n{head}\n{e}")
    except Exception as e:
        reason = str(e)[:160]
        # 라이브·프리미어 예정은 실패가 아니다 — 재시도 예산을 태우지 않고
        # 방송 시작을 기다리는 라이브 재확인 경로로 보낸다(사용자 알림도 구분).
        # 자동 감지분이 그사이 라이브로 확인되면 기다리지 않고 건너뛴다(2026-11-03 사용자 지시)
        if v.get("channel_id") != "manual" and cm_detect._LIVE_EVENT_RE.search(reason):
            db.queue_set_status(v["id"], "skipped", f"live_skip:{reason}"[:160])
            log(f"[drain] 라이브 → 건너뜀: {reason[:110]}")
            return
        if cm_detect._LIVE_PENDING_RE.search(reason):
            db.queue_defer(
                v["id"], reason, error_kind="live_upcoming",
                retry_after_seconds=3600, max_attempts=None,
            )
            db.queue_bump_attempt(v["id"], -1)   # claim이 올린 시도 횟수 되돌림
            cm_notify.notify(
                f"📡 라이브/프리미어 예정 영상 — 방송 후 자동 처리 예정\n"
                f"{head}\n{reason}\n{v['url']}"
            )
            return
        # 비공개 전환·삭제·멤버십 전용은 기다린다고 풀리지 않는다. 재시도 예산을
        # 태우고 같은 알림을 5번 보내는 대신 즉시 종료한다(캡처 경로에는 이미 있던
        # 판정을 본편에도 적용 — 2026-08-27: All-In 에피소드가 비공개로 바뀌며 노출).
        if cm_detect._META_PERMANENT_RE.search(reason):
            db.queue_set_status(v["id"], "skipped", reason)
            log(f"[drain] 처리 불가(영구 사유) → 재시도 안 함: {reason[:110]}")
            cm_notify.notify(
                f"ℹ️ 처리 불가 — 비공개·삭제·멤버십 전환 (재시도 안 함)\n"
                f"{head}\n{reason}\n{v['url']}"
            )
            return
        # 실패 문구가 직전과 글자 하나까지 같으면 기다린다고 달라지지 않는다. 영구
        # 사유가 메시지에 드러나지 않는 경우를 잡는 그물이다 — 2026-09-01 멤버십
        # 전용 영상이 비로그인에는 무음 트랙으로 내려와, 메타·다운로드는 멀쩡히
        # 성공하고 전사만 두 번 다 '161자 붕괴'로 끝났다(62MB 다운로드 + 1시간
        # 오디오 whisper 2회를 매 시도마다 반복). 자연 실패는 자·초·길이가 조금씩
        # 달라 완전 일치가 드물다.
        prev = (v.get("last_fail_reason") or "").strip()
        if (prev and prev == reason.strip() and (v.get("attempt_count") or 0) >= 1
                and not cm_detect._SERVER_RESTART_RE.search(reason)):
            db.queue_set_status(v["id"], "skipped", f"동일 실패 반복 — 재시도 중단: {reason}")
            log(f"[drain] 동일 실패 반복 → 재시도 중단: {reason[:110]}")
            cm_notify.notify(
                f"ℹ️ 동일 실패 반복 — 재시도 중단\n"
                f"{head}\n{reason}\n{v['url']}"
            )
            return
        state = db.queue_defer(
            v["id"], reason, error_kind="pipeline_transient",
            retry_after_seconds=cm_base._retry_delay(v.get("attempt_count")),
            max_attempts=cm_base.MAX_PIPELINE_ATTEMPTS,
        )
        cm_notify.notify(
            f"⚠️ 자동 처리 실패 — {'재시도 예약' if state == 'deferred' else '재시도 소진'}\n"
            f"{head}\n{reason}\n{v['url']}"
        )


# ── 단일 인스턴스 락 + 진입점 ──────────────────────────────────────────
def _acquire_lock():
    os.makedirs(os.path.dirname(cm_base.LOCK_PATH), exist_ok=True)
    fp = open(cm_base.LOCK_PATH, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    fp.write(str(os.getpid()))
    fp.flush()
    return fp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="감지/필터만 출력(변경 없음)")
    ap.add_argument("--poll-only", action="store_true", help="감지·적재만")
    ap.add_argument("--drain-only", action="store_true", help="큐 처리만")
    ap.add_argument("--announce", action="store_true",
                    help="아웃박스의 알림만 stdout으로 배출(hermes 크론 shim 전용)")
    args = ap.parse_args()

    if args.announce:                 # hermes 크론 경로: 순수 알림 배출(DB/락 불필요)
        cm_notify.announce()
        return 0

    db.init()

    if args.dry_run:
        n = cm_detect.poll_channels(dry=True)
        log(f"[dry-run] 처리 대상(신규) {n}건 — 실제 적재/처리는 안 함")
        log(f"[dry-run] 현재 큐: {db.queue_counts()}")
        return 0

    lock = _acquire_lock()
    if lock is None:
        log("[lock] 이미 실행 중 — 종료(다음 주기에 이어서 처리)")
        return 0

    try:
        # 이 락을 잡은 프로세스만 watch_queue를 claim한다. 따라서 락 획득 시점에
        # 남아 있는 processing은 이전 프로세스/재부팅으로 끊긴 고아 작업이다.
        # stale 대기 없이 즉시 pending으로 되돌려 이번 주기에 이어서 처리한다.
        recovered = db.queue_recover_interrupted()
        if recovered:
            log(f"[recover] 중단된 processing {recovered}건 → pending (즉시 재개)")
        if not args.drain_only:
            q = cm_detect.poll_channels()
            log(f"[poll] 신규 적재 {q}건, 큐: {db.queue_counts()}")
        if not args.poll_only:
            drain()
            log(f"[drain] 완료, 큐: {db.queue_counts()}")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
