"""최소 회귀 테스트 — 핵심 동작 고정(전체 커버리지 목표 아님).

대상: 마크다운 왕복/파서 일치, 경로 이탈 차단, DB upsert·검색·삭제,
키프레임 타임스탬프→섹션 배정, 원격 접근 게이팅.

실행:  .venv/bin/python -m pytest -q
주의:  conftest.py가 DB_PATH를 임시 파일로 격리한다(실제 index.db·res/ 미사용).
"""
import os

import app
import db
import keyframe_report


# ── ① 마크다운 I/O 왕복 + app·db 파서 일치(드리프트 가드) ──────────────
def test_markdown_save_parse_roundtrip(tmp_path):
    meta = {
        "title": "테스트 영상 제목", "uploader": "채널A", "duration": "123",
        "webpage_url": "https://youtu.be/abc", "categories": ["News", "Tech"],
        "tags": ["alpha", "beta"],
    }
    md = str(tmp_path / "x.md")
    app._save_md(md, meta, "전사 본문\n둘째 줄")

    parsed, body = app._parse_md(md)
    assert body == "전사 본문\n둘째 줄"
    assert parsed["title"] == "테스트 영상 제목"
    assert parsed["uploader"] == "채널A"
    assert parsed["categories"] == ["News", "Tech"]
    assert parsed["tags"] == ["alpha", "beta"]


def test_app_and_db_frontmatter_parsers_agree(tmp_path):
    """app._parse_yaml_front_matter 와 db._parse_yaml_front_matter 가 같은 결과여야(중복 구현 드리프트 방지)."""
    md = str(tmp_path / "y.md")
    app._save_md(md, {"title": "T", "uploader": "U", "tags": ["x", "y"]}, "본문")
    fm = open(md, encoding="utf-8").read().split("---\n", 2)[1]
    assert app._parse_yaml_front_matter(fm) == db._parse_yaml_front_matter(fm)


# ── ② 경로 이탈 차단 ───────────────────────────────────────────────────
def test_path_traversal_blocked():
    ok, err = app._check_res_path(os.path.join(app.RES_DIR, "20260101", "x.md"))
    assert err is None and ok.startswith(os.path.realpath(app.RES_DIR))

    for bad in ("/etc/passwd", os.path.join(app.RES_DIR, "..", "..", "etc", "passwd")):
        _, e = app._check_res_path(bad)
        assert e is not None, f"이탈 경로가 허용됨: {bad}"


# ── ③ DB upsert·검색·삭제 (임시 RES_DIR/DB로 격리) ─────────────────────
def test_db_upsert_search_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "RES_DIR", str(tmp_path))   # 실제 res/ 미사용
    db.init()
    date_dir = tmp_path / "20260101"
    date_dir.mkdir()
    md = str(date_dir / "202601011200_0m10s_pyt.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("---\ntitle: 파이테스트제목 QWERTY\nuploader: 채널\nduration: 10\n"
                "---\n\n# 파이테스트제목 QWERTY\n\n전사 본문\n")

    assert db.upsert(md) is True
    assert any(it["title"] == "파이테스트제목 QWERTY" for it in db.list_items())
    assert any(it["title"] == "파이테스트제목 QWERTY" for it in db.search("QWERTY"))

    db.delete(os.path.realpath(md))
    assert all(it["title"] != "파이테스트제목 QWERTY" for it in db.list_items())


# ── ④ 키프레임 타임스탬프 → 섹션 배정 (Tier3) ─────────────────────────
def test_keyframe_section_by_timestamp():
    headings = [{"idx": 0, "start": 0}, {"idx": 1, "start": 60}, {"idx": 2, "start": 120}]
    kept = [{"ts": 10, "section": None}, {"ts": 75, "section": None}, {"ts": 200, "section": None}]
    keyframe_report._assign_sections_by_time(kept, headings)
    assert [k["section"] for k in kept] == [0, 1, 2]


def test_keyframe_ts_label_and_hms():
    assert keyframe_report._parse_ts_label("제목 [3:20]") == 200
    assert keyframe_report._parse_ts_label("[1:02:03]") == 3723
    assert keyframe_report._parse_ts_label("시각 없음") is None
    assert keyframe_report._hms(200) == "03:20"      # 분은 2자리 패딩
    assert keyframe_report._hms(3723) == "1:02:03"
    # 라벨↔시각 왕복 일관성
    assert keyframe_report._parse_ts_label("[" + keyframe_report._hms(200) + "]") == 200


# ── ⑤ 원격 접근 게이팅(읽음·삭제는 허용, 전사·키프레임·프롬프트는 차단) ──
def test_remote_access_gating():
    c = app.app.test_client()
    remote = {"REMOTE_ADDR": "100.64.0.1"}

    # 차단 → 이력 페이지로 302 리다이렉트
    assert c.get("/keyframes/status", environ_base=remote).status_code == 302
    assert c.post("/start", environ_base=remote).status_code == 302

    # 허용(이력 조회) → 리다이렉트 아님
    assert c.get("/history", environ_base=remote).status_code != 302

    # 원격 삭제는 '유지'로 결정됨 → 게이팅 통과(잘못된 본문이라 핸들러가 안전하게 거부, 302 아님)
    r = c.delete("/history/item", json={"txt_path": "/etc/passwd"}, environ_base=remote)
    assert r.status_code != 302

    # 로컬(루프백)은 전부 허용
    assert c.get("/keyframes/status").status_code == 200
