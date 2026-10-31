#!/usr/bin/env python3
"""최근 N일 전사 영상의 오디오를 다시 받아 보관함(res/audio_archive)에 채운다.

2026-10-31 사용자 지시: 오디오 90일 보관(app._archive_audio)을 도입하기 전 일주일 동안 전사한 영상도 오디오를
남겨 둔다(최근 것부터). 차단당하지 않게 한 편씩 받고 사이에 무작위 간격(기본 60~150초)을 둔다. 대체 클라이언트
(tv_simply·web_embedded — 서비스의 403 3차 단계)로 먼저 받고, 403이면 75초 뒤 기본 클라이언트로 다시, 그래도
차단이면 30분 쉬고 한 번 더 시도한다. 차단이 연달아 3편이면 멈춘다(다시 돌리면 이미 받은 것은 건너뛴다).

파일은 서비스와 같은 규칙으로 둔다: res/audio_archive/<전사일 YYYYMMDD>/<stem>.mp3. 보관 기한은 전사일
기준이므로 파일 시각을 stem 앞 12자리(전사 시작 시각)로 맞춘다 — 서비스의 90일 정리가 그대로 지운다.
구간 전사(_from<N>s) 영상도 전체 오디오를 받는다.

    .venv/bin/python tools/backfill_audio_archive.py --days 7 [--dry-run] [--gap 60 150]
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta

import yt_dlp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "index.db")
ARCHIVE = os.environ.get("AUDIO_ARCHIVE_DIR", os.path.join(ROOT, "res", "audio_archive"))
BLOCK_HINTS = ("403", "429", "sign in to confirm", "not a bot", "too many requests")


def _ffmpeg_dir() -> str:
    path = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    return os.path.dirname(path)


def targets(days: int) -> list[sqlite3.Row]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT date, stem, webpage_url, duration FROM items WHERE date>=? AND webpage_url LIKE '%youtu%' "
            "ORDER BY date DESC, stem DESC", (since,)).fetchall()
    finally:
        conn.close()


def dest_of(row: sqlite3.Row) -> str:
    return os.path.join(ARCHIVE, row["date"], row["stem"] + ".mp3")


def download(url: str, dest: str) -> None:
    """대체 클라이언트(tv_simply·web_embedded)로 먼저 받고, 403이면 75초 뒤 기본 클라이언트로 한 번 더.
    첫 실행에서 기본 클라이언트는 영상마다 403을 두 번 맞은 뒤에야 대체 클라이언트로 받혀서(서비스도 최근 1주
    403 재시도 205회), 일괄 받기에서는 헛요청을 줄이려고 순서를 바꿨다."""
    try:
        return _download_once(url, dest, alt_client=True)
    except Exception as e:  # noqa: BLE001
        if "403" not in str(e):
            raise
        time.sleep(75)
        return _download_once(url, dest, alt_client=False)


def _download_once(url: str, dest: str, *, alt_client: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="yt-audio-") as tmp:
        base = os.path.join(tmp, "a")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": base,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "ffmpeg_location": _ffmpeg_dir(),
            "quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 60, "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads": 1,
            "sleep_interval_requests": 1,
        }
        if alt_client:
            opts["extractor_args"] = {"youtube": {"player_client": ["tv_simply", "web_embedded"]}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        got = base + ".mp3"
        if not os.path.exists(got):
            raise RuntimeError("mp3가 만들어지지 않았습니다")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(got, dest)


def stamp_of(stem: str) -> float | None:
    try:
        return datetime.strptime(stem[:12], "%Y%m%d%H%M").timestamp()
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--gap", type=int, nargs=2, default=(60, 150), metavar=("MIN", "MAX"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = targets(a.days)
    todo = [r for r in rows if not os.path.exists(dest_of(r))]
    hours = sum((r["duration"] or 0) for r in todo) / 3600
    print(f"대상 {len(rows)}편 · 받을 것 {len(todo)}편 · {hours:.1f}시간 분량", flush=True)
    if a.dry_run:
        return 0

    ok = fail = blocked_streak = 0
    for i, r in enumerate(todo, 1):
        dest = dest_of(r)
        for attempt in (1, 2):
            try:
                t0 = time.time()
                download(r["webpage_url"], dest)
                st = stamp_of(r["stem"])
                if st:
                    os.utime(dest, (st, st))
                ok += 1
                blocked_streak = 0
                print(f"[{i}/{len(todo)}] 받음 {r['stem'][:60]} ({os.path.getsize(dest) / 1048576:.0f}MB, "
                      f"{time.time() - t0:.0f}초)", flush=True)
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if any(h in msg.lower() for h in BLOCK_HINTS):
                    blocked_streak += 1
                    print(f"[{i}/{len(todo)}] 차단 응답({blocked_streak}회 연속): {msg[:160]}", flush=True)
                    if blocked_streak >= 3:
                        print("차단 응답이 3번 연달아 나와 멈춥니다 — 나중에 다시 돌리면 이어서 받습니다.", flush=True)
                        return 2
                    if attempt == 1:
                        time.sleep(1800)
                        continue
                else:
                    print(f"[{i}/{len(todo)}] 실패(건너뜀) {r['stem'][:60]}: {msg[:160]}", flush=True)
                fail += 1
                break
        if i < len(todo):
            time.sleep(random.uniform(*a.gap))
    print(f"완료: 받음 {ok} · 실패 {fail}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
