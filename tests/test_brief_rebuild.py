"""한눈 요약 재생성 — 소제목 하나당 불릿 하나(2026-11-02 사용자 지시)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brief_rebuild as B  # noqa: E402

DOC = """# 제목

## 1. 메타정보

| 항목 | 내용 |
| --- | --- |
| 제목 | X |

## 2. 한눈 요약

- 옛 불릿 하나
- 옛 불릿 둘

## 3. 핵심 내용

### 매출이 늘었다 [03:45]

<div class="kf-strip"><figure><img src="/sframe/a.jpg"></figure></div>

매출은 **40%** 늘었다. 용어\\*도 있다.

<div class="term-notes">
<p class="term-note">* <strong>용어</strong> — 설명</p>
</div>

### 비용이 문제다 [1:07:02]

비용은 두 배다.

## 4. 덧붙임

끝.
"""


def test_headings_and_prompt_strip_noise():
    assert B.headings(DOC) == ["매출이 늘었다", "비용이 문제다"]
    prompt, n = B.build_prompt(DOC)
    assert n == 2
    assert "정확히 2개" in prompt and "1. 매출이 늘었다\n2. 비용이 문제다" in prompt
    assert "kf-strip" not in prompt and "term-note" not in prompt
    assert "옛 불릿" not in prompt and "덧붙임" not in prompt     # 본문(핵심 내용)만 근거


def test_parse_requires_one_bullet_per_heading():
    assert B.parse("- **40%** 늘었다 [03:45]\n- 비용\\*은 두 배다.", 2) == ["**40%** 늘었다", "비용은 두 배다."]
    assert B.parse("머리말\n1. 하나\n2) 둘\n\n맺음말", 2) == ["하나", "둘"]
    assert B.parse("- 하나", 2) is None
    assert B.parse("- 하나\n- 둘\n- 셋", 2) is None


def test_replace_keeps_everything_else():
    out = B.replace(DOC, ["새 하나", "새 둘"])
    assert "## 2. 한눈 요약\n\n- 새 하나\n- 새 둘\n\n## 3. 핵심 내용" in out
    assert "옛 불릿" not in out
    assert out.split("## 3. 핵심 내용")[1] == DOC.split("## 3. 핵심 내용")[1]
    assert out.split("## 2. 한눈 요약")[0] == DOC.split("## 2. 한눈 요약")[0]


def test_unusual_structure_is_left_alone():
    no_heads = DOC.split("### 매출")[0]
    assert B.build_prompt(no_heads) is None
    assert B.build_prompt("# 제목\n\n본문만") is None
    assert B.replace("# 제목\n\n본문만", ["x"]) == "# 제목\n\n본문만"


def test_app_falls_back_to_first_brief(monkeypatch):
    import app
    calls = []

    def fake(prompt, label, model, effort, purpose="title"):
        calls.append((model, purpose))
        return "- 하나뿐", ""
    monkeypatch.setitem(app._TITLE_TR_PROVIDERS, "opus", fake)
    assert app._rebuild_brief(DOC, "opus", "m", "t") == DOC           # 불릿 수가 다르면 그대로
    assert calls == [("m", "brief")]

    monkeypatch.setitem(app._TITLE_TR_PROVIDERS, "opus",
                        lambda *a, **k: ("- **매출**이 40% 늘었다.\n- 비용은 두 배다.", ""))
    out = app._rebuild_brief(DOC, "opus", "m", "t")
    assert "- **매출**이 40% 늘었다.\n- 비용은 두 배다." in out and "옛 불릿" not in out

    monkeypatch.setitem(app._TITLE_TR_PROVIDERS, "opus", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert app._rebuild_brief(DOC, "opus", "m", "t") == DOC           # 예외도 저장을 막지 않는다
    monkeypatch.setenv("YT_BRIEF_REBUILD", "0")
    assert app._rebuild_brief(DOC, "opus", "m", "t") == DOC
