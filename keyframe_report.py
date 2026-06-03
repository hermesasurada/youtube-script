#!/usr/bin/env python3
"""[프로토타입] 영상 키프레임 + 요약 → HTML 리포트 (Tier 2: 자료성 필터 + 섹션 정렬).

흐름:
  1) 저해상도 영상 다운로드 → ffmpeg 장면전환 후보 프레임 추출(+타임스탬프)
  2) Claude CLI 비전 1회 호출로 각 프레임을 (a)자료성 여부 판별 (b)요약 소제목에 정렬
     - keep=false: 화자 토킹헤드/청중/블랙·전환/정보없음 → 제거
     - keep=true : 슬라이드·차트·다이어그램·스크린샷·제품/기기/시연 등 자료성
  3) 요약 본문 각 소제목 아래에 해당 프레임을 끼워 넣어 리포트 HTML 생성(이미지 base64 내장)

사용:
  python keyframe_report.py <summary.md>                 # 다운로드+추출+분류
  python keyframe_report.py <summary.md> --frames-dir D  # 기존 프레임 재사용(파일명 NN_MMSS.jpg)
"""
from __future__ import annotations

import argparse
import base64
import glob
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
FFMPEG      = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE     = shutil.which("ffprobe") or "ffprobe"
FFMPEG_DIR  = os.path.dirname(FFMPEG)

def _claude_bin() -> str:
    """claude CLI 경로(호출 시마다 재해석) — Claude 자동업데이트로 버전 폴더가 바뀌어도
    삭제된 옛 경로를 쓰지 않도록 '존재하는 최신 설치본'을 고른다."""
    env = os.environ.get("CLAUDE_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("claude")
    if found:
        return found
    cands = [c for c in glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"))
        if os.path.exists(c)]
    if cands:
        cands.sort(key=os.path.getmtime)   # 가장 최근 설치본(존재하는 것만)
        return cands[-1]
    return env or "claude"
SCENE_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("SCENE_THRESHOLD", "0.3"))))  # ffmpeg 필터 삽입값 — [0,1] clamp
MAX_CANDIDATES  = int(os.environ.get("MAX_CANDIDATES", "40"))
MIN_GAP         = int(os.environ.get("MIN_GAP", "6"))   # 근접 중복 제거 간격(초)
VISION_MODEL    = os.environ.get("VISION_MODEL", "sonnet")
VIDEO_MAXH      = int(os.environ.get("VIDEO_MAXH", "1080"))   # 다운로드 최대 높이(해상도)
FRAME_WIDTH     = int(os.environ.get("FRAME_WIDTH", "1708"))  # 프레임 가로폭(이전 854의 2배)
# subprocess 타임아웃(초) — 멈춘 ffmpeg/ffprobe/claude가 키프레임 세마포어를 영구 점유하는 것 방지
PROBE_TIMEOUT   = int(os.environ.get("PROBE_TIMEOUT", "30"))
FFMPEG_TIMEOUT  = int(os.environ.get("FFMPEG_TIMEOUT", "600"))
VISION_TIMEOUT  = int(os.environ.get("VISION_TIMEOUT", "300"))


def log(*a): print(*a, flush=True)
def _hms(sec: float) -> str:
    s = int(sec); h, r = divmod(s, 3600); m, s = divmod(r, 60)
    return f"{h:01d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ── 요약 파싱: 제목/URL/본문/소제목(##,###) ──────────────────────────

def parse_summary(md_path: str) -> dict:
    text = open(md_path, encoding="utf-8", errors="replace").read()
    title = (re.search(r"^#\s+(.+)$", text, re.M) or [None, os.path.basename(md_path)])
    title = title.group(1).strip() if hasattr(title, "group") else title[1]
    m = re.search(r"\|\s*URL\s*\|\s*(\S+)\s*\|", text)
    url = m.group(1).strip() if m else ""
    headings = []
    for mm in re.finditer(r"^(#{2,3})\s+(.+)$", text, re.M):
        htext = mm.group(2).strip()
        headings.append({"idx": len(headings), "level": len(mm.group(1)),
                         "text": htext, "start": _parse_ts_label(htext)})
    return {"title": title, "url": url, "markdown": text, "headings": headings}


def _parse_ts_label(text: str) -> float | None:
    """소제목의 [mm:ss] 또는 [h:mm:ss] 라벨(앞/뒤 무관) → 시작 초. 없으면 None."""
    m = re.search(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]", text)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    return (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))


# ── 프레임 확보 ───────────────────────────────────────────────────────

def download_video(url: str, out_noext: str) -> str | None:
    import yt_dlp
    log(f"[1] 영상 다운로드(≤{VIDEO_MAXH}p): {url}")
    with yt_dlp.YoutubeDL({
        "format": f"bestvideo[height<={VIDEO_MAXH}]+bestaudio/best[height<={VIDEO_MAXH}]/best",
        "outtmpl": out_noext + ".%(ext)s", "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_DIR, "quiet": True, "no_warnings": True,
        "socket_timeout": 60, "retries": 5,
    }) as ydl:
        ydl.download([url])
    for ext in ("mp4", "mkv", "webm", "m4v"):
        if os.path.exists(out_noext + "." + ext):
            return out_noext + "." + ext
    return None


def _probe_duration(video: str) -> float:
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-show_format", "-print_format", "json", video],
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        log(f"    [warn] ffprobe 실패/타임아웃: {e}")
        return 0.0


def extract_candidates(video: str, outdir: str) -> list[tuple[float, str]]:
    """하이브리드 후보 추출: 균등 간격(전 구간 커버) + 장면전환(컷/데모) → 근접 중복 제거 → 캡.

    화면공유/슬라이드형(정지 화면 길게 유지)은 장면전환만으론 누락 → 균등 간격으로 보강.
    """
    os.makedirs(outdir, exist_ok=True)
    pairs: list[tuple[float, str]] = []
    dur = _probe_duration(video)

    # 1) 균등 간격 — 전체 타임라인 보장 커버
    if dur > 0:
        interval = max(15, int(dur / MAX_CANDIDATES))
        try:
            ri = subprocess.run([FFMPEG, "-hide_banner", "-i", video,
                            "-vf", f"fps=1/{interval},scale={FRAME_WIDTH}:-2", "-vsync", "vfr",
                            "-q:v", "3", os.path.join(outdir, "iv_%03d.jpg")],
                           capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
            if ri.returncode != 0:
                log(f"    [warn] ffmpeg 균등추출 rc={ri.returncode}: {ri.stderr.strip().splitlines()[-1] if ri.stderr.strip() else ''}")
        except subprocess.TimeoutExpired:
            log(f"    [warn] ffmpeg 균등추출 {FFMPEG_TIMEOUT}s 타임아웃 — 부분 결과 사용")
        for i, f in enumerate(sorted(g for g in os.listdir(outdir) if g.startswith("iv_"))):
            pairs.append((i * interval, os.path.join(outdir, f)))
        log(f"[2] 균등 간격 {interval}s × {len([p for p in pairs])}장 (영상 {int(dur)}s)")

    # 2) 장면전환 — 짧은 컷/데모/B-roll 포착
    scene_stderr = ""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", video,
                            "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo,scale={FRAME_WIDTH}:-2",
                            "-vsync", "vfr", "-q:v", "3", os.path.join(outdir, "scene_%03d.jpg")],
                           capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        scene_stderr = r.stderr or ""
        if r.returncode != 0:
            log(f"    [warn] ffmpeg 장면추출 rc={r.returncode}: {scene_stderr.strip().splitlines()[-1] if scene_stderr.strip() else ''}")
    except subprocess.TimeoutExpired as e:
        scene_stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) if e.stderr else ""
        log(f"    [warn] ffmpeg 장면추출 {FFMPEG_TIMEOUT}s 타임아웃 — 부분 결과 사용")
    times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", scene_stderr)]
    sc = sorted(g for g in os.listdir(outdir) if g.startswith("scene_"))
    if len(times) < len(sc):   # 타임스탬프 누락 → 해당 프레임은 0.0(시작)으로 잘못 배치될 수 있음
        log(f"    [warn] 장면 pts_time {len(times)}개 < 프레임 {len(sc)}장 — 일부 시각 미상(0.0)")
    for i, f in enumerate(sc):
        pairs.append((times[i] if i < len(times) else 0.0, os.path.join(outdir, f)))
    log(f"    장면전환 {len(sc)}장 → 후보 합계 {len(pairs)}장")

    # 3) 시간순 정렬 → 근접 중복 제거 → 캡
    #    장면전환(scene_) 프레임은 의미있는 컷이므로 6초 근접 제거에서 '보존'하고,
    #    균등 간격(iv_) 프레임만 직전 채택분과 6초 미만이면 솎는다.
    is_scene = lambda p: os.path.basename(p).startswith("scene_")
    pairs.sort(key=lambda x: x[0])
    deduped, last = [], -999.0
    for ts, p in pairs:
        if is_scene(p) or (ts - last >= MIN_GAP):
            deduped.append((ts, p)); last = ts
    # 캡: 초과 시에도 장면전환 우선 보존(균등부터 솎고, 장면전환만 초과하면 그때 균등 샘플)
    if len(deduped) > MAX_CANDIDATES:
        scenes = [x for x in deduped if is_scene(x[1])]
        ivs    = [x for x in deduped if not is_scene(x[1])]
        if len(scenes) >= MAX_CANDIDATES:
            step = len(scenes) / MAX_CANDIDATES
            sel = [scenes[int(i * step)] for i in range(MAX_CANDIDATES)]
        else:
            need = MAX_CANDIDATES - len(scenes)
            step = (len(ivs) / need) if need else 1
            sel = scenes + [ivs[int(i * step)] for i in range(need)] if ivs else scenes
        deduped = sorted(sel, key=lambda x: x[0])
    log(f"    중복제거·캡 후 후보 {len(deduped)}장 (장면전환 보존)")
    return deduped


def load_frames_dir(d: str) -> list[tuple[float, str]]:
    out = []
    for f in sorted(glob.glob(os.path.join(d, "*.jpg"))):
        m = re.search(r"_(\d{4})\.jpg$", f)  # NN_MMSS.jpg
        ts = (int(m.group(1)[:2]) * 60 + int(m.group(1)[2:])) if m else 0.0
        out.append((float(ts), f))
    log(f"[2*] 기존 프레임 재사용 {len(out)}장 ({d})")
    return out


# ── 비전: 자료성 판별 + 섹션 정렬 (단일 호출) ─────────────────────────

def classify_and_assign(frames: list[tuple[float, str]], headings: list[dict]) -> dict:
    names = [os.path.basename(p) for _, p in frames]
    hlist = "\n".join(f'{h["idx"]}: {h["text"]}' for h in headings)
    refs  = " ".join("@" + p for _, p in frames)
    prompt = f"""영상 캡처 {len(frames)}장을 분석한다. 파일명(순서대로):
{chr(10).join(names)}

[요약 소제목 목록]
{hlist}

각 캡처에 대해 판단:
- keep=true 조건: 슬라이드·차트/그래프·다이어그램·스크린샷·데이터 시각화·제품/기기/시연/수술·장비 등 '자료로서 정보를 전달'하는 화면
- keep=false 조건: 발표자/진행자/인터뷰이 얼굴·토킹헤드, 청중, 블랙/전환 화면, 흐릿하거나 정보없는 장면
- **중복 제거**: 여러 장이 동일하거나 거의 같은 화면(같은 슬라이드·같은 장면·같은 인물 구도)이면, 그 중 가장 선명하고 정보가 잘 보이는 1장만 keep=true로 하고 나머지는 keep=false. 단 차트의 데이터·텍스트·피사체 등 '내용'이 다르면 서로 다른 것으로 보고 각각 유지할 것.
- keep=true면 위 소제목 중 내용상 가장 관련있는 번호(section)와 8단어 이내 한국어 caption 부여

JSON 배열로만 답하라(다른 설명 금지):
[{{"name":"<파일명>","keep":<bool>,"section":<번호 or null>,"type":"slide|chart|diagram|screenshot|product|demo|broll|talking_head|decorative","caption":"<8단어 이내>"}}]

이미지: {refs}"""
    log(f"[3] 비전 분류+정렬 ({len(frames)}장, 1회 호출)")

    def _call() -> list | None:
        try:
            r = subprocess.run([_claude_bin(), "-p", "--model", VISION_MODEL, prompt],
                               capture_output=True, text=True, timeout=VISION_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"  비전 호출 {VISION_TIMEOUT}s 타임아웃")
            return None
        out = (r.stdout or "").strip()
        # 코드펜스 제거
        out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out, flags=re.M).strip()
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, list) else None
        except (json.JSONDecodeError, ValueError):
            return None

    data = _call()
    if data is None:               # 1회 재시도(모델 형식 일탈/타임아웃 대비)
        log("  비전 응답 실패 → 재시도")
        data = _call()
    if data is None:
        # None = '비전 호출 자체 실패'(파싱/타임아웃). 빈 dict('자료 없음')과 구분해 호출측에 전파.
        log("  비전 호출 실패(재시도 후) — vision_failed")
        return None
    res = {d["name"]: d for d in data if isinstance(d, dict) and d.get("name")}
    kept = sum(1 for d in res.values() if d.get("keep"))
    log(f"    유지 {kept} / 드롭 {len(res) - kept}")
    return res


# ── 리포트 HTML ───────────────────────────────────────────────────────

REPORT_TMPL = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
<style>
 :root{{--fg:#1a1a1a;--muted:#666;--border:#e5e5e5;--accent:#2563eb;}}
 *{{box-sizing:border-box;}}
 body{{margin:0;font-family:-apple-system,'Pretendard',system-ui,sans-serif;color:var(--fg);background:#f5f5f5;line-height:1.75;}}
 .wrap{{max-width:960px;margin:0 auto;padding:2rem 1.5rem 4rem;}}
 .hd{{border-bottom:2px solid var(--fg);padding-bottom:1rem;margin-bottom:1.5rem;}}
 .hd h1{{font-size:1.6rem;margin:0 0 .4rem;letter-spacing:-.02em;}}
 .hd a{{color:var(--accent);font-size:.85rem;word-break:break-all;}}
 .hd .sub{{color:var(--muted);font-size:.82rem;margin-top:.3rem;}}
 .card{{background:#fff;border:1px solid var(--border);border-radius:12px;padding:1.5rem 1.9rem 2.2rem;}}
 .md h2{{font-size:1.2rem;border-bottom:1px solid var(--border);padding-bottom:.3rem;margin:2rem 0 .6rem;}}
 .md h3{{font-size:1.02rem;color:var(--accent);margin:1.4rem 0 .4rem;}}
 .md table{{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.9rem;}}
 .md th,.md td{{border:1px solid var(--border);padding:.45rem .7rem;text-align:left;}}
 .md th{{background:#fafafa;}} .md strong{{font-weight:700;}}
 .shots{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.8rem;margin:.8rem 0 1.4rem;}}
 figure{{margin:0;border:1px solid var(--border);border-radius:9px;overflow:hidden;background:#fff;}}
 figure img{{width:100%;display:block;cursor:zoom-in;}}
 .lb{{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;z-index:999;cursor:zoom-out;padding:1.5rem;}}
 .lb.open{{display:flex;}}
 .lb img{{max-width:96vw;max-height:92vh;border-radius:4px;box-shadow:0 6px 40px rgba(0,0,0,.5);}}
 figcaption{{font-size:.8rem;color:var(--muted);padding:.45rem .6rem;}}
 figcaption b{{color:var(--accent);font-family:ui-monospace,monospace;font-weight:600;margin-right:.4rem;}}
 .appendix-hd{{font-size:1.1rem;border-bottom:1px solid var(--border);padding-bottom:.3rem;margin:2.5rem 0 1rem;}}
</style></head><body><div class="wrap">
 <div class="hd"><h1>{title}</h1>{url_html}
  <div class="sub">자료성 키프레임 {nkept}장 · 섹션 정렬 리포트(프로토타입)</div></div>
 <div class="card"><div class="md" id="md"></div>
  <div id="appendix"></div></div>
<script id="src" type="text/plain">{markdown}</script>
<script id="shots" type="application/json">{shots_json}</script>
<script>
 const R=new marked.Renderer();
 R.link=(h,t,x)=>{{const u=(typeof h==='object')?h.href:h;const tx=(typeof h==='object')?(h.text||u):(x||u);return `<a href="${{u}}" target="_blank" rel="noopener">${{tx}}</a>`;}};
 marked.use({{renderer:R,gfm:true,breaks:false}});
 function fixCjk(s){{const st=[];s=s.replace(/```[\\s\\S]*?```|`[^`\\n]+`/g,m=>{{st.push(m);return '\\x01'+(st.length-1)+'\\x01';}});
  s=s.replace(/\\*\\*([^\\s][^\\n]*?[^\\s]|\\S)\\*\\*/g,(_,t)=>'<strong>'+t+'</strong>');
  return s.replace(/\\x01(\\d+)\\x01/g,(_,i)=>st[+i]);}}
 function fig(s){{return `<figure><img src="data:image/jpeg;base64,${{s.b64}}" loading="lazy"><figcaption><b>${{s.ts}}</b>${{s.caption||''}}</figcaption></figure>`;}}
 let md=document.getElementById('src').textContent.replace(/^---\\n[\\s\\S]*?\\n---\\n?/,'');
 document.getElementById('md').innerHTML=marked.parse(fixCjk(md));
 const shots=JSON.parse(document.getElementById('shots').textContent);
 const heads=[...document.querySelectorAll('#md h2,#md h3')];
 const used=new Set();
 // 섹션별 프레임 삽입(소제목 바로 뒤)
 const bySec={{}};
 shots.forEach((s,i)=>{{ if(s.section!=null&&heads[s.section]){{(bySec[s.section]=bySec[s.section]||[]).push(s);used.add(i);}} }});
 Object.entries(bySec).forEach(([idx,arr])=>{{
   const div=document.createElement('div');div.className='shots';div.innerHTML=arr.map(fig).join('');
   heads[idx].insertAdjacentElement('afterend',div);
 }});
 // 섹션 미정 프레임 → 부록
 const rest=shots.filter((s,i)=>!used.has(i));
 if(rest.length){{document.getElementById('appendix').innerHTML=
   '<div class="appendix-hd">기타 자료 캡처</div><div class="shots">'+rest.map(fig).join('')+'</div>';}}
 // 라이트박스: 캡처 클릭 시 원본(풀 해상도) 확대 (이벤트 위임)
 document.addEventListener('click',e=>{{const im=e.target.closest('.shots figure img');if(!im)return;const o=document.getElementById('lb');document.getElementById('lb-img').src=im.src;o.classList.add('open');}});
 document.addEventListener('keydown',e=>{{if(e.key==='Escape')document.getElementById('lb').classList.remove('open');}});
</script>
<div class="lb" id="lb" onclick="this.classList.remove('open')"><img id="lb-img" alt=""></div>
</div></body></html>"""


def build_report(meta: dict, frames: list[tuple[float, str]], verdict: dict, out_html: str):
    shots = []
    for ts, path in frames:
        v = verdict.get(os.path.basename(path), {})
        if not v.get("keep"):
            continue
        with open(path, "rb") as _f:
            b64 = base64.b64encode(_f.read()).decode()
        shots.append({"ts": _hms(ts), "b64": b64, "section": v.get("section"),
                      "caption": _html.escape(v.get("caption") or "")})  # 비전 출력 — innerHTML 삽입 전 escape
    url_html = f'<a href="{meta["url"]}" target="_blank">{meta["url"]}</a>' if meta["url"] else ""
    html = REPORT_TMPL.format(
        title=meta["title"], url_html=url_html, nkept=len(shots),
        markdown=meta["markdown"].replace("</script>", "<\\/script>"),
        shots_json=json.dumps(shots, ensure_ascii=False).replace("</script>", "<\\/script>"))
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as _f:
        _f.write(html)
    return len(shots)


# ── 서버 통합 모드: 요약 md에 키프레임 스트립 주입 ──────────────────

_KF_STRIP_RE = re.compile(r"\n*<div class=\"kf-strip\">.*?</div>[ \t]*\n*", re.S)
_KF_APX_RE   = re.compile(r"\n*##\s*기타 자료 캡처[ \t]*\n*")


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
        figs = "".join(
            f'<figure><img src="{url_base}/{it["file"]}" loading="lazy" alt="">'
            f'<figcaption><b>{_hms(it["ts"])}</b> {_html.escape(it.get("caption") or "")}</figcaption></figure>'
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
    open(md_path, "w", encoding="utf-8").write("\n".join(out))


def generate_keyframes(summary_md_path: str, url: str, frames_out_dir: str, url_base: str) -> dict:
    """서버 진입점: 영상→키프레임→비전 분류·정렬→프레임 저장→요약 md 주입.

    반환: {"ok":bool, "n_frames":int, "reason":str?}
      - reason="download_failed" 면 호출측은 음성 요약만 유지(폴백).
    """
    meta = parse_summary(summary_md_path)
    url = (url or meta.get("url") or "").strip()
    if not url:
        return {"ok": False, "reason": "no_url"}
    kept: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        video = download_video(url, os.path.join(tmp, "v"))
        if not video:
            return {"ok": False, "reason": "download_failed"}
        # 무거운 작업(추출·비전)은 임시 폴더에서만 수행 — 완성 전엔 기존 결과를 건드리지 않는다.
        # (중단/재기동되어도 기존 캡처·요약이 보존됨)
        cands = extract_candidates(video, os.path.join(tmp, "f"))
        if not cands:
            return {"ok": False, "reason": "no_frames"}    # ffmpeg 추출 실패/0장
        verdict = classify_and_assign(cands, meta["headings"])  # 중복은 비전이 keep=false로 표시
        if verdict is None:
            return {"ok": False, "reason": "vision_failed"}  # 비전 호출 실패 — '자료 없음'과 구분
        for ts, path in cands:
            v = verdict.get(os.path.basename(path), {})
            if not v.get("keep"):
                continue
            kept.append({"ts": ts, "tmp": path, "file": f"kf_{int(ts):05d}.jpg",
                         "section": v.get("section"), "caption": v.get("caption", "")})
        if not kept:
            return {"ok": True, "n_frames": 0, "reason": "no_material"}
        # ── 여기서부터 교체(원자적에 가깝게, 마지막에 빠르게) ──
        _assign_sections_by_time(kept, meta["headings"])   # Tier3 시간범위 배정(라벨 없으면 비전 유지)
        os.makedirs(frames_out_dir, exist_ok=True)
        for old in glob.glob(os.path.join(frames_out_dir, "kf_*.jpg")):  # 기존 프레임 정리
            try:
                os.remove(old)
            except OSError:
                pass
        for k in kept:
            try:
                shutil.copy(k["tmp"], os.path.join(frames_out_dir, k["file"]))
            except OSError:
                pass
        _augment_summary_md(summary_md_path, meta["headings"], kept, url_base)
    return {"ok": True, "n_frames": len(kept)}


def _assign_sections_by_time(kept: list[dict], headings: list[dict]):
    """시간 라벨이 달린 소제목 기준으로 각 프레임을 '등장 시각이 속한 섹션'에 배정.

    timed = [(섹션idx, 시작초)] 오름차순. 프레임 ts 이하의 가장 큰 시작초 섹션에 배정.
    ts가 첫 섹션보다 앞서면 첫 시간섹션에. 시간 라벨이 하나도 없으면 비전 결과 유지.
    """
    timed = sorted(((h["idx"], h["start"]) for h in headings if h.get("start") is not None),
                   key=lambda x: x[1])
    if not timed:
        return
    for k in kept:
        ts = k["ts"]
        sec = timed[0][0]
        for idx, start in timed:
            if start <= ts:
                sec = idx
            else:
                break
        k["section"] = sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("summary")
    ap.add_argument("--url")
    ap.add_argument("--frames-dir")
    args = ap.parse_args()
    if not os.path.isfile(args.summary):
        sys.exit("요약 .md 경로 필요")
    meta = parse_summary(args.summary)
    meta["url"] = args.url or meta["url"]
    stem = os.path.splitext(os.path.basename(args.summary))[0]

    if args.frames_dir:
        frames = load_frames_dir(args.frames_dir)
        verdict = classify_and_assign(frames, meta["headings"]) or {}
        out = os.path.join(REPORTS_DIR, f"{stem}_report.html")
        n = build_report(meta, frames, verdict, out)
    else:
        if not meta["url"]:
            sys.exit("URL 없음(--url)")
        with tempfile.TemporaryDirectory() as tmp:
            video = download_video(meta["url"], os.path.join(tmp, "v"))
            if not video:
                sys.exit("다운로드 실패")
            frames = extract_candidates(video, os.path.join(tmp, "f"))
            verdict = classify_and_assign(frames, meta["headings"]) or {}
            out = os.path.join(REPORTS_DIR, f"{stem}_report.html")
            n = build_report(meta, frames, verdict, out)
    log(f"[4] 리포트 저장: {out} (자료 프레임 {n}장, {os.path.getsize(out)//1000}KB)")
    print("REPORT_PATH=" + out)


if __name__ == "__main__":
    main()
