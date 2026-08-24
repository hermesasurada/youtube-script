#!/usr/bin/env python3
"""외국어 영상 전사문을 요약 없이 통째로 한국어 번역한다.

기존 자동 처리(전사→요약→캡처)와 완전히 분리된 별도 작업이다.
- 대상: 전사 본문이 한국어가 아닌 영상 (최근 것부터)
- 엔진: 로컬 qwen3.8-27b (OpenAI 호환 API)
- 결과: res/translated/{date}/{stem}.md
- 진행 상태는 transcript_translation 테이블에 남겨, 중간에 끊겨도 청크 단위로 이어서 한다.

    python3 transcript_translator.py --once          # 대기 중 1건 처리(런처가 30분마다 호출)
    python3 transcript_translator.py --path <md>     # 특정 전사 지정 처리
    python3 transcript_translator.py --status        # 큐 현황
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
import time

import db
import document_io

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
RES_DIR    = os.path.join(BASE_DIR, "res")
TRANS_DIR  = os.path.join(RES_DIR, "translated")

# 번역 백엔드 — 1차 DGX Spark(vLLM), 2차 로컬 oMLX.
# Spark가 청크당 1.4배 빠르지만 별도 머신이라 꺼져 있을 수 있어, 연결이 안 되면
# 맥 안의 oMLX로 넘어가 번역을 계속한다(둘 다 Qwen3.8 계열이라 결과가 비슷하다).
QWEN_BASE   = os.environ.get("QWEN_BASE_URL", "http://192.168.1.125:8000/v1")
QWEN_MODEL  = os.environ.get("QWEN_MODEL", "qwen3.8-27b")
OMLX_BASE   = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8080/v1")
OMLX_MODEL  = os.environ.get("OMLX_MODEL", "Qwen3.8-27B-Alis-MLX-6bit")
QWEN_TIMEOUT = int(os.environ.get("QWEN_TIMEOUT", "900"))

BACKENDS = [("spark", QWEN_BASE, QWEN_MODEL), ("omlx", OMLX_BASE, OMLX_MODEL)]
_active = 0          # 한 번 폴백하면 이 프로세스 동안 유지(죽은 서버를 매 청크 두드리지 않게)

# 청크가 크면 호출 오버헤드가 줄지만 출력이 길어져 중단 위험이 커진다.
# 실측(16 tok/s)상 4천자 내외가 한 번에 안정적으로 나오는 크기다.
CHUNK_CHARS   = int(os.environ.get("TRANSLATE_CHUNK_CHARS", "4000"))

# 자동 처리 하한(전사일). 이보다 과거 영상은 큐에 올리지 않는다 — 오래된 것까지
# 전부 훑으면 몇 주가 걸리는데 실효가 낮다. 필요하면 --path 로 개별 지정해 돌린다
# (수동 지정은 이 하한을 무시한다).
TRANSLATE_SINCE = os.environ.get("TRANSLATE_SINCE", "2026-08-01")
CTX_TAIL_CHARS = 240      # 직전 청크 꼬리를 문맥으로 물려 용어·화자를 잇는다
MAX_TOKENS    = 6144

_HANGUL_RE = re.compile(r"[가-힣]")
_TS_RE     = re.compile(r"^\[\d+:\d+(?::\d+)?\]")

# 요약 프롬프트(prompt.txt)의 표기 규칙을 번역용으로 옮긴 것.
# 핵심 차이: 여기서는 절대 요약·축약하지 않는다.
SYSTEM_PROMPT = """유튜브 영상 전사문을 한국어로 번역한다.

**가장 중요: 요약하지 않는다.** 원문의 모든 문장을 빠짐없이 옮긴다. 임의로 줄이거나
합치거나 생략하지 말 것. 분량은 원문에 상응해야 한다.

표기 규칙:
- **제품·서비스·브랜드·회사·모델명은 음차하지 말고 원문 표기를 그대로 쓴다** —
  NVIDIA, Anthropic, ChatGPT, OpenAI, SpaceX, S&P 500처럼.
- **인물 이름도 원어를 그대로 쓴다** — "샘 올트먼"이 아니라 **Sam Altman**, "젠슨 황"이 아니라
  **Jensen Huang**. 한국어 음차로 바꾸지 말 것. 전사기가 음차로만 적었고 원어 철자를
  확신할 수 없을 때만 음차를 남긴다.
- 회사·기관명도 같다 — "엔비디아"가 아니라 **NVIDIA**, "오픈에이아이"가 아니라 **OpenAI**.
  한국 기업·기관은 한국어 표기를 쓴다(삼성전자, 금융위원회).
- 전문용어는 널리 쓰이는 한국어 용어가 있으면 그것을 쓰고, 없으면 원문을 유지한다.
  처음 나올 때만 괄호로 원문을 병기한다 — 추론(inference).
- **영어 관용구·업계 은어·약어는 음차하거나 직역하지 말고 뜻이 통하는 한국어로 풀어 쓴다.**
  원문 표현을 살릴 필요가 있으면 풀어 쓴 뒤 괄호에 병기한다.
  예: "5000억 달러 오버 또는 언더?"가 아니라 **"5000억 달러를 넘을까요, 못 넘을까요?"**,
  "종료 ARR"이 아니라 **"연말 기준 ARR(exit ARR)"**, "언더를 택했다"가 아니라
  **"넘지 못한다는 쪽에 걸었다"**. 한국어로 옮겼을 때 뜻이 통하지 않는 표현은 그대로 두지 않는다.
  다만 발화자의 어조·강조는 유지한다(요약이 아니라 번역이므로 문장을 없애지는 않는다).
- 숫자·단위·금액은 원문 값을 그대로 유지한다. 임의로 환산하지 않는다.
- 맥락상 어느 대상인지 헷갈리는 이름은 주변 내용을 근거로 판단한다. 확신이 없으면
  원문 표기를 그대로 둔다(임의로 고치지 말 것).

형식 규칙:
- `[mm:ss]` 형태의 타임스탬프는 원문에 있던 위치에 그대로 유지한다.
- 줄바꿈 구조를 원문과 동일하게 유지한다.
- 구어체의 말더듬·중복(`the the`, `you know`)은 자연스럽게 다듬되 내용은 보존한다.
- 번역문만 출력한다. 설명·머리말·코드펜스를 붙이지 않는다."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _client(idx: int = 0):
    from openai import OpenAI
    _, base, _model = BACKENDS[idx]
    return OpenAI(base_url=base, api_key="none", timeout=QWEN_TIMEOUT)


def active_backend() -> str:
    return BACKENDS[_active][0]


def split_body(md_text: str) -> tuple[str, str]:
    """전사 md → (프론트매터 포함 머리부, 번역할 본문). 본문은 H1 다음부터."""
    lines = md_text.splitlines()
    seps = [i for i, l in enumerate(lines) if l.strip() == "---"]
    start = seps[1] + 1 if len(seps) >= 2 else 0
    # H1 제목 줄은 번역 대상에서 빼고 머리부로 넘긴다(제목은 title_ko가 따로 있다)
    for i in range(start, min(start + 5, len(lines))):
        if lines[i].startswith("# "):
            start = i + 1
            break
    return "\n".join(lines[:start]), "\n".join(lines[start:]).strip()


def is_foreign(body: str) -> bool:
    """전사 본문이 한국어가 아닌지. 앞부분 표본의 한글 비율로 판정한다."""
    sample = body[:4000]
    letters = [ch for ch in sample if ch.isalpha()]
    if not letters:
        return False
    han = sum(1 for ch in letters if _HANGUL_RE.match(ch))
    return (han / len(letters)) < 0.15


def chunk_body(body: str) -> list[str]:
    """타임스탬프 블록 경계로 자른다 — 문장이 청크 중간에서 끊기지 않게."""
    lines = body.splitlines()
    chunks, cur, size = [], [], 0
    for ln in lines:
        # 새 타임스탬프 블록이 시작될 때만 자를 수 있다
        if size >= CHUNK_CHARS and _TS_RE.match(ln.strip()) and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def _is_conn_error(msg: str) -> bool:
    m = msg.lower()
    return "connection" in m or "timeout" in m or "refused" in m or "unreachable" in m


def _call(user: str, temperature: float, rep_penalty: float) -> tuple[str, str]:
    """활성 백엔드로 한 청크 번역. 연결이 안 되면 짧게 재시도한 뒤 다음 백엔드로 넘어간다.

    (한 번의 connection error로 영상 전체가 영구 실패 처리된 사례가 있었다)
    """
    global _active
    last = None
    for idx in range(_active, len(BACKENDS)):
        name, _base, model = BACKENDS[idx]
        for attempt in range(2):
            try:
                r = _client(idx).chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": user}],
                    max_tokens=MAX_TOKENS, temperature=temperature,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False},
                                "repetition_penalty": rep_penalty},
                )
                if idx != _active:
                    log(f"  ✓ {name} 백엔드로 전환")
                    _active = idx
                break
            except Exception as e:                   # noqa: BLE001
                last = e
                if not _is_conn_error(str(e)):
                    raise                            # 연결 외 오류는 폴백 대상이 아니다
                if attempt == 0:
                    log(f"  ↻ {name} 연결 실패 — 20초 뒤 재시도")
                    time.sleep(20)
        else:
            log(f"  ⤳ {name} 사용 불가 — 다음 백엔드 시도")
            continue
        break
    else:
        raise last
    out = (r.choices[0].message.content or "").strip()
    if out.startswith("```"):                     # 가끔 코드펜스로 감싼다
        out = re.sub(r"^```[a-z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out).strip()
    return out, (r.choices[0].finish_reason or "")


def _looks_degenerate(out: str, chunk: str, finish: str) -> bool:
    """같은 말을 max_tokens까지 반복하는 폭주를 걸러낸다.

    번역문은 원문과 분량이 비슷해야 하므로, 출력이 원문의 2.5배를 넘거나
    한 줄이 비정상적으로 길면 정상 번역이 아니다(실제로 'happened happened…'가
    5만 자 넘게 생성된 적이 있다).
    """
    if len(out) > len(chunk) * 2.5:
        return True
    if finish == "length" and len(out) > len(chunk) * 1.6:
        return True
    longest = max((len(l) for l in out.splitlines()), default=0)
    return longest > 3000


def translate_chunk(chunk: str, prev_tail: str = "") -> str:
    user = chunk
    if prev_tail:
        user = (f"[직전까지의 번역 끝부분 — 용어와 화자를 잇기 위한 참고. "
                f"다시 번역하지 말 것]\n{prev_tail}\n\n[여기부터 번역]\n{chunk}")
    out, finish = _call(user, 0.3, 1.05)
    if _looks_degenerate(out, chunk, finish):
        # 폭주는 샘플링 운에 좌우되므로, 온도를 낮추고 반복 페널티를 올려 한 번 더.
        log(f"  ↻ 폭주 감지({len(out):,}자, finish={finish}) — 재시도")
        out2, finish2 = _call(user, 0.15, 1.15)
        if not _looks_degenerate(out2, chunk, finish2):
            return out2
        log(f"  ⚠ 재시도도 비정상({len(out2):,}자) — 짧은 쪽을 채택")
        return out2 if len(out2) < len(out) else out
    return out


def out_path_for(md_path: str) -> str:
    """res/{date}/{stem}.md → res/translated/{date}/{stem}.md"""
    rel = os.path.relpath(os.path.abspath(md_path), RES_DIR)
    return os.path.join(TRANS_DIR, rel)


def translate_file(md_path: str, yt_id: str = "") -> dict:
    """전사 하나를 통째로 번역해 저장. 청크 단위로 이어쓰기 때문에 중단에 안전하다."""
    md = open(md_path, encoding="utf-8").read()
    head, body = split_body(md)
    if not body.strip():
        return {"ok": False, "error": "본문 없음"}
    if not is_foreign(body):
        db.set_translation_state(yt_id, md_path, "", "skipped", 0, 0, "한국어 영상")
        return {"ok": True, "skipped": "한국어 영상"}

    chunks = chunk_body(body)
    dest = out_path_for(md_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 이어하기: 이미 끝난 청크 수만큼 건너뛴다
    st = db.get_translation_state(yt_id) if yt_id else None
    done_n = (st or {}).get("chunks_done") or 0
    parts: list[str] = []
    if done_n and os.path.isfile(dest):
        prev = open(dest, encoding="utf-8").read()
        marker = "\n\n<!--CHUNK-->\n\n"
        # 머리부(프론트매터·H1·마커)를 떼고 번역 청크만 복원한다. 이걸 빼먹으면
        # parts[0]에 머리부가 섞여 들어가고, _assemble이 머리부를 다시 붙여
        # 재개할 때마다 프론트매터가 한 벌씩 쌓인다.
        m = re.search(r"<!--TRANSLATED_BY:[^>]*-->[ \t]*\n?"
                      r"(?:<!--TRANSLATE_PARTIAL:[^>]*-->[ \t]*\n?)?", prev)
        body_prev = prev[m.end():] if m else prev
        parts = [p for p in body_prev.split(marker)]
        parts = parts[:done_n]                       # 신뢰할 수 있는 만큼만 유지
        parts = [p.strip() for p in parts if p.strip()]
        log(f"이어하기: {len(parts)}/{len(chunks)} 청크 완료 상태")
        done_n = len(parts)
    else:
        done_n = 0

    db.set_translation_state(yt_id, md_path, dest, "processing", done_n, len(chunks), "")
    t_all = time.time()
    for i in range(done_n, len(chunks)):
        tail = parts[-1][-CTX_TAIL_CHARS:] if parts else ""
        t0 = time.time()
        try:
            out = translate_chunk(chunks[i], tail)
        except Exception as e:                        # noqa: BLE001
            db.set_translation_state(yt_id, md_path, dest, "failed", len(parts),
                                     len(chunks), str(e)[:300])
            log(f"청크 {i+1}/{len(chunks)} 실패: {e}")
            return {"ok": False, "error": str(e), "done": len(parts), "total": len(chunks)}
        parts.append(out)
        # 청크마다 저장 → 중단돼도 여기까지는 남는다
        document_io.atomic_write_text(
            dest, _assemble(head, parts, len(chunks)))
        db.set_translation_state(yt_id, md_path, dest, "processing", len(parts),
                                 len(chunks), "")
        log(f"청크 {i+1}/{len(chunks)} 완료 ({len(out):,}자, {time.time()-t0:.0f}s)")

    document_io.atomic_write_text(dest, _assemble(head, parts, len(chunks), final=True))
    db.set_translation_state(yt_id, md_path, dest, "done", len(parts), len(chunks), "")
    log(f"완료: {os.path.basename(dest)} ({time.time()-t_all:.0f}s)")
    return {"ok": True, "path": dest, "chunks": len(chunks)}


def _assemble(head: str, parts: list[str], total: int, final: bool = False) -> str:
    marker = "\n\n<!--CHUNK-->\n\n"
    body = marker.join(parts)
    note = "" if final else f"\n<!--TRANSLATE_PARTIAL:{len(parts)}/{total}-->\n"
    return f"{head}\n<!--TRANSLATED_BY:{QWEN_MODEL}-->{note}\n{body}\n"


def _since_ts() -> float | None:
    """TRANSLATE_SINCE(YYYY-MM-DD) → unix timestamp. 비우면 하한 없음."""
    s = (TRANSLATE_SINCE or "").strip()
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d"))
    except ValueError:
        log(f"TRANSLATE_SINCE 형식 오류({s!r}) — 하한 없이 진행")
        return None


def pick_next() -> dict | None:
    """최근 영상부터, 아직 번역 안 된 외국어 전사 1건(전사일 하한 적용)."""
    rows = db.translation_candidates(limit=40, since_ts=_since_ts())
    for r in rows:
        p = r["md_path"]
        if not p or not os.path.isfile(p):
            continue
        try:
            _, body = split_body(open(p, encoding="utf-8").read())
        except Exception:
            continue
        if not body.strip():
            continue
        if not is_foreign(body):
            db.set_translation_state(r["yt_id"], p, "", "skipped", 0, 0, "한국어 영상")
            continue
        return r
    return None


LOCK_PATH = os.path.join(RES_DIR, ".translate.lock")


def acquire_lock():
    """단일 인스턴스 보장 — 한 건 처리가 30분(런처 주기)을 넘겨도 겹치지 않게 한다.
    반환한 파일 객체를 살려 둬야 잠금이 유지된다."""
    os.makedirs(RES_DIR, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="대기 중 1건 처리")
    ap.add_argument("--path", help="특정 전사 md 경로 처리")
    ap.add_argument("--status", action="store_true", help="현황 출력")
    a = ap.parse_args()

    db.init()
    if a.status:
        for row in db.translation_status_counts():
            print(f"  {row['status']:<10} {row['n']}")
        cur = db.get_translation_inflight()
        if cur:
            print(f"  진행중: {cur['chunks_done']}/{cur['chunks_total']} — {cur['md_path']}")
        since = _since_ts()
        print(f"  자동 처리 하한: 전사일 {TRANSLATE_SINCE} 이후"
              if since else "  자동 처리 하한: 없음(전체)")
        print(f"  남은 후보: {len(db.translation_candidates(9999, since))}건"
              " (한국어 포함 — 실제 번역 대상은 이보다 적다)")
        return 0

    # 실제 번역을 돌리는 경로는 모두 같은 잠금을 쓴다. 한 건이 런처 주기(30분)를
    # 넘겨도 두 프로세스가 같은 파일에 동시에 쓰지 않게 하려는 것.
    if a.path or a.once:
        lock = acquire_lock()
        if lock is None:
            log("이미 처리 중(잠금 보유) — 종료")
            return 0

    if a.path:
        p = os.path.abspath(a.path)
        yt = db.yt_id_for_md(p) or ""
        log(f"지정 처리: {os.path.basename(p)}")
        r = translate_file(p, yt)
        log(str(r))
        return 0 if r.get("ok") else 1

    if a.once:
        nxt = pick_next()
        if not nxt:
            log("대상 없음")
            return 0
        log(f"대상: {nxt['title'][:60]}")
        r = translate_file(nxt["md_path"], nxt["yt_id"])
        log(str(r))
        return 0 if r.get("ok") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
