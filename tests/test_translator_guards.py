"""전사 번역 결함 가드(2026-09-26): 중국어 누출 교정과 줄 반복 루프 판정."""
import transcript_translator as tt

SRC = "[00:00] So one example of building compute for the future\nis the massive Ohio data center\n"


def test_han_leaks_flags_chinese_slips_but_keeps_normal_hanja():
    assert tt.han_leaks("[01:31]顺便说一下, Boeing이 2004년") == ["顺便说一下"]
    assert tt.han_leaks("해저 케이블을铺设하려는 시도") == ["铺设"]
    assert tt.han_leaks("민粹주의가 퍼졌다") == ["粹"]            # 한 글자라도 한글 사이면 누출
    assert tt.han_leaks("반(反)이민 정서, 前 CEO, 對중국 수출") == []
    assert tt.han_leaks("Liang Wenfeng(梁文锋)이 말했다", source="Liang Wenfeng 梁文锋") == []


def test_repeat_loop_is_degenerate_even_under_length_limit():
    loop = "[00:00] 시작\n" + "대규모로 컴퓨터를 구축해야 하고, 대규모로 컴퓨터를 구축해야 합니다.\n" * 6
    src = "".join(f"[00:{i:02d}] distinct source sentence number {i} about compute\n" for i in range(8))
    assert len(loop) < len(src) * 2.5                  # 분량 기준만으로는 못 잡는 크기
    assert tt._looks_degenerate(loop, src, "stop")
    # 원문 자체가 반복(whisper 반복)이면 따라간 것이라 폭주가 아니다
    src_loop = "[00:00] start\n" + "we need to build compute at scale.\n" * 6
    assert not tt._looks_degenerate(loop, src_loop, "stop")
    # 짧은 맞장구 반복은 정상
    assert not tt._looks_degenerate("네.\n" * 10 + "좋습니다.", SRC, "stop")


def test_collapse_repeats_keeps_first_line_only():
    text = "[00:10] 가\n같은 문장이 계속 반복되는 긴 줄입니다.\n같은 문장이 계속 반복되는 긴 줄입니다.\n끝"
    assert tt._collapse_repeats(text) == "[00:10] 가\n같은 문장이 계속 반복되는 긴 줄입니다.\n끝"


def test_repair_leaks_replaces_only_bad_lines(monkeypatch):
    out = "[00:00] 첫 줄은 멀쩡합니다.\n해저 케이블을铺设하려는 시도들입니다.\n[01:31]顺便说一下, Boeing이 2004년에"
    seen = {}

    def fake_call(user, temp, rep, *, title=None, system=tt.SYSTEM_PROMPT, purpose="translate"):
        seen.update(user=user, system=system, purpose=purpose)
        return "1\t해저 케이블을 부설하려는 시도들입니다.\n2\t[01:31]참고로, Boeing이 2004년에", "stop"

    monkeypatch.setattr(tt, "_call", fake_call)
    fixed = tt._repair_leaks(out, SRC, title="t")
    assert fixed == "[00:00] 첫 줄은 멀쩡합니다.\n해저 케이블을 부설하려는 시도들입니다.\n[01:31]참고로, Boeing이 2004년에"
    assert seen["purpose"] == "repair" and seen["system"] != tt.SYSTEM_PROMPT
    assert "첫 줄은" not in seen["user"]                 # 멀쩡한 줄은 보내지 않는다


def test_repair_leaks_rejects_bad_fix_and_survives_errors(monkeypatch):
    out = "[01:31]顺便说一下, Boeing이 2004년에 발표했습니다"
    # 교정본에 여전히 한자가 있거나 타임스탬프를 잃으면 원래 줄 유지
    monkeypatch.setattr(tt, "_call", lambda *a, **k: ("1\t참고로, Boeing이 2004년에 발표했습니다", "stop"))
    assert tt._repair_leaks(out, SRC) == out
    monkeypatch.setattr(tt, "_call", lambda *a, **k: ("1\t[01:31]那里 Boeing이 2004년에 발표했습니다", "stop"))
    assert tt._repair_leaks(out, SRC) == out

    def boom(*a, **k):
        raise RuntimeError("backend down")
    monkeypatch.setattr(tt, "_call", boom)
    assert tt._repair_leaks(out, SRC) == out           # 교정 실패가 번역을 깨뜨리지 않는다


def test_chunk_flow_retries_loop_then_repairs_leak(monkeypatch):
    calls = []
    loop = "대규모로 컴퓨터를 구축해야 하고, 대규모로 컴퓨터를 구축해야 합니다.\n" * 6

    def fake_call(user, temp, rep, *, title=None, system=tt.SYSTEM_PROMPT, purpose="translate"):
        calls.append(purpose)
        if purpose == "repair":
            return "1\t[00:00] 오하이오 데이터센터를 건설하는 것입니다", "stop"
        if len(calls) == 1:
            return loop, "stop"
        return "[00:00] 오하이오 데이터센터를建设하는 것입니다", "stop"

    monkeypatch.setattr(tt, "_call", fake_call)
    out = tt._translate_chunk_raw(SRC, title="t")
    assert calls == ["translate", "translate", "repair"]
    assert out == "[00:00] 오하이오 데이터센터를 건설하는 것입니다"
