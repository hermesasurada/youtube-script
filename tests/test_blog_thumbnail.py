"""블로그 썸네일 = 메인 화면 영상 썸네일(2026-10-29) — R2 주소만, 원제 줄 뒤, 실패해도 발행은 그대로."""
import app
from hermes_blogger import r2

BODY = '<div style="x">\n<p data-ys-original-title="1" style="y">원제 : T</p><h2>A</h2></div>'


def _fake(monkeypatch, puts, data=b"\xff\xd8" + b"0" * 4000):
    monkeypatch.setattr(r2, "configured", lambda: True)
    monkeypatch.setattr(app, "_blog_thumb_bytes", lambda yt_id: data)

    def put(key, blob, content_type=None, **kw):
        puts.append((key, content_type))
        return "https://pub-x.r2.dev/" + key
    monkeypatch.setattr(r2, "put", put)


def test_thumbnail_goes_after_original_title_with_r2_url(monkeypatch):
    puts = []
    _fake(monkeypatch, puts)
    out = app._insert_blog_thumbnail(BODY, "abcde12345", "https://www.youtube.com/watch?v=abcde12345")
    assert puts[0][0].startswith("yt/thumb/abcde12345-") and puts[0][1] == "image/jpeg"
    assert out.index("원제 :") < out.index('data-ys-thumb="1"') < out.index("<h2>")
    assert 'src="https://pub-x.r2.dev/yt/thumb/abcde12345-' in out
    assert "data:image" not in out and "i.ytimg.com" not in out
    assert app._insert_blog_thumbnail(out, "abcde12345") == out          # 두 번 넣지 않는다


def test_without_original_title_goes_to_wrapper_start(monkeypatch):
    _fake(monkeypatch, [])
    out = app._insert_blog_thumbnail('<div style="x"><h2>A</h2></div>', "abcde12345")
    assert out.startswith('<div style="x">\n<div data-ys-thumb="1"')


def test_failures_publish_unchanged(monkeypatch):
    _fake(monkeypatch, [], data=None)
    assert app._insert_blog_thumbnail(BODY, "abcde12345") == BODY        # 그림 없음
    _fake(monkeypatch, [])
    monkeypatch.setattr(r2, "put", lambda *a, **k: (_ for _ in ()).throw(r2.R2Error("boom")))
    assert app._insert_blog_thumbnail(BODY, "abcde12345") == BODY        # 업로드 실패
    monkeypatch.setattr(r2, "configured", lambda: False)
    assert app._insert_blog_thumbnail(BODY, "abcde12345") == BODY        # R2 미설정
    assert app._insert_blog_thumbnail(BODY, "") == BODY                  # 영상 id 없음(오디오 파일)
