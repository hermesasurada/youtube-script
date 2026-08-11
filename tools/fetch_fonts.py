#!/usr/bin/env python3
"""웹폰트를 로컬로 내려받아 self-host 한다.

Google Fonts / jsDelivr(Pretendard) 로 나가던 외부 요청을 없애기 위한 스크립트.
받아둔 결과물(static/fonts/webfonts/)은 gitignore 대상이라, 새로 받아야 할 때
이 스크립트를 다시 돌리면 된다.

    python3 tools/fetch_fonts.py

한글 폰트는 unicode-range 서브셋으로 잘게 쪼개져 있는데, 그 구조를 그대로 보존한다.
브라우저는 로컬에서도 실제 쓰인 글자에 해당하는 서브셋만 내려받는다.
"""
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(ROOT, "static", "fonts")
WEB_DIR = os.path.join(FONT_DIR, "webfonts")

# woff2 를 받으려면 최신 브라우저 UA 가 필요하다(구형 UA 로는 ttf 를 준다).
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

GOOGLE_CSS = (
    "https://fonts.googleapis.com/css2"
    "?family=Inter:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@400;500"
    "&family=Noto+Sans+KR:wght@400;500;600;700"
    "&family=Noto+Serif+KR:wght@400;600;700"
    "&family=Gowun+Batang:wght@400;700"
    "&family=Nanum+Myeongjo:wght@400;700"
    "&family=Song+Myung"
    "&display=swap"
)
PRETENDARD_BASE = ("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9"
                   "/dist/web/variable/")
PRETENDARD_CSS = PRETENDARD_BASE + "pretendardvariable.min.css"


def fetch(url: str, binary: bool = False, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return data if binary else data.decode("utf-8")
        except Exception as e:          # noqa: BLE001 - 네트워크 오류는 그냥 재시도
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"{url} 실패: {last}")


def localize(css: str, url_prefix: str, name_of) -> tuple[str, dict]:
    """CSS 안의 원격 url(...) 을 로컬 경로로 바꾸고, 받아야 할 목록을 돌려준다."""
    jobs: dict[str, str] = {}          # 원격 URL -> 로컬 파일명

    def sub(m):
        remote = m.group(1).strip("'\"")
        fname = name_of(remote)
        jobs[remote] = fname
        return f"url({url_prefix}{fname})"

    out = re.sub(r"url\(([^)]+\.woff2[^)]*)\)", sub, css)
    return out, jobs


def download_all(jobs: dict, label: str):
    os.makedirs(WEB_DIR, exist_ok=True)
    todo = [(u, f) for u, f in jobs.items()
            if not os.path.exists(os.path.join(WEB_DIR, f))]
    print(f"  {label}: 총 {len(jobs)}개 중 {len(todo)}개 신규")
    if not todo:
        return
    done = [0]

    def one(pair):
        url, fname = pair
        dest = os.path.join(WEB_DIR, fname)
        data = fetch(url, binary=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"    {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(one, todo))
    print(f"    {done[0]}/{len(todo)} 완료")


def main() -> int:
    os.makedirs(WEB_DIR, exist_ok=True)

    print("Google Fonts")
    gcss = fetch(GOOGLE_CSS)
    # gstatic URL 은 .../s/notosanskr/v36/xxxx.woff2 형태라 두 단계를 이름에 담는다.
    def gname(url: str) -> str:
        parts = [p for p in url.split("/") if p]
        fam = parts[-3] if len(parts) >= 3 else "font"
        return f"{fam}-{parts[-1]}"
    gcss, gjobs = localize(gcss, "./webfonts/", gname)
    download_all(gjobs, "google")
    with open(os.path.join(FONT_DIR, "google.css"), "w", encoding="utf-8") as f:
        f.write("/* 자동 생성: tools/fetch_fonts.py — 직접 수정하지 말 것 */\n")
        f.write(gcss)

    print("Pretendard")
    pcss = fetch(PRETENDARD_CSS)
    pjobs_src = {}

    def pname(rel: str) -> str:
        # CSS 안의 경로가 '../../../packages/...' 형태라 urljoin 으로 풀어야 한다.
        abs_url = urllib.parse.urljoin(PRETENDARD_CSS, rel.split("?")[0])
        fname = os.path.basename(abs_url)
        pjobs_src[abs_url] = fname
        return fname

    pcss, _ = localize(pcss, "./webfonts/", pname)
    download_all(pjobs_src, "pretendard")
    with open(os.path.join(FONT_DIR, "pretendard.css"), "w", encoding="utf-8") as f:
        f.write("/* 자동 생성: tools/fetch_fonts.py — 직접 수정하지 말 것 */\n")
        f.write(pcss)

    total = sum(os.path.getsize(os.path.join(WEB_DIR, f))
                for f in os.listdir(WEB_DIR) if not f.endswith(".part"))
    n = len([f for f in os.listdir(WEB_DIR) if not f.endswith(".part")])
    print(f"\n완료: {n}개 파일, {total / 1024 / 1024:.1f}MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
