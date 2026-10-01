"""각주 표식(\\*)과 각주 행의 짝 맞추기 — 2026-09-28 사용자 지시."""
import term_marks as tm

NOTE = '<div class="term-notes">\n<p class="term-note">* <strong>{}</strong> — 설명</p>\n</div>'


def _doc(body: str, brief: str = "- 요지") -> str:
    return f"# 제목\n\n## 2. 한눈 요약\n\n{brief}\n\n## 3. 핵심 내용\n\n### 섹션 [00:00]\n\n{body}\n"


def test_missing_mark_is_added_after_first_occurrence_and_paren():
    md = _doc("본문에 ARC-AGI(추론 벤치마크)를 쓴다. 다시 ARC-AGI.\n\n" + NOTE.format("ARC-AGI (Abstraction and Reasoning Corpus)"))
    out, st = tm.fix(md)
    assert "ARC-AGI(추론 벤치마크)\\*를" in out and out.count("\\*") == 1 and st["added"] == 1


def test_orphan_mark_is_removed_but_paired_marks_stay():
    md = _doc("하이퍼바이저\\*와 SFT\\*를 쓴다.\n\n" + NOTE.format("SFT (Supervised Fine-Tuning)"))
    out, st = tm.fix(md)
    assert "하이퍼바이저와" in out and "SFT\\*를" in out and st["removed"] == 1


def test_korean_body_with_english_note_pairs_by_order():
    """본문은 한글 '리소그래피', 각주는 영문 'lithography' — 같은 단위에서 수가 같으면 짝이다."""
    md = _doc("EUV 리소그래피\\*가 핵심이다.\n\n" + NOTE.format("lithography"))
    out, st = tm.fix(md)
    assert out == md and st["removed"] == 0


def test_heading_and_brief_marks_are_removed_and_body_gets_the_mark():
    md = _doc("해자가 넓다.\n\n" + NOTE.format("해자"), brief="- 해자\\*가 핵심")
    md = md.replace("### 섹션 [00:00]", "### 해자\\* 이야기 [00:00]")
    out, st = tm.fix(md)
    assert "### 해자 이야기" in out and "- 해자가 핵심" in out and "해자\\*가 넓다" in out


def test_spacing_variant_and_unit_prefix_are_found():
    md = _doc("하이퍼 스케일러가 150bp 올렸다.\n\n" + NOTE.format("하이퍼스케일러") + "\n" + NOTE.format("bp (Basis Point)"))
    out, _ = tm.fix(md)
    assert "하이퍼 스케일러\\*가" in out and "150bp\\* 올렸다" in out


def test_name_star_and_bold_close_are_preserved():
    md = _doc("궁수자리 A\\* 주변과 **무기한 선물\\*\\***을 본다.")
    out, _ = tm.fix(md)
    assert "궁수자리 A\\*" in out and "**무기한 선물**을" in out


def test_note_without_body_term_is_left_alone():
    md = _doc("다른 표기만 있다.\n\n" + NOTE.format("tenbagger"))
    out, st = tm.fix(md)
    assert out == md and st["unplaced"] == 1


def test_star_belonging_to_earlier_note_is_not_order_paired():
    """FAA 각주는 앞 단락 묶음에, FAA 표식은 다음 단락에 있을 때 '남은 수가 같다'고 다른
    각주(온톨로지)와 짝지으면 안 된다 — 두 번 돌리면 결과가 흔들렸다(실제 사례)."""
    md = _doc("DCA\\*에서 사고가 났다.\n\n" + NOTE.format("FAA (Federal Aviation Administration)") + "\n"
              + NOTE.format("DCA") + "\n\nFAA\\*는 개편안을 냈다.\n\n" + NOTE.format("온톨로지")
              + "\n\n공통 **온톨로지**를 공유한다.")
    once, _ = tm.fix(md)
    twice, _ = tm.fix(once)
    assert once == twice
    assert "FAA\\*는" in once and "**온톨로지**\\*를" in once


def test_misplaced_mark_moves_to_the_term():
    """DHH 사례: 'PR' 각주의 표식이 볼드 문장 끝에 붙어 있으면 'PR' 바로 뒤로 옮긴다."""
    md = _doc("바이브 코딩\\*을 맡겼더니 **개별 PR은 정당했지만 아키텍처가 무너져**\\* 되돌렸다.\n\n"
              + '<div class="term-notes">\n<p class="term-note">* <strong>바이브 코딩</strong> — 설명</p>\n'
              + '<p class="term-note">* <strong>PR (Pull Request)</strong> — 설명</p>\n</div>')
    out, st = tm.fix(md)
    assert "**개별 PR\\*은" in out and "무너져** 되돌렸다" in out
    assert tm.fix(out)[0] == out
    # 각주 묶음이 나뉘어 있어도 멀리 떨어진 표식을 PR 짝으로 보지 않는다
    md2 = _doc("바이브 코딩\\*을 맡겼더니 **개별 PR은 정당했지만 아키텍처가 무너져**\\* 되돌렸다.\n\n"
               + NOTE.format("바이브 코딩") + "\n" + NOTE.format("PR (Pull Request)"))
    out2, _ = tm.fix(md2)
    assert "**개별 PR\\*은" in out2 and "무너져** 되돌렸다" in out2



def test_mark_before_parenthetical_original_is_kept_and_new_marks_go_after_paren():
    md = _doc("구글의 트랜스포머\\*(transformer) 이후 활성값(activations)으로 사고한다.\n\n"
              + '<div class="term-notes">\n<p class="term-note">* <strong>transformer</strong> — 설명</p>\n'
              + '<p class="term-note">* <strong>activations</strong> — 설명</p>\n</div>')
    out, _ = tm.fix(md)
    assert "트랜스포머\\*(transformer)" in out and "활성값(activations)\\*으로" in out
    assert tm.fix(out)[0] == out


def test_label_with_inner_parenthetical_matches_body_term():
    md = _doc("생성에 쓰일 **KV 캐시**\\*도 만든다.\n\n" + NOTE.format("KV (Key-Value) 캐시"))
    out, st = tm.fix(md)
    assert out == md and st["removed"] == 0


def test_captions_are_never_footnote_targets():
    """키프레임 캡션(<div class="kf-strip">…<figcaption>)은 각주 대상이 아니다(2026-09-10·10-01 사용자
    지시). 각주 용어가 캡션에만 있어도 캡션에 별표를 넣지 않는다."""
    import term_marks
    md = ('## 3. 핵심 내용\n\n### 밸류에이션 [00:00]\n\n'
          '<div class="kf-strip"><figure><img src="/sframe/x/kf_1.jpg" alt=""><figcaption><b>02:13</b> '
          '선행 PER 17배로 2015년 이후 최저</figcaption></figure></div>\n\n'
          '선행 PER은 역사적 저점 수준이다.\n\n'
          '<p class="term-note">* <strong>PER</strong> 주가수익비율</p>\n')
    out, _ = term_marks.fix(md)
    cap = out[out.index('<figcaption>'):out.index('</figcaption>')]
    assert '*' not in cap.replace('<b>02:13</b>', '')
