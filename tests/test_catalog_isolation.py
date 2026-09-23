"""중앙 LLM 카탈로그가 없거나 깨져도 요약·캡처 설정 경로가 멈추지 않는지(2026-09-23).

카탈로그는 부가 정보라, 모듈 import 실패가 서버·모니터를 죽이면 안 된다. 실제 환경을
건드리지 않도록 문법 오류가 있는 `llm_catalog.py`를 경로 맨 앞에 끼워 재현한다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROBE = r"""
import json, llm_gateway as g
slots = [{"model": "claude-opus-5-5", "effort": "medium"}, {"model": "gpt-6-sol", "effort": "medium"},
         {"model": "grok-4.7", "effort": "medium"}]
caps = ["claude-opus-5-5", "gpt-6-sol", "grok-4.7"]
legacy = {"opus": "claude-opus-5-5", "gpt": "gpt-6-astra", "grok": "grok"}
print(json.dumps({
    "fallback": getattr(g.llm_catalog, "AVAILABLE", True) is False,
    "slots": g.normalize_summary_slots(slots, [], preserve_unknown=True),
    "valid": g.is_valid_summary_slots(slots, existing=slots),
    "capture": g.normalize_capture_models(caps, legacy),
    "family": {m: g.model_family(m) for m in ["claude-opus-5-5", "gpt-6-luna", "grok-4.7"]},
    "grok_id": g.grok_call_model("grok-4.7"),
}))
"""


def _run_with_broken_catalog(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "llm_catalog.py").write_text("def broken(:\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=f"{broken}{os.pathsep}{REPO}", HERMES_LLM_LOG_DISABLED="1")
    return subprocess.run([sys.executable, "-c", PROBE], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def test_broken_catalog_module_falls_back_and_keeps_settings(tmp_path):
    proc = _run_with_broken_catalog(tmp_path)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "대체 모드" in proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["fallback"] is True
    assert [(s["model"], s["effort"]) for s in out["slots"]] == [
        ("claude-opus-5-5", "medium"), ("gpt-6-sol", "medium"), ("grok-4.7", "medium")]
    assert out["valid"] is True
    assert out["capture"] == ["claude-opus-5-5", "gpt-6-sol", "grok-4.7"]
    assert out["family"] == {"claude-opus-5-5": "opus", "gpt-6-luna": "gpt", "grok-4.7": "grok"}
    assert out["grok_id"] == "grok-4.7"


def test_fallback_module_is_permissive():
    sys.path.insert(0, str(REPO))
    import llm_catalog_fallback as fb
    assert "gpt-6-sol" in fb.Choices("codex")
    assert list(fb.Choices("codex")) == []
    assert fb.valid("anything", "ultra") is True
    assert fb.resolve("grok-4.7") is None
    assert [o["value"] for o in fb.options("grok", selected=["grok-4.7", "grok-4.7"])] == ["grok-4.7"]
