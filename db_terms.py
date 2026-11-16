"""각주 제외 용어 쿼리 — 표기 정규화, 뜻 단위 제외(senses), 로컬 LLM 뜻 판정 캐시.

2026-11-16 리팩토링 4단계에서 db.py에서 옮겨 왔다(동작 변경 없음). 호출은 예전처럼 `db.이름`으로 한다 —
db.py가 이 모듈의 이름을 다시 내보낸다. 이 모듈 안에서도 db의 다른 이름은 `db.`를 붙여 부른다
(시험의 monkeypatch(db, …)·importlib.reload(db)가 예전처럼 먹게).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
import db

_TERM_HYPHENS_RE = re.compile(r"[\u2010-\u2015]")


def normalize_term(term: str) -> str:
    """비교 키: 공백 정리 + 하이픈 계열 통일 + casefold. 표시는 원문(term)을 쓴다."""
    t = db._TERM_HYPHENS_RE.sub("-", (term or "").replace("\u00a0", " "))
    t = re.sub(r"\s+", " ", t).strip()
    return t.casefold()


def list_term_exclusions() -> list[str]:
    return [r["term"] for r in db._conn().execute(
        "SELECT term FROM term_exclusions ORDER BY rowid")]   # 추가한 순서


def term_exclusion_times() -> dict[str, float]:
    """제외 용어 → 추가 시각(epoch). 각주가 언제 사라졌는지 판단하는 데 쓴다."""
    out: dict[str, float] = {}
    for r in db._conn().execute("SELECT key, added_at FROM term_exclusions"):
        try:
            out[r["key"]] = datetime.strptime(r["added_at"], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            out[r["key"]] = 0.0
    return out


def sense_key(definition: str) -> str:
    """각주 설명문 비교 키 — 영숫자·한글만 남긴 소문자. 화면(common.js `_defKey`)과 같은 규칙."""
    return re.sub(r"[^0-9a-z가-힣]", "", (definition or "").lower())


def add_term_exclusion(term: str, sense: str = "") -> bool:
    """제외 용어 추가. sense(✕를 누른 각주의 설명문)가 있으면 그 뜻을 기록한다 — 이미 있는
    용어면 새 뜻만 덧붙인다. 새로 들어갔거나 뜻이 늘었으면 True."""
    key = db.normalize_term(term)
    if not key:
        return False
    display = re.sub(r"\s+", " ", (term or "").strip())
    sense = re.sub(r"\s+", " ", (sense or "").strip())[:300]
    with db._lock:
        conn = db._conn()
        row = conn.execute("SELECT senses FROM term_exclusions WHERE key = ?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO term_exclusions (key, term, added_at, senses) VALUES (?, ?, ?, ?)",
                (key, display, db._now(), json.dumps([sense] if sense else [], ensure_ascii=False)))
        else:
            senses = db._senses_of(row["senses"])
            if not sense or db.sense_key(sense) in {db.sense_key(x) for x in senses}:
                return False
            if not senses:
                # 예전에 뜻 없이(표기만으로) 뺀 용어 — 그대로 모든 뜻을 빼고 있으니 넓히지 않는다
                return False
            senses.append(sense)
            conn.execute("UPDATE term_exclusions SET senses = ? WHERE key = ?",
                         (json.dumps(senses, ensure_ascii=False), key))
            # 뜻이 늘면 '다른 뜻'으로 판정해 둔 각주가 새 뜻과 같을 수 있으니 다시 판정한다
            conn.execute("DELETE FROM term_sense_checks WHERE key = ? AND same = 0", (key,))
        if sense:
            conn.execute(
                "INSERT OR REPLACE INTO term_sense_checks (key, def_key, same, definition, checked_at) "
                "VALUES (?, ?, 1, ?, ?)", (key, db.sense_key(sense), sense, db._now()))
        return True


def remove_term_exclusion(term: str) -> bool:
    key = db.normalize_term(term)
    with db._lock:
        cur = db._conn().execute("DELETE FROM term_exclusions WHERE key = ?", (key,))
        db._conn().execute("DELETE FROM term_sense_checks WHERE key = ?", (key,))
        return cur.rowcount > 0


def _senses_of(raw) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def term_exclusion_senses() -> dict[str, dict]:
    """정규화 키 → {term, senses}. senses가 빈 목록이면 표기만으로 빼는(모든 뜻) 용어."""
    return {r["key"]: {"term": r["term"], "senses": db._senses_of(r["senses"])}
            for r in db._conn().execute("SELECT key, term, senses FROM term_exclusions ORDER BY rowid")}


def set_term_exclusion_senses(key: str, senses: list[str]) -> None:
    """일괄 채우기(term_senses --backfill)용 — 뜻 목록을 통째로 바꾼다."""
    with db._lock:
        db._conn().execute("UPDATE term_exclusions SET senses = ? WHERE key = ?",
                        (json.dumps(senses, ensure_ascii=False), key))


def term_sense_checks() -> dict[tuple[str, str], tuple[bool, float]]:
    """(용어 키, 설명 키) → (같은 뜻인가, 판정 시각 epoch)."""
    out = {}
    for r in db._conn().execute("SELECT key, def_key, same, checked_at FROM term_sense_checks"):
        try:
            t = datetime.strptime(r["checked_at"], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            t = 0.0
        out[(r["key"], r["def_key"])] = (bool(r["same"]), t)
    return out


def set_term_sense_check(key: str, definition: str, same: bool) -> None:
    with db._lock:
        db._conn().execute(
            "INSERT OR REPLACE INTO term_sense_checks (key, def_key, same, definition, checked_at) "
            "VALUES (?, ?, ?, ?, ?)", (key, db.sense_key(definition), 1 if same else 0, definition, db._now()))
