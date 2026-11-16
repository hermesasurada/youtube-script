"""llm_runners — 계열별 실행기와 Claude stream-json 파싱(2026-11-16 리팩토링 1단계)."""
import json
from contextlib import nullcontext

import llm_gateway
import llm_runners as R
import yt_models
import yt_titles


def _no_log(monkeypatch):
    monkeypatch.setattr(llm_gateway, "llm_track", lambda *a, **kw: nullcontext(None))
    monkeypatch.setattr(llm_gateway, "llm_begin", lambda *a, **kw: None)


def _events(*lines, rc=0):
    out = [llm_gateway.StreamEvent("stdout", data=json.dumps(x) + "\n") for x in lines]
    out.append(llm_gateway.StreamEvent("complete", returncode=rc, stderr=""))
    return out


def _drain(gen):
    seen = []
    try:
        while True:
            seen.append(next(gen))
    except StopIteration as stop:
        return seen, stop.value


def test_stream_claude_yields_model_then_text_and_returns_result(monkeypatch):
    _no_log(monkeypatch)
    captured = {}

    def stream_command(cmd, **kw):
        captured["cmd"], captured["input"] = cmd, kw["input_text"]
        return iter(_events(
            {"type": "system", "model": "claude-opus-5-5"},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": "가"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": "나"}}},
            {"type": "result", "is_error": False, "result": "가나다"},
        ))
    monkeypatch.setattr(llm_gateway, "stream_command", stream_command)
    seen, out = _drain(R.stream_claude("P", claude_bin="claude", model="opus", effort="high",
                                       timeout=5, system_args=["--x"]))
    assert seen == [("model", "claude-opus-5-5"), ("text", "가"), ("text", "나")]
    assert (out.text, out.error, out.used_model, out.chunks) == ("가나다", None, "claude-opus-5-5", ["가", "나"])
    assert captured["input"] == "P" and captured["cmd"][-2:] == ["--effort", "high"] and "--x" in captured["cmd"]


def test_stream_claude_error_result_and_no_effort_flag_by_default(monkeypatch):
    _no_log(monkeypatch)
    cmds = []
    monkeypatch.setattr(llm_gateway, "stream_command", lambda cmd, **kw: cmds.append(cmd) or iter(_events(
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "부분"}}},
        {"type": "result", "is_error": True, "result": "usage limit"}, rc=1)))
    _, out = _drain(R.stream_claude("P", claude_bin="claude", model="opus", timeout=5))
    assert out.error == "usage limit" and out.text == "부분"
    assert "--effort" not in cmds[0]


def test_stream_claude_logs_even_when_consumer_closes(monkeypatch):
    finished = []
    monkeypatch.setattr(llm_gateway, "llm_begin", lambda *a, **kw: object())
    monkeypatch.setattr(R, "finish_claude_stream_log", lambda *a: finished.append(a))
    monkeypatch.setattr(llm_gateway, "stream_command", lambda cmd, **kw: iter(_events(
        {"type": "system", "model": "m"},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "x"}}})))
    gen = R.stream_claude("P", claude_bin="claude", model="opus", timeout=5)
    assert next(gen) == ("model", "m")
    gen.close()                                   # 클라이언트 중단
    assert len(finished) == 1 and finished[0][1] is None     # result 이벤트 없이 끝남 → 이력에 '중단됨'


def test_run_text_postprocess_empty_is_failure_and_raw_mode_is_not(monkeypatch):
    _no_log(monkeypatch)
    monkeypatch.setattr(llm_gateway, "run_codex_prompt",
                        lambda p, **kw: llm_gateway.ProcessResult(0, "  ", ""))
    assert R.run_text("gpt", "p", model="gpt-x", purpose="summary", title=None, timeout=5,
                      postprocess=str.strip) == ("", "gpt 빈 응답")
    assert R.run_text("gpt", "p", model="gpt-x", purpose="title", title=None, timeout=5) == ("  ", "")
    assert R.run_text("nope", "p", model="x", purpose="title", title=None, timeout=5)[1].startswith("지원하지 않는")


def test_run_text_grok_timeout_and_missing_binary(monkeypatch, tmp_path):
    _no_log(monkeypatch)
    assert R.run_text("grok", "p", model="", purpose="title", title=None, timeout=5,
                      grok_bin=str(tmp_path / "none")) == ("", "grok 실행파일 없음")
    monkeypatch.setattr(llm_gateway, "run_command",
                        lambda cmd, **kw: llm_gateway.ProcessResult(-1, "", "", timed_out=True))
    body, err = R.run_text("grok", "p", model="", purpose="title", title=None, timeout=7, grok_bin="/bin/echo")
    assert body == "" and err == "grok 타임아웃(7s)"


def test_run_vision_claude_reads_refs_with_given_env_and_gpt_attaches_images(monkeypatch):
    _no_log(monkeypatch)
    seen = {}

    def run_command(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return llm_gateway.ProcessResult(0, json.dumps({"type": "result", "result": "[1]"}), "")
    monkeypatch.setattr(llm_gateway, "run_command", run_command)
    out, err = R.run_vision("claude", "P @/tmp/a.jpg", model="claude-sonnet-5-5", images=["/tmp/a.jpg"], title="t",
                            timeout=9, finish=lambda call, r: ("판정", r.stdout), claude_bin=lambda: "claude",
                            claude_env=lambda: {"X": "1"})
    assert (out, err) == (("판정", "[1]\n"), "")                      # 봉투를 벗긴 글을 finish에 넘긴다
    assert seen["cmd"] == ["claude", "-p", "--model", "claude-sonnet-5-5", "--output-format", "json", "P @/tmp/a.jpg"]
    assert seen["kw"] == {"timeout": 9, "env": {"X": "1"}}             # 추론 수준·최소 구성 인자는 붙이지 않는다

    def run_codex_prompt(prompt, **kw):
        seen["codex"] = kw
        return llm_gateway.ProcessResult(0, "[2]", "")
    monkeypatch.setattr(llm_gateway, "run_codex_prompt", run_codex_prompt)
    out, err = R.run_vision("gpt", "P", model="gpt-x", images=["/tmp/a.jpg"], title=None, timeout=9,
                            finish=lambda call, r: r.stdout, claude_bin=lambda: "", claude_env=dict)
    assert (out, err) == ("[2]", "") and seen["codex"] == {"model": "gpt-x", "timeout": 9, "images": ["/tmp/a.jpg"]}


def test_run_vision_launch_failures_return_error_without_calling_finish(monkeypatch, tmp_path):
    _no_log(monkeypatch)
    called = []
    finish = lambda call, r: called.append(r) or "x"                    # noqa: E731
    common = dict(model="grok-4.7", images=[], title=None, timeout=5, finish=finish, claude_bin=lambda: "", claude_env=dict)
    monkeypatch.setattr(llm_gateway, "resolve_grok_bin", lambda: str(tmp_path / "none"))
    assert R.run_vision("grok", "P", **common) == (None, "grok 실행파일 없음")
    monkeypatch.setattr(llm_gateway, "run_codex_prompt", lambda p, **kw: (_ for _ in ()).throw(RuntimeError("codex 없음")))
    assert R.run_vision("gpt", "P", **common) == (None, "gpt 실행 오류: codex 없음")
    assert R.run_vision("gemini", "P", **common)[1].startswith("지원하지 않는")
    assert called == []


def test_title_runner_drops_unknown_grok_effort(monkeypatch):
    import app
    seen = {}
    monkeypatch.setattr(yt_models, "_llm_text", lambda family, prompt, **kw: seen.update(kw, family=family) or ("ok", ""))
    monkeypatch.setattr(llm_gateway.llm_catalog, "levels", lambda model: ["low", "high"])
    assert yt_titles._TITLE_TR_PROVIDERS["grok"]("p", "label", "grok-4.7", "xhigh") == ("ok", "")
    assert seen["effort"] == "default" and seen["purpose"] == "title" and seen["title"] == "label"
    yt_titles._TITLE_TR_PROVIDERS["gpt"]("p", "label", None, "xhigh", purpose="brief")
    assert seen["family"] == "gpt" and seen["effort"] == "xhigh" and seen["purpose"] == "brief"
