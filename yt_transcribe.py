"""whisper 전사 — 언어 감지, 반복 붕괴 판정, 구간 분할 재전사, 말 없는 영상 판정.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import os
import re
import subprocess
import yt_core
import logging

log = logging.getLogger("yt-script")

def _seg_start_sec(seg: dict) -> float | None:
    """whisper 세그먼트 시작 시각(초). offsets(ms) 우선, timestamps 문자열 폴백."""
    off = (seg.get("offsets") or {}).get("from")
    if isinstance(off, (int, float)):
        return off / 1000.0
    ts = (seg.get("timestamps") or {}).get("from")  # "00:00:04,000"
    if isinstance(ts, str):
        m = re.match(r"(\d+):(\d+):(\d+)", ts)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return None


def _ts_tag(sec: float) -> str:
    s = int(sec); h, r = divmod(s, 3600); m, s = divmod(r, 60)
    return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


def _parse_transcript(json_path: str, offset: float = 0) -> str | None:
    """whisper JSON → 본문. 약 15초 간격으로 [mm:ss] 시각 마커를 삽입해
    이후 요약기가 주제별 시작 시각을 라벨링(Tier3 타임스탬프 정렬)할 수 있게 한다.

    offset은 구간 전사(시작 시각 지정)에서 잘라낸 앞부분 길이 —
    마커를 원본 영상 기준으로 되돌려 캡처가 엉뚱한 장면을 뽑지 않게 한다."""
    if not os.path.exists(json_path):
        return None
    with open(json_path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    cleaned, prev, last_bucket = [], "", -1
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text or text == prev:
            continue
        prev = text
        start = _seg_start_sec(seg)
        if start is not None:
            start += offset
            bucket = int(start // 15)            # 약 15초 간격으로만 마커 삽입(가독성)
            if bucket != last_bucket:
                last_bucket = bucket
                text = f"{_ts_tag(start)} {text}"
        cleaned.append(text)
    # 주의: JSON 삭제는 md 저장이 확정된 뒤 _finish_transcription에서 수행한다.
    return "\n".join(cleaned)


def _trim_audio_head(audio_path: str, start_sec: int) -> None:
    """오디오 앞부분(0~start_sec)을 잘라낸다 — 시작 시각 지정 전사용.

    yt-dlp의 download_ranges(구간만 받기)는 ffmpeg가 googlevideo에 직접 붙어
    403에 막히므로 쓰지 않는다. 전체를 기존 경로로 받아(403 재시도·잘림 감지
    그대로 유효) 여기서 잘라낸다 — 네트워크는 그대로지만 전사 시간이 줄어든다.
    """
    tmp = audio_path + ".trim.mp3"
    ff = os.path.join(yt_core.FFMPEG_LOCATION, "ffmpeg") if yt_core.FFMPEG_LOCATION else "ffmpeg"
    r = subprocess.run(
        [ff, "-y", "-loglevel", "error", "-ss", str(int(start_sec)),
         "-i", audio_path, "-c", "copy", tmp],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError((r.stderr or "ffmpeg 실패").strip().splitlines()[-1][:200])
    os.replace(tmp, audio_path)
    yt_core._duration_cache.pop(audio_path, None)   # 길이 캐시 무효화(잘라낸 뒤 길이가 바뀐다)


_LANG_RE = re.compile(r"auto-detected language:\s*(\w+)\s*\(p\s*=\s*([\d.]+)\)")
_LANG_PROBE_SEC = 60          # 표본 구간 길이
_LANG_MIN_TOTAL = 120         # 이보다 짧으면 표본 뽑을 여지가 없어 whisper 기본 감지에 맡긴다
_LANG_MIN_P     = 0.5         # 이 확신도 미만이면 명시하지 않고 auto로 둔다
_LANG_OVERRIDE_P = 0.85       # 표본 판정이 이보다 약하면 제작자 선언 언어가 이긴다


def _norm_lang(value) -> str:
    """yt-dlp language(ko / en-US / None) → whisper 언어코드(ko / en). 모르면 ""."""
    code = str(value or "").strip().lower().split("-")[0]
    return code if re.fullmatch(r"[a-z]{2,3}", code) else ""


def _choose_language(detected: str, p: float, declared: str) -> tuple[str, str]:
    """표본 감지 결과와 제작자 선언 언어로 (쓸 언어, 백지일 때 시도할 대체 언어)를 정한다.

    표본이 음악·무음에 걸리면 whisper는 엉뚱한 언어를 그럭저럭한 확신도로 낸다
    (2026-09-03 지식채널e: 5분 한국어 영상의 25/50/75% 표본이 전부 음악 구간이라
    ru(p=0.63)로 판정 → 러시아어로 디코딩돼 33자만 남음. 두 번 다 같은 결과라
    '동일 실패 반복' 규칙이 영구 실패로 굳힐 케이스). 제작자 선언 언어가 있고
    표본 판정이 약하면(p < _LANG_OVERRIDE_P) 선언 언어를 쓴다. 선언 언어도 틀릴 수
    있으므로(업로더 기본값 en 등) 반대편을 대체 후보로 남겨, 결과가 백지면 한 번 더
    돌린다.
    """
    if detected and p >= _LANG_MIN_P:
        if declared and declared != detected and p < _LANG_OVERRIDE_P:
            return declared, detected
        return detected, (declared if declared and declared != detected else "")
    if declared:
        return declared, (detected or "")
    return "", ""


def _detect_language(audio_path: str, total: float, threads: str) -> tuple[str, float]:
    """오디오 본문 여러 지점을 표본으로 언어를 판정한다.

    whisper는 앞 30초만 보고 언어를 정하는데, 인트로가 무음·음악이면 거기서 낸
    환각 텍스트를 근거로 엉뚱한 언어를 고른다. 그렇게 한 번 빗나가면 파일 전체가
    그 언어로 디코딩돼 사실상 백지가 된다(실측 2026-08-27: 45분 한국어 영상이
    "Thank you for watching!" 환각 때문에 en(p=0.31)으로 판정돼 237자만 남음.
    같은 파일의 중간 구간을 재면 ko(p=0.99)).

    25/50/75% 지점을 각각 재고 언어별 확신도를 합산해 고른다(다국어 영상에서
    한 지점이 삽입 클립에 걸려도 주 언어가 이긴다). 반환: (언어코드, 확신도).
    """
    if total < _LANG_MIN_TOTAL:
        return "", 0.0
    ffmpeg = os.path.join(yt_core.FFMPEG_LOCATION, "ffmpeg") if yt_core.FFMPEG_LOCATION else "ffmpeg"
    scores: dict[str, float] = {}
    best: dict[str, float] = {}
    for frac in (0.25, 0.5, 0.75):
        # whisper-cli의 -dl은 -ot(오프셋)을 무시하고 언제나 파일 앞부분을 본다.
        # 그래서 표본 구간을 실제로 잘라낸 임시 파일을 만들어 넘긴다(-c copy라 즉시).
        probe = f"{audio_path}.lang{int(frac * 100)}.mp3"
        try:
            cut = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-ss", str(int(total * frac)),
                 "-t", str(_LANG_PROBE_SEC), "-i", audio_path, "-c", "copy", probe],
                capture_output=True, text=True, timeout=120)
            if cut.returncode != 0 or not os.path.exists(probe):
                continue
            r = subprocess.run(
                [yt_core.WHISPER_EXE, "-m", yt_core.MODEL_PATH, "-f", probe, "-dl", "--threads", threads],
                capture_output=True, text=True, timeout=180, cwd=yt_core.BASE_DIR)
        except Exception as e:
            log.warning("language probe failed at %.0f%%: %s", frac * 100, e)
            continue
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
        m = _LANG_RE.search((r.stdout or "") + (r.stderr or ""))
        if not m:
            continue
        lang, p = m.group(1), float(m.group(2))
        scores[lang] = scores.get(lang, 0.0) + p
        best[lang] = max(best.get(lang, 0.0), p)
    if not scores:
        return "", 0.0
    lang = max(scores, key=scores.get)
    return lang, best[lang]


_COLLAPSE_MIN_RUN    = 50     # 같은 문장이 이만큼 연달아 나오면 붕괴로 본다
_COLLAPSE_MIN_SECS   = 120    # 같은 문장이 이만큼의 시간을 덮어도 붕괴다(세그먼트가 길 때)
# 같은 문장이 창 하나(30초)를 통째로 차지하는 세그먼트로 되풀이되면 짧아도 붕괴다(2026-10-31 월가아재 62분:
# 한 문장이 30초 세그먼트 3개로 반복돼 93초가 비었는데 120초 기준에 못 미쳐 그대로 저장됐다). 정상 발화는
# 한 문장이 25초 넘는 세그먼트가 되지 않고, 같은 문장이 연달아 그렇게 나오는 일은 더더욱 없다.
_COLLAPSE_LONG_SEG   = 25     # 이 길이(초) 이상인 세그먼트가
_COLLAPSE_LONG_REPS  = 2      # 같은 문장의 연속 반복 안에서 이만큼 나오면 붕괴
# 단, 무음·음악 구간의 정형 환각('We'll be right back.'·'자막은 설정에서…')은 빠진 말이 없으니 세지 않는다
# — 구간 분할 재전사(VAD 없음)는 오히려 무음 환각을 되살린다.
_SUBTITLE_CREDIT_RE  = re.compile(r"^(?:자막은 설정에서 선택하실 수 있습니다|(?:한글)?자막 ?(?:by|제공|제작)\b.*)[.!。 ]*$", re.I)


def _is_filler_line(text: str) -> bool:
    return bool(_HALLUCINATION_LINE_RE.match(text) or _NOISE_MARK_RE.fullmatch(text)
                or _SUBTITLE_CREDIT_RE.match(text))
# 2~6문장 묶음이 통째로 되풀이되는 붕괴(2026-10-17, CNBC Lutnick·Jensen Huang 33분 — 4문장이 430번 돌았는데
# 같은 문장 '연속'만 보던 판정을 통과했다. 기존 전사 1,420편에서 10편이 이 모양이었다).
_COLLAPSE_CYCLE_MAX  = 6      # 묶음 길이(문장 수) 상한
_COLLAPSE_CYCLE_RUN  = 20     # 묶음이 이만큼 되풀이되고
_COLLAPSE_CYCLE_SECS = 60     # 이만큼의 시간을 덮으면 붕괴(시각 없는 출력은 50회)
_COLLAPSE_CONTEXT    = 64     # 붕괴 시 재전사에 쓸 컨텍스트 상한(토큰)
# 붕괴 재전사 뒤 '반복이 사라졌는가'만 보면 부족하다 — 2026-09-10 Tom Brown G20(24분)은
# 컨텍스트 제한 재전사가 반복 없이 끝났지만 3분 15초 이후가 통째로 비어 2,983자만 남았다.
# 세그먼트가 실제로 덮는 시간이 전체의 이 비율에 못 미치면 구간 분할로 다시 돈다.
_MIN_COVERAGE        = float(os.environ.get("WHISPER_MIN_COVERAGE", "0.6"))
_CHUNK_SEC           = int(os.environ.get("WHISPER_CHUNK_SEC", "600"))   # 분할 재전사 구간(초)
# 1차 전사 옵션(2026-10-31 A/B 18편, llm-ab-harness VERDICTS §7): 1차부터 컨텍스트 64 + VAD +
# no-speech 0.6 → ko CER 4.7→3.3%, en WER 10.7→8.7%, 1차 붕괴 8→2편, 소요 79→39분, 무음 인트로
# 상투 환각 제거. 1차가 이미 컨텍스트 제한이면 붕괴 시 컨텍스트 64 재전사를 건너뛰고 바로 구간 분할.
# 비상 스위치(예전 경로로 복귀): WHISPER_VAD=0 · WHISPER_FIRST_CONTEXT=-1(무제한) · WHISPER_NO_SPEECH=0.8.
_FIRST_CONTEXT       = int(os.environ.get("WHISPER_FIRST_CONTEXT", "64"))   # 음수 = 무제한
_NO_SPEECH_THOLD     = os.environ.get("WHISPER_NO_SPEECH", "0.6")
_VAD_ENABLED         = os.environ.get("WHISPER_VAD", "1") != "0"
VAD_MODEL_PATH       = os.environ.get("WHISPER_VAD_MODEL", os.path.join(yt_core.WHISPER_DIR, "ggml-silero-v6.2.0.bin"))
# VAD는 1차 전체 전사에만 쓴다 — --offset-t/--duration이 VAD로 줄인 타임라인에 적용돼 구간 분할과
# 섞이면 엉뚱한 구간을 자른다. 커버리지(_coverage_ratio)는 붕괴 재전사 결과(VAD 없음)에만 쓰인다.
_VAD_ARGS            = ["--vad-threshold", "0.4", "--vad-min-silence-duration-ms", "500",
                        "--vad-speech-pad-ms", "200"]


def _vad_ready() -> bool:
    return _VAD_ENABLED and os.path.isfile(VAD_MODEL_PATH)


def _repeat_cycle(items: list[tuple[str, float, float]]) -> tuple[int, int, float, str]:
    """서로 다른 2~6문장 묶음이 그대로 되풀이되는 가장 긴 구간 → (반복 횟수, 묶음 길이, 덮은 초, 예시 문장).
    정상 대화의 '네.'·'Yeah.' 같은 맞장구는 사이 문장이 매번 달라 묶음이 이어지지 않는다."""
    texts = [t for t, _, _ in items]
    best = (0, 0, 0.0, "")
    for k in range(2, _COLLAPSE_CYCLE_MAX + 1):
        run, start = 0, 0
        for i in range(k, len(texts)):
            if texts[i] == texts[i - k] and len(set(texts[i - k:i])) == k:
                if run == 0:
                    start = i - k
                run += 1
                reps = run // k
                if reps > best[0]:
                    best = (reps, k, max(0.0, items[i][2] - items[start][1]), texts[i])
            elif texts[i] != texts[i - k]:
                run = 0
    return best


def _looks_collapsed(json_path: str) -> tuple[bool, str]:
    """whisper가 반복 루프에 빠졌는지 판정한다.

    한번 빠지면 그 반복이 다시 컨텍스트로 들어가 스스로를 강화해서 파일 끝까지
    못 빠져나온다(2026-08-28 실측: 5시간 팟캐스트가 3분 29초에 붕괴해 같은 문장
    18,741회로 채워짐 — 고유 문장은 55개뿐이었다).
    """
    try:
        with open(json_path, encoding="utf-8", errors="replace") as f:
            segs = json.load(f).get("transcription", [])
    except Exception:
        return False, ""
    items = []
    for seg in segs:
        t = (seg.get("text") or "").strip()
        if not t:
            continue
        off = seg.get("offsets") or {}
        try:
            a, b = int(off.get("from", 0)) / 1000, int(off.get("to", 0)) / 1000
        except (TypeError, ValueError):
            a, b = 0.0, 0.0
        items.append((t, a, b))
    texts = [t for t, _, _ in items]
    if len(texts) < 20:
        return False, ""
    prev, run, worst, worst_txt, dropped = "", 0, 0, "", 0
    run_start, worst_secs, worst_secs_txt = 0.0, 0.0, ""
    long_reps, long_hit = 0, None
    for t, a, b in items:
        if t == prev:
            run += 1
            dropped += 1
            if b - a >= _COLLAPSE_LONG_SEG and not _is_filler_line(t):
                long_reps += 1
                if long_reps >= _COLLAPSE_LONG_REPS and long_hit is None:
                    long_hit = (a, t)
            if run > worst:
                worst, worst_txt = run, t
            # 횟수와 별개로 '같은 문장이 덮는 시간'도 잰다 — whisper가 30초짜리 긴 세그먼트로
            # 붕괴하면 10분이 27회밖에 안 돼 횟수 기준(50)을 통과한다(2026-09-15 CNBC 12분).
            if b - run_start > worst_secs:
                worst_secs, worst_secs_txt = b - run_start, t
        else:
            run = 0
            run_start = a
            long_reps = 0
        prev = t
    ratio = dropped / len(texts)
    reps, period, span, sample = _repeat_cycle(items)
    if (reps >= _COLLAPSE_CYCLE_RUN and span >= _COLLAPSE_CYCLE_SECS) or reps >= _COLLAPSE_MIN_RUN:
        return True, f"{period}문장 묶음 {reps}회 반복({span:.0f}초), 중복 {ratio:.0%} ({sample[:40]})"
    if worst_secs >= _COLLAPSE_MIN_SECS:
        return True, f"같은 문장이 {worst_secs:.0f}초 연속, 중복 {ratio:.0%} ({worst_secs_txt[:40]})"
    # 판정은 '같은 문장이 길게 이어지는가'만 본다. 중복 비율 단독 조건은 오탐이 잦았다
    # — 5시간 대담에서 "Yeah." 같은 맞장구가 연달아 나오면 정상 전사도 비율이 쉽게
    # 올라간다(2026-08-28: 29만자 정상 전사가 '여전히 붕괴'로 찍혔다). 실제 붕괴는
    # 언제나 긴 연속 반복을 동반한다(그 사례는 18,740회 대 정상 22회로 자릿수가 다르다).
    if worst >= _COLLAPSE_MIN_RUN:
        return True, f"같은 문장 {worst}회 연속, 중복 {ratio:.0%} ({worst_txt[:40]})"
    if long_hit is not None:
        return True, (f"같은 문장이 {_COLLAPSE_LONG_SEG}초+ 세그먼트로 되풀이({yt_core._format_duration(long_hit[0])}), "
                      f"중복 {ratio:.0%} ({long_hit[1][:40]})")
    return False, ""


def _run_whisper(job_id: str, audio_path: str, language: str,
                 threads: str, total: float, max_context: int | None = None,
                 *, offset_ms: int | None = None, duration_ms: int | None = None,
                 output_prefix: str | None = None, vad: bool | None = None) -> int:
    """whisper.cpp 1회 실행. offset/duration/output_prefix는 구간 분할 재전사용.
    max_context=None이면 1차 기본값(_FIRST_CONTEXT), vad=None이면 구간 지정이 없을 때만 VAD."""
    q    = yt_core.jobs[job_id]["queue"]
    stop = yt_core.jobs[job_id]["stop_event"]

    if language == "auto":
        cached = yt_core.jobs.get(job_id, {}).get("resolved_language")
        if cached:                       # 붕괴 재전사 등 같은 오디오를 다시 돌릴 때
            language = cached
        else:
            detected, p = _detect_language(audio_path, total, threads)
            declared = yt_core.jobs.get(job_id, {}).get("declared_language") or ""
            chosen, alt = _choose_language(detected, p, declared)
            if chosen:
                language = chosen
                yt_core.jobs.setdefault(job_id, {})["resolved_language"] = chosen
                yt_core.jobs[job_id]["alt_language"] = alt
                how = ("제작자 선언 언어 우선(표본 %s %.0f%%)" % (detected, p * 100)
                       if chosen == declared and detected and chosen != detected
                       else "본문 표본 기준")
                q.put(f"언어 감지: {chosen} — {how}")
                log.info("language detected: %s (p=%.2f, declared=%s, alt=%s) %s",
                         chosen, p, declared or "-", alt or "-", os.path.basename(audio_path))
            elif detected:
                log.info("language probe inconclusive: %s (p=%.2f) — auto 유지", detected, p)

    # 동시 전사 제한: 슬롯을 못 얻으면 대기 안내 후 블로킹 획득.
    if not yt_core._transcribe_sem.acquire(blocking=False):
        q.put("다른 전사가 진행 중입니다 — 대기열에서 대기...")
        yt_core._transcribe_sem.acquire()
    try:
        if stop.is_set():
            return -1
        cmd = [yt_core.WHISPER_EXE, "-m", yt_core.MODEL_PATH, "-f", audio_path,
               "--language", language, "--threads", threads,
               "--output-json", "--temperature", "0",
               "--best-of", "5", "--beam-size", "5",
               "--no-speech-thold", _NO_SPEECH_THOLD, "--flash-attn"]
        if max_context is None and _FIRST_CONTEXT >= 0:
            max_context = _FIRST_CONTEXT
        if max_context is not None:
            cmd += ["--max-context", str(max_context)]
        if vad is None:
            vad = not offset_ms and not duration_ms
        if vad and _vad_ready():
            cmd += ["--vad", "--vad-model", VAD_MODEL_PATH, *_VAD_ARGS]
        if offset_ms:
            cmd += ["--offset-t", str(int(offset_ms))]
        if duration_ms:
            cmd += ["--duration", str(int(duration_ms))]
        if output_prefix:
            cmd += ["--output-file", output_prefix]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=yt_core.BASE_DIR,
        )
        yt_core.jobs[job_id]["proc"] = proc
        for raw in iter(proc.stdout.readline, b""):
            if stop.is_set():
                proc.kill()
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("whisper_print_timings"):
                continue
            if line.strip():
                yt_core.jobs[job_id]["last_log"] = line.strip()
            q.put(line)
            yt_core._emit_progress(q, line, total)
        try:
            proc.wait(timeout=60)          # stdout EOF 후엔 즉시 끝나야 함 — 좀비 방지 안전망
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        yt_core.jobs[job_id]["proc"] = None
        return proc.returncode
    finally:
        yt_core._transcribe_sem.release()


def _coverage_ratio(json_path: str, total: float) -> float:
    """whisper 세그먼트가 실제로 덮는 시간의 비율(0~1). JSON을 못 읽으면 0."""
    if not total or total <= 0:
        return 1.0
    try:
        with open(json_path, encoding="utf-8", errors="replace") as f:
            segs = json.load(f).get("transcription", [])
    except Exception:
        return 0.0
    spans = []
    for s in segs:
        if not (s.get("text") or "").strip():
            continue
        off = s.get("offsets") or {}
        try:
            a, b = int(off.get("from", 0)) / 1000, int(off.get("to", 0)) / 1000
        except (TypeError, ValueError):
            continue
        if b > a:
            spans.append((a, b))
    spans.sort()
    covered, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                covered += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        covered += cur_b - cur_a
    return max(0.0, min(1.0, covered / total))


def _ms_to_ts(ms: int) -> str:
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _merge_whisper_chunks(chunk_files: list[tuple[int, str]], out_path: str) -> int:
    """구간별 whisper JSON을 하나로 합친다. 반환값은 세그먼트 수.

    whisper.cpp는 --offset-t를 줘도 절대 시각으로 찍지만, 혹시 상대 시각으로 온
    구간(첫 세그먼트가 offset보다 한참 앞)은 offset을 더해 절대 시각으로 맞춘다.
    """
    merged: list[dict] = []
    for offset_ms, path in chunk_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                segs = json.load(f).get("transcription", [])
        except Exception:
            continue
        shift = 0
        if segs:
            try:
                first = int((segs[0].get("offsets") or {}).get("from", 0))
            except (TypeError, ValueError):
                first = 0
            if offset_ms and first < offset_ms - 1000:
                shift = offset_ms
        for s in segs:
            off = dict(s.get("offsets") or {})
            try:
                a, b = int(off.get("from", 0)) + shift, int(off.get("to", 0)) + shift
            except (TypeError, ValueError):
                continue
            merged.append({**s, "offsets": {"from": a, "to": b},
                           "timestamps": {"from": _ms_to_ts(a), "to": _ms_to_ts(b)}})
    merged.sort(key=lambda s: s["offsets"]["from"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"transcription": merged}, f, ensure_ascii=False)
    return len(merged)


def _run_whisper_chunked(job_id: str, audio_path: str, language: str,
                         threads: str, total: float, chunk_sec: int = _CHUNK_SEC) -> int:
    """오디오를 고정 길이 구간으로 잘라 구간마다 컨텍스트 없이 새로 전사한 뒤 합친다.

    붕괴는 컨텍스트가 스스로를 강화해 생기므로 구간 경계마다 컨텍스트를 끊으면
    한 구간이 무너져도 나머지는 살아남는다. 결과는 audio_path + ".json"에 쓴다.
    """
    q = yt_core.jobs[job_id]["queue"]
    stop = yt_core.jobs[job_id]["stop_event"]
    total_ms = int(max(total, 0) * 1000)
    if total_ms <= 0:
        return -1
    step = max(60, int(chunk_sec)) * 1000
    chunks: list[tuple[int, str]] = []
    n = (total_ms + step - 1) // step
    for i in range(n):
        if stop.is_set():
            return -1
        off = i * step
        prefix = f"{audio_path}.chunk{i:03d}"
        q.put(f"구간 분할 재전사 {i + 1}/{n} ({yt_core._format_duration(off / 1000)}~)")
        rc = _run_whisper(job_id, audio_path, language, threads, total, max_context=0,
                          offset_ms=off, duration_ms=min(step, total_ms - off),
                          output_prefix=prefix, vad=False)
        if rc != 0:
            log.warning("chunked whisper failed at chunk %d/%d (rc=%s)", i + 1, n, rc)
        elif os.path.isfile(prefix + ".json"):
            chunks.append((off, prefix + ".json"))
    if not chunks:
        return -1
    count = _merge_whisper_chunks(chunks, audio_path + ".json")
    for _, p in chunks:
        try:
            os.remove(p)
        except OSError:
            pass
    log.info("chunked whisper merged: %d segments from %d/%d chunks", count, len(chunks), n)
    return 0 if count else -1


# 말 없는 구간에서 whisper가 내는 표시·정형 문장 — 본문 길이에서 뺀다(2026-10-14, yt #1500 지식채널e:
# 화면 자막+음악 5분 영상이 '♪'·'*Splash*'와 'Thank you.'·'We'll be right back.' 같은 환각 문장만으로
# 분당 57자가 돼 기준(40자)을 넘겨 요약까지 됐다). 줄 전체가 이것뿐일 때만 지운다.
_NOISE_MARK_RE = re.compile(r"[♪♫♬]+|\*[^*\n]{1,30}\*|\[(?:music|음악|applause|박수|laughter|웃음|silence|무음)[^\]]*\]"
                            r"|\((?:music|음악|applause|박수|laughter|웃음)[^)]*\)", re.I)
_HALLUCINATION_LINE_RE = re.compile(
    r"^(?:thank you(?: (?:so|very) much)?(?: for watching| for listening)?|thanks for watching|"
    r"(?:we'?ll|i'?ll) be right back|(?:i'?ll )?see you (?:next time|in the next (?:video|one))|bye(?:[- ]bye)?|"
    r"please subscribe(?: to (?:my|our|the) channel)?|subtitles? by[^.]*|you|"
    r"시청해 ?주셔서 감사합니다|구독과 좋아요(?: 부탁드립니다)?|감사합니다)[.!。 ]*$", re.I)


def _speech_body(transcript: str) -> str:
    """전사 본문에서 시각 마커·음악/효과음 표시·환각 정형 문장 줄을 뺀 실제 말."""
    lines = []
    for line in yt_core._TS_TAG_RE.sub("", transcript or "").splitlines():
        line = _NOISE_MARK_RE.sub("", line).strip()
        if line and not _HALLUCINATION_LINE_RE.match(line):
            lines.append(line)
    return "\n".join(lines)


def _transcript_sparse(job_id: str, transcript: str | None) -> str:
    """길이 대비 본문이 사실상 비어 있으면(무음·잘린 오디오·언어 오판) 사유 문자열, 아니면 ""."""
    if transcript is None:
        return ""
    secs = float(yt_core.jobs[job_id].get("clip_duration")
                 or (yt_core.jobs[job_id].get("meta") or {}).get("duration") or 0)
    mins = secs / 60
    body = _speech_body(transcript)   # 시각 마커·음악 표시·환각 정형 문장은 본문 길이에서 제외
    if mins >= 1 and len(body) / mins < yt_core.MIN_CHARS_PER_MIN:
        return (f"전사 내용이 비어 있습니다: {len(body)}자 / "
                f"{yt_core._format_duration(secs)} (분당 {len(body) / mins:.0f}자)")
    # 1분 미만은 분당 기준이 너무 흔들려 따로 본다 — 실제 말이 몇 낱말도 안 되면 음성 없는 영상
    # (2026-11-01 사용자 지시, Rocket Lab 37초 무음 클립이 빈 요약으로 저장됐다). '음성 없는 영상'은
    # 채널 모니터가 영구 실패로 보는 문구라 재시도하지 않는다.
    if 0 < mins < 1 and len(body) < yt_core.MIN_SHORT_CHARS:
        return (f"전사 내용이 비어 있습니다: {len(body)}자 / {yt_core._format_duration(secs)}"
                f" — 1분 미만 음성 없는 영상")
    return ""
