"""각주 제외의 '뜻' 판정(2026-10-30 사용자 지시).

각주 제외(term_exclusions)는 원래 표기만 봤다. 그래서 AI 문맥에서 뺀 '하네스'(모델 실행 환경)가
자동차 영상의 '하네스'(차량 배선 장치) 각주까지 지웠다. 이제 ✕를 누른 각주의 설명문을 그 용어의
'뜻'으로 저장하고, 같은 표기의 다른 각주는 설명이 그 뜻과 같을 때만 뺀다.

같은 뜻인지는 글자 유사도로 못 가린다(같은 뜻의 설명끼리도 표현이 달라 2글자 겹침이 0인 경우가
많다 — 온톨로지·CDS·관리처분인가). 그래서 로컬 Qwen(Spark → oMLX)에게 한 번 묻고 결과를
term_sense_checks에 캐시한다. 판정 전·실패 시에는 예전처럼 뺀다(보수적 기본값) — 판정이 늦어도
화면이 예전보다 나빠지지는 않는다.

판정 시점: 요약 저장 직후(_reindex_summary), 요약을 열 때(아직 판정 안 된 각주가 있으면),
✕로 뜻이 늘었을 때(그 용어의 모든 요약). 모두 백그라운드 스레드.

    python term_senses.py --backfill [--dry-run]   # 기존 제외 용어의 뜻 채우기 + 전수 판정
"""
from __future__ import annotations

import argparse
import glob
import html
import os
import re
import threading

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_DIR = os.path.join(BASE_DIR, "res", "summary")

_NOTE_RE = re.compile(r'<p class="term-note">(.*?)</p>', re.S)
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

_SYSTEM = "용어 해설 두 개가 같은 뜻인지 판정한다. SAME 또는 DIFFERENT 한 단어로만 답한다."


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", fragment or ""))).strip()


def label_of(raw_label: str) -> str:
    """'용어 (원어)' → '용어' — 화면(common.js `_noteLabel`)과 같은 기준."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", _text(raw_label)).strip()


def notes_in(text: str) -> list[tuple[str, str]]:
    """요약 원문의 각주 행 → [(용어, 설명문)]. 설명문은 각주 행에서 용어 부분을 뺀 나머지."""
    out = []
    for inner in _NOTE_RE.findall(text or ""):
        m = _STRONG_RE.search(inner)
        if not m:
            continue
        label = label_of(m.group(1))
        rest = _text(inner[:m.start()] + " " + inner[m.end():])
        definition = re.sub(r"^[\s*—–:：\-]+", "", rest).strip()
        if label and definition:
            out.append((label, definition))
    return out


def judge(term: str, definition: str, senses: list[str]) -> bool | None:
    """definition이 senses 중 하나와 같은 뜻이면 True, 다르면 False, 판정 불가면 None."""
    if db.sense_key(definition) in {db.sense_key(s) for s in senses}:
        return True
    listed = "\n".join(f"{i}. {s}" for i, s in enumerate(senses, 1))
    user = (f"용어: {term}\n\n"
            f"사용자가 각주를 빼기로 한 뜻:\n{listed}\n\n"
            f"판정할 각주의 해설:\n{definition}\n\n"
            "두 해설이 같은 분야에서 같은 대상·개념을 가리키면 SAME이다. 표현·관점·범위·자세함이 달라도, "
            "한쪽이 더 좁거나 넓게 설명해도 SAME이다(예: '모바일용 저전력 메모리' ↔ '전력 소모를 줄인 DRAM "
            "규격', 'AI를 인간 의도에 맞추는 연구' ↔ '모델이 원하는 형태로 답하도록 추가 학습하는 과정').\n"
            "DIFFERENT는 표기만 같고 전혀 다른 분야·대상일 때뿐이다(예: 'AI 모델 실행 환경' ↔ '차량 배선 장치', "
            "'코드 변경 병합 요청' ↔ '홍보 활동', 'AI 자기 개선 루프' ↔ '주가 상대강도지수').\n"
            "애매하면 SAME.")
    out = _ask(user, term)
    if out is None:
        return None
    m = re.search(r"\b(SAME|DIFFERENT)\b", out.upper()[-40:])     # 답은 끝 단어(추론 뒤)
    if not m:
        return None
    return m.group(1) == "SAME"


def _ask(user: str, term: str) -> str | None:
    """Spark → oMLX 순서로 묻는다. 추론(thinking)을 켠다 — 끄면 표현만 다른 같은 뜻을 '다름'으로
    자주 오판했다(2026-10-30 실측: 15건 중 14건 오판 → 추론 켜고 경계 사례 7건 전부 정답, 건당 5~30초).
    판정은 백그라운드라 지연은 괜찮다. 둘 다 안 되면 None."""
    try:
        import transcript_translator as tt
    except Exception:  # noqa: BLE001 — 판정은 부가 기능, 실패하면 예전처럼 뺀다
        return None
    for idx, (_name, base, model) in enumerate(tt.BACKENDS):
        try:
            with tt._llm_track("local", model, purpose="term_sense", title=term,
                               reasoning="think", backend="http", host=tt._llm_host(base)) as call:
                r = tt._client(idx).chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                    max_tokens=4000, temperature=0.0,
                    extra_body={"chat_template_kwargs": {"enable_thinking": True}})
                tt._llm_record_response(call, r)
            return (r.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            if not tt._is_conn_error(str(e)):
                return None
    return None


def pending(text: str, excl: dict | None = None, checks: dict | None = None) -> list[tuple[str, str, str]]:
    """이 요약에서 아직 판정이 없는 (용어 키, 용어, 설명문). 뜻이 기록된 제외 용어만 대상."""
    excl = db.term_exclusion_senses() if excl is None else excl
    checks = db.term_sense_checks() if checks is None else checks
    out, seen = [], set()
    for label, definition in notes_in(text):
        key = db.normalize_term(label)
        info = excl.get(key)
        if not info or not info["senses"]:
            continue
        k = (key, db.sense_key(definition))
        if k in checks or k in seen:
            continue
        seen.add(k)
        out.append((key, info["term"], definition))
    return out


def check_texts(texts, log=None) -> int:
    """여러 요약 원문의 미판정 각주를 판정해 캐시에 넣는다. 판정한 수."""
    excl = db.term_exclusion_senses()
    checks = db.term_sense_checks()
    done = 0
    for text in texts:
        for key, term, definition in pending(text, excl, checks):
            same = judge(term, definition, excl[key]["senses"])
            if same is None:
                continue
            db.set_term_sense_check(key, definition, same)
            checks[(key, db.sense_key(definition))] = (same, 0.0)
            done += 1
            if log:
                log(f"{'같음' if same else '다름'}  {term}: {definition[:60]}")
    return done


_lock = threading.Lock()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def schedule(path: str) -> None:
    """요약 하나의 미판정 각주를 백그라운드에서 판정한다(없으면 스레드를 띄우지 않는다)."""
    try:
        if not pending(_read(path)):
            return
    except Exception:  # noqa: BLE001 — 부가 기능
        return

    def run():
        with _lock:                       # Spark에 판정을 한 줄로 보낸다
            try:
                check_texts([_read(path)])
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=run, daemon=True).start()


def _summary_files() -> list[str]:
    return sorted(glob.glob(os.path.join(SUMMARY_DIR, "**", "*.md"), recursive=True))


def schedule_term(term: str) -> None:
    """뜻이 늘어난 용어를 쓰는 모든 요약을 백그라운드에서 다시 판정한다."""
    key = db.normalize_term(term)

    def run():
        with _lock:
            try:
                texts = [t for t in map(_read, _summary_files())
                         if any(db.normalize_term(lb) == key for lb, _ in notes_in(t))]
                check_texts(texts)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=run, daemon=True).start()


def kept_keys() -> list[str]:
    """화면용: 뜻이 다르다고 판정돼 남길 각주의 '용어 키\x01설명 키' 목록(common.js가 같은 키로 맞춘다)."""
    return [f"{k}\u0001{d}" for (k, d), (same, _) in db.term_sense_checks().items() if not same]


def _file_date(path: str) -> str:
    m = re.search(r"/(\d{8})/", path)
    return m.group(1) if m else ""


def backfill(dry_run: bool = False) -> None:
    """뜻이 비어 있는 기존 제외 용어에 뜻을 채운다: 제외한 날 이전(같은 날 포함) 요약 가운데 가장
    최근 것의 각주 설명(✕를 누른 화면일 가능성이 가장 높다), 없으면 가장 이른 것. 각주가 하나도
    남아 있지 않은 용어는 비워 둔다(표기만으로 빼는 예전 동작 유지). 그 뒤 전수 판정."""
    excl = db.term_exclusion_senses()
    added = {r["key"]: r["added_at"] for r in db._conn().execute(   # noqa: SLF001 — 추가 시각만 읽는다
        "SELECT key, added_at FROM term_exclusions")}
    files = _summary_files()
    texts = {p: _read(p) for p in files}
    found: dict[str, list[tuple[str, str]]] = {}
    for p, t in texts.items():
        for label, definition in notes_in(t):
            key = db.normalize_term(label)
            if key in excl:
                found.setdefault(key, []).append((_file_date(p), definition))
    filled = 0
    for key, info in excl.items():
        if info["senses"] or key not in found:
            continue
        day = (added.get(key) or "")[:10].replace("-", "")
        cands = sorted(found[key])
        before = [c for c in cands if c[0] and c[0] <= day]
        sense = (before[-1] if before else cands[0])[1]
        print(f"뜻 채움  {info['term']}: {sense[:70]}")
        filled += 1
        if not dry_run:
            db.set_term_exclusion_senses(key, [sense])
            db.set_term_sense_check(key, sense, True)
    print(f"뜻 채운 용어 {filled}개 / 각주가 남아 있지 않아 비워 둔 용어 "
          f"{sum(1 for k, v in excl.items() if not v['senses'] and k not in found)}개")
    if dry_run:
        return
    n = check_texts(texts.values(), log=lambda s: print("  " + s))
    print(f"판정 {n}건")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        db.init()                       # 스키마(v26 senses 열) 보장 — 서버보다 먼저 돌려도 된다
        backfill(a.dry_run)
    else:
        ap.print_help()
