/* ── Hermes 색상 모드 — sa·td·wm 공용 ─────────────────────────────────────
   세 저장소에 같은 파일이 있다(sa static/hermes-theme.js, td static/hermes-theme.js,
   wm static/hermes-theme.js). 한쪽을 고치면 나머지도 맞춘다.

   선택값(시스템·라이트·다크)은 localStorage 'hermes-theme'에 두고, 실제로 그릴
   색은 <html data-theme="light|dark">로 늘 확정해 둔다. 시스템이면 OS 설정을
   따라가고 바뀌면 즉시 반영한다. CSS는 [data-theme="dark"] 하나만 보면 된다.

   <head>에서 defer 없이 불러야 첫 화면이 깜빡이지 않는다.
   버튼: HermesTheme.mount(부모요소, {className, before}) — 누르면 세 항목 메뉴가 뜬다.
   className을 주면 앱의 헤더 아이콘 버튼 모양을 그대로 쓴다. */
(function (global) {
  'use strict';
  const KEY = 'hermes-theme';
  const root = document.documentElement;
  const media = global.matchMedia ? global.matchMedia('(prefers-color-scheme: dark)') : null;
  const ICONS = {
    system: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/><path d="M12 16v4"/>',
    light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/>'
      + '<path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/>'
      + '<path d="m19.07 4.93-1.41 1.41"/>',
    dark: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  };
  const MODES = [['system', '시스템'], ['light', '라이트'], ['dark', '다크']];
  const svg = mode => '<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + ICONS[mode] + '</svg>';
  const buttons = [];
  let menu = null;

  function preference() {
    try {
      const v = global.localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : 'system';
    } catch (e) { return 'system'; }
  }
  function resolved(pref) {
    const p = pref || preference();
    if (p !== 'system') return p;
    return media && media.matches ? 'dark' : 'light';
  }
  function apply() {
    const pref = preference();
    const theme = resolved(pref);
    // 바꾸는 순간 카드마다 걸린 transition이 제각각 흘러 반쯤 바뀐 화면이 보인다 — 한 프레임 끈다.
    if (root.getAttribute('data-theme') && root.getAttribute('data-theme') !== theme) {
      root.classList.add('hermes-theme-switching');
      global.requestAnimationFrame(() => global.requestAnimationFrame(() =>
        root.classList.remove('hermes-theme-switching')));
    }
    root.setAttribute('data-theme', theme);
    root.setAttribute('data-theme-pref', pref);
    buttons.forEach(paint);
    if (menu) menu.querySelectorAll('[data-mode]').forEach(el =>
      el.setAttribute('aria-checked', String(el.dataset.mode === pref)));
    try { global.dispatchEvent(new CustomEvent('hermes-theme', { detail: { pref, theme } })); } catch (e) { /* 구형 브라우저 */ }
  }
  function set(mode) {
    try {
      if (mode === 'light' || mode === 'dark') global.localStorage.setItem(KEY, mode);
      else global.localStorage.removeItem(KEY);
    } catch (e) { /* 저장 불가(사생활 모드) — 이번 화면에만 적용 */ root.setAttribute('data-theme-pref', mode); }
    apply();
  }
  function label(pref) {
    return (MODES.find(m => m[0] === pref) || MODES[0])[1];
  }
  function paint(btn) {
    const pref = preference();
    btn.innerHTML = svg(pref);
    btn.title = `색상 모드: ${label(pref)}`;
    btn.setAttribute('aria-label', `색상 모드: ${label(pref)}`);
  }

  function closeMenu() {
    if (!menu) return;
    menu.remove();
    menu = null;
    buttons.forEach(b => b.setAttribute('aria-expanded', 'false'));
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('keydown', onKey, true);
  }
  function onOutside(e) {
    if (menu && !menu.contains(e.target) && !buttons.some(b => b.contains(e.target))) closeMenu();
  }
  function onKey(e) {
    if (e.key === 'Escape') { const owner = menu && menu._owner; closeMenu(); if (owner) owner.focus(); }
  }
  function openMenu(btn) {
    closeMenu();
    const pref = preference();
    menu = document.createElement('div');
    menu.className = 'hermes-theme-menu';
    menu.setAttribute('role', 'menu');
    menu._owner = btn;
    menu.innerHTML = MODES.map(([mode, text]) =>
      `<button type="button" role="menuitemradio" data-mode="${mode}" aria-checked="${mode === pref}">`
      + `${svg(mode)}<span>${text}</span><span class="hermes-theme-check" aria-hidden="true">✓</span></button>`).join('');
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    const w = menu.offsetWidth;
    menu.style.top = `${Math.round(r.bottom + 6)}px`;
    menu.style.left = `${Math.round(Math.max(8, Math.min(r.right - w, global.innerWidth - w - 8)))}px`;
    menu.addEventListener('click', e => {
      const item = e.target.closest('[data-mode]');
      if (!item) return;
      set(item.dataset.mode);
      closeMenu();
      btn.focus();
    });
    btn.setAttribute('aria-expanded', 'true');
    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('keydown', onKey, true);
    const current = menu.querySelector('[aria-checked="true"]');
    if (current) current.focus();
  }

  /** parent 안에 색상 모드 버튼을 붙인다. before를 주면 그 요소 앞에 넣는다. */
  function mount(parent, opts) {
    if (!parent) return null;
    const o = opts || {};
    const btn = document.createElement('button');
    btn.type = 'button';
    // 앱의 기존 아이콘 버튼 클래스를 주면 그 모양을 그대로 쓰고, 없으면 기본 모양.
    btn.className = 'hermes-theme-trigger ' + (o.className || 'hermes-theme-btn');
    btn.setAttribute('aria-haspopup', 'menu');
    btn.setAttribute('aria-expanded', 'false');
    btn.addEventListener('click', () => (menu && menu._owner === btn ? closeMenu() : openMenu(btn)));
    parent.insertBefore(btn, o.before || null);
    buttons.push(btn);
    paint(btn);
    return btn;
  }

  if (media) {
    const onChange = () => { if (preference() === 'system') apply(); };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }
  // 다른 탭에서 바꾸면 따라간다.
  global.addEventListener('storage', e => { if (e.key === KEY) apply(); });
  global.addEventListener('resize', closeMenu);
  apply();

  // 상단 고정 영역(.hermes-gnb)이 붙어 있는 동안 밑줄을 긋는다(hermes-theme.css).
  function markStuck() {
    const stuck = (global.scrollY || 0) > 0;
    document.querySelectorAll('.hermes-gnb').forEach(el => el.classList.toggle('is-stuck', stuck));
  }
  global.addEventListener('scroll', markStuck, { passive: true });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', markStuck);
  else markStuck();
  global.addEventListener('load', markStuck);   // 새로고침 뒤 스크롤 위치 복원까지 반영

  global.HermesTheme = { mount, set, preference, resolved: () => resolved() };
})(window);
