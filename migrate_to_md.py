"""
res/ 하위의 기존 .json + .txt 파일 쌍을 .md 파일 하나로 통합.

--dry  : 변경 목록만 출력 (기본값)
--run  : 실제 변환 실행
-y     : 확인 없이 바로 실행 (--run 과 함께)
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR  = os.path.join(BASE_DIR, "res")

_MD_META_ORDER = [
    "title", "uploader", "channel", "channel_url",
    "duration", "upload_date", "webpage_url", "id",
    "categories", "tags", "source_file",
]
_SKIP_KEYS = {"view_count", "like_count"}


def _save_md(md_path: str, meta: dict, transcript: str) -> None:
    lines = ["---"]
    written = set()
    for key in _MD_META_ORDER:
        val = meta.get(key)
        if val is None or val == "" or val == []:
            continue
        written.add(key)
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {val}")
    for key, val in meta.items():
        if key in written or key in _SKIP_KEYS or val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {val}")
    lines += ["---", ""]
    if meta.get("title"):
        lines += [f"# {meta['title']}", ""]
    lines.append(transcript)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(dry: bool) -> None:
    mode = "[DRY RUN]" if dry else "[MIGRATE]"
    converted = skipped = errors = 0

    for date_dir in sorted(os.listdir(RES_DIR)):
        date_path = os.path.join(RES_DIR, date_dir)
        if not os.path.isdir(date_path):
            continue

        for fname in sorted(os.listdir(date_path)):
            if not fname.endswith(".json"):
                continue

            stem      = fname[:-5]
            json_path = os.path.join(date_path, fname)
            txt_path  = os.path.join(date_path, stem + ".txt")
            md_path   = os.path.join(date_path, stem + ".md")

            # 이미 md가 있으면 건너뜀
            if os.path.exists(md_path):
                skipped += 1
                continue

            # JSON 읽기
            try:
                with open(json_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                print(f"  ERROR  {date_dir}/{fname}: {e}")
                errors += 1
                continue

            # view_count, like_count 제외
            meta = {k: v for k, v in meta.items() if k not in _SKIP_KEYS}

            # txt 읽기 (없으면 빈 문자열)
            transcript = ""
            if os.path.exists(txt_path):
                try:
                    with open(txt_path, encoding="utf-8", errors="replace") as f:
                        transcript = f.read().strip()
                except Exception as e:
                    print(f"  WARN   {date_dir}/{stem}.txt 읽기 실패: {e}")

            title = (meta.get("title") or stem)[:55]
            print(f"\n  {date_dir}/  \"{title}\"")
            print(f"    {fname} + {stem}.txt  →  {stem}.md")

            if not dry:
                try:
                    _save_md(md_path, meta, transcript)
                    os.remove(json_path)
                    if os.path.exists(txt_path):
                        os.remove(txt_path)
                except Exception as e:
                    print(f"      ERROR: {e}")
                    errors += 1
                    continue
            converted += 1

    print(f"\n{mode} 완료: {converted}개 변환, {skipped}개 건너뜀, {errors}개 오류")
    if dry and converted:
        print("실제 변환하려면:  python migrate_to_md.py --run")


if __name__ == "__main__":
    dry = "--run" not in sys.argv
    if not dry and "-y" not in sys.argv:
        ans = input("기존 .json/.txt 파일을 .md로 변환하고 삭제합니다. 계속하시겠습니까? (y/N) ").strip().lower()
        if ans != "y":
            print("취소됨.")
            sys.exit(0)
    main(dry)
