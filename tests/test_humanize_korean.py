"""요약 저장 직전 im-not-ai 결정적 윤문."""
import humanize_korean as hk
import app


def test_c11_drops_comma_after_connective_but_keeps_geurigo_hajiman():
    src = "보이게 했고, 식약처는 단속했다. 그리고, 시장은 커졌다. 하지만, 근거는 없다. 일하지만, 쉬어야 한다."
    out = hk.humanize_summary(src)
    assert "했고 식약처" in out
    assert "그리고, 시장" in out
    assert "하지만, 근거" in out
    assert "일하지만 쉬어야" in out


def test_i3_geotida_endings():
    src = (
        "시점이 지금이라는 것이다. 쓸모가 생긴다는 것이다. "
        '남은 질문은 "어떻게"라는 것이다. 인사 시스템이라는 것이다.'
    )
    out = hk.humanize_summary(src)
    assert "지금이다." in out
    assert "생긴다." in out
    assert '"어떻게"다.' in out
    assert "인사 시스템이다." in out
    assert "것이다" not in out


def test_i3_geotida_split_by_bold():
    src = "사람을 더 뽑게 된다**는 것이다. 에이전트를 만든다."
    out = hk.humanize_summary(src)
    assert "뽑게 된다**." in out
    assert "것이다" not in out


def test_a16_gageotigeot_and_d4_jeonrye():
    out = hk.humanize_summary("접근 제어가 그것이다. 전례 없던 능력이다.")
    assert out == "접근 제어다. 이전에는 없던 능력이다."


def test_quotes_and_keyframes_are_untouched():
    src = (
        '그는 "도달했다는 것이다"고 했다.\n\n'
        '<div class="kf-strip"><figure><img src="/sframe/x.jpg"></figure></div>\n\n'
        "단속은 극소수였고, 업체는 빠져나갔다."
    )
    out = hk.humanize_summary(src)
    assert '"도달했다는 것이다"' in out
    assert '<div class="kf-strip">' in out
    assert "극소수였고 업체" in out


def test_numbers_and_bullets_survive():
    src = "- 판매가 1,200종이었고, 한 병은 9,900원이다.\n- 영상은 6조 원이라고 본다."
    out = hk.humanize_summary(src)
    assert "1,200종" in out
    assert "9,900원" in out
    assert out.startswith("- ")
    assert "영상은 6조 원이라고 본다." in out
    assert "이었고 한 병" in out


def test_english_only_is_noop():
    src = "# Title\n\nHello, world, and so on."
    assert hk.humanize_summary(src) == src


def test_prepare_summary_body_runs_humanize(monkeypatch):
    monkeypatch.delenv("HUMANIZE_SUMMARY", raising=False)
    out = app._prepare_summary_body("메타.\n# 제목\n\n했고, 끝이라는 것이다.")
    assert out.startswith("# 제목")
    assert "했고 끝이다." in out


def test_humanize_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HUMANIZE_SUMMARY", "0")
    src = "했고, 끝이라는 것이다."
    assert hk.humanize_summary(src) == src
