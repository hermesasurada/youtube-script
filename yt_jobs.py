"""URL·파일 전사 작업 실행(run_job/run_file_job) — 다운로드, 썸네일, 오디오 보관, 전사 마무리.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import json
import html as _htmlmod
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
import yt_download
import yt_listing
import yt_core
import yt_transcribe
import logging

log = logging.getLogger("yt-script")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def _set_job_error(job_id: str, stage: str, message: object) -> str:
    """Persist a concise, UI-safe failure reason for the result endpoint."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(message or "알 수 없는 오류"))
    text = re.sub(r"\s+", " ", text).strip()[:800]
    with yt_core.jobs_lock:
        job = yt_core.jobs.get(job_id)
        if job:
            job["status"] = "error"
            job["error_stage"] = stage
            job["error_message"] = text
    return text


_THUMB_SOURCES = ("mqdefault", "hqdefault", "sddefault", "maxresdefault")


_WATCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_localized_title(yt_id: str, lang: str = "ko", channel_id: str = "") -> str:
    """유튜브가 해당 언어 시청자에게 보여주는 제목(현지화 제목). 없으면 "".

    제작자가 언어별 제목을 달아 두면 유튜브는 한국 시청자에게 그 제목을 보여주는데,
    yt-dlp(lang=ko 포함)·oEmbed·og:title은 전부 원제만 준다. 시청 페이지를
    Accept-Language: ko 로 받으면 플레이어 메타(videoDetails / primaryInfo)에 현지화
    제목이 실린다. og:title(원제)과 다른 것만 현지화 제목으로 인정한다.
    """
    yt_id = (yt_id or "").strip()
    if not re.fullmatch(r"[\w-]{5,20}", yt_id):
        return ""
    req = urllib.request.Request(
        f"https://www.youtube.com/watch?v={yt_id}",
        headers={"User-Agent": _WATCH_UA,
                 "Accept-Language": f"{lang}-KR,{lang};q=0.9,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    og = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    original = yt_core._nfc(_htmlmod.unescape(og.group(1)).strip()) if og else ""
    cands: list[str] = []
    i = page.find("ytInitialPlayerResponse")
    if i >= 0:
        j = page.find("{", i)
        try:
            obj, _ = json.JSONDecoder().raw_decode(page[j:])
            cands.append((obj.get("videoDetails") or {}).get("title") or "")
        except Exception:
            pass
    m = re.search(r'"videoPrimaryInfoRenderer":\{"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', page)
    if m:
        try:
            cands.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            cands.append(m.group(1))
    for t in cands:
        t = yt_core._nfc((t or "").strip())
        if t and t != original:
            return t
    # 시청 페이지가 원제만 줄 때가 많다(2026-11-07 실측) — 채널 영상 목록의 한국어 제목으로 보강
    if channel_id:
        return yt_core._nfc(yt_listing.korean_title(yt_listing.korean_entries(channel_id).get(yt_id), original))
    return ""


def save_thumbnail(yt_id: str, *, force: bool = False, channel_id: str = "") -> str | None:
    """영상 썸네일을 res/thumbs/{yt_id}.jpg 로 내려받는다(이미 있으면 그대로 둔다).

    force=True면 사본이 있어도 다시 받는다(메타 갱신 버튼). 새 사본이 실패하면
    기존 파일은 건드리지 않는다 — 갱신 실패가 기존 썸네일을 지우면 안 된다.

    전사 시점에 한 번만 받아 두고, 이후 조회는 이 사본만 쓴다(외부 재요청 없음).
    나중에 비공개·멤버십 전용으로 바뀌면 원본 URL이 막히므로 그 전에 확보해 둔다.
    실패해도 파이프라인엔 영향이 없다(썸네일 자리가 빌 뿐).
    """
    yt_id = (yt_id or "").strip()
    if not yt_id or not re.fullmatch(r"[\w-]{5,20}", yt_id):
        return None
    dest = os.path.join(yt_core.THUMB_DIR, f"{yt_id}.jpg")
    if not force and os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return dest
    os.makedirs(yt_core.THUMB_DIR, exist_ok=True)
    # 한국어 썸네일이 있으면 그것부터(주소가 서명돼 있어 채널 목록에서만 얻는다, 2026-11-07)
    ko_thumb = (yt_listing.korean_entries(channel_id).get(yt_id) or {}).get("thumb") if channel_id else ""
    if ko_thumb:
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    ko_thumb, headers={"User-Agent": _WATCH_UA}), timeout=15) as r:
                data = r.read()
            if len(data) >= 2048:
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                log.info("thumbnail saved: %s (ko, %d bytes)", yt_id, len(data))
                return dest
        except Exception as e:
            log.warning("korean thumbnail failed: %s (%s)", yt_id, e)
    for name in _THUMB_SOURCES:
        try:
            with urllib.request.urlopen(
                f"https://i.ytimg.com/vi/{yt_id}/{name}.jpg", timeout=15) as r:
                data = r.read()
        except Exception:
            continue
        # YouTube는 없는 해상도에 회색 자리표시(120x90, ~1KB)를 준다 — 크기로 거른다
        if len(data) < 2048:
            continue
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)      # 부분 파일이 보이지 않도록 원자적 교체
        log.info("thumbnail saved: %s (%s, %d bytes)", yt_id, name, len(data))
        return dest
    return None


def _archive_audio(audio_path: str) -> None:
    """전사가 끝난 다운로드 오디오를 보관함(날짜 폴더)으로 옮기고 보관 기한이 지난 파일을 지운다."""
    if yt_core.AUDIO_RETENTION_DAYS <= 0:
        os.remove(audio_path)
        log.info("source audio removed: %s", os.path.basename(audio_path))
        return
    day = os.path.join(yt_core.AUDIO_ARCHIVE_DIR, datetime.now().strftime("%Y%m%d"))
    os.makedirs(day, exist_ok=True)
    dest = os.path.join(day, os.path.basename(audio_path))
    os.replace(audio_path, dest)
    os.utime(dest)   # 보관 기한은 전사(보관) 시각 기준 — yt-dlp가 파일 시각을 게시일로 맞추는 설정이어도 흔들리지 않게
    log.info("source audio archived: %s", os.path.basename(audio_path))
    _prune_audio_archive()


def _prune_audio_archive() -> None:
    """보관함에서 AUDIO_RETENTION_DAYS일 지난 오디오(수정 시각 기준)와 빈 날짜 폴더를 지운다."""
    cutoff = time.time() - yt_core.AUDIO_RETENTION_DAYS * 86400
    removed = freed = 0
    try:
        days = os.listdir(yt_core.AUDIO_ARCHIVE_DIR)
    except OSError:
        return
    for sub in days:
        d = os.path.join(yt_core.AUDIO_ARCHIVE_DIR, sub)
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            path = os.path.join(d, fname)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    if removed:
        log.info("audio archive pruned: %d files (%.0f MB, >%dd)", removed, freed / 1024 / 1024,
                 yt_core.AUDIO_RETENTION_DAYS)


def _cleanup_failed_artifacts(audio_path: str, json_path: str, delete_audio: bool) -> None:
    """전사 실패 시 남은 오디오·whisper JSON을 지운다.

    재시도는 언제나 새로 내려받으므로 남겨둘 이유가 없는데, 정리가 없어서 실패가
    반복될수록 쌓였다(2026-08-28: 5시간 팟캐스트 5회 실패로 1.4GB, 전체 2.1GB).
    사용자가 올린 파일(delete_audio=False)은 원본이므로 건드리지 않는다.
    """
    targets = [json_path] + ([audio_path] if delete_audio and yt_core.DELETE_AUDIO_AFTER else [])
    for path in targets:
        try:
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                os.remove(path)
                log.info("failed-job cleanup: %s (%.0f MB)",
                         os.path.basename(path), size / 1024 / 1024)
        except OSError as e:
            log.warning("failed-job cleanup skipped: %s (%s)", os.path.basename(path), e)


def _finish_transcription(job_id: str, audio_path: str, rc: int, md_path: str,
                          delete_audio: bool = False) -> None:
    q = yt_core.jobs[job_id]["queue"]
    json_path = audio_path + ".json"
    if rc == 0:
        transcript = yt_transcribe._parse_transcript(json_path, yt_core.jobs[job_id].get("start_offset") or 0)
        # 방어 B: 길이 대비 본문이 사실상 비어 있으면(무음·잘린 오디오) 성공 처리하지 않는다.
        # 저장까지 하면 요약 LLM 호출과 캡처가 낭비되고 빈 요약본이 이력에 남는다.
        if transcript is not None:
            detail = yt_transcribe._transcript_sparse(job_id, transcript)
            if detail and yt_core.jobs[job_id].get("no_speech") and "음성 없는 영상" not in detail:
                detail += f" — 두 언어({yt_core.jobs[job_id]['no_speech']}) 모두 백지, 음성 없는 영상"
            if detail:
                _set_job_error(job_id, "transcribe", detail)
                log.warning("transcript too sparse: %s — %s", os.path.basename(md_path), detail)
                _cleanup_failed_artifacts(audio_path, json_path, delete_audio)
                q.put(f"오류: {detail}")
                q.put(None)
                return
        if transcript is not None:
            meta = yt_core.jobs[job_id].get("meta", {})
            yt_core._save_md(md_path, meta, transcript)
            yt_core.jobs[job_id].update({"status": "done", "result": transcript, "output_file": md_path})
            # md 저장이 확정된 뒤에만 whisper JSON 삭제(실패 시 재전사 없이 복구 가능).
            try:
                os.remove(json_path)
            except OSError:
                pass
            # 전사본(.md)이 단일 진실 소스. 다운로드한 오디오는 저장 확정 후 보관함으로 옮겨 90일 뒤 지운다.
            # delete_audio=True(URL 잡)이고 KEEP_AUDIO 미설정일 때만. 사용자 업로드 파일은 보존.
            if delete_audio and yt_core.DELETE_AUDIO_AFTER:
                try:
                    _archive_audio(audio_path)
                except OSError as e:
                    log.warning("audio cleanup failed: %s (%s)", os.path.basename(audio_path), e)
            # 썸네일 사본 확보(비공개 전환 대비). 실패해도 전사 결과엔 영향 없음.
            try:
                meta_ = yt_core.jobs[job_id].get("meta") or {}
                save_thumbnail(meta_.get("id") or "",
                               channel_id=yt_listing.channel_id_of(meta_.get("channel_url") or ""))
            except Exception as e:
                log.warning("thumbnail save failed: %s", e)
            log.info("transcription done: %s", os.path.basename(md_path))
            q.put(f"✅ 전사 저장됨: {os.path.basename(md_path)}")
            q.put({"type": "progress", "pct": 100})
            q.put(None)
            return
    if rc == 0:
        detail = "Whisper 결과 파일을 읽을 수 없습니다."
    else:
        tail = yt_core.jobs[job_id].get("last_log") or "상세 로그 없음"
        detail = f"Whisper 전사 실패 (종료 코드 {rc}): {tail}"
    _set_job_error(job_id, "transcribe", detail)
    log.warning("transcription failed (rc=%s): %s", rc, os.path.basename(md_path))
    _cleanup_failed_artifacts(audio_path, json_path, delete_audio)
    q.put(None)


def _transcribe_and_finish(job_id: str, audio_path: str, lang: str,
                           thr: str, total: float, md_path: str,
                           delete_audio: bool = False) -> None:
    """전사 공통 꼬리: stage 전환 → whisper 실행 → 취소 확인 → 결과 저장.

    run_job / run_file_job 양쪽이 공유한다.
    delete_audio=True 면 전사 성공 시 원본 오디오 삭제(URL 다운로드 건 전용).
    """
    q    = yt_core.jobs[job_id]["queue"]
    stop = yt_core.jobs[job_id]["stop_event"]
    # 방어 A: 받아온 오디오가 원본 길이보다 크게 짧으면 다운로드가 잘린 것 —
    # 전사·요약·캡처를 태우기 전에 여기서 실패로 끊는다(재시도는 큐 정책이 맡는다).
    if total > 0:
        actual = yt_core.get_file_duration(audio_path)
        if actual > 0 and actual < total * yt_core.MIN_AUDIO_RATIO:
            detail = (f"오디오가 잘렸습니다: {yt_core._format_duration(actual)} / 원본 "
                      f"{yt_core._format_duration(total)} ({actual / total:.0%})")
            _set_job_error(job_id, "download", detail)
            log.warning("audio truncated: %s (%.0f/%.0fs)", os.path.basename(audio_path), actual, total)
            _cleanup_failed_artifacts(audio_path, audio_path + ".json", delete_audio)
            q.put(f"오류: {detail}")
            q.put(None)
            return
    yt_core._stage(q, "transcribe")
    q.put("Whisper 전사 시작...")
    rc = yt_transcribe._run_whisper(job_id, audio_path, lang, thr, total)
    # 반복 붕괴는 컨텍스트 누적이 원인이라 같은 조건으로 다시 돌려도 똑같이 무너진다.
    # 큐 재시도(다운로드부터 다시)를 태우기 전에, 받아둔 오디오로 즉시 한 번만
    # 컨텍스트를 제한해 다시 전사한다. 정상 전사에는 추가 비용이 없다.
    if rc == 0 and not stop.is_set():
        collapsed, detail = yt_transcribe._looks_collapsed(audio_path + ".json")
        if collapsed:
            log.warning("whisper collapse detected: %s — %s",
                        os.path.basename(audio_path), detail)
            direct = 0 <= yt_transcribe._FIRST_CONTEXT <= yt_transcribe._COLLAPSE_CONTEXT
            if direct:
                # 1차가 이미 컨텍스트 제한 — 같은 재전사는 소용없으니 바로 구간 분할로 간다.
                q.put(f"반복 붕괴 감지({detail}) — {yt_transcribe._CHUNK_SEC // 60}분 구간으로 분할 재전사")
                still, detail2, coverage = True, detail, 0.0
            else:
                q.put(f"반복 붕괴 감지({detail}) — 컨텍스트 제한으로 재전사")
                rc = yt_transcribe._run_whisper(job_id, audio_path, lang, thr, total,
                                  max_context=yt_transcribe._COLLAPSE_CONTEXT)
                still, detail2 = yt_transcribe._looks_collapsed(audio_path + ".json")
                coverage = yt_transcribe._coverage_ratio(audio_path + ".json", total) if rc == 0 else 0.0
            # 반복이 사라졌어도 내용이 비면 실패다 — 세그먼트가 덮는 시간으로 판정한다.
            if rc == 0 and not stop.is_set() and (still or coverage < yt_transcribe._MIN_COVERAGE):
                log.warning("collapse retry insufficient: %s — 붕괴=%s, 커버리지 %.0f%% → 구간 분할 재전사",
                            os.path.basename(audio_path), "예" if still else "아니오", coverage * 100)
                if not direct:
                    q.put(f"재전사 결과가 비어 있음(커버리지 {coverage:.0%}) — {yt_transcribe._CHUNK_SEC // 60}분 구간으로 분할 재전사")
                rc = yt_transcribe._run_whisper_chunked(job_id, audio_path, lang, thr, total)
                coverage = yt_transcribe._coverage_ratio(audio_path + ".json", total) if rc == 0 else 0.0
                log.info("chunked retry coverage: %.0f%%", coverage * 100)
            else:
                log.info("collapse retry result: %s (커버리지 %.0f%%)",
                         "여전히 붕괴" if still else f"정상화({detail2 or 'OK'})", coverage * 100)
    # 언어 오판 폴백: 결과가 백지에 가깝고 대체 언어 후보가 있으면 그 언어로 한 번만
    # 다시 돌린다. 어느 쪽이 맞는지는 결과가 말해 준다 — 백지가 아닌 쪽이 정답이다.
    if rc == 0 and not stop.is_set() and lang == "auto":
        alt = yt_core.jobs[job_id].get("alt_language") or ""
        used = yt_core.jobs[job_id].get("resolved_language") or ""
        if alt and alt != used:
            sparse = yt_transcribe._transcript_sparse(
                job_id, yt_transcribe._parse_transcript(audio_path + ".json", yt_core.jobs[job_id].get("start_offset") or 0))
            if sparse:
                log.warning("sparse with lang=%s (%s) — retry with alt lang=%s: %s",
                            used, sparse, alt, os.path.basename(audio_path))
                q.put(f"언어 오판 의심({used}: {sparse}) — {alt}로 재전사")
                yt_core.jobs[job_id]["resolved_language"] = alt
                yt_core.jobs[job_id]["alt_language"] = ""
                rc = yt_transcribe._run_whisper(job_id, audio_path, alt, thr, total)
                # 두 언어 모두 백지면 언어 문제가 아니라 말이 없는 오디오다(2026-09-03
                # 지식채널e: 화면 자막+배경음악 구성, 유튜브 ASR도 5분에 가사 8줄뿐).
                # 표시해 두면 finish가 사유에 남기고 모니터는 재시도 없이 종료한다.
                if rc == 0 and yt_transcribe._transcript_sparse(
                        job_id, yt_transcribe._parse_transcript(audio_path + ".json",
                                                  yt_core.jobs[job_id].get("start_offset") or 0)):
                    yt_core.jobs[job_id]["no_speech"] = f"{used}·{alt}"
    if stop.is_set():
        yt_core.jobs[job_id]["status"] = "cancelled"
        q.put("중지됨.")
        q.put(None)
        return
    _finish_transcription(job_id, audio_path, rc, md_path, delete_audio=delete_audio)


# ── Job runners ───────────────────────────────────────────────────────

def _job_guard(fn, job_id: str, *a) -> None:
    """워커 스레드 최상위 가드: 예상치 못한 예외로 스레드가 죽어도
    job status를 error로 내리고 SSE done 센티넬(None)을 넣어 UI가 멈추지 않게 한다."""
    try:
        fn(job_id, *a)
    except Exception as e:
        log.exception("job %s crashed", job_id)
        try:
            _set_job_error(job_id, "worker", f"처리기 내부 오류: {e}")
            j = yt_core.jobs.get(job_id)
            if j:
                j["queue"].put(f"오류: {e}")
                j["queue"].put(None)
        except Exception:
            pass


def _fetch_video_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


def _apply_video_info(job_id: str, info: dict, start_sec: int) -> tuple[str, str, float, float]:
    """yt-dlp 메타를 잡 상태에 싣고 (title, uploader, total, clip)을 돌려준다.

    run_job이 처음 받은 영상과, 원본으로 갈아탄 뒤의 영상 양쪽에 같은 방식으로 쓴다."""
    title    = yt_core._nfc(info.get("title", "audio"))
    uploader = yt_core._nfc(info.get("uploader") or info.get("channel") or "—")
    total    = float(info.get("duration") or 0)
    clip = max(0.0, total - start_sec) if (start_sec and total) else total
    yt_core.jobs[job_id]["total_duration"] = clip
    yt_core.jobs[job_id]["start_offset"]   = start_sec
    yt_core.jobs[job_id]["clip_duration"]  = clip
    yt_core.jobs[job_id]["meta"] = {k: yt_core._nfc(info[k]) for k in yt_core._META_KEYS if info.get(k) is not None}
    # 제작자가 선언한 언어(yt-dlp `language`, 예: ko / en-US). whisper 표본 감지가 음악
    # 인트로에 걸려 빗나갈 때 이걸 우선한다 — _choose_language 참조.
    yt_core.jobs[job_id]["declared_language"] = yt_transcribe._norm_lang(info.get("language"))
    if start_sec:
        # md 프론트매터에는 구간 정보를 남기고 duration은 실제 전사 길이로 맞춘다.
        yt_core.jobs[job_id]["meta"]["clip_start"] = start_sec
        if clip:
            yt_core.jobs[job_id]["meta"]["duration"] = clip
    return title, uploader, total, clip


def run_job(job_id: str, params: dict) -> None:
    q    = yt_core.jobs[job_id]["queue"]
    stop = yt_core.jobs[job_id]["stop_event"]
    url  = params["url"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 8))
    try:
        start_sec = max(0, int(params.get("start_sec") or 0))
    except (TypeError, ValueError):
        start_sec = 0

    q.put("영상 정보 가져오는 중...")
    try:
        info = _fetch_video_info(url)
    except Exception as e:
        log.warning("video info fetch failed: %s — %s", url, e)
        detail = _set_job_error(job_id, "metadata", f"영상 정보 조회 실패: {e}")
        q.put(f"오류: {detail}")
        q.put(None)
        return

    # 번역·재게시 영상(비즈카페 등)도 그 영상을 그대로 전사한다. 원본 영상 링크는 찾지도
    # 달지도 않는다(2026-11-10 사용자 지시로 설명문 링크 읽기까지 삭제).
    title, uploader, total, clip = _apply_video_info(job_id, info, start_sec)

    if start_sec and total and start_sec >= total - 5:
        detail = _set_job_error(
            job_id, "metadata",
            f"시작 시각({yt_core._format_duration(start_sec)})이 영상 길이"
            f"({yt_core._format_duration(total)})를 벗어납니다.")
        q.put(f"오류: {detail}")
        q.put(None)
        return

    def _announce(title_: str, total_: float, clip_: float, start_: int) -> None:
        if total_ > 0:
            if start_:
                q.put(f"영상 길이: {yt_core._format_duration(total_)} — "
                      f"{yt_core._format_duration(start_)}부터 전사({yt_core._format_duration(clip_)})")
            else:
                q.put(f"영상 길이: {yt_core._format_duration(total_)}")
            q.put({"type": "duration", "seconds": clip_})
        q.put({"type": "videoinfo", "title": title_, "uploader": uploader})

    _announce(title, total, clip, start_sec)

    out_dir = yt_core._dated_dir()

    def _plan_paths(title_: str, clip_: float, start_: int) -> tuple[str, str]:
        ts   = datetime.now().strftime("%Y%m%d%H%M")
        stem = f"{ts}_{yt_core._dur_tag(clip_)}_{yt_core._safe_stem(title_)}"
        if start_:
            stem += f"_from{int(start_)}s"
        path = yt_core.unique_path(out_dir, stem, ".mp3")
        return path, os.path.splitext(os.path.basename(path))[0]

    audio_path, stem_final = _plan_paths(title, clip, start_sec)

    def _progress_hook(d):
        if stop.is_set():
            raise Exception("사용자가 취소함")
        if d["status"] == "downloading":
            q.put(f"  다운로드: {d.get('_percent_str', '').strip()}  {d.get('_speed_str', '').strip()}")
        elif d["status"] == "finished":
            q.put("다운로드 완료")

    def _download_once(url_: str, title_: str, total_: float, audio_path_: str) -> str | None:
        """오디오를 받는다. 성공하면 None, 실패하면 오류 문자열. 취소는 예외 대신 문자열."""
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.splitext(audio_path_)[0],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "ffmpeg_location": yt_core.FFMPEG_LOCATION,
            "quiet": True,
            "progress_hooks": [_progress_hook],
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 1,
        }
        yt_core._stage(q, "download")
        q.put(f"다운로드 중: {title_}")
        def _check_length() -> None:
            # 스트림을 중간에 끊는 차단 변형은 yt-dlp가 예외 없이 끝난다
            # (실례: 68분 영상이 6회 연속 5~7분에서 잘림). 길이가 크게 모자라면
            # 잘린 파일을 지우고 403과 동일하게 다음 단계로 넘긴다.
            if total_ > 0 and os.path.exists(audio_path_):
                actual = yt_core.get_file_duration(audio_path_)
                if actual > 0 and actual < total_ * yt_core.MIN_AUDIO_RATIO:
                    os.remove(audio_path_)
                    raise Exception(
                        f"HTTP Error 403(스트림 잘림): {actual:.0f}/{total_:.0f}s")

        def _on_retry(dl_attempt: int, wait: int, nxt: str, e: Exception) -> None:
            log.info("download 403 — %s 후 %s으로 재시도(%d/2): %s (%s)",
                     f"{wait}초" if wait else "즉시", nxt, dl_attempt, title_, e)
            q.put(f"403 차단 감지 — {f'{wait}초 뒤 ' if wait else ''}{nxt}으로 재시도")

        try:
            # 403 재시도: 1차 즉시(다른 실험 버킷 기대) → 2차 75초 뒤(새 세션·새 토큰) →
            # 3차는 대체 클라이언트로 전환(틀은 yt_download.download_with_retry).
            yt_download.download_with_retry(
                url_, ydl_opts, alt_attempts=(3,), retryable=yt_download.is_403,
                after_download=_check_length, on_retry=_on_retry, should_stop=stop.is_set)
        except Exception as e:
            return "사용자가 취소함" if stop.is_set() else str(e)
        if stop.is_set():
            return "사용자가 취소함"
        if not os.path.exists(audio_path_):
            return "다운로드 결과 오디오 파일을 찾을 수 없습니다."
        return None

    dl_err = _download_once(url, title, total, audio_path)
    if dl_err:
        if stop.is_set():
            yt_core.jobs[job_id]["status"] = "cancelled"
        else:
            log.warning("download failed: %s — %s", title, dl_err)
            detail = _set_job_error(job_id, "download", f"영상 다운로드 실패: {dl_err}")
            q.put(f"다운로드 오류: {detail}")
        q.put(None)
        return

    if start_sec:
        yt_core._stage(q, "download")
        q.put(f"{yt_core._format_duration(start_sec)} 이전 구간 잘라내는 중...")
        try:
            yt_transcribe._trim_audio_head(audio_path, start_sec)
        except Exception as e:
            log.warning("audio trim failed: %s — %s", os.path.basename(audio_path), e)
            detail = _set_job_error(job_id, "download", f"시작 시각 잘라내기 실패: {e}")
            q.put(f"오류: {detail}")
            q.put(None)
            return

    q.put(f"저장됨: {os.path.basename(audio_path)}")
    _transcribe_and_finish(job_id, audio_path, lang, thr, clip,
                           yt_core.unique_path(out_dir, stem_final, ".md"),
                           delete_audio=True)  # URL 다운로드본은 전사 후 삭제


def run_file_job(job_id: str, file_path: str, params: dict) -> None:
    q    = yt_core.jobs[job_id]["queue"]
    stop = yt_core.jobs[job_id]["stop_event"]
    lang = params.get("language", "auto")
    thr  = str(params.get("threads", 8))

    q.put(f"파일: {os.path.basename(file_path)}")

    title, uploader = "N/A", "N/A"
    meta_json = os.path.splitext(file_path)[0] + ".json"
    if os.path.exists(meta_json):
        try:
            with open(meta_json, encoding="utf-8") as f:
                m = json.load(f)
            title    = yt_core._nfc(m.get("title") or "N/A")
            uploader = yt_core._nfc(m.get("uploader") or m.get("channel") or "N/A")
        except Exception:
            pass
    q.put({"type": "videoinfo", "title": title, "uploader": uploader})

    total = yt_core.get_file_duration(file_path)
    yt_core.jobs[job_id]["total_duration"] = total
    yt_core.jobs[job_id]["meta"] = {
        "title":       title if title != "N/A" else "",
        "uploader":    uploader if uploader != "N/A" else "",
        "duration":    total,
        "source_file": yt_core._nfc(os.path.basename(file_path)),
    }
    if total > 0:
        q.put(f"파일 길이: {yt_core._format_duration(total)}")
        q.put({"type": "duration", "seconds": total})

    out_dir = yt_core._dated_dir()
    stem    = datetime.now().strftime("%Y%m%d%H%M_") + os.path.splitext(os.path.basename(file_path))[0]
    _transcribe_and_finish(job_id, file_path, lang, thr, total,
                           yt_core.unique_path(out_dir, stem, ".md"))


def _cleanup_old_jobs() -> None:
    while True:
        time.sleep(1800)
        cutoff = time.time() - 3600
        with yt_core.jobs_lock:
            stale = [jid for jid, j in yt_core.jobs.items()
                     if j["status"] != "running" and j["start_time"] < cutoff]
            for jid in stale:
                yt_core.jobs.pop(jid)
