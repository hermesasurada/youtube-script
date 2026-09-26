"""회사·브랜드명 원문 표기(A안)와 영문 이름 뒤 조사 교정 — 2026-09-26 사용자 지시."""
import os

import app
import notation
import transcript_translator as tt


def test_common_korean_brand_spellings_become_original():
    assert notation.normalize("엔비디아가 발표했고 구글은 테슬라의 차를 샀다") == \
        "NVIDIA가 발표했고 Google은 Tesla의 차를 샀다"
    assert notation.normalize("오픈AI와의 계약, 마이크로소프트 제품") == "OpenAI와의 계약, Microsoft 제품"
    # 다른 낱말의 일부는 건드리지 않는다
    assert notation.normalize("애플리케이션, 구글링, 아마존강") == "애플리케이션, 구글링, 아마존강"
    # 직접 인용·태그 속성은 그대로
    assert notation.normalize('"엔비디아" <a title="구글">') == '"엔비디아" <a title="구글">'


def test_particles_follow_the_korean_final_sound():
    assert notation.normalize("Anthropic가 미쳤다") == "Anthropic이 미쳤다"
    assert notation.normalize("OpenAI·NVIDIA·Anthropic와 함께") == "OpenAI·NVIDIA·Anthropic과 함께"
    assert notation.normalize("Google가 Apple를 Intel으로") == "Google이 Apple을 Intel로"
    assert notation.normalize("Anthropic이사회") == "Anthropic이사회"


def test_summary_keeps_original_title_and_meta_table():
    text = "# 엔비디아 주가 전망\n| 업로더 | 엔비디아 코리아 |\n엔비디아가 발표했다"
    assert notation.normalize_summary(text) == \
        "# 엔비디아 주가 전망\n| 업로더 | 엔비디아 코리아 |\nNVIDIA가 발표했다"
    assert app._polish_summary("엔비디아가 발표했고, 구글은 따랐다") == "NVIDIA가 발표했고 Google은 따랐다"


def test_title_translation_normalizes_only_foreign_sources():
    assert app._preserve_title_terms("Anthropic went CRAZY", "Anthropic가 미쳤다") == "Anthropic이 미쳤다"
    # 원래 한국어 제목은 원문이라 손대지 않는다
    assert app._preserve_title_terms("엔비디아 실적 분석", "엔비디아 실적 분석") == "엔비디아 실적 분석"
    p = app._TITLE_TR_PROMPT
    assert "마크 저커버그 ×" in p and "엔비디아 ×" in p and "나사 ×" in p
    assert "Anthropic가 ×" in p


def test_three_prompts_share_original_brand_rule():
    root = os.path.dirname(app.__file__)
    for name in ("prompt.txt", "prompt_default.txt"):
        text = open(os.path.join(root, name), encoding="utf-8").read()
        assert "엔비디아·구글·테슬라·유튜브처럼 한글 표기가 흔한 이름도 원문으로 쓴다" in text
        assert "한글 표기가 더 일반적인 이름(유튜브, 엔비디아)" not in text
        assert "젠슨 황 ×" in text
        # 각주: 상한 없음, 풀네임이 나와도 해설, 별표 짝 맞춤, 음차 외래어 제목은 원어만
        assert "본문에 풀네임이나 역할이 나와도 해설한다" in text
        assert "별표 없는 각주, 각주 없는 별표를 만들지 않는다" in text
        assert "플라이휠, 모라토리엄, 임베딩" in text
    sp = tt.SYSTEM_PROMPT
    assert "엔비디아, 구글, 테슬라, 유튜브)도 원문으로" in sp
    assert "Neocloud" in sp and "데이터 비누지" in sp and "Groq" in sp


def test_translation_glossary_carries_names_across_chunks():
    parts = ["[00:00] David Friedberg와 Chamath Palihapitiya가 나왔다. David Friedberg는",
             "[05:00] Jason Calacanis가 진행한다"]
    g = tt.proper_noun_glossary(parts)
    assert g[0] == "David Friedberg" and "Chamath Palihapitiya" in g and "Jason Calacanis" in g
    assert tt.proper_noun_glossary(["UCESLZhusAkFfsNsApnjF1 채널"]) == []


def test_ambiguous_names_need_a_direct_particle():
    """Astra 검토(2026-09-26): 회사명 말고도 쓰이는 이름은 문맥이 분명할 때만 바꾼다."""
    for text in ("아마존 열대우림", "아마존의 열대우림", "애플 파이", "블록체인 오라클 문제", "3 테슬라 자석"):
        assert notation.normalize(text) == text
    assert notation.normalize("아마존이 인수했고 애플은 발표했다") == "Amazon이 인수했고 Apple은 발표했다"
    assert notation.normalize("구글 클라우드") == "Google 클라우드"          # 모호하지 않은 이름은 그대로 적용


def test_summary_protection_spans_lines_and_particles_cross_bold():
    text = '```\n엔비디아\n```\n"여러 줄\n엔비디아 인용"\n엔비디아가 발표'
    assert notation.normalize_summary(text) == '```\n엔비디아\n```\n"여러 줄\n엔비디아 인용"\nNVIDIA가 발표'
    assert notation.normalize("**Anthropic**가 발표") == "**Anthropic**이 발표"
    # 캡처 경로·링크 주소 안의 이름은 파일 경로라 바꾸지 않는다
    img = '![](res/summary/x/현대차_테슬라.frames/kf_1.jpg) 테슬라/엔비디아 비교'
    assert notation.normalize_summary(img) == '![](res/summary/x/현대차_테슬라.frames/kf_1.jpg) Tesla/NVIDIA 비교'
    assert notation.normalize("Starship가 떴다") == "Starship이 떴다"
    assert notation.normalize("**애플**이") == "**Apple**이"


def test_glossary_keeps_all_caps_and_lowercase_led_names():
    g = tt.proper_noun_glossary(["NVIDIA·NASA·AMD·IBM·xAI·OpenAI 그리고 GPU, CEO David Friedberg"])
    assert g == ["NVIDIA", "NASA", "AMD", "IBM", "xAI", "OpenAI", "David Friedberg"]


def test_footnote_title_rule_matches_viewer_normalization():
    """화면은 `한글 (English)` 각주 제목을 영어만 남긴다 — 프롬프트도 원어만 쓰게 한다."""
    root = os.path.dirname(app.__file__)
    text = open(os.path.join(root, "prompt.txt"), encoding="utf-8").read()
    assert "`비추력 (specific impulse)`" not in text
    assert "`flywheel`, `specific impulse`처럼 원어만" in text
    assert "이번 원문에 더 분명한 철자가 나오면 원문을 우선" in open(tt.__file__, encoding="utf-8").read()
