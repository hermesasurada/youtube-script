"""라우트: 이력(/history/*, /info)과 블로그 발행·삭제.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import os
import re
import shutil
import subprocess
import db
from flask import request
from yt_web import app
import yt_core
import yt_jobs
import yt_blog
import logging

log = logging.getLogger("yt-script")

@app.route("/history/distill", methods=["PATCH"])
def history_distill():
    """영상 단위 증류 설정 — distill: true(포함) / false(제외) / null(미설정=채널 따름)."""
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    path = ((item.get("summary_path") or item["md_path"]) if item
            else (data.get("path") or "").strip())
    if not path:
        return yt_core._json({"error": "item_id 또는 path 필요"}, 400)
    raw = data.get("distill", "__missing__")
    if raw == "__missing__":
        return yt_core._json({"error": "distill 필요(true/false/null)"}, 400)
    value = None if raw is None else bool(raw)
    if not db.set_item_distill(path, value):
        return yt_core._json({"error": "항목을 찾을 수 없습니다."}, 404)
    return yt_core._json({"ok": True, **(db.get_item_distill(path) or {})})


@app.route("/history")
def get_history():
    q = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread_only") == "1"
    try:
        revision = db.history_revision()
        # revision 단축 응답은 전체 목록에만 적용한다. 검색/필터 조건이 달라지면
        # 같은 DB revision이어도 결과 집합은 다르므로 실제 쿼리를 수행해야 한다.
        if not q and not unread_only and request.args.get("revision") == revision:
            return yt_core._json({"revision": revision, "unchanged": True})
        items = (db.search(q, unread_only=unread_only)
                 if q else db.list_history_items(unread_only=unread_only))
        revision = db.history_revision()
    except Exception:
        items = []
        revision = ""
    return yt_core._json({"revision": revision, "items": items})


@app.route("/history/check")
def history_check():
    """영상 중복 확인: 같은 yt_id를 이미 처리한 이력이 있으면 날짜·제목 반환(추가 전 경고용)."""
    item = db.find_by_yt_id(request.args.get("yt_id") or "")
    if not item:
        return yt_core._json({"exists": False})
    return yt_core._json({"exists": True, "date": item["date"], "title": item["title"]})


@app.route("/history/mark_read", methods=["PATCH"])
def mark_read():
    data = request.get_json(force=True) or {}
    is_read  = bool(data.get("is_read", True))
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if item:
        ok = db.mark_read_by_id(item["item_id"], is_read)
    else:
        txt_path = (data.get("txt_path") or "").strip()
        abs_path, err = yt_core._check_res_path(txt_path)
        if err:
            return err
        ok = db.mark_read(abs_path, is_read)
    if not ok:
        return yt_core._json({"error": "항목을 찾을 수 없습니다."}, 404)
    return yt_core._json({"ok": True, "is_read": is_read, "revision": db.history_revision()})


@app.route("/history/bookmark", methods=["PATCH"])
def history_bookmark():
    """영상 북마크 설정/해제 — 카드 썸네일의 북마크 아이콘이 사용."""
    data = request.get_json(force=True) or {}
    on = bool(data.get("bookmark", True))
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if not item or not db.set_bookmark_by_id(item["item_id"], on):
        return yt_core._json({"error": "항목을 찾을 수 없습니다."}, 404)
    return yt_core._json({"ok": True, "bookmark": on, "revision": db.history_revision()})


@app.route("/history/item", methods=["DELETE"])
def history_delete():
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if item:
        abs_path, err = yt_core._check_res_path(item["md_path"])
        if err:
            return err
        summary_path = item.get("summary_path")
    else:
        txt_path = (data.get("txt_path") or "").strip()
        abs_path, err = yt_core._check_res_path(txt_path)
        if err:
            return err
        # 레거시 경로 호출은 기존 파일 배치 규칙으로 요약 경로를 유추한다.
        try:
            rel = os.path.relpath(abs_path, yt_core.RES_DIR)
            summary_path = os.path.join(yt_core.SUMMARY_DIR, rel)
        except ValueError:
            summary_path = None
    # DB에서 먼저 제거
    db.delete(abs_path)
    # 파일 삭제
    deleted = []
    errors = []
    for p in filter(None, [abs_path, summary_path]):
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(p)
            except OSError as e:
                errors.append(str(e))
    # 키프레임 디렉토리({stem}.frames/)도 함께 정리(누수 방지)
    if summary_path:
        frames_dir = os.path.splitext(summary_path)[0] + ".frames"
        if os.path.isdir(frames_dir):
            try:
                shutil.rmtree(frames_dir)
                deleted.append(frames_dir)
            except OSError as e:
                errors.append(str(e))
    return yt_core._json({"ok": True, "deleted": deleted, "errors": errors,
                  "revision": db.history_revision()})


@app.route("/history/text", methods=["POST"])
def history_text():
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    raw_path = item["md_path"] if item else (data.get("txt_path") or "")
    abs_path, err = yt_core._check_res_path(raw_path)
    if err:
        return err
    if not os.path.exists(abs_path):
        return yt_core._json({"error": "파일 없음"}, 404)
    try:
        _, transcript = yt_core._parse_md(abs_path)
        return yt_core._json({"text": transcript, "translation": _translation_payload(abs_path)})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


def _translation_payload(md_path: str) -> dict | None:
    """전사 전문 번역 상태 + 본문. 별도 작업(transcript_translator.py)의 결과물.

    아직 진행 중이어도 지금까지 번역된 부분은 보여준다(청크마다 저장되므로).
    """
    try:
        st = db.get_translation_for_path(md_path)
    except Exception:
        return None
    if not st:
        return None
    out = {"status": st.get("status"), "done": st.get("chunks_done") or 0,
           "total": st.get("chunks_total") or 0, "text": ""}
    p = st.get("out_path") or ""
    if p and os.path.isfile(p):
        try:
            raw = open(p, encoding="utf-8").read()
            # 머리부(프론트매터)와 마커 주석을 모두 걷어내고 본문만 넘긴다
            seps = [i for i, l in enumerate(raw.splitlines()) if l.strip() == "---"]
            if len(seps) >= 2:
                raw = "\n".join(raw.splitlines()[seps[1] + 1:])
            body = re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()
            # 제목은 모달 헤더에 이미 있으므로 본문 H1은 뺀다
            out["text"] = re.sub(r"\A#\s+.*?\n+", "", body, count=1).strip()
        except Exception:
            pass
    return out


def _reveal_in_file_manager(abs_path: str) -> None:
    """OS별 파일 탐색기에서 해당 파일/폴더를 보여준다."""
    exists  = os.path.exists(abs_path)
    target  = abs_path if exists else os.path.dirname(abs_path)
    if yt_core.IS_WIN:
        if exists:
            subprocess.Popen(["explorer", f"/select,{abs_path}"])
        else:
            subprocess.Popen(["explorer", target])
    elif yt_core.IS_MAC:
        if exists:
            subprocess.Popen(["open", "-R", abs_path])
        else:
            subprocess.Popen(["open", target])
    else:  # Linux 등
        subprocess.Popen(["xdg-open", target])


@app.route("/history/explore", methods=["POST"])
def history_explore():
    abs_path, err = yt_core._check_res_path(request.get_json(force=True).get("txt_path") or "")
    if err:
        return err
    try:
        _reveal_in_file_manager(abs_path)
        return yt_core._json({"ok": True})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


@app.route("/info", methods=["POST"])
def video_info():
    if yt_jobs.yt_dlp is None:
        return yt_core._json({"error": "yt-dlp not available"}, 400)
    data = request.get_json(force=True)
    url  = (data.get("url") or "").strip()
    if not url:
        return yt_core._json({"error": "URL 없음"}, 400)
    try:
        with yt_jobs.yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return yt_core._json({
            "title":    yt_core._nfc(info.get("title") or ""),
            "uploader": yt_core._nfc(info.get("uploader") or info.get("channel") or ""),
            "duration": float(info.get("duration") or 0),
        })
    except Exception as e:
        return yt_core._json({"error": str(e)}, 400)


@app.route("/history/publish-blog", methods=["POST"])
def history_publish_blog():
    """요약을 내 블로그스팟에 바로 발행한다 — 공통 라이브러리 hermes_blogger(wm과 토큰 공유).

    본문 HTML은 뷰어가 '블로거용 내보내기' 렌더러(mdToBloggerHtml)로 만들어 보낸다 —
    제목은 글 제목 필드로만 가고 본문엔 없다(mdToBloggerHtml이 H1을 뺀다). 이미 발행된
    영상은 다시 올리지 않고 기존 URL을 돌려준다. 라벨은 채널명.

    `mode="update"`면 새 글을 만들지 않고 기존 글(blog_post_id)의 제목·본문을 지금 내용으로
    덮어쓴다 — 요약을 다시 만들거나 메모를 고친 뒤 쓰는 경로다. URL은 그대로 유지되고
    blog_published_at만 갱신돼 '발행 뒤 변경' 판정(_blog_state)이 다시 초기화된다.
    """
    import hermes_blogger as bl
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if not item:
        return yt_core._json({"error": "item_id 필요"}, 400)
    # mode="update": 이미 발행된 글을 지금 요약·메모 기준으로 덮어쓴다(발행 버튼의 '수정').
    # 그 밖에는 기존 동작 그대로 — 이미 발행된 영상을 두 번 올리지 않는다.
    updating = (data.get("mode") or "").strip() == "update"
    if item.get("blog_url") and not updating:
        return yt_core._json({"status": "exists", "url": item["blog_url"]})
    if updating and not (item.get("blog_url") and item.get("blog_post_id")):
        return yt_core._json({"status": "error", "code": "not_published",
                      "message": "아직 발행되지 않은 요약입니다"}, 400)
    html = (data.get("html") or "").strip()
    title = (item.get("title_ko") or data.get("title") or item.get("title") or "").strip()
    if not html or not title:
        return yt_core._json({"status": "error", "code": "bad_request", "message": "title/html 필요"}, 400)
    html = yt_blog._ensure_blog_original_title(html, item.get("title"), item.get("title_ko"))
    html = yt_blog._include_blog_notes(html, db.get_summary_notes(item.get("md_path") or ""))
    if not bl.configured():
        return yt_core._json({"status": "error", "code": "not_configured",
                      "message": "블로그 연동 미설정 — 서버에서 hermes-blogger auth 실행 필요"})
    html = yt_blog._insert_blog_thumbnail(html, item.get("yt_id") or "", item.get("webpage_url") or "")
    uploader = (item.get("uploader") or "").strip()
    labels = [uploader] if uploader and uploader != "—" else []
    post_id = str(item.get("blog_post_id") or "")
    try:
        if updating:
            # 제목도 함께 맞춘다(번역 제목이 바뀐 경우). URL·발행 상태는 그대로 유지된다.
            res = bl.update_post(post_id, title=title, content=html,
                                 labels=yt_blog._blog_labels_for_update(bl, post_id))
        else:
            res = bl.publish(title, html, yt_blog._with_blog_tag(labels))
    except bl.BloggerError as e:
        log.warning("blog %s failed item=%s: %s",
                    "update" if updating else "publish", item["item_id"], e)
        return yt_core._json({"status": "error", "code": "api", "message": str(e)})
    url = res.get("url") or item.get("blog_url") or ""
    db.set_blog_publish(item["item_id"], url, str(res.get("id") or post_id),
                        yt_blog.BLOG_RENDER_VERSION)
    log.info("blog %s item=%s -> %s", "updated" if updating else "published",
             item["item_id"], url)
    fresh = db.get_history_item(item["item_id"]) or dict(item, blog_url=url)
    return yt_core._json({"status": "updated" if updating else "ok", "url": url,
                  "id": res.get("id") or post_id, "blog": yt_blog._blog_state(fresh)})


@app.route("/history/delete-blog", methods=["POST"])
def history_delete_blog():
    """발행한 블로그 글을 지우고 발행 기록을 비운다(2026-10-19 사용자 지시 — 발행 메뉴의 '블로그 글 삭제').

    블로거에서 이미 지워진 글(404)이면 기록만 비운다. 그 밖의 실패는 기록을 그대로 둬
    앱과 블로그가 어긋나지 않게 한다."""
    import hermes_blogger as bl
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if not item:
        return yt_core._json({"error": "item_id 필요"}, 400)
    if not item.get("blog_url"):
        return yt_core._json({"status": "error", "code": "not_published", "message": "발행되지 않은 요약입니다"}, 400)
    post_id = str(item.get("blog_post_id") or "")
    if post_id:
        if not bl.configured():
            return yt_core._json({"status": "error", "code": "not_configured",
                          "message": "블로그 연동 미설정 — 서버에서 hermes-blogger auth 실행 필요"})
        try:
            bl.delete_post(post_id)
        except bl.BloggerError as e:
            if "HTTP 404" not in str(e):
                log.warning("blog delete failed item=%s: %s", item["item_id"], e)
                return yt_core._json({"status": "error", "code": "api", "message": str(e)})
    db.clear_blog_publish(item["item_id"])
    log.info("blog deleted item=%s post=%s (%s)", item["item_id"], post_id or "-", item.get("blog_url"))
    return yt_core._json({"status": "deleted"})
