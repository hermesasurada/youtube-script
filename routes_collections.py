"""라우트: 영상 모음(/collections, /collection/<slug>, /collection/<slug>/items) — 2026-11-19.

모음 페이지는 PC·폰 공용 한 장이다. 영상을 누르면 이력 화면(`/?open=<id>&from=<slug>`, 폰은 `/m?…`)으로 가서
그 요약 창을 바로 열고, 요약 창을 닫으면 모음으로 돌아온다(`index-boot.js`·`mobile.js`).
"""
from flask import abort, redirect, render_template
from yt_web import app
import collections_view
import yt_core


@app.route("/collections")
def collections_index():
    rows = collections_view.summaries()
    if len(rows) == 1:                       # 모음이 하나뿐이면 고르는 화면 없이 바로 연다
        return redirect(f"/collection/{rows[0]['slug']}", 302)
    return render_template("collection.html", slug="", collection=None, collections=rows)


@app.route("/collection/<slug>")
def collection_page(slug):
    c = collections_view.get(slug)
    if not c:
        abort(404)
    return render_template("collection.html", slug=slug, collection=c, collections=None)


@app.route("/collection/<slug>/items")
def collection_items(slug):
    data = collections_view.items(slug)
    if data is None:
        return yt_core._json({"error": "없는 모음입니다"}, 404)
    return yt_core._json(data)
