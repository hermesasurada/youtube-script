"""영상 키프레임 모듈 (서버 전용) — app.py의 /keyframes에서 generate_keyframes()로 호출.

흐름:
  1) 저해상도 영상 다운로드(yt-dlp) → 전사의 시각 단서+균등 간격+장면전환 후보 추출
  2) 비전 LLM에 이미지와 인접 전사를 함께 보내 (a)자료성·품질 판별 (b)요약 소제목에 정렬
     - keep=false: 화자 토킹헤드/청중/블랙·전환/정보없음/중복 → 제거
     - keep=true : 슬라이드·차트·다이어그램·스크린샷·제품/기기/시연 등 자료성
  3) 프레임을 {stem}.frames/ 에 저장하고 요약 md 소제목 아래에 kf-strip(<div>)으로 주입(멱등)
"""
from __future__ import annotations
import html as _html
import os
import re
import shutil
import tempfile

import document_io

import kf_base
import kf_frames
import kf_targets
import kf_vision


def log(*a): print(*a, flush=True)


# ── 서버 통합 모드: 요약 md에 키프레임 스트립 주입 ──────────────────

_KF_STRIP_RE = re.compile(r"\n*<div class=\"kf-strip\">.*?</div>[ \t]*\n*", re.S)
_KF_APX_RE   = re.compile(r"\n*##\s*기타 자료 캡처[ \t]*\n*")
_KF_FIGURE_RE = re.compile(
    r'<figure>.*?src="[^"]*/(?P<file>kf_(?P<ts>\d{5})\.jpg)".*?</figure>', re.S
)
_KF_EMPTY_STRIP_RE = re.compile(r"\n*<div class=\"kf-strip\">\s*</div>[ \t]*\n*", re.S)


def _remove_opening_figures(
    text: str, window: tuple[float, float]
) -> tuple[str, list[str]]:
    removed: list[str] = []
    start, end = window

    def replace(match: re.Match) -> str:
        if start <= int(match.group("ts")) <= end:
            removed.append(match.group("file"))
            return ""
        return match.group(0)

    cleaned = _KF_FIGURE_RE.sub(replace, text)
    cleaned = _KF_EMPTY_STRIP_RE.sub("\n\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
    return cleaned, removed


def prune_summary_opening_frames(summary_md_path: str) -> dict:
    """Remove previously generated Sequoia opening frames from one summary."""
    window = kf_targets._opening_exclusion_for_summary(summary_md_path)
    if not window:
        return {"removed": 0, "window": None}
    with open(summary_md_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    cleaned, removed = _remove_opening_figures(text, window)
    if not removed:
        return {"removed": 0, "window": window}

    document_io.atomic_write_text(summary_md_path, cleaned)
    frame_dir = os.path.splitext(summary_md_path)[0] + ".frames"
    for filename in set(removed):
        try:
            os.remove(os.path.join(frame_dir, filename))
        except FileNotFoundError:
            pass
    return {"removed": len(set(removed)), "window": window}


# 캡션은 각주 대상이 아니다(2026-09-10 지시). 비전 모델이 본문 문체를 따라 용어 뒤에
# 별표를 붙여 오는 경우가 있어(최근 요약 120건 중 26건) 주입 시점에 떼어 낸다.
_CAPTION_MARK_RE = re.compile(r"\\?\*+")


def _caption_text(caption: str | None) -> str:
    """이미지 캡션에서 용어 각주 표시(별표)를 제거한다."""
    cleaned = _CAPTION_MARK_RE.sub("", caption or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _augment_summary_md(md_path: str, headings: list[dict], kept: list[dict], url_base: str):
    """요약 md에 가로 스크롤 kf-strip(<div>) 주입. 재실행 시 기존 주입 제거(멱등).

    배치 정책: '한 소제목당 한 묶음'. 각 섹션의 프레임을 소제목 직후에 하나의
    가로 스크롤 스트립으로 모은다(이미지↔텍스트 잦은 교차 방지). 섹션 간 분산은
    요약의 ### 소제목 수로 자연 확보된다. 주입 <div>는 앞뒤 빈 줄로 독립 블록화.
    """
    text = open(md_path, encoding="utf-8", errors="replace").read()
    # 이전 주입 제거(멱등) — 제거 후 문단이 붙지 않도록 빈 줄로 치환하고 과한 공백 정리
    text = _KF_STRIP_RE.sub("\n\n", text)
    text = _KF_APX_RE.sub("\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    nheads = len(headings)

    bysec: dict = {}
    for k in kept:
        s = k["section"] if (k.get("section") is not None and 0 <= k["section"] < nheads) else None
        bysec.setdefault(s, []).append(k)
    for arr in bysec.values():
        arr.sort(key=lambda x: x["ts"])

    def strip_html(items):
        # 이미지 아래 캡션(시각 + 한 줄 설명)은 달지 않는다(2026-10-24 사용자 지시) — 설명은 대체 텍스트로만 남긴다
        figs = "".join(
            f'<figure><img src="{url_base}/{it["file"]}" loading="lazy" '
            f'alt="{_html.escape(_caption_text(it.get("caption")), quote=True)}"></figure>'
            for it in items)
        return f'<div class="kf-strip">{figs}</div>'

    lines = text.split("\n")
    # 각 섹션(소제목)의 heading 줄 인덱스
    head_line_of_sec: dict = {}
    hc = -1
    for i, ln in enumerate(lines):
        if re.match(r"^#{2,3}\s+", ln):
            hc += 1
            head_line_of_sec[hc] = i

    inserts: dict = {}                # heading 줄 인덱스 -> strip_html (섹션당 1개)
    for sec, frames in bysec.items():
        if sec is None:
            continue
        hl = head_line_of_sec.get(sec)
        if hl is not None:
            inserts[hl] = strip_html(frames)

    # 주입되는 <div>는 앞뒤에 빈 줄을 둬야 marked가 독립 HTML 블록으로 처리한다.
    out = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i in inserts:
            out.append("")
            out.append(inserts[i])
            out.append("")
    if None in bysec:                 # 섹션 미정 → 부록
        if out and out[-1].strip() != "":
            out.append("")
        out.append("## 기타 자료 캡처")
        out.append("")
        out.append(strip_html(bysec[None]))
        out.append("")
    document_io.atomic_write_text(md_path, "\n".join(out))


def _commit_keyframes(summary_md_path: str, frames_out_dir: str, headings: list[dict],
                      kept: list[dict], url_base: str) -> None:
    """완성된 프레임 묶음과 요약 변경을 한 트랜잭션처럼 교체한다.

    새 프레임은 대상과 같은 파일시스템의 staging 디렉터리에 모두 복사한 뒤
    디렉터리 rename으로 승격한다. 요약 주입까지 실패하면 기존 프레임을 복원해
    재시도 중에도 마지막 정상 결과가 깨지지 않게 한다.
    """
    parent = os.path.dirname(frames_out_dir)
    os.makedirs(parent, exist_ok=True)
    base = os.path.basename(frames_out_dir)
    staging = tempfile.mkdtemp(prefix=f".{base}.staging-", dir=parent)
    backup = None
    installed = False
    try:
        for k in kept:
            shutil.copy2(k["tmp"], os.path.join(staging, k["file"]))

        if os.path.exists(frames_out_dir):
            if not os.path.isdir(frames_out_dir):
                raise OSError(f"프레임 경로가 디렉터리가 아닙니다: {frames_out_dir}")
            backup = tempfile.mkdtemp(prefix=f".{base}.backup-", dir=parent)
            os.rmdir(backup)  # os.replace 대상 이름만 확보
            os.replace(frames_out_dir, backup)

        os.replace(staging, frames_out_dir)
        staging = ""
        installed = True
        with document_io.summary_lock(summary_md_path):     # 인명 교정과 동시 쓰기 방지
            _augment_summary_md(summary_md_path, headings, kept, url_base)
    except Exception:
        if installed and os.path.isdir(frames_out_dir):
            shutil.rmtree(frames_out_dir)
        if backup and os.path.isdir(backup):
            os.replace(backup, frames_out_dir)
            backup = None
        raise
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging)
        if backup and os.path.isdir(backup):
            shutil.rmtree(backup)


def generate_keyframes(summary_md_path: str, url: str, frames_out_dir: str, url_base: str,
                       *, skip_claude: bool = False, model_order=None) -> dict:
    """서버 진입점: 영상→키프레임→비전 분류·정렬→프레임 저장→요약 md 주입.

    반환: {"ok":bool, "n_frames":int, "reason":str?}
      - reason="download_failed" 면 호출측은 음성 요약만 유지(폴백).
    """
    meta = kf_targets.parse_summary(summary_md_path)
    transcript_body = ""
    transcript_segments: list[tuple[float, str]] = []
    transcript_path = kf_targets._paired_transcript_path(summary_md_path)
    if transcript_path:
        try:
            _, transcript_body = document_io.read_markdown(transcript_path)
            transcript_segments = kf_targets._timestamped_segments(transcript_body, max_seconds=None)
        except OSError as e:
            log(f"    [warn] 캡처용 전사 읽기 실패: {e}")
    target_times = kf_targets._visual_capture_targets(transcript_body, meta["headings"])
    url = (url or meta.get("url") or "").strip()
    if not url:
        return {"ok": False, "reason": "no_url"}
    kept: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        video = kf_frames.download_video(url, os.path.join(tmp, "v"))
        if not video:
            return {"ok": False, "reason": "download_failed"}
        # 무거운 작업(추출·비전)은 임시 폴더에서만 수행 — 완성 전엔 기존 결과를 건드리지 않는다.
        # (중단/재기동되어도 기존 캡처·요약이 보존됨)
        cands = kf_frames.extract_candidates(video, os.path.join(tmp, "f"), target_times)
        opening_window = kf_targets._opening_exclusion_for_summary(summary_md_path)
        if opening_window:
            before = len(cands)
            cands = kf_targets._exclude_opening_candidates(cands, opening_window)
            log(
                "    Sequoia 오프닝 제외: "
                f"{kf_base._hms(opening_window[0])}~{kf_base._hms(opening_window[1])}, "
                f"후보 {before - len(cands)}장 제거"
            )
        if not cands:
            return {"ok": False, "reason": "no_frames"}    # ffmpeg 추출 실패/0장
        verdict = kf_vision.classify_and_assign(
            cands, meta["headings"], transcript_segments=transcript_segments,
            skip_claude=skip_claude,
            model_order=model_order,
            title=meta.get("title") or os.path.basename(summary_md_path),
        )  # 중복은 비전이 keep=false로
        if verdict is None:
            # 비전 호출 실패 — 실패 사유를 error로 전달(호출측이 usage limit/장애를 분류)
            return {"ok": False, "reason": "vision_failed", "error": kf_vision._LAST_VISION_ERR}
        for ts, path in cands:
            v = verdict.get(os.path.basename(path), {})
            if not v.get("keep"):
                continue
            item = {"ts": ts, "tmp": path, "file": f"kf_{int(ts):05d}.jpg",
                    "section": v.get("section"), "caption": v.get("caption", "")}
            score_parts = []
            for key in ("relevance", "information", "readability"):
                try:
                    score_parts.append(max(0.0, min(5.0, float(v[key]))))
                except (KeyError, TypeError, ValueError):
                    pass
            if score_parts:
                item["_vision_score"] = sum(score_parts) / len(score_parts)
            kept.append(item)
        kept = kf_vision._dedupe_same_second(kept)
        if not kept:
            return {"ok": True, "n_frames": 0, "reason": "no_material"}
        # ── 여기서부터 교체(완성본만 승격, 실패하면 기존 프레임 복원) ──
        kf_vision._assign_sections_by_time(kept, meta["headings"])   # 의미 배정이 없을 때만 시간범위 폴백
        kept = kf_vision._limit_frames_per_section(kept)
        _commit_keyframes(summary_md_path, frames_out_dir, meta["headings"], kept, url_base)
    return {"ok": True, "n_frames": len(kept)}
