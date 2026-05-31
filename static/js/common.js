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

  /** 마크다운 → HTML. YAML 프론트매터 제거 + CJK 강조 보정 후 marked.js. */
  function renderMarkdown(src) {
    src = String(src || '').replace(/^---\n[\s\S]*?\n---\n?/, '');
    src = _fixCjkEmphasis(src);
    if (global.marked && typeof global.marked.parse === 'function') {
      return global.marked.parse(src);
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
        const parts = u.pathname.split('/');
        const i = parts.findIndex(p => p === 'shorts' || p === 'embed' || p === 'live');
        if (i !== -1 && parts[i + 1]) return parts[i + 1];
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
})(window);
