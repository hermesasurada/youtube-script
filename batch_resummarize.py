#!/usr/bin/env python3
"""조건 맞는 이력의 요약(및 옵션 키프레임) 일괄 재작업.

대상 기본 필터:
  - duration >= 10분 (600초)
  - is_read = 0 (안읽음)
  - SUMMARY_COMPRESS >= 50 (원문 대비 요약 글자수 비율)
  - 전사 md + 요약 md 존재

사용:
  python3 batch_resummarize.py --dry-run          # 대상 목록만
  python3 batch_resummarize.py                   # 요약 재생성
  python3 batch_resummarize.py --with-keyframes  # 요약 후 키프레임까지
  python3 batch_resummarize.py --min-compress 50 --min-duration 600
  python3 batch_resummarize.py --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

import db

BASE = os.environ.get("YTS_BASE", "http://127.0.0.1:5001")
SUMM_TIMEOUT = int(os.environ.get("BATCH_SUMM_TIMEOUT", "900"))
KF_TIMEOUT = int(os.environ.get("BATCH_KF_TIMEOUT", "1800"))

_COMPRESS_RE = re.compile(r"<!--\s*SUMMARY_COMPRESS:(\d+)\s*-->")
_MODEL_RE = re.compile(r"<!--\s*SUMMARY_MODEL:[\s\S]*?-->\s*", re.M)


def log(msg: str) -> None:
    print(msg, flush=True)


def _compress_of(summary_path: str, txt_path: str) -> int | None:
    """마커 우선, 없으면 글자수 비율 계산."""
    try:
        with open(summary_path, encoding="utf-8", errors="replace") as f:
            summary = f.read()
    except OSError:
        return None
    m = _COMPRESS_RE.search(summary)
    if m:
        return int(m.group(1))
    body = _MODEL_RE.sub("", summary)
    body = _COMPRESS_RE.sub("", body).lstrip("\n")
    try:
        with open(txt_path, encoding="utf-8", errors="replace") as f:
            tchars = len(f.read())
    except OSError:
        return None
    if not tchars or not body:
        return None
    return max(1, round(len(body) / tchars * 100))


def find_candidates(
    *,
    min_duration: float,
    min_compress: int,
    unread_only: bool,
) -> list[dict]:
    db.init()
    items = db.list_items(unread_only=unread_only)
    out: list[dict] = []
    for it in items:
        if unread_only and it.get("is_read"):
            continue
        dur = float(it.get("duration") or 0)
        if dur < min_duration:
            continue
        txt = it.get("txt_path") or it.get("md_path")
        sp = it.get("summary_path")
        if not txt or not os.path.isfile(txt):
            continue
        if not sp or not os.path.isfile(sp):
            continue
        pct = _compress_of(sp, txt)
        if pct is None or pct < min_compress:
            continue
        out.append({
            "title": it.get("title") or os.path.basename(txt),
            "duration": dur,
            "compress": pct,
            "txt_path": txt,
            "summary_path": sp,
            "webpage_url": it.get("webpage_url") or "",
            "date": it.get("date"),
        })
    # 압축률 높은 것부터(가장 장황한 요약 우선)
    out.sort(key=lambda x: (-x["compress"], -x["duration"]))
    return out


def get_prompt() -> str:
    try:
        r = requests.get(f"{BASE}/prompt", timeout=15)
        r.raise_for_status()
        return r.json().get("prompt") or ""
    except Exception as e:
        log(f"[warn] /prompt 실패: {e}")
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
        return ""


def resummarize(txt_path: str, prompt: str, *, skip_claude: bool = False) -> tuple[bool, str]:
    """성공 여부, 메시지. 서버가 디스크에 저장."""
    body: dict = {"txt_path": txt_path, "prompt": prompt}
    if skip_claude:
        body["skip_claude"] = True
        body["mode"] = "grok"
    summary_err: str | None = None
    await_err = False
    done = False
    try:
        with requests.post(
            f"{BASE}/summarize",
            json=body,
            stream=True,
            timeout=(30, SUMM_TIMEOUT),
        ) as sr:
            sr.raise_for_status()
            for raw in sr.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                raw = raw.strip()
                if raw == "event: error":
                    await_err = True
                elif raw == "event: done":
                    done = True
                elif await_err and raw.startswith("data:"):
                    payload = raw[5:].strip()
                    try:
                        summary_err = json.loads(payload) if payload else "요약 실패"
                    except Exception:
                        summary_err = payload or "요약 실패"
                    await_err = False
    except requests.Timeout:
        return False, f"timeout({SUMM_TIMEOUT}s)"
    except Exception as e:
        return False, str(e)
    if summary_err:
        return False, str(summary_err)
    if not done:
        return False, "done 이벤트 없음(스트림 중단?)"
    return True, "ok"


def rekeyframes(txt_path: str, url: str, *, skip_claude: bool = False) -> tuple[bool, str]:
    body: dict = {"txt_path": txt_path, "url": url}
    if skip_claude:
        body["skip_claude"] = True
        body["mode"] = "grok"
    try:
        r = requests.post(f"{BASE}/keyframes", json=body, timeout=KF_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, str(e)
    if data.get("ok"):
        return True, f"frames={data.get('n_frames', '?')}"
    return False, f"{data.get('reason') or 'fail'}: {data.get('error') or ''}"


def main() -> int:
    ap = argparse.ArgumentParser(description="일괄 재요약 (안읽음·장시간·고압축률)")
    ap.add_argument("--dry-run", action="store_true", help="대상만 출력")
    ap.add_argument("--with-keyframes", action="store_true", help="요약 후 키프레임 재생성")
    ap.add_argument("--min-duration", type=float, default=600, help="초 (기본 600=10분)")
    ap.add_argument("--min-compress", type=int, default=50, help="압축률 하한 %% (기본 50)")
    ap.add_argument("--include-read", action="store_true", help="읽음 항목도 포함")
    ap.add_argument("--limit", type=int, default=0, help="최대 N건")
    ap.add_argument("--skip-claude", action="store_true", help="Grok 요약만")
    ap.add_argument("--sleep", type=float, default=2.0, help="건 사이 대기(초)")
    args = ap.parse_args()

    # 서버 헬스
    try:
        requests.get(f"{BASE}/", timeout=5).raise_for_status()
    except Exception as e:
        log(f"[error] 서버 접속 실패 ({BASE}): {e}")
        return 1

    cands = find_candidates(
        min_duration=args.min_duration,
        min_compress=args.min_compress,
        unread_only=not args.include_read,
    )
    if args.limit:
        cands = cands[: args.limit]

    log(
        f"대상 {len(cands)}건  "
        f"(dur>={args.min_duration:.0f}s, compress>={args.min_compress}%, "
        f"{'all' if args.include_read else 'unread'}, "
        f"keyframes={'on' if args.with_keyframes else 'off'})"
    )
    for i, c in enumerate(cands, 1):
        log(
            f"  [{i:02d}] {int(c['duration'])//60:3d}m  compress={c['compress']:3d}%  "
            f"{c['title'][:70]}"
        )

    if args.dry_run or not cands:
        return 0

    prompt = get_prompt()
    if not prompt or "{transcript}" not in prompt:
        log("[error] 유효한 프롬프트 없음 ({transcript} 필요)")
        return 1

    ok_n = fail_n = 0
    results: list[tuple[str, str, str]] = []  # title, status, detail

    for i, c in enumerate(cands, 1):
        title = c["title"][:60]
        log(f"\n== [{i}/{len(cands)}] 재요약 시작  compress={c['compress']}%  {title}")
        t0 = time.time()
        ok, msg = resummarize(c["txt_path"], prompt, skip_claude=args.skip_claude)
        elapsed = time.time() - t0
        if not ok:
            fail_n += 1
            log(f"   FAIL summarize ({elapsed:.0f}s): {msg}")
            results.append((title, "fail_sum", msg))
            # systemic 한도면 중단
            if re.search(r"usage limit|rate.?limit|한도|credit balance|quota", msg or "", re.I):
                log("[stop] 사용 한도 감지 — 이후 중단")
                break
            continue

        new_pct = _compress_of(c["summary_path"], c["txt_path"])
        log(f"   OK summarize ({elapsed:.0f}s)  compress {c['compress']}% → {new_pct}%")

        if args.with_keyframes:
            url = c["webpage_url"]
            if not url:
                log("   skip keyframes (webpage_url 없음)")
                results.append((title, "ok_sum_no_url", f"{c['compress']}→{new_pct}"))
            else:
                t1 = time.time()
                kok, kmsg = rekeyframes(c["txt_path"], url, skip_claude=args.skip_claude)
                log(
                    f"   {'OK' if kok else 'FAIL'} keyframes ({time.time()-t1:.0f}s): {kmsg}"
                )
                results.append(
                    (title, "ok_full" if kok else "ok_sum_kf_fail",
                     f"{c['compress']}→{new_pct}; kf={kmsg}")
                )
        else:
            results.append((title, "ok_sum", f"{c['compress']}→{new_pct}"))

        ok_n += 1
        if args.sleep and i < len(cands):
            time.sleep(args.sleep)

    log(f"\n완료: ok={ok_n} fail={fail_n} / total_attempted={ok_n + fail_n}")
    for title, st, det in results:
        log(f"  {st:16s}  {det:20s}  {title}")
    return 0 if fail_n == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
