/* youtube-script 공유 프론트엔드 유틸 (index/mobile/summary 공용).
 *
 * 로드 순서: marked.min.js → common.js
 * 목적: 마크다운 렌더링·이스케이프·날짜/유튜브ID·이력 API 호출을 한 곳에 모아
 *       템플릿 간 중복(및 "한쪽만 수정됨" 류 버그)을 제거한다.
 */
(function (global) {
  'use strict';

  // ── marked.js 공통 설정: 링크는 새 탭 + noopener ──────────────────
  if (global.marked && typeof global.marked.use === 'function') {
    const renderer = new global.marked.Renderer();
    renderer.link = function (href, title, text) {
      const h = (typeof href === 'object') ? href.href : href;
      const t = (typeof href === 'object') ? (href.text || h) : (text || h);
      return `<a href="${h}" target="_blank" rel="noopener noreferrer">${t}</a>`;
    };
    global.marked.use({ renderer, breaks: false, gfm: true });
  }

  /**
   * 한글(CJK) 인접 강조 보정.
   * CommonMark flanking 규칙상 닫는 `**`가 구두점 뒤·CJK 앞이면(예: `**"...것"**이며`)
   * 강조로 인식되지 않는다. marked로 넘기기 전 코드 영역을 보호한 뒤
   * 굵게/기울임 강조를 직접 strong/em 으로 변환해 일관 렌더링한다.
   * (lookbehind 미사용 — 구형 사파리 호환)
   */
  function _fixCjkEmphasis(src) {
    const stash = [];
    src = src.replace(/```[\s\S]*?```|`[^`\n]+`/g, (m) => {
      stash.push(m);
      return '\x01C' + (stash.length - 1) + '\x01';
    });
    // 틸드 무력화: 본문 취소선 금지 정책 + 한국어 범위표현(예: 5~8개)이 GFM 단일틸드
    // 취소선으로 오인돼 두 범위 사이가 통째로 <del> 되는 것 방지. 엔티티라 화면엔 '~'로 표시.
    // (코드 스팬은 위에서 stash로 보호됨)
    src = src.replace(/~/g, '&#126;');
    src = src.replace(/\*\*([^\s][^\n]*?[^\s]|\S)\*\*/g, (_, t) => '<strong>' + t + '</strong>');
    src = src.replace(/(^|[^*])\*([^\s*][^\n*]*?[^\s*]|[^\s*])\*(?!\*)/g,
                      (_, p, t) => p + '<em>' + t + '</em>');
    src = src.replace(/\x01C(\d+)\x01/g, (_, i) => stash[+i]);
    return src;
  }

  /**
   * 렌더된 h3 소제목 꾸미기: 앞에 아이콘(▸) 추가, 본문 내 [mm:ss] 시각 라벨은
   * 추출해 제목 '뒤'에 회색(.kf-time)으로 배치. (위치 무관 — 앞/뒤 라벨 모두 처리)
   */
  function _decorateHeadings(html) {
    return html.replace(/<h3([^>]*)>([\s\S]*?)<\/h3>/g, (_, attrs, inner) => {
      let body = inner, time = '';
      const m = inner.match(/\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*/);
      if (m) {
        body = (inner.slice(0, m.index) + inner.slice(m.index + m[0].length)).trim();
        time = ` <span class="kf-time">${m[1]}</span>`;
      }
      return `<h3${attrs}><span class="kf-ico">▸</span> ${body}${time}</h3>`;
    });
  }

  /**
   * 요약 전용 의미 변환(가드되어 일반 마크다운엔 무해):
   *  1) `## 메타정보` + 표  → 칩 헤더(.ys-meta): 업로더·날짜·길이·YouTube 링크
   *  2) `## 한눈 요약` + ul → 강조 callout 카드(.ys-tldr)
   */
  function _decorateSummary(html, model) {
    if (typeof document === 'undefined') return html;
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    const h2s = [...tpl.content.querySelectorAll('h2')];

    const metaH = h2s.find(h => /메타\s*정보/.test(h.textContent));
    if (metaH && metaH.nextElementSibling && metaH.nextElementSibling.tagName === 'TABLE') {
      const tbl = metaH.nextElementSibling;
      const kv = {};
      tbl.querySelectorAll('tr').forEach(tr => {
        const c = tr.querySelectorAll('td,th');
        if (c.length >= 2) kv[c[0].textContent.trim()] = c[1];
      });
      const chips = [];
      if (kv['업로더']) chips.push(`<span class="ys-chip ys-chip-up">${kv['업로더'].innerHTML}</span>`);
      if (kv['날짜'])   chips.push(`<span class="ys-chip">${kv['날짜'].innerHTML}</span>`);
      if (kv['길이'])   chips.push(`<span class="ys-chip ys-chip-dur">${kv['길이'].innerHTML}</span>`);
      const urlA = kv['URL'] && kv['URL'].querySelector('a');
      if (urlA) chips.push(`<a class="ys-chip ys-chip-link" href="${urlA.href}" target="_blank" rel="noopener noreferrer">YouTube에서 보기 ↗</a>`);
      if (model) chips.push(`<span class="ys-chip ys-chip-model" title="이 요약을 생성한 LLM 모델">🧠 ${escapeHtml(model)}</span>`);
      if (chips.length) {
        const bar = document.createElement('div');
        bar.className = 'ys-meta';
        bar.innerHTML = chips.join('');
        tbl.replaceWith(bar);
        metaH.remove();
      }
    }

    const tldrH = h2s.find(h => /한눈\s*요약/.test(h.textContent));
    if (tldrH && tldrH.nextElementSibling && tldrH.nextElementSibling.tagName === 'UL') {
      const ul = tldrH.nextElementSibling;
      const card = document.createElement('section');
      card.className = 'ys-tldr';
      card.innerHTML = '<div class="ys-tldr-label">✦ 한눈 요약</div>';
      ul.replaceWith(card);
      card.appendChild(ul);
      tldrH.remove();
    }
    return tpl.innerHTML;
  }

  /** 마크다운 → HTML. YAML 프론트매터 제거 + CJK 강조 보정 후 marked.js + 소제목·요약 꾸미기. */
  function renderMarkdown(src) {
    src = String(src || '').replace(/^---\n[\s\S]*?\n---\n?/, '');
    // 요약 모델 마커(HTML 주석) 추출 → 메타 칩으로 이동(YouTube 보기 옆). 본문에선 제거.
    let model = '';
    src = src.replace(/<!--\s*SUMMARY_MODEL:([\s\S]*?)-->\s*/, (_, m) => { model = m.trim(); return ''; });
    src = _fixCjkEmphasis(src);
    if (global.marked && typeof global.marked.parse === 'function') {
      return _decorateSummary(_decorateHeadings(global.marked.parse(src)), model);
    }
    // marked 미로딩 시 최소 폴백(이스케이프된 평문)
    return '<pre>' + escapeHtml(src) + '</pre>';
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }
  const attrEscape = escapeHtml;

  /** YYYYMMDD → "YYYY-MM-DD" (그 외 형식은 원본 반환). */
  function fmtDate(d) {
    const s = String(d || '');
    const m = s.match(/^(\d{4})(\d{2})(\d{2})$/);
    return m ? `${m[1]}-${m[2]}-${m[3]}` : s;
  }

  /** YouTube URL → videoId ('' if 추출 실패). */
  function ytVideoId(url) {
    if (!url) return '';
    try {
      const u = new URL(url);
      const h = u.hostname.replace(/^www\./, '');
      if (h === 'youtu.be') return u.pathname.slice(1).split('/')[0];
      if (h.endsWith('youtube.com')) {
        const v = u.searchParams.get('v');
        if (v) return v;
        const m = u.pathname.match(/^\/(?:shorts|embed|v|live)\/([^/?#]+)/);
        if (m) return m[1];
      }
    } catch (_) {}
    return '';
  }

  // ── 이력 관련 API 호출(엔드포인트 경로를 한 곳에서 관리) ──────────
  async function apiMarkRead(txtPath, isRead) {
    const r = await fetch('/history/mark_read', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ txt_path: txtPath, is_read: isRead }),
    });
    return r.ok;
  }

  async function apiDeleteItem(txtPath) {
    const r = await fetch('/history/item', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ txt_path: txtPath }),
    });
    return r.ok;
  }

  async function apiSummaryContent(path) {
    const r = await fetch('/summary/content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    return r.json();
  }

  global.YS = {
    renderMarkdown,
    escapeHtml,
    attrEscape,
    fmtDate,
    ytVideoId,
    apiMarkRead,
    apiDeleteItem,
    apiSummaryContent,
  };

  // ── 키프레임 스트립(가로 스크롤) + 라이트박스(원본 보기) ────────────
  // 요약 md에 주입된 <div class="kf-strip">…</div> 를 모든 요약 뷰어에서 공통 처리.
  function _setupKeyframeUI() {
    if (document.getElementById("ys-kf-style")) return;
    const css = `
.kf-strip{display:flex;gap:.7rem;overflow-x:auto;padding:.5rem .15rem .9rem;margin:.6rem 0 1.2rem;-webkit-overflow-scrolling:touch;}
.kf-strip figure{margin:0;flex:0 0 auto;width:280px;border:1px solid var(--border,#e5e5e5);border-radius: 2px;overflow:hidden;background:var(--surface,#fff);box-shadow:0 1px 3px rgba(0,0,0,.07);transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease;}
.kf-strip figure:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(0,0,0,.16);border-color:var(--border-strong,var(--border,#ccc));}
.kf-strip img{display:block;width:100%;height:158px;object-fit:cover;cursor:zoom-in;}
.kf-strip figcaption{display:flex;align-items:baseline;gap:.45rem;font-size:.74rem;color:var(--muted,#666);padding:.42rem .6rem .48rem;line-height:1.45;}
.kf-strip figcaption b{color:var(--highlight,var(--accent,#2563eb));font-family:ui-monospace,monospace;font-size:.68rem;font-weight:600;flex-shrink:0;background:var(--highlight-soft,rgba(99,102,241,.1));padding:.06rem .38rem;border-radius: 2px;}
/* ── 메타 칩 헤더(메타정보 표 → 변환) ── */
.ys-meta{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;margin:.4rem 0 1.4rem;padding-bottom:1.1rem;border-bottom:1px solid var(--border,#e5e5e5);}
.ys-chip{display:inline-flex;align-items:center;gap:.35em;font-size:.78rem;color:var(--muted,#666);background:var(--surface2,#f3f0e9);border:1px solid var(--border,#e5e5e5);border-radius: 2px;padding:.26rem .72rem;line-height:1.25;}
.ys-chip-up{color:var(--text,#222);font-weight:600;}
.ys-chip-dur{font-family:ui-monospace,monospace;font-size:.72rem;letter-spacing:.02em;}
.ys-chip a{color:inherit;text-decoration:none;}
a.ys-chip-link{color:var(--highlight,#2563eb);border-color:color-mix(in oklab,var(--highlight,#2563eb) 38%,transparent);background:var(--highlight-soft,rgba(99,102,241,.08));text-decoration:none;font-weight:600;transition:filter .15s;}
a.ys-chip-link:hover{filter:brightness(1.12);text-decoration:none;}
.ys-chip-model{font-family:ui-monospace,monospace;font-size:.72rem;letter-spacing:.01em;}
/* ── 한눈 요약 callout ── */
.ys-tldr{position:relative;margin:1.2rem 0 1.7rem;padding:1rem 1.25rem 1.05rem 1.35rem;background:linear-gradient(135deg,var(--highlight-soft,rgba(99,102,241,.08)),transparent 78%),var(--surface2,#f6f3ec);border:1px solid var(--border,#e5e5e5);border-radius: 2px;overflow:hidden;}
.ys-tldr::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(180deg,var(--highlight,#2563eb),color-mix(in oklab,var(--highlight,#2563eb) 35%,transparent));}
.ys-tldr-label{font-size:.7rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--highlight,#2563eb);margin-bottom:.55rem;}
.ys-tldr ul{margin:0 !important;padding-left:1.15rem !important;}
.ys-tldr li{margin-bottom:.3rem;}  /* font-size/line-height는 본문 .sum-md li 규칙을 물려받아 동일 크기(일반·몰입 모두) */
.ys-tldr li::marker{color:var(--highlight,#2563eb);}
.ys-lb{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;z-index:99999;cursor:zoom-out;}
.ys-lb.open{display:block;}
.ys-lb-viewport{position:absolute;inset:0;overflow:hidden;touch-action:pinch-zoom;}
.ys-lb-track{display:flex;height:100%;will-change:transform;transition:transform .3s cubic-bezier(.22,.61,.36,1);}
.ys-lb-track.dragging{transition:none;}
.ys-lb-slide{flex:0 0 100%;height:100%;display:flex;align-items:center;justify-content:center;padding:1.5rem;}
.ys-lb-slide img{max-width:88vw;max-height:92vh;border-radius: 2px;box-shadow:0 6px 40px rgba(0,0,0,.5);pointer-events:none;-webkit-user-drag:none;user-select:none;}
.ys-lb-nav{position:fixed;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.4);color:#fff;border:none;font-size:2.2rem;line-height:1;width:52px;height:72px;border-radius: 2px;cursor:pointer;display:flex;align-items:center;justify-content:center;user-select:none;}
.ys-lb-nav:hover{background:rgba(0,0,0,.7);}
.ys-lb-prev{left:1.2rem;} .ys-lb-next{right:1.2rem;}
.ys-lb-count{position:fixed;bottom:1.2rem;left:50%;transform:translateX(-50%);color:rgba(255,255,255,.85);font-size:13px;font-family:ui-monospace,monospace;background:rgba(0,0,0,.5);padding:.15rem .65rem;border-radius: 2px;}
.ys-lb-cap{position:fixed;bottom:3rem;left:50%;transform:translateX(-50%);max-width:80vw;text-align:center;color:rgba(255,255,255,.92);font-size:13.5px;line-height:1.5;background:rgba(0,0,0,.5);padding:.4rem .85rem;border-radius: 2px;}
.ys-lb-cap:empty{display:none;}
.ys-lb-cap b{color:#9db4ff;font-family:ui-monospace,monospace;margin-right:.4rem;}
.kf-ico{display:none;}  /* 좌측 강조 바가 마커 역할 — 글리프 중복 제거 */
.kf-time{margin-left:auto;flex-shrink:0;color:var(--muted,#999);font-weight:600;font-size:.7em;letter-spacing:.02em;font-family:ui-monospace,monospace;background:var(--surface,#fff);border:1px solid var(--border,#e5e5e5);padding:.14em .55em;border-radius: 2px;}
/* 요약 소제목(h3) 리본: 좌측 강조 바 + 틴트, 시각 pill은 우측 정렬 */
.sum-md h3,.md-body h3,.markdown h3{display:flex;align-items:center;gap:.5em;background:linear-gradient(90deg,var(--highlight-soft,rgba(99,102,241,.1)),transparent 88%);border-left:3px solid var(--highlight,var(--accent,#6366f1));padding:.5rem .8rem;border-radius: 2px;margin:1.7rem 0 .75rem;}`;
    const st = document.createElement("style");
    st.id = "ys-kf-style"; st.textContent = css;
    document.head.appendChild(st);

    const lb = document.createElement("div");
    lb.className = "ys-lb"; lb.id = "ys-lb";
    lb.innerHTML = '<div class="ys-lb-viewport"><div class="ys-lb-track"></div></div>'
                 + '<button class="ys-lb-nav ys-lb-prev" aria-label="이전">‹</button>'
                 + '<button class="ys-lb-nav ys-lb-next" aria-label="다음">›</button>'
                 + '<div class="ys-lb-cap"></div>'
                 + '<span class="ys-lb-count"></span>';
    document.body.appendChild(lb);
    const lbViewport = lb.querySelector(".ys-lb-viewport");
    const lbTrack = lb.querySelector(".ys-lb-track");
    const lbPrev = lb.querySelector(".ys-lb-prev");
    const lbNext = lb.querySelector(".ys-lb-next");
    const lbCount = lb.querySelector(".ys-lb-count");
    const lbCap = lb.querySelector(".ys-lb-cap");
    let lbList = [], lbCaps = [], lbIdx = 0;   // 현재 섹션 이미지 src·캡션 목록 + 인덱스

    function _lbBuild() {                       // 현재 섹션 이미지로 슬라이드 트랙 구성
      lbTrack.innerHTML = lbList
        .map((src) => `<div class="ys-lb-slide"><img alt="" src="${src}"></div>`)
        .join("");
    }
    function _lbPos(instant) {                  // 트랙을 현재 인덱스 위치로(instant=애니메이션 없이)
      if (instant) lbTrack.classList.add("dragging");
      lbTrack.style.transform = `translateX(${-lbIdx * 100}%)`;
      if (instant) { void lbTrack.offsetWidth; lbTrack.classList.remove("dragging"); }
    }
    function lbShow(i, instant) {
      if (!lbList.length) return;
      lbIdx = Math.max(0, Math.min(i, lbList.length - 1));   // 클램프(순환 안 함)
      _lbPos(instant);
      lbCap.innerHTML = lbCaps[lbIdx] || "";   // 이미지 하단 캡션(넘버링 위)
      const multi = lbList.length > 1;
      lbPrev.style.display = (multi && lbIdx > 0) ? "" : "none";                 // 처음이면 이전 숨김
      lbNext.style.display = (multi && lbIdx < lbList.length - 1) ? "" : "none"; // 끝이면 다음 숨김
      lbCount.style.display = multi ? "" : "none";
      lbCount.textContent = multi ? `${lbIdx + 1}/${lbList.length}` : "";
    }
    // 라이트박스 history 연동 + 스와이프 상태
    let lbPushed = false;   // 라이트박스용 history 상태 push 여부
    let lbAbsorb = false;   // 사용자 닫기로 생기는 정리 popstate 1회 흡수
    let lbSwiped = false;   // 스와이프 직후 click(닫기) 억제
    function lbClose() {
      if (!lb.classList.contains("open")) return;
      lb.classList.remove("open");
      if (lbPushed) { lbPushed = false; lbAbsorb = true; history.back(); }  // push했던 상태 정리(popstate는 아래서 흡수)
    }

    lb.addEventListener("click", (e) => {
      if (lbSwiped) { lbSwiped = false; return; }      // 스와이프/드래그 끝에서 따라오는 click은 무시
      if (e.target.closest(".ys-lb-cap")) return;      // 캡션 클릭은 무시
      lbClose();                                       // 그 외(배경/이미지) 탭 → 닫기(화살표는 stopPropagation)
    });
    lbPrev.addEventListener("click", (e) => { e.stopPropagation(); lbShow(lbIdx - 1); });
    lbNext.addEventListener("click", (e) => { e.stopPropagation(); lbShow(lbIdx + 1); });

    // 이벤트 위임: 스트립 이미지 클릭 → 같은 섹션 이미지들로 라이트박스 구성
    document.addEventListener("click", (e) => {
      const im = e.target.closest && e.target.closest(".kf-strip img");
      if (!im) return;
      const strip = im.closest(".kf-strip");
      const imgs = strip ? [...strip.querySelectorAll("img")] : [im];
      lbList = imgs.map((x) => x.src);
      lbCaps = imgs.map((x) => {                 // 각 이미지의 figcaption(시각·설명)
        const cap = x.closest("figure")?.querySelector("figcaption");
        return cap ? cap.innerHTML : "";
      });
      _lbBuild();
      lbShow(imgs.indexOf(im), true);       // 열 때는 애니메이션 없이 해당 이미지로
      lb.classList.add("open");
      lbPushed = true;
      history.pushState({ ysLb: 1 }, "");   // 뒤로가기 1회 = (아래 모달이 아니라) 라이트박스만 닫기
    });
    // 캡처 단계 등록 → 다른 모달의 ESC 핸들러(버블)보다 먼저 가로챔.
    // 라이트박스가 열려 있을 때만 처리하고 stopPropagation으로 이벤트를 막아,
    // ESC가 아래의 요약/전사 팝업까지 닫는 것을 방지(라이트박스만 닫힘).
    document.addEventListener("keydown", (e) => {
      if (!lb.classList.contains("open")) return;
      if (e.key === "Escape") { e.stopPropagation(); lbClose(); }
      else if (e.key === "ArrowLeft") { e.stopPropagation(); lbShow(lbIdx - 1); }
      else if (e.key === "ArrowRight") { e.stopPropagation(); lbShow(lbIdx + 1); }
    }, true);

    // 뒤로가기(popstate): 라이트박스가 열려 있으면 그것만 닫고 아래 모달 핸들러는 막는다.
    // 캡처 단계 + stopImmediatePropagation → 등록 순서와 무관하게 모바일 모달 popstate보다 우선.
    window.addEventListener("popstate", (e) => {
      if (lbAbsorb) { lbAbsorb = false; e.stopImmediatePropagation(); return; }   // 사용자 닫기 정리 pop 흡수
      if (lb.classList.contains("open")) {
        lb.classList.remove("open"); lbPushed = false;
        e.stopImmediatePropagation();   // 아래 모달까지 닫히지 않게
      }
    }, true);

    // 모바일: 손가락을 따라 트랙이 움직이고, 놓으면 다음/이전으로 스냅(부드러운 전환)
    let _tx = 0, _ty = 0, _drag = false, _moved = false;
    lb.addEventListener("touchstart", (e) => {
      if (!lbList.length || e.touches.length > 1) { _drag = false; lbTrack.classList.remove("dragging"); return; }  // 핀치는 브라우저에 양보
      const t = e.changedTouches[0];
      _tx = t.clientX; _ty = t.clientY; _drag = true; _moved = false;
      lbTrack.classList.add("dragging");           // 드래그 중엔 전환 끄고 즉시 추종
    }, { passive: true });
    lb.addEventListener("touchmove", (e) => {
      if (!_drag) return;
      if (e.touches.length > 1) { _drag = false; lbTrack.classList.remove("dragging"); lbShow(lbIdx); return; }  // 핀치 시작 → 드래그 취소·제자리
      const t = e.changedTouches[0], dx = t.clientX - _tx, dy = t.clientY - _ty;
      if (!_moved && Math.abs(dx) < Math.abs(dy)) { _drag = false; lbTrack.classList.remove("dragging"); return; }  // 세로 제스처
      if (Math.abs(dx) > 6) _moved = true;
      const atEnd = (lbIdx === 0 && dx > 0) || (lbIdx === lbList.length - 1 && dx < 0);
      const off = atEnd ? dx * 0.35 : dx;          // 양 끝에선 저항(고무줄)
      lbTrack.style.transform = `translateX(calc(${-lbIdx * 100}% + ${off}px))`;
    }, { passive: true });
    lb.addEventListener("touchend", (e) => {
      if (!_drag) return;
      _drag = false; lbTrack.classList.remove("dragging");
      const dx = e.changedTouches[0].clientX - _tx;
      const w = lbViewport.clientWidth || 1;
      if (_moved) lbSwiped = true;                 // 드래그였으면 직후 click(닫기) 억제
      if (Math.abs(dx) > Math.min(60, w * 0.18)) lbShow(lbIdx + (dx < 0 ? 1 : -1));  // 임계 넘으면 이동
      else lbShow(lbIdx);                           // 아니면 제자리 스냅(애니메이션)
    }, { passive: true });
  }
  if (document.body) _setupKeyframeUI();
  else document.addEventListener("DOMContentLoaded", _setupKeyframeUI);

  global.YS.setupKeyframeUI = _setupKeyframeUI;
})(window);
