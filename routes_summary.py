"""라우트: 요약(/summarize, /summary/*, /prompt*, /terms/excluded, /people/excluded).

2026-11-16 리팩토링 2단계에서 app.py에서 옮겨 왔다(동작 변경 없음).
"""
import os
import db
import document_io
import term_senses
import name_fix
import keyframe_report
import summary_policy
import llm_gateway
from flask import Response, request
from yt_web import app
import yt_core
import yt_summary
import yt_blog
import logging

log = logging.getLogger("yt-script")

@app.route("/summarize", methods=["POST"])
def summarize():
    """전사 md 파일 경로(txt_path) 기반 요약. 잡 생명주기/메모리 상태와 무관하게 동작."""
    data = request.get_json(force=True) or {}
    abs_path, err = yt_core._check_res_path(data.get("txt_path") or "")
    if err:
        return err
    if not os.path.isfile(abs_path):
        return yt_core._json({"error": "전사 파일을 찾을 수 없습니다."}, 404)

    # 메타+본문 전체를 주입하되, 반복 오프닝이 확인된 Sequoia 영상은
    # 저장 원문을 보존한 채 본 인터뷰 시작 전 내용만 요약 입력에서 제외한다.
    with open(abs_path, encoding="utf-8", errors="replace") as f:
        transcript_blob = f.read()
    if not transcript_blob.strip():
        return yt_core._json({"error": "전사 결과가 없습니다."}, 400)
    transcript_meta, transcript_body = document_io.parse_markdown_text(transcript_blob)
    # 6분 이하 영상은 요약하지 않는다(2026-11-12 사용자 지시, summary_policy).
    skip_reason = summary_policy.summary_skip_reason(transcript_meta)
    if skip_reason:
        log.info("summarize skipped: %s (%s)", os.path.basename(abs_path), skip_reason)
        return yt_core._json({"error": skip_reason, "skipped": "short_video"}, 422)
    transcript_body, opening_cutoff = keyframe_report.trim_sequoia_opening(
        transcript_meta, transcript_body
    )
    if opening_cutoff is not None:
        transcript_blob = document_io.render_markdown(
            transcript_meta, transcript_body, key_order=yt_core._MD_META_ORDER
        )
        log.info(
            "Sequoia opening omitted from summary input: %s (before %s)",
            os.path.basename(abs_path), keyframe_report._hms(opening_cutoff),
        )

    template = yt_summary._inject_term_exclusions(data.get("prompt") or yt_core.DEFAULT_PROMPT,
                                       yt_summary._term_exclusions_for_prompt())
    prompt = template.replace("{transcript}", transcript_blob)
    save_path = yt_summary._summary_path_for_md(abs_path)
    skip_claude = bool(
        data.get("skip_claude")
        or str(data.get("mode") or "").lower() == "grok"
    )
    requested_order = data.get("models") or data.get("model_order")
    model_order = (llm_gateway.normalize_model_order(requested_order)
                   if requested_order is not None else None)
    reasoning_levels = data.get("reasoning_levels")
    if reasoning_levels is not None and not llm_gateway.is_valid_reasoning_levels(reasoning_levels):
        return yt_core._json({"error": "유효하지 않은 요약 추론 수준입니다."}, 400)
    # 모니터 요약 슬롯(구체 모델+추론). 있으면 models·reasoning_levels보다 우선한다.
    slots = data.get("slots")
    if slots is not None and not llm_gateway.is_valid_summary_slots(slots, db.get_monitor_summary_slots()["slots"]):
        return yt_core._json({"error": "유효하지 않은 요약 슬롯입니다."}, 400)
    log.info(
        "summarize start: %s%s",
        os.path.basename(abs_path),
        " (skip_claude→grok)" if skip_claude else "",
    )
    # LLM 이력의 title — 전사 frontmatter 제목, 없으면 파일명.
    video_title = (str((transcript_meta or {}).get("title") or "").strip()
                   or os.path.basename(abs_path))

    def guarded_summary():
        with yt_core._summarize_sem:
            if slots is not None or model_order is not None:
                yield from yt_summary._summarize_ordered(
                    prompt, save_path, model_order,
                    transcript_chars=len(transcript_blob),
                    reasoning_levels=reasoning_levels,
                    title=video_title, slots=slots,
                )
            else:
                yield from yt_summary._summarize_with_claude(
                    prompt, save_path, skip_claude=skip_claude,
                    transcript_chars=len(transcript_blob),
                    title=video_title,
                )

    return Response(guarded_summary(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/prompt", methods=["GET"])
def get_prompt():
    """요약 프롬프트 + Grok 문체 보정 노트.

    grok_note는 Grok이 요약을 맡을 때 본 프롬프트 앞에 붙는 고정 지시문이다.
    코드 상수라 화면에서 고칠 수 없고, 무엇이 실제로 들어가는지 보여 주기만 한다.
    """
    prompt = yt_core.DEFAULT_PROMPT
    if os.path.exists(yt_core.PROMPT_FILE):
        with open(yt_core.PROMPT_FILE, encoding="utf-8") as f:
            prompt = f.read()
    return yt_core._json({"prompt": prompt, "grok_note": yt_summary._GROK_STYLE_NOTE})


@app.route("/people/excluded", methods=["POST"])
def people_excluded():
    """인명 소개에서 빼기/되돌리기 {person_id, excluded}. yt·td 공용 캐시라 양쪽 모두에 적용된다."""
    data = request.get_json(force=True) or {}
    try:
        pid = int(data.get("person_id"))
    except (TypeError, ValueError):
        return yt_core._json({"ok": False, "error": "person_id 필요"}, 400)
    ok = bool(name_fix.name_resolver) and name_fix.name_resolver.set_excluded(pid, bool(data.get("excluded", True)))
    return yt_core._json({"ok": ok})


@app.route("/terms/excluded", methods=["GET", "POST", "DELETE"])
def term_exclusions():
    """각주 제외 용어 목록 조회/추가/삭제. 요약 뷰어의 ✕ 버튼과 프롬프트 패널이 쓴다."""
    if request.method == "GET":
        return yt_core._json(yt_summary._term_exclusion_state())
    data = request.get_json(force=True) or {}
    term = str(data.get("term") or "").strip()
    if not term or len(term) > 80:
        return yt_core._json({"error": "용어를 입력하세요(80자 이내)."}, 400)
    if request.method == "POST":
        sense = str(data.get("sense") or "").strip()     # ✕를 누른 각주의 설명문(직접 입력은 없음)
        added = db.add_term_exclusion(term, sense) if sense else db.add_term_exclusion(term)
        if added and sense:
            term_senses.schedule_term(term)               # 같은 표기의 다른 각주를 이 뜻과 맞춰 본다
        return yt_core._json({"ok": True, "added": added, **yt_summary._term_exclusion_state()})
    removed = db.remove_term_exclusion(term)
    return yt_core._json({"ok": True, "removed": removed, **yt_summary._term_exclusion_state()})


@app.route("/prompt/default")
def get_default_prompt():
    """'기본값으로 되돌리기'용 — repo에 저장된 스냅샷을 매번 새로 읽는다."""
    return yt_core._json({"prompt": yt_core._default_prompt()})


@app.route("/prompt", methods=["POST"])
def save_prompt():
    data = request.get_json(force=True) or {}
    prompt = data.get("prompt", "")
    # {transcript} 자리표시자 필수 — 없으면 요약 시 전사 본문이 주입되지 않음.
    if "{transcript}" not in prompt:
        return yt_core._json({"error": "{transcript} 자리표시자가 필요합니다."}, 400)
    document_io.atomic_write_text(yt_core.PROMPT_FILE, prompt)
    return yt_core._json({"ok": True})


@app.route("/summary/content", methods=["POST"])
def summary_content():
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    path = (item.get("summary_path") if item else (data.get("path") or "").strip())
    if not path:
        return yt_core._json({"error": "요약 파일 없음"}, 404)
    abs_path = os.path.realpath(path)
    summary_real = os.path.realpath(yt_core.SUMMARY_DIR)
    if not (abs_path.startswith(summary_real + os.sep) or abs_path == summary_real):
        return yt_core._json({"error": "접근 거부"}, 403)
    if not os.path.isfile(abs_path):
        return yt_core._json({"error": "파일 없음"}, 404)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        term_senses.schedule(abs_path)        # 제외 용어와 표기가 같은 각주의 뜻 판정(뒤에서, 없으면 무동작)
        # 증류 설정을 함께 실어 뷰어가 별도 요청 없이 현재 상태를 표시한다.
        return yt_core._json({"content": content, "notes": db.get_summary_notes(item["md_path"]) if item else {},
                      "people": name_fix.people_for(abs_path),     # 인명 클릭 소개(토스트)
                      "blog": yt_blog._blog_state(item),
                      "distill": db.get_item_distill(abs_path),
                      "title_ko": db.get_title_ko(abs_path),
                      "is_read": bool((item or {}).get("is_read")),
                      "blog_url": (item or {}).get("blog_url") or "",
                      "webpage_url": (item or {}).get("webpage_url") or ""})
    except Exception as e:
        return yt_core._json({"error": str(e)}, 500)


@app.route("/summary/note", methods=["POST"])
def summary_note():
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    if not item or not item.get("summary_path"):
        return yt_core._json({"error": "요약 항목이 필요합니다"}, 400)
    key, body = data.get("section_key"), data.get("body")
    if not isinstance(key, str) or not key or len(key) > 1000:
        return yt_core._json({"error": "잘못된 소제목입니다"}, 400)
    if not isinstance(body, str) or len(body) > 10000:
        return yt_core._json({"error": "메모는 10,000자 이내로 입력해주세요"}, 400)
    db.save_summary_note(item["md_path"], key, body)
    # 메모도 블로그 본문에 실리므로, 저장 즉시 '발행 이후 변경' 상태를 함께 돌려준다
    # (뷰어가 발행 버튼을 다시 열지 않고도 갱신할 수 있게).
    return yt_core._json({"body": body.strip(), "blog": yt_blog._blog_state(item)})


@app.route("/summary/section", methods=["POST"])
def summary_section():
    """요약의 소제목(###) 한 구간을 직접 고쳐 저장한다.

    뷰어가 자기 화면의 마크다운을 `###` 단위로 잘라 고친 구간의 '원래 텍스트'와 '새 텍스트'를
    함께 보낸다. 서버는 마크다운 구조를 해석하지 않고 원래 텍스트가 파일에 정확히 한 번
    나오는지만 확인한 뒤 갈아 끼운다 — 다른 곳에서 파일이 바뀌었으면(요약 재생성, 다른 탭
    편집) 찾지 못하거나 여러 번 걸리므로 덮어쓰지 않고 409로 돌려보낸다.
    """
    data = request.get_json(force=True) or {}
    item, err = yt_core._history_item_from_payload(data)
    if err:
        return err
    path = (item or {}).get("summary_path") or ""
    if not path or not os.path.isfile(path):
        return yt_core._json({"error": "요약 파일 없음"}, 404)
    abs_path = os.path.realpath(path)
    summary_real = os.path.realpath(yt_core.SUMMARY_DIR)
    if not (abs_path.startswith(summary_real + os.sep) or abs_path == summary_real):
        return yt_core._json({"error": "접근 거부"}, 403)
    original, body = data.get("original"), data.get("body")
    if not isinstance(original, str) or not original.strip():
        return yt_core._json({"error": "원본 구간이 필요합니다"}, 400)
    if not isinstance(body, str) or not body.strip():
        return yt_core._json({"error": "내용을 비울 수 없습니다"}, 400)
    if len(body) > 200_000:
        return yt_core._json({"error": "구간이 너무 깁니다"}, 400)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return yt_core._json({"error": str(e)}, 500)
    hits = content.count(original)
    if hits != 1:
        return yt_core._json({"error": "요약이 그 사이 바뀌었습니다. 새로고침 후 다시 편집해주세요.",
                      "code": "conflict"}, 409)
    updated = content.replace(original, body)
    try:
        document_io.atomic_write_text(abs_path, updated)
    except OSError as e:
        return yt_core._json({"error": "저장 실패: " + str(e)}, 500)
    yt_summary._reindex_summary(abs_path)
    log.info("summary section edited item=%s (%d→%d chars)",
             item.get("item_id"), len(content), len(updated))
    fresh = db.get_history_item(item["item_id"]) or item
    return yt_core._json({"content": updated, "blog": yt_blog._blog_state(fresh)})
