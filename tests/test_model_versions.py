"""요약 슬롯별 모델 버전(모니터 모델 선택기) — 2026-09-23."""
import llm_gateway


DEFAULTS = {"opus": "opus", "gpt": "gpt-6-astra"}


def test_choices_include_new_models():
    assert "claude-opus-5-5" in llm_gateway.MODEL_VERSION_CHOICES["opus"]
    assert {"gpt-6-sol", "gpt-6-luna"} <= set(llm_gateway.MODEL_VERSION_CHOICES["gpt"])


def test_normalize_keeps_valid_and_restores_invalid():
    out = llm_gateway.normalize_model_versions(
        {"opus": "claude-opus-5-5", "gpt": "gpt-9-fake"}, DEFAULTS)
    assert out == {"opus": "claude-opus-5-5", "gpt": "gpt-6-astra"}


def test_normalize_empty_uses_defaults():
    assert llm_gateway.normalize_model_versions(None, DEFAULTS) == DEFAULTS


def test_patch_rejects_unknown_version():
    import app
    client = app.app.test_client()
    r = client.patch("/channels/model-orders", json={"model_versions": {"gpt": "gpt-9-fake"}})
    assert r.status_code == 400


def test_shared_selector_files_match_website_monitor():
    """선택기 JS·CSS는 wm과 같은 파일이다 — 한쪽만 고치면 디자인이 갈라진다."""
    import os
    here = os.path.dirname(os.path.dirname(__file__))
    wm = os.path.expanduser("~/projects/website-monitor/static")
    pairs = (("static/js/model-selector.js", "model-selector.js"),
             ("static/css/model-selector.css", "model-selector.css"))
    for mine, theirs in pairs:
        other = os.path.join(wm, theirs)
        if not os.path.exists(other):
            continue                      # wm이 없는 환경에서는 건너뛴다
        with open(os.path.join(here, mine), encoding="utf-8") as a, open(other, encoding="utf-8") as b:
            assert a.read() == b.read(), f"{mine}가 wm 사본과 다르다"
