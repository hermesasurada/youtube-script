"""감시기 알림 — 텔레그램 전송과 신규 영상 안내(`notify`·`announce`).

전송 실패는 처리를 멈추지 않는다(2026-11-17 channel_monitor에서 분리).
"""
from __future__ import annotations

import os
import fcntl
import requests
import db

import cm_base

def log(msg: str) -> None:
    print(msg, flush=True)


def _notify_head(v: dict, title: str) -> str:
    """알림 상단 블록: 채널명이 있으면 '📺 채널명\\n제목', 없으면 제목만."""
    chan = db.channel_name_by_cid(v.get("channel_id"))
    return f"📺 {chan}\n{title}" if chan else title


def notify(text: str) -> None:
    """알림 적재 — launchd 로그(stdout) + 아웃박스(hermes 크론 shim이 --announce로 stdout 배달).

    무거운 폴/드레인은 launchd가, 알림 배달은 hermes 크론이 담당(중앙 텔레그램, 토큰 불필요).
    자격증명이 직접 설정돼 있으면(youtube-monitor.env) 텔레그램 직발송도 병행(옵션).
    """
    log(text)
    try:
        os.makedirs(os.path.dirname(cm_base.OUTBOX), exist_ok=True)
        with open(cm_base.OUTBOX, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(text.rstrip() + "\n\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        log(f"[notify] 아웃박스 기록 실패: {e}")
    if cm_base.TG_TOKEN and cm_base.TG_CHAT:   # 선택: 직접 발송(기본 경로는 hermes 크론)
        try:
            requests.post(
                f"https://api.telegram.org/bot{cm_base.TG_TOKEN}/sendMessage",
                json={"chat_id": cm_base.TG_CHAT, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:
            log(f"[notify] telegram 실패: {e}")


def announce() -> int:
    """아웃박스에 쌓인 알림을 stdout으로 출력(→ hermes 크론이 텔레그램 배달) 후 비운다.

    hermes 크론 shim에서 `--announce`로 호출. 새 알림이 없으면 무음(아무것도 출력 안 함).
    launchd 드레이너의 append와 flock으로 경쟁 안전.
    """
    try:
        f = open(cm_base.OUTBOX, "a+", encoding="utf-8")
    except OSError:
        return 0
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read().strip()
        f.seek(0)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
    if content:
        print(content, flush=True)   # 이 stdout만 텔레그램으로 배달됨
    return 1 if content else 0
