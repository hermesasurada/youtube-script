"""라우트: 이력(/history/*, /info)과 블로그 발행·삭제.

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import hashlib
import os
import re
import shutil
import subprocess
import db
import document_io
import yt_listing
from flask import request
from yt_web import app
import yt_core
import yt_jobs
import yt_titles
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


@app.route("/history/refresh-meta", methods=["POST"])
def history_refresh_meta():
    """제목·썸네일을 지금 유튜브 기준으로 다시 가져온다(요약 뷰어의 갱신 버튼).

    영상이 공개 뒤 제목을 고치거나 썸네일을 바꾸는 일이 잦은데, 우리 사본은 전사
    시점에 굳는다. 가져오지 못하면(비공개·멤버십 전환·네트워크) 기존 값을 그대로
    둔다 — 갱신 실패가 멀쩡한 제목·썸네일을 지우면 안 된다.

    제목이 바뀌면 세 곳을 함께 고친다: 전사 md의 front matter, 요약 md의 H1,
    그리고 items(재색인). 번역은 원문이 바뀐 뒤 다시 만든다.
    """
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if not item:
        return yt_core._json({"error": "item_id 필요"}, 400)

    yt_id = (item.get("yt_id") or "").strip()
    if not yt_id:
        return yt_core._json({"error": "영상 id가 없는 항목입니다."}, 400)

    changed, notes = [], []

    # 1) 현재 메타 조회 — 실패해도 썸네일 갱신은 따로 시도한다.
    new_title = ""
    try:
        if yt_jobs.yt_dlp is None:
            raise RuntimeError("yt-dlp를 사용할 수 없습니다")
        with yt_jobs.yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(item.get("webpage_url")
                                    or f"https://www.youtube.com/watch?v={yt_id}",
                                    download=False)
        new_title = yt_core._nfc((info.get("title") or "").strip())
    except Exception as e:
        notes.append(f"제목 조회 실패: {str(e)[:120]}")

    old_title = (item.get("title") or "").strip()

    # 현지화 제목은 원제가 외국어일 때만 의미가 있다. 원제가 이미 한국어인 영상에
    # 현지화 제목을 번역 슬롯(title_ko)으로 넣으면, 화면이 그것을 제목으로 올리고
    # 원제를 '원문'인 양 밑에 병기해 한국어 제목 두 개가 겹쳐 보인다
    # (2026-09-19 A41EXYozypg). 게다가 그 영상은 제작자가 유튜브 제목 A/B 테스트를
    # 돌리는 중이라 조회할 때마다 다른 변형이 와서, 제목 자리에 반영하면 갱신할 때마다
    # 파일명·front matter·요약 H1이 흔들린다. 그래서 한국어 원제 영상에서는
    # 현지화 제목을 아예 쓰지 않고 원제 하나만 유지한다.
    korean_source = bool(db._HANGUL_RE.search(new_title or old_title))
    try:
        localized = "" if korean_source else yt_jobs.fetch_localized_title(
            yt_id, channel_id=yt_listing.channel_id_of(item.get("channel_url") or ""))
    except Exception as e:
        localized = ""
        notes.append(f"현지화 제목 조회 실패: {str(e)[:120]}")

    if new_title and new_title != old_title:
        md_path = item.get("md_path") or ""
        try:
            if md_path and os.path.isfile(md_path):
                meta, body = yt_core._parse_md(md_path)
                meta["title"] = new_title
                document_io.atomic_write_text(
                    md_path,
                    document_io.render_markdown(meta, body, key_order=yt_core._MD_META_ORDER))
            # 요약 md의 첫 H1도 함께 — 뷰어가 읽는 제목은 이쪽이다.
            sm = item.get("summary_path") or ""
            if sm and os.path.isfile(sm):
                with open(sm, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                new_text, n = re.subn(r"^# .*$", "# " + new_title.replace("\\", "\\\\"),
                                      text, count=1, flags=re.M)
                if n:
                    document_io.atomic_write_text(sm, new_text)
            for path in (md_path, item.get("summary_path") or ""):
                if path and os.path.isfile(path):
                    try:
                        db.upsert(path)
                    except Exception:
                        pass
            changed.append("title")
        except Exception as e:
            notes.append(f"제목 반영 실패: {str(e)[:120]}")

    # 2) 썸네일은 제목과 무관하게 항상 최신본을 시도한다(제목만 그대로인 경우도 있다).
    try:
        before = os.path.join(yt_core.THUMB_DIR, f"{yt_id}.jpg")
        # 크기만 비교하면 같은 크기의 다른 그림을 놓친다 — 내용 해시로 본다.
        def _digest(path):
            try:
                with open(path, "rb") as f:
                    return hashlib.sha1(f.read()).hexdigest()
            except OSError:
                return ""
        old_sig = _digest(before)
        if yt_jobs.save_thumbnail(yt_id, force=True,
                          channel_id=yt_listing.channel_id_of(item.get("channel_url") or "")):
            if _digest(before) != old_sig:
                changed.append("thumbnail")
        else:
            notes.append("썸네일을 받지 못했습니다(기존 유지)")
    except Exception as e:
        notes.append(f"썸네일 갱신 실패: {str(e)[:120]}")

    # 3) 한국어 제목. 유튜브가 한국 시청자에게 보여주는 현지화 제목이 있으면 그걸
    #    쓴다 — 제작자가 단 제목이 LLM 번역보다 낫고, 사용자가 유튜브에서 보는 것과
    #    일치한다(2026-09-03 모더나 건: 원제는 그대로인데 한국어 제목만 바뀌어
    #    '변경 없음'으로 보였다). 없으면 원제가 바뀐 경우에만 LLM으로 다시 번역한다.
    cur_ko = db.get_title_ko(item.get("summary_path") or item.get("md_path") or "") or ""
    title_ko = cur_ko
    final_title = new_title or old_title
    if korean_source:
        # 한국어 원제에는 번역이 없어야 한다. 예전 갱신이 남긴 값이 있으면 지운다.
        if cur_ko:
            db.set_title_ko(yt_id, "")
            title_ko = ""
            changed.append("title_ko")
    elif localized and localized != final_title:
        if localized != cur_ko:
            db.set_title_ko(yt_id, localized)
            title_ko = localized
            changed.append("title_ko")
    elif "title" in changed or not cur_ko:
        # YouTube 한국어 제목이 없다 — 원제가 바뀌었거나 번역이 비어 있으면 LLM으로 다시
        # 번역한다(2026-11-10). 비워 두고(NULL) 뒤에서 번역하므로 화면은 다음 조회에 바뀐다.
        db.clear_title_ko(yt_id)
        yt_titles._translate_titles_async()
        if cur_ko:
            title_ko = ""
            changed.append("title_ko")

    return yt_core._json({
        "changed": changed,
        "title": new_title or old_title,
        "title_ko": title_ko or "",
        "notes": notes,
    })


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
            res = bl.update_post(post_id, title=title, content=html)
        else:
            res = bl.publish(title, html, labels)
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
