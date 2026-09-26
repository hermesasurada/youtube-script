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
