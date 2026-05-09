"""
res/ 하위 파일들을 '{일시}_{길이}_{제목}' 형식으로 일괄 변환.
--dry  : 변경 목록만 출력 (기본값)
--run  : 실제 변환 실행
-y     : 확인 없이 바로 실행 (--run 과 함께 사용)
"""
import io
import json
import os
import re
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
RES_DIR   = os.path.join(BASE_DIR, "res")

# 기존 'audio_YYYYMMDDHHMM_dur' 패턴
_OLD_PAT  = re.compile(r'^audio_(\d{12})_(.+)$')
# 이미 새 형식 'YYYYMMDDHHMM_dur_title' 패턴
_NEW_PAT  = re.compile(r'^(\d{12})_(\w+?)_(.+)$')


def _safe_stem(title: str, maxlen: int = 60) -> str:
    s = re.sub(r'[^\w\s]', '', title)   # 영문·숫자·한글·언더바 외 제거
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('_')
    return s[:maxlen] if s else 'untitled'


def _dur_tag(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s   = divmod(rem, 60)
    return (f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s") if secs > 0 else "0s"


def _free_stem(directory: str, stem: str) -> str:
    """이미 존재하는 이름이면 숫자 suffix를 붙여 충돌 회피."""
    candidate, i = stem, 1
    while os.path.exists(os.path.join(directory, candidate + '.json')):
        candidate = f"{stem}_{i}"
        i += 1
    return candidate


def main(dry: bool) -> None:
    mode    = "[DRY RUN]" if dry else "[RENAME ]"
    renamed = skipped = errors = 0

    for date_dir in sorted(os.listdir(RES_DIR)):
        date_path = os.path.join(RES_DIR, date_dir)
        if not os.path.isdir(date_path):
            continue

        for fname in sorted(os.listdir(date_path)):
            if not fname.endswith('.json'):
                continue

            old_stem  = fname[:-5]
            json_path = os.path.join(date_path, fname)

            # ── JSON 읽기 ──────────────────────────────────────────────
            try:
                with open(json_path, encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception as e:
                print(f"  ERROR  {date_dir}/{fname}: {e}")
                errors += 1
                continue

            title = (meta.get('title') or '').strip()
            if not title:
                print(f"  SKIP   {date_dir}/{fname}: title 없음")
                skipped += 1
                continue

            # ── 타임스탬프·길이 추출 ───────────────────────────────────
            m_old = _OLD_PAT.match(old_stem)   # audio_YYYYMMDDHHMM_dur
            m_new = _NEW_PAT.match(old_stem)   # YYYYMMDDHHMM_dur_...

            if m_old:
                ts  = m_old.group(1)           # 202604272104
                dur = m_old.group(2)           # 23m39s
            elif m_new:
                ts  = m_new.group(1)
                dur = m_new.group(2)
            else:
                # 타임스탬프가 없으면 파일 mtime에서 생성
                mtime = os.path.getmtime(json_path)
                ts    = datetime.fromtimestamp(mtime).strftime("%Y%m%d%H%M")
                dur   = _dur_tag(float(meta.get('duration') or 0))

            # ── 새 파일명 확정 ─────────────────────────────────────────
            safe_title    = _safe_stem(title)
            new_stem_base = f"{ts}_{dur}_{safe_title}"

            if new_stem_base == old_stem:
                skipped += 1
                continue

            new_stem = _free_stem(date_path, new_stem_base)

            # ── 같은 stem을 가진 모든 파일 수집 ───────────────────────
            to_rename = []
            for fn in os.listdir(date_path):
                base, ext = os.path.splitext(fn)
                if base == old_stem:
                    to_rename.append((fn, new_stem + ext))

            print(f"\n  {date_dir}/  \"{title[:55]}\"")
            for old_name, new_name in sorted(to_rename):
                print(f"    {old_name}  →  {new_name}")
                if not dry:
                    try:
                        os.rename(
                            os.path.join(date_path, old_name),
                            os.path.join(date_path, new_name),
                        )
                    except Exception as e:
                        print(f"      ERROR: {e}")
                        errors += 1
            renamed += len(to_rename)

    print(f"\n{mode} 완료: {renamed}개 파일 변환, {skipped}개 건너뜀, {errors}개 오류")
    if dry and renamed:
        print("실제 변환하려면:  python rename_res.py --run")


if __name__ == '__main__':
    dry = '--run' not in sys.argv
    if not dry and '-y' not in sys.argv:
        ans = input("실제로 파일명을 변경합니다. 계속하시겠습니까? (y/N) ").strip().lower()
        if ans != 'y':
            print("취소됨.")
            sys.exit(0)
    main(dry)
