"""요약 속 인명 원문 표기 교정 + 인명 소개(2026-10-06 사용자 지시, 공용 name_resolver 사용).

요약을 저장하면 뒤에서 한 번 돈다(schedule). 한국어 영상의 외국 인명은 전사에 한글로만
남는데(제이컵 컥슨), 웹 검색으로 출처 2곳 이상이 확인한 철자로 바꾸고 화면의 인명 클릭
소개에 쓸 목록을 남긴다. 실패해도 요약은 그대로다 — 이 모듈의 공개 함수는 예외를 내지 않는다.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading

import document_io

_LIB = os.path.expanduser("~/projects/hermes-llm-log")
if _LIB not in sys.path:
    sys.path.append(_LIB)
try:
    import name_resolver
except Exception:                                   # 공용 모듈이 없으면 기능만 꺼진다
    name_resolver = None

log = logging.getLogger("name_fix")
SERVICE = "yt"
ENABLED = os.environ.get("NAME_FIX", "1") != "0"
_sem = threading.Semaphore(1)                       # 한 번에 한 편(검색 호출 폭주 방지)
_NOTE_RE = re.compile(r"<!--NAME_FIX:[^>]*-->\n?")


def doc_id(summary_path: str) -> str:
    return os.path.splitext(os.path.basename(summary_path))[0]




def _detect_text(md: str) -> str:
    """찾기 입력: 메타 표·HTML 캡처를 뺀 본문(한눈 요약부터)."""
    body = md.split("## 2.", 1)[1] if "## 2." in md else md
    body = re.sub(r'<div class="kf-strip">.*?</div>', "", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return body.strip()


def _saved_choices() -> tuple:
    """설정 팝업 '인명 확인'에서 고른 (판별, 검색) 모델 — 매번 DB에서 읽어 재시작 없이 반영한다.

    빈 값이면 None(모듈 기본 Haiku 5.5). 설정을 못 읽어도 인명 교정은 기본값으로 계속 돈다.
    """
    try:
        import db
        saved = db.get_name_models()
        out = []
        for kind in ("detect", "lookup"):
            v = saved.get(kind) or {}
            model = str(v.get("model") or "").strip()
            out.append({"model": model, "effort": str(v.get("effort") or "default").strip() or "default"}
                       if model else None)
        return tuple(out)
    except Exception as e:                          # noqa: BLE001 — 설정은 부가 정보
        log.warning("name_fix 모델 설정 읽기 실패(기본값 사용): %s", e)
        return None, None


def _doc_date(summary_path: str) -> str:
    m = re.search(r"/(\d{4})(\d{2})(\d{2})/", summary_path)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def process(summary_path: str, *, dry_run: bool = False, on_change=None) -> dict:
    """한 편 처리. 결과 {"mapping": [...], "people": [...], "changed": n}."""
    if not (ENABLED and name_resolver) or not os.path.isfile(summary_path):
        return {}
    try:
        md = open(summary_path, encoding="utf-8", errors="replace").read()
        title = (re.search(r"^# (.+)$", md, re.M) or [None, ""])[1]
        changed = {"n": 0}

        def apply(_doc, mapping):
            with document_io.summary_lock(summary_path):
                cur = open(summary_path, encoding="utf-8", errors="replace").read()
                new, n = name_resolver.replace_names(cur, mapping)
                if not n:
                    return
                pairs = sorted({f"{s}→{d}" for s, d in mapping})
                note = "<!--NAME_FIX: " + "; ".join(pairs) + "-->\n"
                new = _NOTE_RE.sub("", new)
                # 근거 확인·되돌리기용 흔적 — 맨 앞의 모델 표시 주석은 그대로 두고 제목 바로 앞에 넣는다
                h1 = re.search(r"^# ", new, re.M)
                new = new[:h1.start()] + note + new[h1.start():] if h1 else note + new
                document_io.atomic_write_text(summary_path, new)
                changed["n"] = n
            if on_change:
                on_change(summary_path)

        detect_choice, lookup_choice = _saved_choices()
        res = name_resolver.process_docs(
            SERVICE, {doc_id(summary_path): {"text": _detect_text(md), "date": _doc_date(summary_path),
                                             "title": title}},
            apply_fn=apply, dry_run=dry_run,
            detect_choice=detect_choice, lookup_choice=lookup_choice)
        out = (res or {}).get(doc_id(summary_path), {})
        out["changed"] = changed["n"]
        if out.get("mapping") or out.get("people"):
            log.info("name_fix %s: 치환 %d건 %s, 인물 %d명", os.path.basename(summary_path), changed["n"],
                     out.get("mapping"), len(out.get("people") or []))
        return out
    except Exception as e:                          # noqa: BLE001 — 본업 보호
        log.warning("name_fix 실패(요약은 그대로): %s: %s", os.path.basename(summary_path), e)
        return {}


def schedule(summary_path: str, on_change=None) -> None:
    """요약 저장 직후 호출 — 뒤에서 한 편씩 처리한다."""
    if not (ENABLED and name_resolver) or not summary_path:
        return

    def run():
        with _sem:
            process(summary_path, on_change=on_change)
    threading.Thread(target=run, name="name-fix", daemon=True).start()


def people_for(summary_path: str) -> list:
    if not name_resolver or not summary_path:
        return []
    try:
        return name_resolver.people_for_docs(SERVICE, [doc_id(summary_path)]).get(doc_id(summary_path), [])
    except Exception:
        return []
