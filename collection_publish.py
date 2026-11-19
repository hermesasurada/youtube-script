"""영상 모음 공개 사본 — 모음 목록·모음·영상별 요약을 읽기 전용 정적 페이지로 만들어 R2 공개 버킷에 올린다
(2026-11-19 사용자 지시 '포츈500 리스트 페이지랑 하위 페이지들을 무료로 공개').

yt 서버는 이 맥과 Tailscale에서만 열리고(lan_guard), 영상을 누르면 앱 전체(큐·설정·메모)로 넘어가므로 그대로 열지 않는다.
대신 `collections_view.COLLECTIONS`에서 `"public": True`인 모음만 사본을 만든다.

- 실린다: 모음 목록, 모음(일자별·시간순), 영상마다 요약 페이지(한눈 요약+본문+각주+원본 영상 링크).
- 안 실린다: 전사·전문 번역, 캡처 이미지, 내 의견 메모, 읽음 표시, 편집·발행 버튼. 검색엔진 색인 끔(noindex).
- 요약 페이지는 화면과 같은 렌더러(`YS.buildSummaryHtmlFile`, 'HTML 파일로 내보내기'와 같은 함수)로 만든다 —
  헤드리스 크롬이 yt 서버의 `/collection/<slug>/public-render`를 열어 결과를 넘긴다. 그래서 yt 서버가 떠 있어야 한다.
- 올리는 곳: hermes-blogger의 R2 버킷(`~/.hermes/r2.env`) 아래 `c/`. r2.dev 공개 주소는 폴더 주소에 index.html을
  붙여 주지 않으므로 링크는 모두 `…/index.html`처럼 파일까지 적는다.
- 자동 갱신: 채널 모니터(launchd 20분)가 매 주기 끝에 `sync_if_changed()`를 부른다. 요약·번역 제목·제외 용어·
  화면 코드가 바뀐 모음만 다시 만들고, 내용이 바뀐 파일만 올린다(`res/public/manifest.json`).

수동 실행: `.venv/bin/python -m collection_publish [--force] [--local]`
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date

import collections_view
import db
import yt_core

PREFIX = "c"
PUBLIC_DIR = os.path.join(yt_core.BASE_DIR, "res", "public")
SITE_DIR = os.path.join(PUBLIC_DIR, "site")             # 마지막으로 만든 사본(확인용)
MANIFEST = os.path.join(PUBLIC_DIR, "manifest.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML_CACHE = "public, max-age=300"
# 결과물 모양을 바꾸면 올린다 — 서명이 바뀌어 다음 주기에 전부 다시 만든다.
FORMAT_VERSION = "2026-11-19-1"
# 서명에 넣는 화면 코드(바뀌면 요약 페이지 모양이 바뀐다)
_CODE_FILES = ("static/js/common.js", "static/css/index.css", "static/css/hermes-theme.css",
               "templates/collection_public.html", "templates/collection_render.html")
_KF_STRIP = re.compile(r'<div class="kf-strip">[\s\S]*?</div>\s*')


def public_slugs() -> list[str]:
    return [s for s, c in collections_view.COLLECTIONS.items() if c.get("public")]


def _server_base() -> str:
    return f"http://127.0.0.1:{int(os.environ.get('PORT', '5001'))}"


def _fmt_dur(sec: float) -> str:
    s = int(round(sec or 0))
    if not s:
        return ""
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def _day_label(d: str) -> str:
    try:
        t = date.fromisoformat(d)
    except ValueError:
        return ""
    return f"{t.month}월 {t.day}일({'월화수목금토일'[t.weekday()]})"


def _done_items(slug: str) -> tuple[dict, list[dict]]:
    """모음 자료 + 요약이 있는 영상(요약 경로·번역 제목·원본 주소 포함)."""
    data = collections_view.items(slug, fill=False) or {}
    conn = db._conn()
    out = []
    for day in data.get("days", []):
        for it in day["items"]:
            if it["state"] != "done" or not it["item_id"] or not it["has_summary"]:
                continue
            r = conn.execute("SELECT summary_path, title_ko, webpage_url FROM items WHERE rowid=?",
                             (it["item_id"],)).fetchone()
            if not r or not r["summary_path"] or not os.path.isfile(r["summary_path"]):
                continue
            out.append(dict(it, summary_path=r["summary_path"], title_ko=(r["title_ko"] or "").strip(),
                            webpage_url=r["webpage_url"] or f"https://www.youtube.com/watch?v={it['yt_id']}"))
    return data, out


def render_payload(slug: str) -> list[dict]:
    """렌더 페이지에 실을 요약 원문 — 캡처 줄은 빼고 넘긴다(공개 사본엔 캡처 이미지를 싣지 않는다)."""
    _, items = _done_items(slug)
    out = []
    for it in items:
        with open(it["summary_path"], encoding="utf-8", errors="replace") as f:
            md = _KF_STRIP.sub("", f.read())
        out.append({"yt_id": it["yt_id"], "md": md, "title_ko": it["title_ko"], "webpage_url": it["webpage_url"]})
    return out


def signature(slug: str) -> str:
    """이 모음 사본의 내용 지문 — 요약 파일·번역 제목·순서·제외 용어·화면 코드·모음 설정."""
    data, items = _done_items(slug)
    h = hashlib.sha256(FORMAT_VERSION.encode())
    h.update(json.dumps(collections_view.COLLECTIONS.get(slug, {}), sort_keys=True, ensure_ascii=False).encode())
    for it in items:
        h.update(json.dumps([it["yt_id"], it["day"], it["time"], it["title"], it["title_ko"],
                             os.path.getmtime(it["summary_path"])], ensure_ascii=False).encode())
    h.update(json.dumps(sorted(db.term_exclusion_times().items()), ensure_ascii=False).encode())
    for rel in _CODE_FILES:
        try:
            with open(os.path.join(yt_core.BASE_DIR, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


def render_pages(slug: str, timeout: int = 600) -> dict:
    """헤드리스 크롬으로 렌더 페이지를 열어 {yt_id: {html, title}}를 받는다."""
    if not os.path.exists(CHROME):
        raise RuntimeError(f"크롬 없음: {CHROME}")
    profile = tempfile.mkdtemp(prefix="yt-public-chrome-")
    try:
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", f"--user-data-dir={profile}", "--virtual-time-budget=300000",
               "--dump-dom", f"{_server_base()}/collection/{slug}/public-render"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', proc.stdout or "")
    if not m or not m.group(1).strip():
        raise RuntimeError(f"렌더 결과 없음(크롬 종료 {proc.returncode}): {(proc.stderr or '')[-300:]}")
    res = json.loads(_html.unescape(m.group(1)))
    if not res.get("ok"):
        raise RuntimeError(f"렌더 실패: {res.get('error')}")
    return res["pages"]


def _finish_summary_page(page_html: str, collection_title: str) -> str:
    """내보내기 파일 → 공개 페이지: 색인 끔, 모음으로 돌아가는 줄, 바닥글 안내 문구."""
    out = page_html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<meta name="robots" content="noindex">', 1)
    nav = ('<style>.pub-nav{margin:0 0 18px;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}'
           '.pub-nav a{color:var(--text-muted,#777);text-decoration:none}.pub-nav a:hover{text-decoration:underline}</style>'
           f'<nav class="pub-nav"><a href="../index.html">← {_html.escape(collection_title)}</a></nav>')
    out = out.replace('<main class="ys-export">', '<main class="ys-export">' + nav, 1)
    out = re.sub(r"요약 파일 만든 때: [^<]*", "원본 영상을 AI로 요약·번역한 글입니다.", out, count=1)
    # 캡처를 뺐으므로 이미지가 남아 있으면 안 된다(남으면 data URI로 들어간 로컬 자원이다)
    out = re.sub(r"<img\b[^>]*>", "", out)
    # 화면 CSS의 주석(작업 메모)은 바깥에 실을 이유가 없다 — 지우고 크기도 줄인다
    out = re.sub(r"<style>[\s\S]*?</style>", lambda m: re.sub(r"/\*[\s\S]*?\*/", "", m.group(0)), out)
    return out


def _render_template(name: str, **ctx) -> str:
    from yt_web import app
    return app.jinja_env.get_template(name).render(**ctx)


def build(slugs: list[str] | None = None) -> dict[str, bytes]:
    """공개 사본 전체 — {R2 키: 내용}. 요약 렌더는 yt 서버와 크롬이 필요하다."""
    slugs = slugs if slugs is not None else public_slugs()
    files: dict[str, bytes] = {}
    listing = []
    for slug in slugs:
        c = collections_view.get(slug)
        data, items = _done_items(slug)
        pages = render_pages(slug) if items else {}
        ko = {it["yt_id"]: it["title_ko"] for it in items}
        days = []
        for day in data.get("days", []):
            rows = []
            for it in day["items"]:
                if it["yt_id"] not in pages:
                    continue
                short_ko = collections_view.short_title(ko.get(it["yt_id"], ""), c["match"])
                rows.append({"yt_id": it["yt_id"], "title": it["title"], "short_title": it["short_title"],
                             "short_ko": short_ko if short_ko and short_ko != it["short_title"] else "",
                             "time": it["time"], "time_src": it.get("time_src", ""), "dur": _fmt_dur(it["duration"])})
                files[f"{PREFIX}/{slug}/v/{it['yt_id']}.html"] = _finish_summary_page(
                    pages[it["yt_id"]]["html"], c["title"]).encode("utf-8")
            if rows:
                days.append({"date": day["date"], "date_label": _day_label(day["date"]), "items": rows})
        for i, d in enumerate(days, 1):
            d["label"] = collections_view.day_label(c, d["date"], i)
        total = sum(len(d["items"]) for d in days)
        files[f"{PREFIX}/{slug}/index.html"] = _render_template(
            "collection_public.html", collection=c, days=days, total=total, collections=None).encode("utf-8")
        listing.append({"slug": slug, "title": c["title"], "subtitle": c.get("subtitle", ""), "total": total,
                        "cover": days[0]["items"][0]["yt_id"] if days else ""})
    files[f"{PREFIX}/index.html"] = _render_template(
        "collection_public.html", collection=None, collections=listing).encode("utf-8")
    return files


def _load_manifest() -> dict:
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(d: dict) -> None:
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST)


def _write_local(files: dict[str, bytes]) -> None:
    for key, data in files.items():
        path = os.path.join(SITE_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def upload(files: dict[str, bytes], manifest: dict) -> list[str]:
    """내용이 바뀐 파일만 올린다. 반환: 올린 키."""
    from hermes_blogger import r2
    hashes = manifest.setdefault("files", {})
    sent = []
    for key, data in sorted(files.items()):
        digest = hashlib.sha256(data).hexdigest()
        if hashes.get(key) == digest:
            continue
        r2.put(key, data, "text/html; charset=utf-8", cache=HTML_CACHE, skip_existing=False)
        hashes[key] = digest
        sent.append(key)
    return sent


def public_url() -> str:
    from hermes_blogger import r2
    return r2.public_url(f"{PREFIX}/index.html")


def publish(force: bool = False, local_only: bool = False, log=print) -> dict:
    """바뀐 모음만 다시 만들어 올린다. 반환: {changed:[slug], uploaded:[key], url}."""
    slugs = public_slugs()
    manifest = _load_manifest()
    sigs = manifest.setdefault("sig", {})
    new_sigs = {s: signature(s) for s in slugs}
    changed = [s for s in slugs if force or sigs.get(s) != new_sigs[s]]
    if not changed and not force:
        return {"changed": [], "uploaded": [], "url": ""}
    files = build(slugs)                 # 목록 페이지가 모든 모음의 편수를 싣기 때문에 사본은 통째로 만든다
    _write_local(files)
    if local_only:
        log(f"[public] 로컬 사본만 만듦: {SITE_DIR} ({len(files)}개)")
        return {"changed": changed, "uploaded": [], "url": ""}
    sent = upload(files, manifest)
    sigs.update(new_sigs)
    _save_manifest(manifest)
    url = public_url()
    log(f"[public] {', '.join(changed)} — 파일 {len(files)}개 중 {len(sent)}개 올림 → {url}")
    return {"changed": changed, "uploaded": sent, "url": url}


def sync_if_changed(log=print) -> None:
    """채널 모니터 주기 끝에서 부른다 — 실패해도 모니터 본업은 그대로(로그만)."""
    if not public_slugs():
        return
    try:
        publish(log=log)
    except Exception as e:  # noqa: BLE001 — 서버 꺼짐·크롬·R2 오류는 다음 주기에 다시
        log(f"[public] 공개 사본 갱신 실패(다음 주기에 재시도): {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="영상 모음 공개 사본을 만들어 R2에 올린다")
    ap.add_argument("--force", action="store_true", help="바뀐 것이 없어도 다시 만든다")
    ap.add_argument("--local", action="store_true", help="올리지 않고 res/public/site에 사본만 만든다")
    args = ap.parse_args(argv)
    db.init()
    res = publish(force=args.force, local_only=args.local)
    if not res["changed"]:
        print("[public] 바뀐 것 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
