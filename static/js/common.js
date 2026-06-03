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

  /** 마크다운 → HTML. YAML 프론트매터 제거 + CJK 강조 보정 후 marked.js + 소제목 꾸미기. */
  function renderMarkdown(src) {
    src = String(src || '').replace(/^---\n[\s\S]*?\n---\n?/, '');
    src = _fixCjkEmphasis(src);
    if (global.marked && typeof global.marked.parse === 'function') {
      return _decorateHeadings(global.marked.parse(src));
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
.kf-strip{display:flex;gap:.55rem;overflow-x:auto;padding:.4rem 0 .7rem;margin:.5rem 0 1.1rem;-webkit-overflow-scrolling:touch;}
.kf-strip figure{margin:0;flex:0 0 auto;width:280px;border:1px solid var(--border,#e5e5e5);border-radius:8px;overflow:hidden;background:var(--surface,#fff);}
.kf-strip img{display:block;width:100%;height:158px;object-fit:cover;cursor:zoom-in;}
.kf-strip figcaption{font-size:.74rem;color:var(--muted,#666);padding:.35rem .5rem;line-height:1.4;}
.kf-strip figcaption b{color:var(--highlight,var(--accent,#2563eb));font-family:ui-monospace,monospace;margin-right:.35rem;}
.ys-lb{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;z-index:99999;cursor:zoom-out;padding:1.5rem;}
.ys-lb.open{display:flex;}
.ys-lb img{max-width:88vw;max-height:92vh;border-radius:4px;box-shadow:0 6px 40px rgba(0,0,0,.5);}
.ys-lb-nav{position:fixed;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.4);color:#fff;border:none;font-size:2.2rem;line-height:1;width:52px;height:72px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;user-select:none;}
.ys-lb-nav:hover{background:rgba(0,0,0,.7);}
.ys-lb-prev{left:1.2rem;} .ys-lb-next{right:1.2rem;}
.ys-lb-count{position:fixed;bottom:1.2rem;left:50%;transform:translateX(-50%);color:rgba(255,255,255,.85);font-size:13px;font-family:ui-monospace,monospace;background:rgba(0,0,0,.5);padding:.15rem .65rem;border-radius:999px;}
.ys-lb-cap{position:fixed;bottom:3rem;left:50%;transform:translateX(-50%);max-width:80vw;text-align:center;color:rgba(255,255,255,.92);font-size:13.5px;line-height:1.5;background:rgba(0,0,0,.5);padding:.4rem .85rem;border-radius:8px;}
.ys-lb-cap:empty{display:none;}
.ys-lb-cap b{color:#9db4ff;font-family:ui-monospace,monospace;margin-right:.4rem;}
.kf-ico{color:var(--highlight,var(--accent,#2563eb));margin-right:.1em;}
.kf-time{color:var(--muted,#999);font-weight:400;font-size:.82em;font-family:ui-monospace,monospace;}
/* 요약 소제목(h3) 리본: 좌측 강조 바 + 강조 틴트 배경(가시성↑, 테마 적응) */
.sum-md h3,.md-body h3,.markdown h3{background:var(--highlight-soft,rgba(99,102,241,.1));border-left:4px solid var(--highlight,var(--accent,#6366f1));padding:.45rem .7rem;border-radius:7px;margin:1.5rem 0 .7rem;}`;
    const st = document.createElement("style");
    st.id = "ys-kf-style"; st.textContent = css;
    document.head.appendChild(st);

    const lb = document.createElement("div");
    lb.className = "ys-lb"; lb.id = "ys-lb";
    lb.innerHTML = '<button class="ys-lb-nav ys-lb-prev" aria-label="이전">‹</button>'
                 + '<img alt="">'
                 + '<button class="ys-lb-nav ys-lb-next" aria-label="다음">›</button>'
                 + '<div class="ys-lb-cap"></div>'
                 + '<span class="ys-lb-count"></span>';
    document.body.appendChild(lb);
    const lbImg = lb.querySelector("img");
    const lbPrev = lb.querySelector(".ys-lb-prev");
    const lbNext = lb.querySelector(".ys-lb-next");
    const lbCount = lb.querySelector(".ys-lb-count");
    const lbCap = lb.querySelector(".ys-lb-cap");
    let lbList = [], lbCaps = [], lbIdx = 0;   // 현재 섹션 이미지 src·캡션 목록 + 인덱스

    function lbShow(i) {
      if (!lbList.length) return;
      lbIdx = Math.max(0, Math.min(i, lbList.length - 1));   // 클램프(순환 안 함)
      lbImg.src = lbList[lbIdx];
      lbCap.innerHTML = lbCaps[lbIdx] || "";   // 이미지 하단 캡션(넘버링 위)
      const multi = lbList.length > 1;
      lbPrev.style.display = (multi && lbIdx > 0) ? "" : "none";                 // 처음이면 이전 숨김
      lbNext.style.display = (multi && lbIdx < lbList.length - 1) ? "" : "none"; // 끝이면 다음 숨김
      lbCount.style.display = multi ? "" : "none";
      lbCount.textContent = multi ? `${lbIdx + 1}/${lbList.length}` : "";
    }
    const lbClose = () => lb.classList.remove("open");

    lb.addEventListener("click", (e) => { if (e.target === lb) lbClose(); });  // 배경만 닫기
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
      lbShow(imgs.indexOf(im));
      lb.classList.add("open");
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
  }
  if (document.body) _setupKeyframeUI();
  else document.addEventListener("DOMContentLoaded", _setupKeyframeUI);

  global.YS.setupKeyframeUI = _setupKeyframeUI;
})(window);
