"""요약 원문의 각주 표식(\\*)과 각주 행의 짝을 맞춘다(2026-09-28 사용자 지시).

모델이 표식을 빠뜨리거나(각주만 있음), 각주 없이 표식만 남기거나, 소제목·한눈 요약에
표식을 붙이는 요약이 약 8% 있었다. 저장 직전(app._polish_summary)과 기존 파일 일괄
교정(`python term_marks.py --apply`)에서 같은 규칙으로 고친다. LLM 호출 없음.

  - `## 3.` 본문의 표식마다 바로 앞 용어에 맞는 각주를 찾는다. 짝이 없으면 지운다.
  - 소제목(###)과 한눈 요약(## 2.)의 표식은 지운다(프롬프트 규칙).
  - 표식이 없는 각주는 그 용어가 본문에 처음 나온 곳 뒤에 표식을 붙인다(괄호 병기·볼드
    닫힘 뒤). 용어가 본문에 없으면(다른 표기) 각주만 둔다.
  - 이스케이프 없이 용어 뒤에 붙은 단독 `*`는 표식으로 보고 `\\*`로 바꾼다.
  - 용어와 다른 소제목 아래 놓인 각주는 용어가 처음 표식된 단락 바로 뒤로 옮긴다.
"""
from __future__ import annotations

import re

NOTE_RE = re.compile(r'<p class="term-note">\s*\*?\s*<strong>(.*?)</strong>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_MARK = "\\*"


def _keys(label: str) -> list[str]:
    label = _TAG_RE.sub("", label).strip()
    base = re.sub(r"\s*\([^()]*\)\s*$", "", label).strip()
    no_paren = re.sub(r"\s*\([^()]*\)", "", label).strip()     # 'KV (Key-Value) 캐시' → 'KV 캐시'
    paren = re.search(r"\(([^()]+)\)\s*$", label)
    parts = [base, no_paren, label] + (paren.group(1).split("/") if paren else [])
    out = []
    for k in parts:
        k = k.strip().lower()
        if len(k) >= 2 and k not in out:
            out.append(k)
    return out


def _plain(s: str) -> str:
    """비교용 — 태그·볼드·이스케이프를 걷어낸 소문자."""
    return _TAG_RE.sub("", s).replace("**", "").replace("\\", "").lower()


_GAP = " \u00a0-\u2010\u2011\u2012\u2013\u2014·"


def _compact(text: str) -> tuple[str, list[int]]:
    """띄어쓰기·하이픈을 뺀 소문자와, 각 글자의 원래 위치."""
    out, pos = [], []
    for i, ch in enumerate(text.lower()):
        if ch in _GAP:
            continue
        out.append(ch)
        pos.append(i)
    return "".join(out), pos


def _ckey(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch not in _GAP)


def _locate(line: str, keys: list[str]) -> int:
    """본문 줄에서 가장 먼저 끝나는 용어 위치의 끝(원래 줄 기준, 없으면 -1).
    띄어쓰기 차이(하이퍼 스케일러)는 무시하고, 영문 용어는 앞뒤 영문과 이어지지 않아야
    한다(앞의 숫자는 허용: 150bp)."""
    comp, pos = _compact(line)
    best = -1
    for key in keys:
        k = _ckey(key)
        if not k:
            continue
        at = comp.find(k)
        while at >= 0:
            pre = comp[at - 1] if at > 0 else " "
            post = comp[at + len(k)] if at + len(k) < len(comp) else " "
            ok_pre = not (k[0].isascii() and k[0].isalnum() and re.match(r"[a-z]", pre))
            ok_post = not (k[-1].isascii() and k[-1].isalnum() and re.match(r"[a-z0-9]", post))
            if ok_pre and ok_post:
                end = pos[at + len(k) - 1] + 1
                if best < 0 or end < best:
                    best = end
                break
            at = comp.find(k, at + 1)
    return best


def _body_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("#", "<", "|", "```")) and "term-note" not in s


def _star_positions(line: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(_MARK), line)]


_TAIL_OK = re.compile(r"\s*(\([^()\n]{0,60}\))?\s*[\"'”’」』)\]]*\s*")


def _paren_after(after: str) -> str:
    """표식 바로 뒤의 괄호 병기 내용('트랜스포머\\*(transformer)'의 transformer)."""
    m = re.match(r"\s*\(([^()\n]{1,60})\)", _plain(after))
    return m.group(1) if m else ""


def _matches(before: str, key_list: list[str], after: str = "") -> bool:
    """표식 바로 앞이 이 용어인가 — 용어와 표식 사이에는 괄호 병기·따옴표·(걷어낸) 볼드
    닫힘만 허용한다(예전엔 35자까지 허용해 'PR은 … 무너져**\\*'도 PR 짝으로 봤다)."""
    plain = _plain(before)[-120:]
    comp, pos = _compact(plain)
    for k in key_list:
        ck = _ckey(k)
        if not ck:
            continue
        at = comp.rfind(ck)
        while at >= 0:
            end = pos[at + len(ck) - 1] + 1
            if _TAIL_OK.fullmatch(plain[end:]):
                return True
            at = comp.rfind(ck, 0, at)
    inside = _ckey(_paren_after(after))
    return bool(inside) and any(_ckey(k) and _ckey(k) in inside for k in key_list)


def _is_name_star(before: str) -> bool:
    """'궁수자리 A\\*'처럼 한 글자 이름 뒤의 별표는 이름의 일부로 본다."""
    token = re.split(r"\s+", _plain(before).rstrip())[-1] if before.strip() else ""
    return len(token) == 1


def fix(md: str) -> tuple[str, dict]:
    """(교정된 마크다운, 통계). 본문(## 3.)이 없으면 그대로.

    짝은 '각주 묶음 + 그 앞 본문 단락들'을 한 단위로 본다(모델이 각주를 해당 단락 바로
    뒤에 두므로). 단위 안에서 글자로 맞는 것부터, 남은 표식·각주 수가 같으면 순서대로
    짝짓는다(본문은 한글 '리소그래피', 각주는 영문 'lithography'인 경우).
    """
    stats = {"removed": 0, "added": 0, "escaped": 0, "unplaced": 0, "relocated": 0}
    if not md or "## 3." not in md:
        return md, stats
    head, core = md.split("## 3.", 1)
    n = head.count(_MARK)
    if n:                                           # 한눈 요약·머리말은 각주 대상이 아니다
        head = head.replace(_MARK, "")
        stats["removed"] += n
    lines = core.split("\n")
    all_keys = [_keys(x) for x in NOTE_RE.findall(core)]

    # 줄을 훑으며 단위를 만든다: 본문 줄의 표식을 모으다가 각주 묶음을 만나면 짝짓는다.
    pending: list[tuple[int, int]] = []            # (줄, 열) — 아직 짝 없는 표식
    unit_lines: list[int] = []                     # 이번 단위의 본문 줄
    orphans: list[tuple[int, int]] = []            # 단위 안에서 짝을 못 찾은 표식
    unmatched_notes: list[tuple[list[str], list[int]]] = []   # (키, 찾아볼 줄들)
    used_note = [False] * len(all_keys)
    force_drop: list[tuple[int, int]] = []         # 자리가 틀린 표식(다른 짝 찾지 않고 지움)
    note_idx = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("#"):
            if _MARK in line:
                stats["removed"] += line.count(_MARK)
                lines[i] = line.replace(_MARK, "")
            orphans.extend(pending); pending = []; unit_lines = []
            i += 1
            continue
        if s.startswith("<div"):
            j = i
            while j < len(lines) and "</div>" not in lines[j]:
                j += 1
            block = "\n".join(lines[i:j + 1])
            if 'class="term-notes"' in block or "term-note" in block:
                cnt = len(NOTE_RE.findall(block))
                idxs = list(range(note_idx, note_idx + cnt))
                note_idx += cnt
                free_stars = list(pending)
                free_notes = []
                for k in idxs:                       # 1) 글자로 맞는 짝
                    hit = next((st for st in free_stars
                                if _matches(lines[st[0]][:st[1]], all_keys[k],
                                            lines[st[0]][st[1] + len(_MARK):])), None)
                    if hit:
                        free_stars.remove(hit); used_note[k] = True
                    else:
                        free_notes.append(k)
                # 2) 남은 수가 같으면 순서대로 짝짓는다. 다만 다른 각주와 글자가 맞는 표식은
                #    그쪽 짝이므로(앞 단락 각주의 용어가 여기 다시 나온 경우) 빼고 센다.
                foreign = [st for st in free_stars
                           if any(_matches(lines[st[0]][:st[1]], ks, lines[st[0]][st[1] + len(_MARK):])
                                  for ks in all_keys)]
                unknown = [st for st in free_stars if st not in foreign]
                if unknown and free_notes and len(unknown) == len(free_notes):
                    # 순서로 짝지은 각주의 용어가 이 단위 본문에 실제로 있으면 표식이 엉뚱한 자리에
                    # 붙은 것이다(DHH: 'PR' 각주의 표식이 볼드 문장 끝 '무너져**\\*'에 붙음).
                    # 그 표식은 지우고 용어 첫 등장 뒤에 다시 단다(아래 4단계).
                    moved = []
                    for st, k in zip(unknown, free_notes):
                        if any(_locate(lines[li], all_keys[k]) >= 0 for li in unit_lines):
                            moved.append((st, k))
                        else:
                            used_note[k] = True
                    unknown = [st for st, _ in moved]
                    free_notes = [k for _, k in moved]
                    force_drop.extend(unknown)
                    unknown = []
                free_stars = foreign + unknown
                orphans.extend(free_stars)
                for k in free_notes:
                    unmatched_notes.append((k, list(unit_lines)))
                pending = []; unit_lines = []
            i = j + 1
            continue
        if _body_line(line):
            # 이스케이프 없는 단독 * 가 각주 용어 바로 뒤에 있으면 표식으로 본다.
            def esc(m: re.Match) -> str:
                if any(_matches(line[: m.start()], ks) for ks in all_keys):
                    stats["escaped"] += 1
                    return _MARK
                return m.group(0)
            line = re.sub(r"(?<![\\*\s])\*(?![*])", esc, line)
            lines[i] = line
            pending.extend((i, c) for c in _star_positions(line))
            unit_lines.append(i)
        i += 1
    orphans.extend(pending)

    # 3) 단위 밖 짝: 남은 표식을 아직 안 쓴 각주와 글자로 맞춰 본다. 끝까지 없으면 지운다.
    drop: dict[int, list[int]] = {}
    for (li, col) in force_drop:
        drop.setdefault(li, []).append(col)
    for (li, col) in orphans:
        before, after = lines[li][:col], lines[li][col + len(_MARK):]
        hit = next((k for k, ks in enumerate(all_keys)
                    if not used_note[k] and _matches(before, ks, after)), None)
        if hit is not None:
            used_note[hit] = True
            unmatched_notes = [x for x in unmatched_notes if x[0] != hit]
            continue
        if _is_name_star(before):
            continue
        drop.setdefault(li, []).append(col)
    for li, cols in drop.items():
        line = lines[li]
        for col in sorted(cols, reverse=True):
            line = line[:col] + line[col + len(_MARK):]
            stats["removed"] += 1
        lines[li] = line

    # 4) 표식 없는 각주: 같은 단위의 본문에서(없으면 본문 전체에서) 용어 첫 등장 뒤에 붙인다.
    body_lines = [k for k, l in enumerate(lines) if _body_line(l) and not l.strip().startswith("<")]
    for k, scope in unmatched_notes:
        if used_note[k]:
            continue
        placed = False
        for li in scope + [x for x in body_lines if x not in scope]:
            line = lines[li]
            best = _locate(line, all_keys[k])
            if best < 0:
                continue
            opened = line.rfind("(", 0, best)
            close = line.find(")", best)
            if opened >= 0 and line.rfind(")", 0, best) < opened and 0 <= close - best <= 60:
                end = close + 1                     # '트랜스포머(transformer)' → 괄호 뒤
                tail = re.match(r"(\*\*)?", line[end:])
                end += tail.end() if tail else 0
            else:
                tail = re.match(r"(\*\*)?(\s*\([^()\n]{1,60}\))?(\*\*)?", line[best:])
                end = best + (tail.end() if tail else 0)
            if line[end:end + len(_MARK)] != _MARK:
                lines[li] = line[:end] + _MARK + line[end:]
                stats["added"] += 1
            used_note[k] = placed = True
            break
        if not placed:
            stats["unplaced"] += 1
    lines, stats["relocated"] = _relocate_notes(lines)
    return head + "## 3." + "\n".join(lines), stats


_NOTE_P_RE = re.compile(r'<p class="term-note">.*?</p>', re.S)


def _relocate_notes(lines: list[str]) -> tuple[list[str], int]:
    """다른 소제목 아래로 간 각주를 용어가 처음 표식된 단락 바로 뒤로 옮긴다(2026-10-02).

    모델이 각주를 글 끝에 몰아 두는 요약이 있다('초지능 협약': 대담 섹션의 OpenShell·
    BlueField 각주가 마지막 '전망' 섹션 뒤에 붙음). 같은 소제목 안에 있는 각주는 그대로 둔다.
    표식을 찾지 못한 각주도 그대로 둔다.
    """
    section = []                                     # 줄마다 속한 ### 소제목 줄 번호
    cur = -1
    for i, l in enumerate(lines):
        if l.strip().startswith("#"):
            cur = i
        section.append(cur)
    blocks = []                                      # (시작, 끝, [각주 행])
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("<div") and "term-notes" in lines[i]:
            j = i
            while j < len(lines) and "</div>" not in lines[j]:
                j += 1
            blocks.append((i, j, _NOTE_P_RE.findall("\n".join(lines[i:j + 1]))))
            i = j + 1
            continue
        i += 1
    if not blocks:
        return lines, 0

    def para_end(li: int) -> int:
        while li + 1 < len(lines) and lines[li + 1].strip() and _body_line(lines[li + 1]):
            li += 1
        return li

    def first_mark(keys: list[str]) -> int:
        for li, l in enumerate(lines):
            if not _body_line(l):
                continue
            for col in _star_positions(l):
                if _matches(l[:col], keys, l[col + len(_MARK):]):
                    return li
        return -1

    def section_lines(start: int) -> list[int]:
        """각주 묶음이 속한 소제목의 제목 줄과 본문 줄(각주 행 제외)."""
        head = section[start]
        out = [head] if head >= 0 else []
        for k in range(head + 1, len(lines)):
            if section[k] != head:
                break
            if _body_line(lines[k]):
                out.append(k)
        return out

    moves = {}                                       # (블록, 행 순번) → 넣을 단락 끝 줄
    for bi, (start, _end, notes) in enumerate(blocks):
        own = section_lines(start)
        for ni, note in enumerate(notes):
            m = NOTE_RE.search(note)
            if not m:
                continue
            keys = _keys(m.group(1))
            target = first_mark(keys)
            # 각주가 놓인 소제목(제목 포함)에 용어가 한 번이라도 나오면 그 자리를 존중한다
            # — 소제목 주제가 그 용어인데 표식만 뒤쪽 단락에 붙은 경우('행동주의' 사례).
            if target < 0 or section[target] == section[start] or any(_locate(lines[k], keys) >= 0 for k in own):
                continue
            moves[(bi, ni)] = para_end(target)
    if not moves:
        return lines, 0

    def following_block(pend: int) -> int:
        """단락 바로 뒤(빈 줄만 건너) 각주 묶음이 있으면 그 블록 번호."""
        k = pend + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        return next((bi for bi, b in enumerate(blocks) if b[0] == k), -1)

    keep = {bi: [n for ni, n in enumerate(b[2]) if (bi, ni) not in moves] for bi, b in enumerate(blocks)}
    extra = {bi: [] for bi in range(len(blocks))}
    new_after: dict[int, list[str]] = {}
    for (bi, ni), pend in sorted(moves.items(), key=lambda x: (x[1], x[0])):
        note = blocks[bi][2][ni]
        fb = following_block(pend)
        if fb >= 0:
            extra[fb].append(note)
        else:
            new_after.setdefault(pend, []).append(note)
    touched = {bi for bi, _ in moves} | {bi for bi, v in extra.items() if v}
    starts = {b[0]: bi for bi, b in enumerate(blocks)}
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i in starts:
            bi = starts[i]
            start, end, _ = blocks[bi]
            if bi not in touched:
                out.extend(lines[start:end + 1])
            else:
                notes = keep[bi] + extra[bi]
                if notes:
                    out.extend(['<div class="term-notes">', *notes, "</div>"])
                elif out and not out[-1].strip() and end + 1 < len(lines) and not lines[end + 1].strip():
                    i = end + 2                      # 빈 블록과 그 뒤 빈 줄을 함께 뺀다
                    continue
            i = end + 1
            continue
        out.append(lines[i])
        if i in new_after:
            out.extend(["", '<div class="term-notes">', *new_after[i], "</div>"])
        i += 1
    return out, len(moves)


def _main() -> int:
    import argparse
    import glob
    import json
    import os
    import shutil
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir")
    args = ap.parse_args()
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res", "summary")
    total = {"files": 0, "changed": 0, "removed": 0, "added": 0, "escaped": 0, "unplaced": 0, "relocated": 0}
    changed = []
    for path in sorted(glob.glob(os.path.join(root, "*", "*.md"))):
        with open(path, encoding="utf-8") as f:
            md = f.read()
        new, st = fix(md)
        total["files"] += 1
        for k in ("removed", "added", "escaped", "unplaced", "relocated"):
            total[k] += st[k]
        if new != md:
            total["changed"] += 1
            changed.append(path)
            if args.apply:
                if args.backup_dir:
                    dst = os.path.join(args.backup_dir, os.path.relpath(path, root))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(path, dst)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
    print(json.dumps(total, ensure_ascii=False))
    if args.backup_dir:
        with open(os.path.join(args.backup_dir, "changed.json"), "w", encoding="utf-8") as f:
            json.dump(changed, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
