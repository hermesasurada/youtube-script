"""인명 확인(판별·웹 검색) 모델 설정 — 2026-10-08 사용자 지시.

설정 팝업의 '인명 확인' 블록에서 고른 값이 monitor_settings(DB v25)에 저장되고,
name_fix가 매번 읽어 name_resolver.process_docs에 넘긴다. Claude·Codex만 허용.
"""
import sqlite3

import app
import db
import name_fix


def _reset_names():
    for kind in db.NAME_MODEL_KINDS:
        db.set_name_model(kind, "", "")


def test_migration_v25_adds_name_model_columns(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db._local.c = None
    try:
        db.init()
        db.set_title_model("gpt-6-luna", "low")
        # v24 상태로 되돌린 DB(새 칸 없음)에서 다시 init → 칸이 생기고 기존 값은 그대로
        with db._lock:
            conn = db._conn()
            for col in db.NAME_MODEL_COLUMNS:
                conn.execute(f"ALTER TABLE monitor_settings DROP COLUMN {col}")
            conn.execute("PRAGMA user_version = 24")
        assert db.get_name_models() == {"detect": {"model": "", "effort": ""},
                                        "lookup": {"model": "", "effort": ""}}   # 칸이 없어도 예외 없음
        db._local.c = None
        db.init()
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 25
        cols = {r[1] for r in conn.execute("PRAGMA table_info(monitor_settings)")}
        conn.close()
        assert set(db.NAME_MODEL_COLUMNS) <= cols
        assert db.get_title_model() == {"model": "gpt-6-luna", "effort": "low"}
        assert db.get_name_models()["detect"] == {"model": "", "effort": ""}
    finally:
        db._local.c = None


def test_name_model_getters_setters_roundtrip():
    db.init()
    try:
        db.set_name_model("detect", "gpt-6-sol", "low")
        db.set_name_model("lookup", "claude-opus-5-5", "high")
        assert db.get_name_models() == {"detect": {"model": "gpt-6-sol", "effort": "low"},
                                        "lookup": {"model": "claude-opus-5-5", "effort": "high"}}
        db.set_name_model("detect", "", "high")          # 빈 모델 = 기본값, 추론도 비운다
        assert db.get_name_models()["detect"] == {"model": "", "effort": ""}
        assert db.get_name_models()["lookup"]["model"] == "claude-opus-5-5"
        try:
            db.set_name_model("bogus", "gpt-6-sol", "low")
            raise AssertionError("모르는 종류는 거부해야 한다")
        except ValueError:
            pass
    finally:
        _reset_names()


def test_patch_name_models_validation(monkeypatch, tmp_path):
    catalog = app.llm_gateway.llm_catalog
    monkeypatch.setenv('HERMES_LLM_CATALOG', str(tmp_path / 'catalog.json'))
    catalog.save(catalog.seed(), 0)
    db.init()
    client = app.app.test_client()
    try:
        _reset_names()
        payload = client.get("/channels").get_json()
        for key in ("name_detect_model", "name_detect_effort", "name_lookup_model", "name_lookup_effort"):
            assert payload[key] == ""
        assert {o["provider"] for o in payload["name_model_options"]} <= {"claude", "codex"}
        assert payload["name_model_default"]["label"]
        before = db.get_monitor_summary_slots()["slots"]
        title = db.get_title_model()
        # 허용: Claude·Codex
        r = client.patch("/channels/model-orders",
                         json={"name_detect_model": "gpt-6-sol", "name_detect_effort": "low",
                               "name_lookup_model": "claude-opus-5-5", "name_lookup_effort": "default"})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert (d["name_detect_model"], d["name_detect_effort"]) == ("gpt-6-sol", "low")
        assert (d["name_lookup_model"], d["name_lookup_effort"]) == ("claude-opus-5-5", "default")
        assert db.get_monitor_summary_slots()["slots"] == before and db.get_title_model() == title
        # 거부: Grok·로컬(웹 검색 도구 없음), 모르는 모델, 모델이 못 받는 추론
        for bad in ({"name_detect_model": "grok"},
                    {"name_lookup_model": "grok", "name_lookup_effort": "default"},
                    {"name_lookup_model": "qwen3.8-27b"},
                    {"name_detect_model": "gpt-9"},
                    {"name_detect_model": "gpt-6-sol", "name_detect_effort": "extreme"}):
            assert client.patch("/channels/model-orders", json=bad).status_code == 400, bad
        assert db.get_name_models()["detect"] == {"model": "gpt-6-sol", "effort": "low"}
        # 저장된 값을 그대로 돌려보내면 목록에서 빠졌어도 받는다
        db.set_name_model("lookup", "claude-retired-1", "default")
        r = client.patch("/channels/model-orders",
                         json={"summary_slots": before, "name_lookup_model": "claude-retired-1",
                               "name_lookup_effort": "default"})
        assert r.status_code == 200 and r.get_json()["name_lookup_model"] == "claude-retired-1"
        # 빈 모델 = 기본값으로 되돌리기
        r = client.patch("/channels/model-orders", json={"name_detect_model": "", "name_detect_effort": "low"})
        assert r.status_code == 200
        assert db.get_name_models()["detect"] == {"model": "", "effort": ""}
    finally:
        _reset_names()


class _FakeResolver:
    def __init__(self):
        self.calls = []

    def process_docs(self, service, docs, **kw):
        self.calls.append(kw)
        return {}

    def replace_names(self, text, mapping):
        return text, 0


def _summary(tmp_path):
    p = tmp_path / "20261008" / "x_summary.md"
    p.parent.mkdir()
    p.write_text("# 제목\n\n## 2. 본문\n제이컵 컥슨이 말했다.\n", encoding="utf-8")
    return str(p)


def test_name_fix_passes_saved_choices(monkeypatch, tmp_path):
    fake = _FakeResolver()
    monkeypatch.setattr(name_fix, "name_resolver", fake)
    monkeypatch.setattr(name_fix, "ENABLED", True)
    path = _summary(tmp_path)
    db.init()
    try:
        _reset_names()
        name_fix.process(path, dry_run=True)
        assert fake.calls[-1]["detect_choice"] is None and fake.calls[-1]["lookup_choice"] is None
        db.set_name_model("detect", "gpt-6-sol", "low")
        db.set_name_model("lookup", "claude-opus-5-5", "high")
        name_fix.process(path, dry_run=True)                 # 재시작 없이 다음 실행에 반영
        assert fake.calls[-1]["detect_choice"] == {"model": "gpt-6-sol", "effort": "low"}
        assert fake.calls[-1]["lookup_choice"] == {"model": "claude-opus-5-5", "effort": "high"}
        # 설정을 못 읽어도 인명 교정은 기본값으로 계속 돈다
        def boom():
            raise sqlite3.OperationalError("db locked")
        monkeypatch.setattr(db, "get_name_models", boom)
        name_fix.process(path, dry_run=True)
        assert len(fake.calls) == 3
        assert fake.calls[-1]["detect_choice"] is None and fake.calls[-1]["lookup_choice"] is None
    finally:
        monkeypatch.undo()
        _reset_names()
