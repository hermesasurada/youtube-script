"""블로그 발행 도우미 — 발행 상태 판정, 원제 줄·메모·썸네일 삽입.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import hashlib
import html as _htmlmod
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
import db
import term_senses
import yt_core
import yt_jobs
import logging

log = logging.getLogger("yt-script")

# 블로그 본문 생성 방식의 버전. 표시 형식을 바꾸면(각주 배치, 메모 모양 등) 이 값을 올려
# 이미 발행된 글이 '수정 필요'로 잡히게 한다. 발행 시 items.blog_render_ver에 기록한다.
BLOG_RENDER_VERSION = "2026-11-07-memo-card-2"


def _excluded_note_mtime(summary_path: str) -> float:
    """이 글의 각주 중 '제외 목록에 들어가 사라진' 것들의 최신 추가 시각(epoch).

    각주 제거는 전역 term_exclusions에 용어를 넣는 방식이라 요약 파일도 메모도
    건드리지 않는다. 그래서 각주만 지우면 발행 상태가 '변경 없음'으로 남아
    재발행을 못 했다(2026-09-22 사용자 지시). 이 글에 실제로 있던 각주와 제외
    목록의 교집합만 보므로, 다른 글 때문에 추가된 용어는 영향을 주지 않는다.
    """
    if not summary_path or not os.path.isfile(summary_path):
        return 0.0
    times = db.term_exclusion_times()
    if not times:
        return 0.0
    try:
        with open(summary_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0.0
    checks = db.term_sense_checks()
    latest = 0.0
    for label, definition in term_senses.notes_in(text):
        # 화면과 같은 기준으로 맞춘다: '용어 (원어)' → '용어'
        key = db.normalize_term(label)
        hit = times.get(key)
        if not hit:
            continue
        same, checked = checks.get((key, db.sense_key(definition)), (True, 0.0))
        # 뜻이 달라 다시 보이게 된 각주는 판정 시각이 변경 시각이다(발행본에는 빠져 있다)
        latest = max(latest, hit if same else max(hit, checked))
    return latest


def _blog_state(item: dict | None) -> dict:
    """발행 버튼이 쓰는 상태 — 발행 여부와 '발행 뒤 내용이 바뀌었는지'.

    비교 대상은 블로그 본문에 실제로 실리는 세 가지다: 요약 파일(mtime), 섹션 메모
    (summary_notes.updated_at), 그리고 이 글의 각주를 지운 제외 용어의 추가 시각.
    셋 중 가장 나중이 발행시각보다 뒤면 수정할 거리가 있다.
    blog_published_at은 로컬 naive 문자열이라 같은 기준(epoch)으로 맞춘다. 발행시각
    기록이 없는 옛 글은 판단할 근거가 없으므로 '수정 가능'으로 본다.
    """
    if not item or not item.get("blog_url"):
        return {"published_at": "", "stale": False, "changed_at": ""}
    published_raw = (item.get("blog_published_at") or "").strip()
    try:
        published = datetime.strptime(published_raw, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        published = 0.0
    edited = db.summary_notes_mtime(item.get("md_path") or "")
    path = item.get("summary_path") or ""
    if path and os.path.isfile(path):
        edited = max(edited, os.path.getmtime(path))
    edited = max(edited, _excluded_note_mtime(path))
    # 1초 여유: 발행 직후 같은 초에 기록된 mtime을 '변경'으로 보지 않는다.
    content_changed = bool(edited and (not published or edited > published + 1))
    # 내용이 그대로여도 표시 형식이 바뀌었으면 발행본은 옛 모양이다.
    outdated_render = (item.get("blog_render_ver") or "") != BLOG_RENDER_VERSION
    return {
        "published_at": published_raw,
        "changed_at": datetime.fromtimestamp(edited).strftime("%Y-%m-%d %H:%M:%S") if edited else "",
        "content_changed": content_changed,
        "outdated_render": outdated_render,
        "stale": content_changed or outdated_render,
    }


# 원제 줄은 작은 글씨지만 본문보다 줄간격이 크면 따로 놀아서, 본문(1.6)에 맞춰 좁힌다.
_BLOG_ORIGINAL_STYLE = (
    "margin:0 0 1.4em;font-size:13px;font-weight:400;"
    "line-height:1.5;color:#7b8393;"
)


def _ensure_blog_original_title(body_html: str, original_title: str | None,
                                translated_title: str | None) -> str:
    """번역 제목으로 발행할 때 외국어 원제를 맨 아래 푸터(없으면 본문 첫 줄)에 보존한다."""
    original = (original_title or "").strip()
    translated = (translated_title or "").strip()
    if not original or not translated or original == translated:
        return body_html
    if 'data-ys-original-title="1"' in body_html:
        return body_html

    block = (
        f'<p data-ys-original-title="1" style="{_BLOG_ORIGINAL_STYLE}">'
        f'원제 : {_htmlmod.escape(original)}</p>'
    )
    wrapper = (re.search(r'<div\b[^>]*data-summary-footer="1"[^>]*>', body_html)
               or re.match(r"\s*<div\b[^>]*>", body_html, flags=re.IGNORECASE))
    if wrapper:
        return body_html[:wrapper.end()] + "\n" + block + body_html[wrapper.end():]
    return block + "\n" + body_html


def _include_blog_notes(body_html, notes):
    """클라이언트의 소제목 식별자를 기준으로 저장된 최신 메모를 삽입한다."""
    import html as html_lib
    # 푸터(원본 영상 링크)는 mdToBloggerHtml이 <div data-summary-footer>로 만든다 — <p>만
    # 찾으면 분리되지 않아 마지막 섹션 메모가 푸터 뒤에 붙는다(2026-09-13 검증에서 발견).
    parts = re.split(r'(?=<h[23]\b|<(?:p|div)\b[^>]*data-summary-footer="1")', body_html, flags=re.I)
    for i, part in enumerate(parts):
        match = re.match(r'<h3\b[^>]*data-summary-section="([^"]*)"[^>]*>', part, re.I)
        if not match:
            continue
        note = notes.get(html_lib.unescape(match[1]))
        if not note:
            continue
        # 블로거는 인라인 style만 먹어서 ::before를 못 쓴다 — 전구를 본문 앞에 직접 넣는다.
        # 메모 모양(2026-11-07 사용자 지시): 왼쪽 강조선+옅은 초록 바탕(클로드 스타일) 대신
        # 노란 메모지 카드 — 테두리 전체·둥근 모서리·옅은 그림자.
        block = ('<aside style="margin:1.1em 0;padding:12px 16px;border:1px solid #ecdca0;'
                 'background:#fff9e3;border-radius:10px;box-shadow:0 1px 3px rgba(120,100,40,.12);'
                 'color:#4a4127;">'
                 # 안쪽은 span(display:block) — 테마가 div에 8px 위아래 여백을 강제해 카드가 부푼다
                 '<span style="display:block;white-space:pre-wrap;font-size:15px;line-height:1.6;">'
                 '\U0001F4A1 ' + html_lib.escape(note) + '</span></aside>')
        # 마지막 섹션도 최상위 본문 wrapper 닫힘 앞에 삽입한다.
        end = part.rfind('</div>') if i == len(parts) - 1 else -1
        parts[i] = part[:end] + block + part[end:] if end >= 0 else part + block
    return ''.join(parts)


# 블로그 썸네일 = 메인 화면 카드의 영상 썸네일(2026-10-29 사용자 지시). Blogger는 본문 첫 이미지로 홈 목록
# 썸네일·og:image를 만들므로 본문 맨 위(원제 줄 바로 뒤)에 넣는다. 이미지는 Cloudflare R2(hermes_blogger.r2)에
# 올린 공개 주소만 쓴다 — data URI(base64)는 블로그 홈 목록을 끊고, i.ytimg.com 직링크는 쓰지 않는다(wm과 같은 원칙).
# 같은 그림의 고해상도(maxresdefault)를 먼저 받고, 안 되면 카드가 쓰는 res/thumbs 사본을 그대로 올린다.
_BLOG_THUMB_CSS = "display:block;max-width:100%;height:auto;margin:0 auto;border-radius:4px;"


def _blog_thumb_bytes(yt_id: str) -> bytes | None:
    if not re.fullmatch(r"[\w-]{5,20}", yt_id or ""):
        return None
    try:
        with urllib.request.urlopen(f"https://i.ytimg.com/vi/{yt_id}/maxresdefault.jpg", timeout=10) as r:
            data = r.read()
        if len(data) >= 2048:          # 없는 해상도는 회색 자리표시(~1KB)
            return data
    except Exception:
        pass
    path = os.path.join(yt_core.THUMB_DIR, f"{yt_id}.jpg")
    if not os.path.isfile(path):
        yt_jobs.save_thumbnail(yt_id)
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data if len(data) > 1024 else None
    except OSError:
        return None


def _insert_blog_thumbnail(body_html: str, yt_id: str, video_url: str = "") -> str:
    """본문 맨 위(원제 줄 뒤, 없으면 감싸개 맨 앞)에 R2에 올린 영상 썸네일을 넣는다. 실패하면 그대로."""
    if not yt_id or 'data-ys-thumb="1"' in body_html:
        return body_html
    try:
        from hermes_blogger import r2
        if not r2.configured():
            log.warning("blog thumbnail skipped: R2 not configured (%s)", r2.ENV_PATH)
            return body_html
        data = _blog_thumb_bytes(yt_id)
        if not data:
            log.info("blog thumbnail skipped %s: no image", yt_id)
            return body_html
        url = r2.put(f"yt/thumb/{yt_id}-{hashlib.sha256(data).hexdigest()[:12]}.jpg", data, "image/jpeg")
    except Exception as e:  # noqa: BLE001 — 썸네일 실패가 발행을 막지 않는다
        log.info("blog thumbnail skipped %s: %s", yt_id, e)
        return body_html
    img = f'<img src="{_htmlmod.escape(url, quote=True)}" alt="" style="{_BLOG_THUMB_CSS}">'
    if video_url:
        img = f'<a href="{_htmlmod.escape(video_url, quote=True)}" target="_blank" rel="noopener">{img}</a>'
    # 썸네일과 첫 소제목 사이 여백(2026-11-07 사용자 지시). 블로그 테마가 `.post-body div`의 margin을
    # 8px !important로 고정해 margin은 무시된다 — padding으로 준다(테마 8px + 1.9em ≈ 2.4em).
    block = f'<div data-ys-thumb="1" style="margin:0;padding:0 0 1.9em;">{img}</div>'
    # 맨 아래 푸터의 원제(2026-11-05)는 자리 표시로 쓰지 않는다 — 썸네일은 본문 맨 앞이다.
    foot = body_html.find('data-summary-footer="1"')
    head = body_html if foot < 0 else body_html[:foot]
    m = re.search(r'<p data-ys-original-title="1"[^>]*>.*?</p>', head, re.S)
    if not m:
        m = re.match(r"\s*<div\b[^>]*>", body_html, flags=re.IGNORECASE)
    if not m:
        return block + "\n" + body_html
    return body_html[:m.end()] + "\n" + block + body_html[m.end():]
