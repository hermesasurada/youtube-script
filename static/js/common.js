/* youtube-script 공유 프론트엔드 유틸 (index/mobile/summary 공용).
 *
 * marked는 작은 로컬 자원으로 선로드하고, KaTeX/본문용 웹폰트는 첫 요약 뒤에 붙인다.
 * 목적: 마크다운 렌더링·이스케이프·날짜/유튜브ID·이력 API 호출을 한 곳에 모아
 *       템플릿 간 중복(및 "한쪽만 수정됨" 류 버그)을 제거한다.
 */
(function (global) {
  'use strict';

  // ── 요약 뷰어 자원 지연 로딩 ─────────────────────────────────────
  let _readerAssetsPromise = null;
  let _markedConfigured = false;
  let _secondaryAssetsStarted = false;

  function _configureMarked() {
    if (_markedConfigured || !global.marked || typeof global.marked.use !== 'function') return;
    const renderer = new global.marked.Renderer();
    renderer.link = function (href, title, text) {
      const h = (typeof href === 'object') ? href.href : href;
      const t = (typeof href === 'object') ? (href.text || h) : (text || h);
      return `<a href="${h}" target="_blank" rel="noopener noreferrer">${t}</a>`;
    };
    global.marked.use({ renderer, breaks: false, gfm: true });
    _markedConfigured = true;
  }

  function _loadStyle(href, id) {
    if (document.getElementById(id)) return Promise.resolve();
    return new Promise(resolve => {
      const el = document.createElement('link');
      el.id = id; el.rel = 'stylesheet'; el.href = href;
      el.onload = resolve; el.onerror = resolve;
      document.head.appendChild(el);
    });
  }

  function _loadScript(src, id, ready) {
    if (ready()) return Promise.resolve();
    const existing = document.getElementById(id);
    if (existing) return new Promise(resolve => {
      existing.addEventListener('load', resolve, { once: true });
      existing.addEventListener('error', resolve, { once: true });
    });
    return new Promise(resolve => {
      const el = document.createElement('script');
      el.id = id; el.src = src; el.async = true;
      el.onload = resolve; el.onerror = resolve;
      document.head.appendChild(el);
    });
  }

  function ensureReaderAssets() {
    if (_readerAssetsPromise) return _readerAssetsPromise;
    const versions = global.YS_ASSET_VERSIONS || {};
    const fontVersion = versions.googleCss ? `?v=${encodeURIComponent(versions.googleCss)}` : '';
    const markedVersion = versions.marked ? `?v=${encodeURIComponent(versions.marked)}` : '';

    // 본문 렌더에 필수인 marked만 기다린다. CDN 왕복 없이 로컬 정적 파일을 사용한다.
    _readerAssetsPromise = _loadScript(
      '/static/vendor/marked.min.js' + markedVersion, 'ys-marked-js', () => !!global.marked
    ).then(() => {
      _configureMarked();

      // 폰트 CSS(944KB)와 수식 엔진은 요약 표시를 막지 않는다. 렌더 직후 시작해
      // 폰트는 swap으로 교체하고, KaTeX 미준비 시 수식은 기존 원문 폴백을 유지한다.
      if (!_secondaryAssetsStarted) {
        _secondaryAssetsStarted = true;
        setTimeout(() => {
          void _loadStyle('/static/fonts/google.css' + fontVersion, 'ys-reader-fonts');
          void _loadStyle('https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css', 'ys-katex-css');
          void _loadScript('https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js',
                           'ys-katex-js', () => !!global.katex);
        }, 600);
      }
    });
    return _readerAssetsPromise;
  }

  // 템플릿에서 marked를 먼저 로드한 경우 공통 renderer 설정을 즉시 적용한다.
  _configureMarked();

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
    // 백슬래시 이스케이프(\* \_ \~ \|) → 리터럴 엔티티로 선치환. 강조/틸드 정규식이
    // '궁수자리 A\*' 같은 이스케이프된 별표를 만나 매칭이 어긋나는 것 방지
    // (예: **궁수자리 A\*** → 예전엔 <strong>…A\</strong>* 로 깨짐). marked도 엔티티는 그대로 통과.
    // `\|` 는 유튜브 제목의 파이프가 표 칸을 쪼개지 않게 한다.
    src = src.replace(/\\([*_~|])/g, (_, ch) => '&#' + ch.charCodeAt(0) + ';');
    // 틸드 무력화: 본문 취소선 금지 정책 + 한국어 범위표현(예: 5~8개)이 GFM 단일틸드
    // 취소선으로 오인돼 두 범위 사이가 통째로 <del> 되는 것 방지. 엔티티라 화면엔 '~'로 표시.
    // (코드 스팬은 위에서 stash로 보호됨)
    src = src.replace(/~/g, '&#126;');
    src = src.replace(/\*\*([^\s*][^\n*]*?[^\s*]|[^\s*])\*\*/g, (_, t) => '<strong>' + t + '</strong>');
    src = src.replace(/(^|[^*])\*([^\s*][^\n*]*?[^\s*]|[^\s*])\*(?!\*)/g,
                      (_, p, t) => p + '<em>' + t + '</em>');
    src = src.replace(/\x01C(\d+)\x01/g, (_, i) => stash[+i]);
    return src;
  }

  /**
   * GFM 표는 헤더 다음 구분행(`| --- | --- |`)이 없으면 표가 아니라 <p>로 붕괴한다.
   * 요약 모델이 구분행을 빼먹으면 메타정보 칩이 깨지므로 삽입한다.
   */
  function _ensureGfmTables(src) {
    const stash = [];
    src = String(src || '').replace(/```[\s\S]*?```/g, (m) => {
      stash.push(m);
      return '\x01T' + (stash.length - 1) + '\x01';
    });
    src = src.replace(/(^|\n)((?:\|[^\n]*\n)+)/g, (all, pre, block) => {
      const trimmed = block.endsWith('\n');
      const lines = (trimmed ? block.slice(0, -1) : block).split('\n');
      if (lines.length < 2) return all;
      if (/^\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[1])) return all;
      const cols = Math.max(0, (lines[0].match(/\|/g) || []).length - 1);
      if (cols < 2) return all;
      lines.splice(1, 0, '|' + ' --- |'.repeat(cols));
      return pre + lines.join('\n') + (trimmed ? '\n' : '');
    });
    return src.replace(/\x01T(\d+)\x01/g, (_, i) => stash[+i]);
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
  function _decorateSummary(html, model, compress) {
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
      // 컴팩트 모드(모바일): 칩 텍스트를 줄이고 정보/링크 두 줄로 고정 배치
      const compact = !!global.YS_META_COMPACT;
      let durHtml = kv['길이'] ? kv['길이'].innerHTML : '';
      if (compact && durHtml && /분/.test(durHtml)) {
        durHtml = durHtml.replace(/\s*\d+초\s*$/, '');   // '1시간 58분 14초' → '1시간 58분'
      }
      const row1 = [], row2 = [];
      if (kv['업로더']) row1.push(`<span class="ys-chip ys-chip-up">${kv['업로더'].innerHTML}</span>`);
      if (kv['날짜'])   row1.push(`<span class="ys-chip">${kv['날짜'].innerHTML}</span>`);
      if (durHtml)      row1.push(`<span class="ys-chip ys-chip-dur">${durHtml}</span>`);
      const urlA = kv['URL'] && kv['URL'].querySelector('a');
      if (urlA) row2.push(`<a class="ys-chip ys-chip-link" href="${urlA.href}" target="_blank" rel="noopener noreferrer">${compact ? 'YouTube' : 'YouTube에서 보기 ↗'}</a>`);
      if (model) row2.push(`<span class="ys-chip ys-chip-model" title="이 요약을 생성한 LLM 모델">🧠 ${escapeHtml(model)}</span>`);
      if (compress) row2.push(`<span class="ys-chip ys-chip-compress" title="요약본 글자수 / 전사 원문 글자수">🗜 ${compact ? '' : '원문 대비 '}${escapeHtml(compress)}%</span>`);
      const chips = row1.concat(row2);
      if (chips.length) {
        const bar = document.createElement('div');
        bar.className = 'ys-meta' + (compact ? ' ys-meta-compact' : '');
        bar.innerHTML = compact
          ? `<div class="ys-meta-row">${row1.join('')}</div><div class="ys-meta-row">${row2.join('')}</div>`
          : chips.join('');
        tbl.replaceWith(bar);
        metaH.remove();
      }
    }

    // 제목 고정용 래퍼 — 첫 h1을 감싼다. applyTitleTranslation이 원문 병기를
    // h1 바로 뒤(afterend)에 넣으므로 병기도 자연히 이 래퍼 안에 들어온다.
    const h1 = tpl.content.querySelector('h1');
    if (h1) {
      const wrap = document.createElement('div');
      wrap.className = 'sum-head-sticky';
      h1.replaceWith(wrap);
      wrap.appendChild(h1);
    }

    const tldrH = h2s.find(h => /한눈\s*요약/.test(h.textContent));
    if (tldrH && tldrH.nextElementSibling && tldrH.nextElementSibling.tagName === 'UL') {
      const ul = tldrH.nextElementSibling;
      // 기본 접힘(open 미설정) — 본문부터 읽고 필요할 때 펼친다(2026-09-03 사용자 지시).
      const card = document.createElement('details');
      card.className = 'ys-tldr';
      card.innerHTML = '<summary class="ys-tldr-label"><span class="ys-tldr-chev" aria-hidden="true">▸</span>✦ 한눈 요약</summary>';
      ul.replaceWith(card);
      card.appendChild(ul);
      tldrH.remove();
    }
    return tpl.innerHTML;
  }

  /** 마크다운 → HTML. YAML 프론트매터 제거 + CJK 강조 보정 후 marked.js + 소제목·요약 꾸미기. */
  function renderMarkdown(src) {
    src = String(src || '').replace(/^---\n[\s\S]*?\n---\n?/, '');
    // 요약 모델 표기 → 메타 칩(YouTube 보기 옆)으로 이동, 본문에선 제거.
    let model = '';
    // (신) HTML 주석 마커
    src = src.replace(/<!--\s*SUMMARY_MODEL:([\s\S]*?)-->\s*/, (_, m) => { model = m.trim(); return ''; });
    // (구) 화면에 보이던 '*🧠 요약 모델: ...*' 라인(상단 또는 하단 --- 구분선과 함께)도 흡수
    src = src.replace(/(?:\n*---[ \t]*\n*)?\*\s*🧠\s*요약\s*모델:\s*([^*\n]+?)\s*\*[ \t]*/g,
      (_, m) => { if (!model) model = m.trim(); return ''; });
    // 압축률 마커 → '원문 대비 N%' 칩
    let compress = '';
    src = src.replace(/<!--\s*SUMMARY_COMPRESS:(\d+)\s*-->\s*/, (_, p) => { compress = p; return ''; });
    // LaTeX 수식 stash: marked·강조보정이 \_ \* \\ 등을 훼손하므로 먼저 빼둔다.
    // 디스플레이 \[...\] → \(...\) → $$...$$ 순. 복원은 marked·꾸미기 이후 KaTeX로.
    const math = [];
    const _stashMath = (tex, display) => { math.push({ tex, display }); return '\x01K' + (math.length - 1) + '\x01'; };
    src = src.replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => _stashMath(t, true));
    src = src.replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => _stashMath(t, true));
    src = src.replace(/\\\(([\s\S]+?)\\\)/g, (_, t) => _stashMath(t, false));
    src = _ensureGfmTables(_fixCjkEmphasis(src));
    let html;
    if (global.marked && typeof global.marked.parse === 'function') {
      html = _decorateSummary(_decorateHeadings(global.marked.parse(src)), model, compress);
    } else {
      html = '<pre>' + escapeHtml(src) + '</pre>';    // marked 미로딩 시 최소 폴백
    }
    if (math.length) html = html.replace(/\x01K(\d+)\x01/g, (_, i) => _renderMath(math[+i]));
    return html;
  }

  /** LaTeX 조각 → KaTeX HTML. KaTeX 미로딩/파싱 실패 시 원문을 코드로 노출(가독 유지). */
  function _renderMath(m) {
    if (global.katex && typeof global.katex.renderToString === 'function') {
      try {
        return global.katex.renderToString(m.tex, { displayMode: m.display, throwOnError: false, output: 'html' });
      } catch (_) { /* 폴백으로 */ }
    }
    const t = escapeHtml(m.tex);
    return m.display ? '<pre class="ys-math-raw">' + t + '</pre>' : '<code class="ys-math-raw">' + t + '</code>';
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
  function _historyRef(ref, legacyKey) {
    return Number.isInteger(ref) ? { item_id: ref } : { [legacyKey]: ref };
  }

  async function apiMarkRead(ref, isRead) {
    const r = await fetch('/history/mark_read', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ..._historyRef(ref, 'txt_path'), is_read: isRead }),
    });
    return r.ok ? r.json() : null;
  }

  async function apiBookmark(ref, on) {
    const r = await fetch('/history/bookmark', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ..._historyRef(ref, 'txt_path'), bookmark: on }),
    });
    return r.ok ? r.json() : null;
  }

  async function apiDeleteItem(ref) {
    const r = await fetch('/history/item', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_historyRef(ref, 'txt_path')),
    });
    return r.ok ? r.json() : null;
  }

  /* 제목·썸네일을 지금 유튜브 기준으로 다시 가져온다. 실패해도 서버가 기존 값을
     유지하므로 호출 측은 결과의 changed/notes만 보고 알리면 된다. */
  async function apiRefreshMeta(ref) {
    const r = await fetch('/history/refresh-meta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_historyRef(ref, 'path')),
    });
    return r.json();
  }

  /* 요약을 블로그스팟에 발행(즉시 공개). 서버가 hermes_blogger로 올리고 items에 기록한다. */
  async function apiPublishBlog(ref, title, html) {
    const r = await fetch('/history/publish-blog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ..._historyRef(ref, 'path'), title, html }),
    });
    return r.json();
  }

  async function apiSummaryContent(ref) {
    const r = await fetch('/summary/content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_historyRef(ref, 'path')),
    });
    return r.json();
  }

  /* ── 구글 블로거 포스팅용 변환 ────────────────────────────────────────
     블로거 본문은 외부 CSS/클래스를 못 쓰므로 **인라인 style**만 유효하다.
     이미지(kf-strip)는 /sframe 로컬 URL이라 블로그에서 깨지므로 제외한다.
     섹션·본문·강조·시각 라벨은 그대로 두고 서식만 입힌다.
     반환: {html, text} — 클립보드에 text/html + text/plain 동시 적재용. */
  const _BL = {           // 블로거 본문 인라인 스타일 팔레트(테마와 무관하게 읽히도록 보수적으로)
    h2:   'margin:2em 0 .7em;padding-bottom:.35em;border-bottom:2px solid #e8e3d8;font-size:1.35em;font-weight:700;line-height:1.4;',
    // 소제목: 본문과 같은 글자 크기로 두되 굵기 + 라임색 형광펜 밑줄로 구분한다.
    // border-bottom은 항상 글자 '아래'에만 그어져 겹칠 수 없어서, 아래쪽 일부만 칠하는
    // 배경 그라디언트로 글자에 살짝 물리게 했다(형광펜 효과).
    // inline-block이라 글자 길이만큼만 칠해진다. 대신 위아래 margin이 인접 문단과
    // 상쇄되지 않으므로(블록끼리면 상쇄됨) 위 여백을 그만큼 줄여 잡았다.
    h3:   'display:inline-block;margin:1.1em 0 .475em;padding:0 .12em .02em;'
        + 'background-image:linear-gradient(transparent 62%, #dcf7a4 62%);'
        + 'font-size:1em;font-weight:700;line-height:1.45;color:#1a1a1a;',
    // 본문은 font-weight를 명시한다 — 블로거 편집기가 붙여넣기 HTML을 span으로 재구성하면서
    // 소제목의 굵기(700)가 뒤따르는 본문까지 번지는 것을 막는다.
    // 본문 글자 크기는 여기서만 준다(2026-08-30 사용자 지시: 본문 15px). 래퍼는
    // 16px 그대로라 소제목(h3, 1em)·h2·푸터는 종전 크기를 유지한다 — 래퍼를
    // 줄이면 소제목까지 같이 작아진다.
    p:    'margin:0 0 1.6em;line-height:1.9;font-weight:400;font-size:15px;',
    li:   'margin:0 0 .7em;line-height:1.85;font-weight:400;font-size:15px;',
    ul:   'margin:0 0 1.6em;padding-left:1.3em;font-weight:400;',
    foot: 'margin:2.5em 0 0;padding-top:1em;border-top:1px solid #e8e3d8;font-size:.85em;color:#8a8279;line-height:1.7;',
  };

  /* 요약 md → 블로그 발행용 본문 DOM.
     남기는 것: '핵심 내용'의 소제목(h3)과 본문. 버리는 것: 이미지·마커·H1·메타표·한눈 요약·목차.
     반환: {root(DocumentFragment), title, url} */
  function _summaryBodyDom(md) {
    let src = String(md || '');
    // 1) 앱 전용 마커·이미지 스트립 제거(마커는 화면 칩용, 이미지는 로컬 URL이라 외부에서 깨짐)
    src = src.replace(/<!--\s*SUMMARY_(?:MODEL|COMPRESS):[\s\S]*?-->\s*/g, '');   // 화면 칩용 마커
    src = src.replace(/<div class="kf-strip">[\s\S]*?<\/div>\s*/g, '');
    src = src.replace(/^---\n[\s\S]*?\n---\n?/, '');            // YAML 프론트매터

    // 2) 마크다운 → HTML (앱 장식 없이 순수 변환. CJK 볼드·수식 보정은 공용 로직 재사용)
    src = _ensureGfmTables(_fixCjkEmphasis(src));
    const math = [];                                            // 수식은 KaTeX 대신 원문 유지(블로거엔 KaTeX 없음)
    src = src.replace(/\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]/g, (_, a, b) => {
      math.push(a || b); return '\x01M' + (math.length - 1) + '\x01';
    });
    let html = (global.marked && global.marked.parse) ? global.marked.parse(src) : escapeHtml(src);
    if (math.length) html = html.replace(/\x01M(\d+)\x01/g, (_, i) => '<i>' + escapeHtml(math[+i]) + '</i>');

    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    const root = tpl.content;

    // 3) 섹션 정리: 본문(핵심 내용의 소제목+본문)만 남긴다.
    //    메타정보 표는 출처 URL만 뽑아 쓰고 제거, 한눈 요약·목차 섹션은 통째로 제거,
    //    '핵심 내용' 헤더는 그 아래 소제목만 남기면 되므로 헤더만 제거.
    let title = '', url = '', brief = null;   // brief = 한눈 요약 불릿(현재 내보내기에선 미사용)
    const h1 = root.querySelector('h1');
    if (h1) { title = h1.textContent.trim(); h1.remove(); }     // 제목은 블로거 '제목' 칸에 넣도록 본문에선 제외
    root.querySelectorAll('details').forEach(d => d.remove());  // 접이식 목차(있을 경우)

    const DROP_SEC = /한눈\s*요약|목\s*차|TL;?DR/i;              // 섹션 전체를 버릴 대상
    [...root.querySelectorAll('h2')].forEach(h => {
      const t = h.textContent.replace(/^\s*\d+\.\s*/, '');
      if (/메타\s*정보/.test(t)) {
        const tbl = h.nextElementSibling;
        if (tbl && tbl.tagName === 'TABLE') {
          const kv = {};
          tbl.querySelectorAll('tr').forEach(tr => {
            const c = tr.querySelectorAll('td,th');
            if (c.length >= 2) kv[c[0].textContent.trim()] = c[1].textContent.trim();
          });
          url = kv['URL'] || '';                                 // 출처 표기용으로만 보관
          tbl.remove();
        }
        h.remove();
      } else if (DROP_SEC.test(t)) {
        let n = h.nextElementSibling;                            // 다음 h2 전까지가 그 섹션
        const isTldr = /한눈\s*요약|TL;?DR/i.test(t);
        h.remove();
        while (n && n.tagName !== 'H2') {
          const nx = n.nextElementSibling;
          // 한눈 요약의 불릿은 버리지 않고 따로 보관한다(블로거 복사에서 상단 박스로 쓴다).
          if (isTldr && !brief && (n.tagName === 'UL' || n.tagName === 'OL')) brief = n.cloneNode(true);
          n.remove();
          n = nx;
        }
      } else if (/핵심\s*내용/.test(t)) {
        h.remove();                                              // 헤더만 제거, 하위 소제목·본문은 유지
      }
    });

    // 4) 소제목 정리 — 번호 접두어와 영상 시각([mm:ss])은 내보내기에 불필요
    root.querySelectorAll('h2').forEach(h => {
      h.textContent = h.textContent.replace(/^\s*\d+\.\s*/, '');
    });
    root.querySelectorAll('h3').forEach(h => {
      const m = h.textContent.match(/^([\s\S]*?)\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*$/);
      if (m) h.textContent = m[1].trim();
    });
    root.querySelectorAll('img,figure,figcaption').forEach(el => el.remove());   // 잔여 이미지 방어
    return { root, title, url, brief };
  }

  function mdToBloggerHtml(md) {
    const { root, title, url, brief } = _summaryBodyDom(md);

    // 블로거는 외부 CSS/클래스가 안 먹으므로 모든 서식을 인라인 style로 준다.
    root.querySelectorAll('h2').forEach(h => h.setAttribute('style', _BL.h2));
    root.querySelectorAll('h3').forEach(h => h.setAttribute('style', _BL.h3));
    root.querySelectorAll('p').forEach(p => p.setAttribute('style', _BL.p));
    root.querySelectorAll('ul,ol').forEach(u => u.setAttribute('style', _BL.ul));
    root.querySelectorAll('li').forEach(li => li.setAttribute('style', _BL.li));
    root.querySelectorAll('a').forEach(a => {
      a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener');
      a.setAttribute('style', 'color:#b0413e;text-decoration:underline;');
    });
    root.querySelectorAll('blockquote').forEach(b => b.setAttribute('style',
      'margin:0 0 1.3em;padding:.2em 0 .2em 1em;border-left:3px solid #e0d9cc;color:#666;'));
    root.querySelectorAll('code').forEach(c => c.setAttribute('style',
      'background:#f4f1eb;padding:.1em .35em;border-radius:3px;font-size:.92em;'));
    // 강조는 굵기를 명시(본문 400 지정과 짝을 이뤄 편집기 변환에도 살아남게)
    root.querySelectorAll('strong,b').forEach(s => s.setAttribute('style', 'font-weight:700;'));

    // 5) 조립: 본문 → 출처 푸터
    const body = document.createElement('div');
    body.setAttribute('style', 'font-size:16px;font-weight:400;color:#242424;word-break:keep-all;');
    body.appendChild(root);
    // 섹션 제거로 남은 앞뒤 빈 텍스트 노드 정리(붙여넣기 시 불필요한 빈 줄 방지)
    while (body.firstChild && body.firstChild.nodeType === 3 && !body.firstChild.textContent.trim()) body.firstChild.remove();
    while (body.lastChild && body.lastChild.nodeType === 3 && !body.lastChild.textContent.trim()) body.lastChild.remove();
    const firstH = body.querySelector('h2,h3');   // 문서 첫 소제목은 위 여백 제거 (박스 삽입 전에 잡는다)
    if (firstH) firstH.setAttribute('style', firstH.getAttribute('style').replace(/margin:[^;]+;/, 'margin:0 0 .425em;'));

    // 한눈 요약은 블로거 복사에서 제외한다(2026-08-26) — 본문 소제목부터 시작.

    if (url) {                                     // 출처는 원본 영상 링크만 남긴다
      const foot = document.createElement('div');
      foot.setAttribute('style', _BL.foot);
      foot.innerHTML = `원본 영상: <a href="${attrEscape(url)}" target="_blank" rel="noopener" style="color:#b0413e;">${attrEscape(url)}</a>`;
      body.appendChild(foot);
    }

    // 6) 서식 붙여넣기가 안 되는 편집기를 위한 평문 대체본
    const text = body.textContent.replace(/\n{3,}/g, '\n\n').trim();
    return { html: body.outerHTML, text, title };
  }

  /* 외국어 제목이면 렌더된 본문의 H1을 '번역 제목 + 원문(작고 연하게)' 2단으로 바꾼다.
     원문은 지우지 않고 아래에 남겨, 검색·대조가 되게 한다. */
  function applyTitleTranslation(rootEl, titleKo) {
    const ko = (titleKo || '').trim();
    if (!rootEl || !ko) return false;
    const h1 = rootEl.querySelector('h1');
    if (!h1) return false;
    const orig = h1.textContent.trim();
    if (!orig || orig === ko) return false;
    h1.textContent = ko;
    const sub = document.createElement('div');
    sub.className = 'sum-title-orig';
    sub.textContent = orig;
    h1.insertAdjacentElement('afterend', sub);
    return true;
  }

  /** 요약 원문은 보존하고 팝업에서만 중복 안내 요소를 제거한다. */
  function stripSummaryPopupChrome(rootEl) {
    if (!rootEl) return;
    rootEl.querySelectorAll('.sum-toc').forEach(el => el.remove());
    // YouTube 링크는 팝업 최상단 액션으로 제공하므로 본문 메타에서 중복 제거한다.
    rootEl.querySelectorAll('.ys-chip-link').forEach(el => el.remove());
    rootEl.querySelectorAll('.ys-meta-row').forEach(row => {
      if (!row.children.length) row.remove();
    });
    rootEl.querySelectorAll('.ys-meta').forEach(meta => {
      if (!meta.children.length) meta.remove();
    });
    [...rootEl.querySelectorAll('h2')].forEach(h => {
      const label = h.textContent.replace(/\s+/g, '').replace(/^\d+[.)]?/, '');
      if (label === '핵심내용') h.remove();
    });
  }

  global.YS = {
    renderMarkdown,
    mdToBloggerHtml,
    escapeHtml,
    attrEscape,
    fmtDate,
    ytVideoId,
    apiMarkRead,
    apiBookmark,
    apiDeleteItem,
    apiSummaryContent,
    apiRefreshMeta,
    apiPublishBlog,
    ensureReaderAssets,
    applyTitleTranslation,
    stripSummaryPopupChrome,
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
/* ── 외국어 제목의 원문 병기(번역 제목이 H1, 원문은 그 아래 작게) ── */
.sum-title-orig{margin:-.55rem 0 1.15rem;font-size:.8em;font-weight:400;line-height:1.5;color:var(--muted,#8a8279);opacity:.85;word-break:keep-all;}
/* ── 제목 고정 래퍼(h1 + 원문 병기) — 배경·패딩은 각 화면 CSS가 지정 ── */
.sum-head-sticky{position:sticky;top:0;z-index:6;}
/* 상·하 여백은 래퍼 패딩이 전담한다. 내부 마진을 남기면 원문 병기 유무에 따라
   아래쪽만 달라진다(h1 margin-bottom vs 병기의 음수 margin-top이 상쇄). */
.sum-head-sticky h1{margin:0 !important;}
.sum-head-sticky .sum-title-orig{margin:.28rem 0 0 !important;}
/* ── 메타 칩 헤더(메타정보 표 → 변환) ── */
.ys-meta{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;margin:.4rem 0 1.4rem;padding-bottom:1.1rem;border-bottom:1px solid var(--border,#e5e5e5);}
.ys-meta-compact{flex-direction:column;align-items:flex-start;gap:.45rem;}
.ys-meta-row{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;width:100%;}
.ys-chip{display:inline-flex;align-items:center;gap:.35em;font-size:.78rem;color:var(--muted,#666);background:var(--surface2,#f3f0e9);border:1px solid var(--border,#e5e5e5);border-radius: 2px;padding:.26rem .72rem;line-height:1.25;}
.ys-chip-up{color:var(--text,#222);font-weight:600;}
.ys-chip-dur{font-family:ui-monospace,monospace;font-size:.72rem;letter-spacing:.02em;}
.ys-chip a{color:inherit;text-decoration:none;}
a.ys-chip-link{color:var(--highlight,#2563eb);border-color:color-mix(in oklab,var(--highlight,#2563eb) 38%,transparent);background:var(--highlight-soft,rgba(99,102,241,.08));text-decoration:none;font-weight:600;transition:filter .15s;}
a.ys-chip-link:hover{filter:brightness(1.12);text-decoration:none;}
.ys-chip-model{font-family:ui-monospace,monospace;font-size:.72rem;letter-spacing:.01em;}
.ys-chip-compress{font-family:ui-monospace,monospace;font-size:.72rem;letter-spacing:.01em;}
/* ── 한눈 요약 callout ── */
.ys-tldr{position:relative;margin:1.2rem 0 1.7rem;padding:1rem 1.25rem 1.05rem 1.35rem;background:linear-gradient(135deg,var(--highlight-soft,rgba(99,102,241,.08)),transparent 78%),var(--surface2,#f6f3ec);border:1px solid var(--border,#e5e5e5);border-radius: 2px;overflow:hidden;}
.ys-tldr::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(180deg,var(--highlight,#2563eb),color-mix(in oklab,var(--highlight,#2563eb) 35%,transparent));}
.ys-tldr-label{font-size:.7rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--highlight,#2563eb);margin-bottom:.55rem;list-style:none;cursor:pointer;user-select:none;display:flex;align-items:center;gap:.4rem;}
.ys-tldr-label::-webkit-details-marker{display:none;}
.ys-tldr-chev{font-size:.6rem;line-height:1;transition:transform .15s;}
.ys-tldr[open] .ys-tldr-chev{transform:rotate(90deg);}
.ys-tldr:not([open]){padding-bottom:.85rem;}
.ys-tldr:not([open]) .ys-tldr-label{margin-bottom:0;}
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
/* 소제목 우측 시각 pill은 표시하지 않는다(2026-09-03 사용자 지시). 요소는 남긴다 —
   목차(_tocLabel)와 시각 이동이 이 값을 읽는다. */
.kf-time{display:none;margin-left:auto;flex-shrink:0;color:var(--muted,#999);font-weight:600;font-size:.7em;letter-spacing:.02em;font-family:ui-monospace,monospace;background:var(--surface,#fff);border:1px solid var(--border,#e5e5e5);padding:.14em .55em;border-radius: 2px;}
/* 요약 소제목(h3) 리본: 좌측 강조 바 + 틴트, 시각 pill은 우측 정렬 */
.sum-md h3,.md-body h3,.markdown h3{display:flex;align-items:center;gap:.5em;background:linear-gradient(90deg,var(--highlight-soft,rgba(99,102,241,.1)),transparent 88%);border-left:3px solid var(--highlight,var(--accent,#6366f1));padding:.5rem .8rem;border-radius: 2px;margin:1.7rem 0 .75rem;}
/* LLM이 본문 단락 바로 뒤에 넣는 문맥형 용어 해설. 세 리더 화면에서 동일하게 보인다. */
.sum-md .term-note,.md-body .term-note,.markdown .term-note{font-size:.82em!important;line-height:1.55;color:var(--muted,#7a7f87)!important;margin:-.28rem 0 1rem!important;padding-left:.72rem;border-left:2px solid var(--border,#d8dadd);}
.sum-md .term-note strong,.md-body .term-note strong,.markdown .term-note strong{color:inherit;}`;
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
