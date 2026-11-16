"""영상 받기와 후보 프레임 추출 — yt-dlp 다운로드, ffmpeg 장면 전환 감지, 화면이 멈춘 구간(hold) 찾기.

진입점은 `download_video`·`extract_candidates`. LLM을 부르지 않는다. 다운로드 재시도 틀은 yt_download
(2026-11-17 keyframe_report에서 분리).
"""
from __future__ import annotations

import bisect
from collections import Counter
import json
import os
import re
import subprocess
import yt_download

import kf_base

def log(*a): print(*a, flush=True)


# ── 프레임 확보 ───────────────────────────────────────────────────────

def download_video(url: str, out_noext: str) -> str | None:
    log(f"[1] 영상 다운로드(≤{kf_base.VIDEO_MAXH}p): {url}")
    opts = {
        "format": f"bestvideo[height<={kf_base.VIDEO_MAXH}]+bestaudio/best[height<={kf_base.VIDEO_MAXH}]/best",
        "outtmpl": out_noext + ".%(ext)s", "merge_output_format": "mp4",
        "ffmpeg_location": kf_base.FFMPEG_DIR, "quiet": True, "no_warnings": True,
        "socket_timeout": 60, "retries": 5,
    }
    # 1차 대체 클라이언트(tv_simply·web_embedded) → 2차 기본 클라이언트 → 3차 75초 뒤 기본.
    # IP 단위 차단(403)으로 웹 계열이 전부 막혀도 이 둘은 통과한다(2026-08-18 실측).
    # 대체를 3차에 두던 때 캡처 332회 중 330회가 기본에서 실패하고 324회가 3차에서야
    # 받혀(2026-10-04~11-03) 75초를 매번 버렸다. 최고 해상도는 기본과 같다.
    # 403만 재시도하던 것을 '빈 파일'·'포맷 없음'(SABR-only 세션, 라이브 종료 직후)까지
    # 넓혔다 — 2026-09-03 집코노미 라이브 캡처가 첫 시도에서 바로 raise돼 폴백을 못 탔다.
    def _on_retry(attempt: int, wait: int, nxt: str, e: Exception) -> None:
        log(f"[1] 다운로드 실패({str(e)[:60]}) — {f'{wait}초 뒤 ' if wait else ''}{nxt}으로 재시도({attempt}/2)")

    yt_download.download_with_retry(
        url, opts, alt_attempts=(1,), retryable=yt_download.is_retryable_video_error, on_retry=_on_retry)
    for ext in ("mp4", "mkv", "webm", "m4v"):
        if os.path.exists(out_noext + "." + ext):
            return out_noext + "." + ext
    return None


def _probe_duration(video: str) -> float:
    try:
        r = subprocess.run([kf_base.FFPROBE, "-v", "quiet", "-show_format", "-print_format", "json", video],
                           capture_output=True, text=True, timeout=kf_base.PROBE_TIMEOUT)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        log(f"    [warn] ffprobe 실패/타임아웃: {e}")
        return 0.0


def _evenly_sample(items: list, count: int) -> list:
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _settle_scene_frames(video: str, outdir: str, pairs: list[tuple[float, str]],
                         dur: float, scene_times: list[float]) -> list[tuple[float, str]]:
    """장면전환 프레임을 '그 장면이 끝나기 직전' 프레임으로 바꾼다(2026-10-02).

    슬라이드형 영상은 새 장면이 제목만 띄운 채 시작하고 그림·표가 하나씩 채워진다(Leo Cui:
    1300초 제목만 → 1346초 다음 장면 직전에 패널 완성). 장면 점수는 그 채워짐을 못 잡아
    장면 시작 프레임만 쓰면 대부분 '제목만 있는 화면'이 캡처됐다. 다음 장면 0.7초 전을 쓴다.
    장면이 2초 미만이면 원래 프레임을 둔다. 실패하면 원래 후보를 그대로 돌려준다.

    후보 상한을 거친 뒤에 부른다 — 전 장면을 select 식 하나에 넣으면 식이 약 4KB를 넘을 때
    ffmpeg가 아무것도 내지 않아 장면이 많은 영상은 교체가 0건이었다(2026-11-04 확인: 299회 중 89회).
    """
    known = sorted(t for t in scene_times if t > 0)
    ends: list[float | None] = []
    for t, path in pairs:
        if not os.path.basename(path).startswith("scene_"):
            ends.append(None)
            continue
        i = bisect.bisect_right(known, t + 0.05)
        nxt = known[i] if i < len(known) else (dur if dur > 0 else None)
        ends.append(nxt - 0.7 if nxt is not None and nxt - t >= 2.0 else None)
    windows = [e for e in ends if e is not None]
    if not windows:
        return pairs
    expr = "+".join(f"between(t,{e:.3f},{e + 0.12:.3f})" for e in windows)
    try:
        r = subprocess.run([kf_base.FFMPEG, "-hide_banner", "-i", video,
                            "-vf", f"select='{expr}',showinfo,scale={kf_base.FRAME_WIDTH}:-2",
                            "-vsync", "vfr", "-q:v", "3", os.path.join(outdir, "scene_end_%04d.jpg")],
                           capture_output=True, text=True, timeout=kf_base.FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"    [warn] 장면 끝 프레임 추출 {kf_base.FFMPEG_TIMEOUT}s 타임아웃 — 장면 시작 프레임 사용")
        return pairs
    if r.returncode != 0:
        log(f"    [warn] 장면 끝 프레임 추출 rc={r.returncode}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
    pts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr or "")]
    files = sorted(g for g in os.listdir(outdir) if g.startswith("scene_end_"))
    got: dict[float, tuple[float, str]] = {}
    for ts, f in zip(pts, files):
        e = next((w for w in windows if w - 0.01 <= ts <= w + 0.13), None)
        if e is not None and e not in got:             # 창마다 첫 프레임 하나
            got[e] = (ts, os.path.join(outdir, f))
    out, settled = [], 0
    for (t, path), e in zip(pairs, ends):
        if e is not None and e in got:
            out.append(got[e]); settled += 1
        else:
            out.append((t, path))
    log(f"    장면 끝 프레임으로 교체 {settled}/{len(windows)}장")
    return sorted(out, key=lambda x: x[0])


# 정지 화면 판별값(2026-11-04 #1494 실측): 1초 간격 160×90 흑백 프레임을 비교한다.
_HOLD_W, _HOLD_H = 160, 90
_HOLD_PIXELS = _HOLD_W * _HOLD_H
_HOLD_PIXEL_DIFF = 14        # 이 밝기 차이부터 '바뀐 픽셀'
_HOLD_STATIC_RATIO = 0.004   # 바뀐 픽셀이 이 비율 미만이면 같은 화면
_HOLD_MIN_SECONDS = 3        # 이만큼 멈춰야 정지 구간
_HOLD_SAME_SLIDE_RATIO = 0.8 # 기존 내용이 이만큼 남으면 같은 장표가 채워지는 중(같은 장표 0.88~1.0, 장표 교체 0.01~0.34)


def _read_gray_seconds(video: str) -> list[bytes]:
    r = subprocess.run([kf_base.FFMPEG, "-v", "error", "-i", video, "-an",
                        "-vf", f"fps=1,scale={_HOLD_W}:{_HOLD_H},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=kf_base.FFMPEG_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"rc={r.returncode}")
    raw = r.stdout
    return [raw[i:i + _HOLD_PIXELS] for i in range(0, len(raw) - _HOLD_PIXELS + 1, _HOLD_PIXELS)]


def _changed_ratio(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) > _HOLD_PIXEL_DIFF for x, y in zip(a, b)) / _HOLD_PIXELS


def _find_holds(frames: list[bytes]) -> list[tuple[int, int]]:
    """화면이 멈춘 구간 (시작 초, 끝 초) 목록."""
    holds, start = [], None
    for sec in range(1, len(frames) + 1):
        still = sec < len(frames) and _changed_ratio(frames[sec - 1], frames[sec]) < _HOLD_STATIC_RATIO
        if still:
            start = sec - 1 if start is None else start
        else:
            if start is not None and sec - 1 - start >= _HOLD_MIN_SECONDS:
                holds.append((start, sec - 1))
            start = None
    return holds


def _content_kept(before: bytes, after: bytes) -> float:
    """before의 내용(배경색이 아닌 픽셀) 중 after에도 그대로 남은 비율."""
    background = Counter(before).most_common(1)[0][0]
    content = [i for i, v in enumerate(before) if abs(v - background) > _HOLD_PIXEL_DIFF]
    if not content:
        return 1.0
    return sum(abs(before[i] - after[i]) <= _HOLD_PIXEL_DIFF for i in content) / len(content)


def _settled_second(ts: float, frames: list[bytes], holds: list[tuple[int, int]]) -> int | None:
    """ts의 장표가 완성된 초. 옮길 필요가 없거나 찾지 못하면 None.

    ts 뒤 첫 정지 화면에서 시작해, 다음 정지 화면에 기존 내용이 남아 있으면 같은 장표로 보고
    따라간다. 기존 내용이 사라지면 장표가 바뀐 것이므로 그 직전 정지 화면에서 멈춘다.
    """
    sec = int(ts)
    limit = sec + kf_base.HOLD_MAX_WAIT
    k = bisect.bisect_left([end for _, end in holds], sec)
    if k >= len(holds) or holds[k][0] > limit or sec >= len(frames):
        return None
    # 움직이는 화면에서 다음 정지 화면까지 멀면 다른 화면일 수 있다. 실측(#1494·862·1644·1634)에서
    # 같은 장표는 내용이 98% 이상 남았고, 인물·영상이 다른 슬라이드로 바뀐 경우는 2~14%였다.
    first = holds[k][0] + 1
    if (first - sec > _HOLD_MIN_SECONDS
            and _content_kept(frames[sec], frames[first]) < _HOLD_SAME_SLIDE_RATIO):
        return None
    while k + 1 < len(holds) and holds[k + 1][0] + 1 <= limit:
        if _content_kept(frames[holds[k][1]], frames[holds[k + 1][0] + 1]) < _HOLD_SAME_SLIDE_RATIO:
            break
        k += 1
    settled = min(holds[k][1] - 1, limit)      # 끝 초는 다음 전환에 걸릴 수 있어 한 칸 앞
    if settled <= sec or _changed_ratio(frames[sec], frames[settled]) < _HOLD_STATIC_RATIO:
        return None
    return settled


def _settle_unfinished_frames(video: str, outdir: str,
                              pairs: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """후보를 그 장표가 완성된 시점의 프레임으로 옮긴다(2026-11-04).

    스크롤텔링 장표는 장면 전환이 거의 잡히지 않고 그림이 서서히 채워진다. 균등·전사 단서
    프레임은 그 도중 아무 시점에나 떨어져 #1494(Leo Cui)는 빈 패널·빈 차트가 캡처됐고,
    후보에 미완성본 한 장뿐이면 비전 판정도 걸러내지 못한다. 실패하면 원래 후보를 돌려준다.
    """
    try:
        frames = _read_gray_seconds(video)
    except Exception as e:
        log(f"    [warn] 정지 화면 분석 실패, 원래 프레임 사용: {e}")
        return pairs
    holds = _find_holds(frames)
    if not holds:
        return pairs
    moved: dict[int, str] = {}
    out: list[tuple[float, str]] = []
    for ts, path in pairs:
        sec = _settled_second(ts, frames, holds)
        if sec is None:
            out.append((ts, path))
            continue
        if sec not in moved:
            target = os.path.join(outdir, f"settled_{sec:06d}.jpg")
            r = subprocess.run([kf_base.FFMPEG, "-v", "error", "-y", "-ss", str(sec), "-i", video,
                                "-frames:v", "1", "-vf", f"scale={kf_base.FRAME_WIDTH}:-2", "-q:v", "3", target],
                               capture_output=True, text=True, timeout=kf_base.PROBE_TIMEOUT)
            moved[sec] = target if r.returncode == 0 and os.path.exists(target) else ""
        out.append((float(sec), moved[sec]) if moved[sec] else (ts, path))
    # 같은 정지 구간의 프레임은 같은 그림이라 한 장만 둔다. 다만 한 화면이 길게 이어지면
    # 소제목 배정을 위해 HOLD_MAX_WAIT 간격으로는 남긴다.
    ends = [end for _, end in holds]
    last_kept: dict[int, float] = {}
    kept: list[tuple[float, str]] = []
    for ts, path in sorted(out, key=lambda x: x[0]):
        k = bisect.bisect_left(ends, int(ts))
        in_hold = k < len(holds) and holds[k][0] <= ts
        if in_hold and k in last_kept and ts - last_kept[k] < kf_base.HOLD_MAX_WAIT:
            continue
        if in_hold:
            last_kept[k] = ts
        kept.append((ts, path))
    log(f"    장표 완성 시점으로 이동 {len(moved)}곳, 후보 {len(pairs)}장 → {len(kept)}장 (정지 구간 {len(holds)}개)")
    return kept


def _uniform_select(interval: int) -> str:
    """균등 후보 select 식 — 간격의 절반 지점부터 interval초마다 한 장.

    fps=1/N은 N/2 지점 프레임을 내면서 시각은 0으로 찍어 i*N으로 적으면 반 간격 빠른
    시각이 됐다(2026-11-04 합성 영상 실측: 30초 간격에서 0·30·60초라 적은 프레임이 실제
    14·44·74초). 뽑는 지점은 그대로 두고 showinfo로 실제 시각을 읽으려고 select를 쓴다.
    """
    return f"gte(t,{interval / 2:g})*(isnan(prev_selected_t)+gte(t-prev_selected_t,{interval}))"


def extract_candidates(video: str, outdir: str,
                       target_times: list[float] | None = None) -> list[tuple[float, str]]:
    """전사 목표+균등 간격+장면전환 후보를 합쳐 근접 중복 제거 후 캡한다.

    화면공유/슬라이드형(정지 화면 길게 유지)은 장면전환만으론 누락 → 균등 간격으로 보강.
    """
    os.makedirs(outdir, exist_ok=True)
    pairs: list[tuple[float, str]] = []
    dur = _probe_duration(video)

    # 1) 균등 간격 — 전체 타임라인 보장 커버
    if dur > 0:
        interval = max(15, int(dur / kf_base.MAX_CANDIDATES))
        uniform_stderr = ""
        try:
            ri = subprocess.run([kf_base.FFMPEG, "-hide_banner", "-i", video,
                            "-vf", f"select='{_uniform_select(interval)}',showinfo,scale={kf_base.FRAME_WIDTH}:-2",
                            "-vsync", "vfr", "-q:v", "3", os.path.join(outdir, "iv_%03d.jpg")],
                           capture_output=True, text=True, timeout=kf_base.FFMPEG_TIMEOUT)
            uniform_stderr = ri.stderr or ""
            if ri.returncode != 0:
                log(f"    [warn] ffmpeg 균등추출 rc={ri.returncode}: {uniform_stderr.strip().splitlines()[-1] if uniform_stderr.strip() else ''}")
        except subprocess.TimeoutExpired as e:
            uniform_stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) if e.stderr else ""
            log(f"    [warn] ffmpeg 균등추출 {kf_base.FFMPEG_TIMEOUT}s 타임아웃 — 부분 결과 사용")
        uniform_pts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", uniform_stderr)]
        for ts, f in zip(uniform_pts, sorted(g for g in os.listdir(outdir) if g.startswith("iv_"))):
            pairs.append((ts, os.path.join(outdir, f)))
        log(f"[2] 균등 간격 {interval}s × {len([p for p in pairs])}장 (영상 {int(dur)}s)")

    # 2) 전사 시각 단서 — 해당 시점 전후를 짧게 훑어 완성된 자료 화면 후보를 보강
    target_times = sorted(set(target_times or []))
    if target_times:
        expressions = [
            f"between(t,{max(0.0, ts - kf_base.TARGET_WINDOW):.3f},{ts + kf_base.TARGET_WINDOW:.3f})"
            for ts in target_times
        ]
        target_stderr = ""
        try:
            rt = subprocess.run([
                kf_base.FFMPEG, "-hide_banner", "-i", video,
                "-vf", (
                    f"fps=1/{max(0.5, kf_base.TARGET_INTERVAL):g},"
                    f"select='{'+'.join(expressions)}',showinfo,scale={kf_base.FRAME_WIDTH}:-2"
                ),
                "-vsync", "vfr", "-q:v", "3", os.path.join(outdir, "target_%03d.jpg"),
            ], capture_output=True, text=True, timeout=kf_base.FFMPEG_TIMEOUT)
            target_stderr = rt.stderr or ""
            if rt.returncode != 0:
                log(f"    [warn] 전사단서 추출 rc={rt.returncode}: "
                    f"{target_stderr.strip().splitlines()[-1] if target_stderr.strip() else ''}")
        except subprocess.TimeoutExpired as e:
            target_stderr = (
                e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes)
                else (e.stderr or "")
            ) if e.stderr else ""
            log(f"    [warn] 전사단서 추출 {kf_base.FFMPEG_TIMEOUT}s 타임아웃 — 부분 결과 사용")
        target_pts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", target_stderr)]
        target_files = sorted(g for g in os.listdir(outdir) if g.startswith("target_"))
        for i, filename in enumerate(target_files):
            if i < len(target_pts):
                pairs.append((target_pts[i], os.path.join(outdir, filename)))
        log(f"[2] 전사 시각단서 {len(target_times)}곳 → 주변 후보 {len(target_files)}장")

    # 3) 장면전환 — 짧은 컷/데모/B-roll 포착
    scene_stderr = ""
    try:
        r = subprocess.run([kf_base.FFMPEG, "-hide_banner", "-i", video,
                            "-vf", f"select='gt(scene,{kf_base.SCENE_THRESHOLD})',showinfo,scale={kf_base.FRAME_WIDTH}:-2",
                            "-vsync", "vfr", "-q:v", "3", os.path.join(outdir, "scene_%03d.jpg")],
                           capture_output=True, text=True, timeout=kf_base.FFMPEG_TIMEOUT)
        scene_stderr = r.stderr or ""
        if r.returncode != 0:
            log(f"    [warn] ffmpeg 장면추출 rc={r.returncode}: {scene_stderr.strip().splitlines()[-1] if scene_stderr.strip() else ''}")
    except subprocess.TimeoutExpired as e:
        scene_stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) if e.stderr else ""
        log(f"    [warn] ffmpeg 장면추출 {kf_base.FFMPEG_TIMEOUT}s 타임아웃 — 부분 결과 사용")
    times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", scene_stderr)]
    sc = sorted(g for g in os.listdir(outdir) if g.startswith("scene_"))
    if len(times) < len(sc):   # 타임스탬프 누락 → 해당 프레임은 0.0(시작)으로 잘못 배치될 수 있음
        log(f"    [warn] 장면 pts_time {len(times)}개 < 프레임 {len(sc)}장 — 일부 시각 미상(0.0)")
    scene_pairs = [(times[i] if i < len(times) else 0.0, os.path.join(outdir, f)) for i, f in enumerate(sc)]
    pairs.extend(scene_pairs)
    log(f"    장면전환 {len(sc)}장 → 후보 합계 {len(pairs)}장")

    # 4) 시간순 정렬 → 근접 중복 제거 → 캡
    #    장면전환(scene_) 프레임은 의미있는 컷이므로 6초 근접 제거에서 '보존'하고,
    #    균등 간격(iv_) 프레임만 직전 채택분과 6초 미만이면 솎는다.
    is_scene = lambda p: os.path.basename(p).startswith("scene_")
    is_target = lambda p: os.path.basename(p).startswith("target_")
    pairs.sort(key=lambda x: x[0])
    deduped, last = [], -999.0
    for ts, p in pairs:
        # 목표 주변 프레임은 비전 모델이 선명도·완성도를 직접 비교해야 하므로 보존한다.
        if is_scene(p) or is_target(p) or (ts - last >= kf_base.MIN_GAP):
            deduped.append((ts, p)); last = ts
    # 캡: 초과 시에도 장면전환 우선 보존(균등부터 솎고, 장면전환만 초과하면 그때 균등 샘플)
    if len(deduped) > kf_base.MAX_CANDIDATES:
        targets = [x for x in deduped if is_target(x[1])]
        scenes = [x for x in deduped if is_scene(x[1])]
        ivs = [x for x in deduped if not is_scene(x[1]) and not is_target(x[1])]
        if targets:
            # 목표 프레임은 최대 60%만 사용하고 나머지는 장면전환/균등 후보로 채운다.
            target_budget = min(len(targets), max(1, int(kf_base.MAX_CANDIDATES * 0.6)))
            sel = _evenly_sample(targets, target_budget)
            remaining = kf_base.MAX_CANDIDATES - len(sel)
            scene_selection = _evenly_sample(scenes, remaining)
            sel += scene_selection
            remaining -= len(scene_selection)
            sel += _evenly_sample(ivs, remaining)
        elif len(scenes) >= kf_base.MAX_CANDIDATES:
            sel = _evenly_sample(scenes, kf_base.MAX_CANDIDATES)
        else:
            need = kf_base.MAX_CANDIDATES - len(scenes)
            sel = scenes + _evenly_sample(ivs, need)
        deduped = sorted(sel, key=lambda x: x[0])
    log(f"    중복제거·캡 후 후보 {len(deduped)}장 (장면전환 보존)")
    if kf_base.SCENE_SETTLE:
        deduped = _settle_scene_frames(video, outdir, deduped, dur, [t for t, _ in scene_pairs])
    if kf_base.HOLD_SETTLE:
        deduped = _settle_unfinished_frames(video, outdir, deduped)
    return deduped
