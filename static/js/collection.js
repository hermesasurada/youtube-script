/* 영상 모음 화면(templates/collection.html, 2026-11-19) — 모음(카드 2열) ↔ 보기(왼쪽 영상 목록 + 오른쪽 요약).
   요약은 이력 화면 팝업으로 넘기지 않고 이 페이지 안에서 연다(사용자 지시). 본문은 요약 창과 같은 렌더러(YS.renderMarkdown
   →제목 번역·목차 생략·섹션 고정·인명 소개·섹션 메모)를 쓴다. 주소는 /collection/<slug>?v=<item_id> — 카드에서 열면 기록을
   하나 쌓아 뒤로가기가 카드 화면으로 돌아오고, 보기 안에서 다른 영상으로 옮길 때는 기록을 바꿔 쓴다. */
(function () {
  const SLUG = window.COLLECTION_SLUG;
  if (!SLUG) return;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtDur = s => { s = Math.round(s || 0); if (!s) return ''; const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}` : `${m}:${String(x).padStart(2,'0')}`; };
  const fmtDay = d => { if (!d) return ''; const t = new Date(d + 'T12:00:00'); return `${t.getMonth() + 1}월 ${t.getDate()}일(${'일월화수목금토'[t.getDay()]})`; };
  const STATE = {pending: '처리 대기', processing: '처리 중', deferred: '재시도 대기', failed: '처리 실패', kf_retry: '캡처 재시도'};
  const UNREAD_KEY = 'ytCollectionUnread';

  const gridView = $('col-grid-view'), viewer = $('col-viewer'), side = $('col-side');
  const body = $('sum-panel-body'), panel = $('sum-panel');
  let data = null, tries = 0, current = 0, loadToken = 0, summaryRead = false;
  let unreadOnly = false;
  try { unreadOnly = localStorage.getItem(UNREAD_KEY) === '1'; } catch (_) {}

  const allItems = () => (data ? data.days.flatMap(d => d.items) : []);
  const openable = it => it.state === 'done' && it.item_id && it.has_summary;
  const findItem = id => allItems().find(it => it.item_id === id);
  const timeLabel = it => it.time ? `${esc(it.time)} ${it.time_src === 'upload' ? '업로드' : '현지'}` : '';

  /* 썸네일 — YouTube에 지금 걸린 것(hqdefault 480×360). 생중계 영상은 서버 사본(/thumb)이 방송 전 기본 그림으로 남아
     있어 쓰지 않는다(못 받을 때만 대신). 4:3 그림의 위아래 검은 띠는 16:9 상자(object-fit: cover)가 잘라 낸다. */
  function thumb(it) {
    const yt = encodeURIComponent(it.yt_id);
    return `<div class="col-thumb"><img src="https://i.ytimg.com/vi/${yt}/hqdefault.jpg" alt="" loading="lazy" onerror="this.onerror=null;this.src='/thumb/${yt}'">`
      + (it.duration ? `<span class="dur">${fmtDur(it.duration)}</span>` : '') + '</div>';
  }
  function badge(it) {
    if (!openable(it)) return `<span class="col-badge">${esc(STATE[it.state] || '요약 없음')}</span>`;
    return it.is_read ? '<span class="col-badge">읽음</span>' : '<span class="col-badge unread">안읽음</span>';
  }

  /* ── 카드 화면 ── */
  function card(it) {
    const meta = `<div class="col-meta"><div class="col-line">${timeLabel(it)}${it.time ? ' ' : ''}${badge(it)}</div>`
      + `<div class="col-name">${esc(it.short_title)}</div>${it.short_ko ? `<div class="col-ko">${esc(it.short_ko)}</div>` : ''}</div>`;
    if (!openable(it)) return `<div class="col-card wait" title="${esc(it.title)}">${thumb(it)}${meta}</div>`;
    return `<a class="col-card${it.is_read ? ' read' : ''}" href="?v=${it.item_id}" data-id="${it.item_id}" title="${esc(it.title)}">${thumb(it)}${meta}</a>`;
  }
  function renderGrid() {
    $('col-unread').setAttribute('aria-pressed', unreadOnly ? 'true' : 'false');
    const c = data.counts;
    $('col-stats').textContent = `요약 ${c.done}편 · 읽음 ${c.read}편` + (c.waiting ? ` · 처리 전 ${c.waiting}편` : '');
    const html = data.days.map(day => {
      const list = day.items.filter(it => !unreadOnly || (openable(it) && !it.is_read));
      if (!list.length) return '';
      return `<section class="col-day"><h2>${esc(day.label)} <span class="d">${esc(fmtDay(day.date))}</span></h2>`
        + `<div class="col-grid">${list.map(card).join('')}</div></section>`;
    }).join('');
    $('col-days').innerHTML = html || '<p class="col-empty">보여 줄 영상이 없습니다.</p>';
    const note = $('col-note');
    note.hidden = !data.filling;
    note.textContent = '일부 영상의 방송 시각을 받는 중이라 그날 맨 뒤에 두었습니다. 잠시 뒤 순서가 맞춰집니다.';
  }

  /* ── 보기 화면: 왼쪽 목록 ── */
  function renderSide() {
    side.innerHTML = data.days.map(day => `<h3>${esc(day.label)} <span class="d">${esc(fmtDay(day.date))}</span></h3>`
      + day.items.map(it => {
        const cls = ['col-row', openable(it) ? '' : 'wait', it.is_read ? 'read' : '', it.item_id === current ? 'active' : '']
          .filter(Boolean).join(' ');
        const dot = openable(it) && !it.is_read ? '<span class="dot" title="안읽음"></span>' : '';
        const line = [timeLabel(it), openable(it) ? '' : esc(STATE[it.state] || '')].filter(Boolean).join(' · ');
        const inner = `${thumb(it)}<div class="col-row-txt"><div class="col-name">${esc(it.short_title)}</div>`
          + `<div class="col-line">${line}</div></div>${dot}`;
        return openable(it)
          ? `<a class="${cls}" href="?v=${it.item_id}" data-id="${it.item_id}" title="${esc(it.title)}">${inner}</a>`
          : `<div class="${cls}" title="${esc(it.title)}">${inner}</div>`;
      }).join('')).join('');
    const act = side.querySelector('.col-row.active');
    if (act) act.scrollIntoView({block: 'nearest'});
    const list = allItems().filter(openable);
    const i = list.findIndex(it => it.item_id === current);
    $('col-prev').disabled = i <= 0;
    $('col-next').disabled = i < 0 || i >= list.length - 1;
  }

  function setSideOpen(on) { viewer.classList.toggle('side-open', !!on); }

  /* ── 화면 전환(주소와 함께) ── */
  function showGrid(how) {
    current = 0;
    viewer.hidden = true;
    gridView.hidden = false;
    $('col-side-toggle').hidden = true;
    $('col-unread').hidden = false;
    setSideOpen(false);
    document.title = $('col-back').closest('.col-top').querySelector('h1').textContent;
    if (how === 'replace') history.replaceState({}, '', location.pathname);
    if (data) renderGrid();
  }
  function openItem(id, how) {
    const it = findItem(id);
    if (!it || !openable(it)) return showGrid(how === 'none' ? 'none' : 'replace');
    const fromGrid = viewer.hidden;
    current = id;
    gridView.hidden = true;
    viewer.hidden = false;
    $('col-side-toggle').hidden = false;
    $('col-unread').hidden = true;                       // '안읽음만'은 카드 화면의 거르기다
    setSideOpen(false);
    const url = `${location.pathname}?v=${id}`;
    if (how === 'push') history.pushState({colView: 1}, '', url);
    else if (how === 'replace') history.replaceState(history.state && history.state.colView ? history.state : {}, '', url);
    renderSide();
    if (fromGrid || how !== 'none') loadSummary(it);
  }
  /* 보기에서 카드 화면으로 — 카드에서 열었으면 그 기록으로 돌아가고(뒤로가기와 같은 결과), 주소로 바로 열었으면 바꿔 쓴다. */
  function backToGrid() {
    if (history.state && history.state.colView) history.back();
    else showGrid('replace');
  }

  /* ── 요약 본문(요약 창과 같은 렌더) ── */
  function setReadUI(isRead) {
    summaryRead = !!isRead;
    const btn = $('sum-read-btn');
    btn.classList.toggle('active', summaryRead);
    btn.querySelector('.read-label').textContent = summaryRead ? '읽음' : '읽음 처리';
    btn.title = summaryRead ? '읽음 상태입니다. 누르면 안읽음으로' : '읽음으로 표시';
  }
  async function loadSummary(it) {
    const token = ++loadToken;
    document.title = it.short_ko || it.short_title;
    body.innerHTML = '<p class="sum-loading">불러오는 중…</p>';
    body.scrollTop = 0;
    $('sum-youtube-link').hidden = true;
    setReadUI(it.is_read);
    try {
      const [d] = await Promise.all([YS.apiSummaryContent(it.item_id), YS.ensureReaderAssets(), YS.ensureTermExclusions()]);
      if (token !== loadToken) return;                 // 그사이 다른 영상을 골랐다
      if (d.error) throw new Error(d.error);
      YS.setSummaryNotes(it.item_id, d.notes);
      body.innerHTML = YS.renderMarkdown(d.content || '');
      YS.applyTitleTranslation(body, d.title_ko || '');
      YS.stripSummaryPopupChrome(body);
      YS.setupStickySummarySections(body);
      YS.decoratePeople(body);
      YS.attachSummaryNotes(body, it.item_id);
      body.scrollTop = 0;
      updateProgress();
      const link = $('sum-youtube-link');
      link.hidden = !d.webpage_url;
      link.href = d.webpage_url || '#';
      setReadUI(d.is_read);
    } catch (e) {
      if (token === loadToken) body.innerHTML = `<p class="sum-error">오류: ${esc(e.message)}</p>`;
    }
  }
  function updateProgress() {
    const bar = $('sum-progress');
    const max = body.scrollHeight - body.clientHeight;
    bar.style.width = max > 4 ? Math.min(100, body.scrollTop / max * 100) + '%' : '0%';
  }
  body.addEventListener('scroll', updateProgress, {passive: true});

  async function toggleRead() {
    const it = findItem(current);
    if (!it) return;
    const next = !summaryRead;
    const btn = $('sum-read-btn');
    btn.disabled = true;
    try {
      if (!await YS.apiMarkRead(it.item_id, next)) throw new Error('상태 변경 실패');
      it.is_read = next;
      data.counts.read += next ? 1 : -1;
      setReadUI(next);
      renderSide();
    } catch (e) {
      console.warn('[read]', e);
    } finally {
      btn.disabled = false;
    }
  }
  function step(dir) {
    const list = allItems().filter(openable);
    const i = list.findIndex(it => it.item_id === current);
    const nx = list[i + dir];
    if (nx) openItem(nx.item_id, 'replace');
  }

  /* ── 글자 크기·글꼴 — 요약 창과 같은 저장값(sumReaderScale·sumReaderFont)을 쓴다 ── */
  const R_MIN = 0.8, R_MAX = 1.6, R_STEP = 0.08;
  let scale = parseFloat(localStorage.getItem('sumReaderScale')) || 1;
  if (!(scale >= R_MIN && scale <= R_MAX)) scale = 1;
  const applyScale = () => panel.style.setProperty('--reader-scale', scale.toFixed(3));
  function adjustScale(dir) {
    scale = Math.round(Math.min(R_MAX, Math.max(R_MIN, scale + dir * R_STEP)) * 1000) / 1000;
    try { localStorage.setItem('sumReaderScale', String(scale)); } catch (_) {}
    applyScale();
  }
  const RF = window.HermesReaderFonts || {resolve: () => null, apply() {}, fillSelect() {}, get: () => null};
  let font = RF.resolve(localStorage.getItem('sumReaderFont'), {legacy: {sans: 'noto'}, fallback: 'noto'});
  function applyFont() {
    RF.apply(panel, font);
    const sel = $('sum-font-select');
    if (sel && !sel.options.length) RF.fillSelect(sel);
    if (sel && font && sel.value !== font) sel.value = font;
  }
  $('sum-font-select').addEventListener('change', e => {
    if (!RF.get(e.target.value)) return;
    font = e.target.value;
    try { localStorage.setItem('sumReaderFont', font); } catch (_) {}
    applyFont();
  });
  applyScale();
  applyFont();

  /* ── 이벤트 ── */
  $('col-days').addEventListener('click', e => {
    const a = e.target.closest('a.col-card[data-id]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    openItem(Number(a.dataset.id), 'push');
  });
  side.addEventListener('click', e => {
    const a = e.target.closest('a.col-row[data-id]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    openItem(Number(a.dataset.id), 'replace');
  });
  $('col-back').addEventListener('click', e => {
    if (viewer.hidden) return;                          // 카드 화면의 뒤로는 모음 목록(링크 그대로)
    e.preventDefault();
    backToGrid();
  });
  $('col-side-toggle').addEventListener('click', () => setSideOpen(!viewer.classList.contains('side-open')));
  $('col-side-backdrop').addEventListener('click', () => setSideOpen(false));
  $('col-prev').addEventListener('click', () => step(-1));
  $('col-next').addEventListener('click', () => step(1));
  $('col-fs-down').addEventListener('click', () => adjustScale(-1));
  $('col-fs-up').addEventListener('click', () => adjustScale(1));
  $('sum-read-btn').addEventListener('click', toggleRead);
  $('col-unread').addEventListener('click', () => {
    unreadOnly = !unreadOnly;
    try { localStorage.setItem(UNREAD_KEY, unreadOnly ? '1' : '0'); } catch (_) {}
    if (data) renderGrid();
  });
  document.addEventListener('keydown', e => {
    if (viewer.hidden || e.target.closest('input,textarea,select,[contenteditable]')) return;
    if (e.key === 'Escape') {
      if (viewer.classList.contains('side-open')) setSideOpen(false);
      else backToGrid();
    }
  });
  window.addEventListener('popstate', () => {
    const id = Number(new URLSearchParams(location.search).get('v'));
    if (id) openItem(id, 'none');
    else showGrid('none');
  });

  async function load() {
    try {
      const r = await fetch(`/collection/${encodeURIComponent(SLUG)}/items`, {cache: 'no-store'});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
      data = d;
      if (!viewer.hidden) renderSide();
      else renderGrid();
      if (data.filling && ++tries <= 6) setTimeout(load, 15000);   // 시각을 받는 동안만 몇 번 다시 묻는다
      return true;
    } catch (e) {
      $('col-stats').textContent = '불러오지 못했습니다: ' + e.message;
      return false;
    }
  }
  load().then(ok => {
    if (!ok) return;
    const id = Number(new URLSearchParams(location.search).get('v'));
    if (id) openItem(id, 'replace');
  });
})();
