"""요약 슬롯별 모델 버전(모니터 모델 선택기) — 2026-09-23."""
import llm_gateway


def test_choices_include_new_models():
    assert "claude-opus-5-5" in llm_gateway.MODEL_VERSION_CHOICES["opus"]
    assert {"gpt-6-sol", "gpt-6-luna"} <= set(llm_gateway.MODEL_VERSION_CHOICES["gpt"])


def test_slots_drop_unknown_models_and_cap_at_five():
    d = [{"model": "opus", "effort": "default"}]
    out = llm_gateway.normalize_summary_slots(
        [{"model": "claude-opus-5-5", "effort": "HIGH"}, {"model": "gpt-9"}], d)
    assert out == [{"model": "claude-opus-5-5", "effort": "high"}]
    assert llm_gateway.normalize_summary_slots([], d) == d
    assert len(llm_gateway.normalize_summary_slots([{"model": "grok"}] * 9, d)) == 5


def test_summary_attempts_follow_slots():
    """요약 실행이 슬롯의 모델·추론을 그대로 쓴다(같은 계열 두 슬롯도 각각)."""
    import app
    att = app._summary_attempts(slots=[{"model": "gpt-6-sol", "effort": "low"},
                                       {"model": "gpt-6-luna", "effort": "high"},
                                       {"model": "claude-opus-5-5", "effort": "default"}])
    assert [(a["family"], a["model"], a["effort"]) for a in att] == [
        ("gpt", "gpt-6-sol", "low"), ("gpt", "gpt-6-luna", "high"),
        ("opus", "claude-opus-5-5", "default")]


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


def test_catalog_changes_validation_and_preserves_legacy(monkeypatch, tmp_path):
    c = llm_gateway.llm_catalog
    monkeypatch.setenv('HERMES_LLM_CATALOG', str(tmp_path / 'catalog.json'))
    data = c.seed()
    data['models'].append(c.entry('gpt-text-only', 'codex', levels=['default', 'low']))
    c.save(data, 0)
    assert llm_gateway.is_valid_summary_slots([{'model': 'gpt-text-only', 'effort': 'low'}])
    assert not llm_gateway.is_valid_summary_slots([{'model': 'gpt-text-only', 'effort': 'high'}])
    assert not llm_gateway.is_valid_capture_models(['gpt-text-only', 'none', 'none'])
    legacy = [{'model': 'retired-model', 'effort': 'high'}]
    assert llm_gateway.normalize_summary_slots(legacy, [], preserve_unknown=True) == legacy
    assert llm_gateway.is_valid_summary_slots(legacy, existing=legacy)
    assert not llm_gateway.is_valid_summary_slots(legacy)
