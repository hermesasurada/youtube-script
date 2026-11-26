// 모바일 이력 화면(/m) 로직 — 2026-11-16 리팩토링 3단계에서 templates/mobile.html 인라인 <script>를 옮겼다.
// 공용 기능은 common.js(window.YS). 이 파일은 일반 <script>로 실려 전역 함수(onclick 등)를 그대로 쓴다.
const SVG = {
  more:       '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.9"/><circle cx="12" cy="12" r="1.9"/><circle cx="19" cy="12" r="1.9"/></svg>',
  eye:        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  eye_off:    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
};

const PAGE = 20;
let ALL = [], FILTERED = [], shownCount = 0, HISTORY_REVISION = '';
/* 미읽음만 보기 — 데스크톱과 같은 'histFilter' 키를 공유해 기기 간 상태가 이어진다 */
function _savedFilter() {
  try { return JSON.parse(localStorage.getItem('histFilter') || 'null') || {}; }
  catch { return {}; }
}
function _saveUnread() {
  try {
    localStorage.setItem('histFilter', JSON.stringify({
      ..._savedFilter(), unread: _unreadOnly, bookmark: _bookmarkOnly, distill: _distillOnly, uploader: _uploader,
      collection: _collection }));
  } catch { /* 스토리지 불가(사파리 프라이빗 등) — 무시 */ }
}
let _unreadOnly = !!_savedFilter().unread;
let _bookmarkOnly = !!_savedFilter().bookmark;
let _distillOnly = !!_savedFilter().distill;   // 증류 대상 + 게시일 7일 이내(데스크톱과 같은 histFilter.distill)
let _uploader = String(_savedFilter().uploader || '');   // 채널 선택(데스크톱과 같은 histFilter.uploader)
let _collection = String(_savedFilter().collection || '');   // 영상 모음(2026-11-22, PC와 같은 histFilter.collection)
let _uploaderTabM = '';   // 채널 팝업에서 '영상 모음' 탭을 보는 중이면 'coll'
let _sort = localStorage.getItem('histSort') || 'desc';   // 'desc'=최신순(기본), 'asc'=과거순 (데스크톱과 공유)
let _sortKey = localStorage.getItem('histSortKey') || 'date';  // 'date'=전사 처리일, 'upload'=영상 게시일 (데스크톱과 공유)
/* 정렬 기준값 — 게시일이 없으면 처리일로 대체(순서 뒤엉킴 방지) */
const SORT_KEYS = ['date', 'upload', 'duration'];   // 기준 버튼이 순환하는 순서(데스크톱과 동일)
// 길이는 숫자로 비교한다(문자열로 두면 '9:59'가 '1:02:00'보다 뒤로 간다).
const _sortValue = i => (_sortKey === 'duration')
  ? (Number(i.duration) || 0)
  : (_sortKey === 'upload' ? (i.upload_date || i.date || '') : (i.date || '')) + ' ' + (i.stem || '');
/* 카드에 찍을 날짜 — 선택한 정렬 기준을 따른다 */
const histDisplayDate = i => (_sortKey === 'upload' ? (i.upload_date || i.date || '') : (i.date || ''));

const $ = id => document.getElementById(id);
// 순수 헬퍼는 공유 모듈(common.js, YS)로 일원화 — 위임만.
const esc = s => YS.escapeHtml(s);
const attrEsc = s => YS.attrEscape(s);
const ytVideoId = url => YS.ytVideoId(url);

async function loadHistory(force) {
  $('state').textContent = '로드 중…';
  try {
    const url = (!force && HISTORY_REVISION)
      ? '/history?revision=' + encodeURIComponent(HISTORY_REVISION) : '/history';
    const r = await fetch(url, { cache: force ? 'no-store' : 'default' });
    const d = await r.json();
    if (!d.unchanged) ALL = d.items || [];
    if (d.revision) HISTORY_REVISION = d.revision;
  } catch (e) {
    $('state').textContent = '오류: ' + e.message;
    return;
  }
  await YS.collections.load(force);   // 영상 모음별 이력 id(모음 필터용)
  _updateSortBtn();   // 저장된 정렬값 → 토글 버튼 라벨 반영
  $('unread-filter-btn').classList.toggle('active', _unreadOnly);
  $('unread-filter-btn').setAttribute('aria-pressed', String(_unreadOnly));   // 저장된 미읽음 상태 반영
  applyFilter();
}

function applyFilter() {
  const q = $('q').value.trim().toLowerCase();
  FILTERED = ALL.filter(i => {
    if (YS.isPendingDelete(i.item_id)) return false;   // 되돌리기 대기 중인 삭제
    if (_uploader && i.uploader !== _uploader) return false;
    if (_collection && !YS.collections.has(_collection, i.item_id)) return false;
    if (q && !(i.title||'').toLowerCase().includes(q) && !(i.title_ko||'').toLowerCase().includes(q)
          && !(i.uploader||'').toLowerCase().includes(q)) return false;
    if (_unreadOnly && i.is_read) return false;
    if (_bookmarkOnly && !i.bookmark) return false;
    if (_distillOnly && !(i.distill && YS.withinRecentDays(i, 7))) return false;   // 증류 대상 + 게시일 7일 이내
    return true;
  });
  // 정렬: 기준(_sortKey)에 따라 전사 처리일 또는 영상 게시일. desc=최신순, asc=과거순.
  FILTERED.sort((a, b) => {
    const ka = _sortValue(a), kb = _sortValue(b);
    const c = ka < kb ? -1 : ka > kb ? 1 : 0;
    return _sort === 'asc' ? c : -c;
  });
  shownCount = 0;
  $('grid').innerHTML = '';
  appendPage();
  $('count').textContent = `${FILTERED.length}건`;
  _paintUploaderBtnM();
  _paintFilterChips();
}

/* 켜진 필터를 글자 칩으로(2026-11-20) — 검색 줄 버튼은 아이콘뿐이라 무엇이 켜졌는지 알기 어려웠다. 누르면 해제 */
function _paintFilterChips() {
  const chips = [];
  const coll = _collection && YS.collections.get(_collection);
  if (coll)          chips.push(['collection', coll.title]);
  if (_uploader)     chips.push(['uploader', _uploader]);
  if (_unreadOnly)   chips.push(['unread', '미읽음만']);
  if (_bookmarkOnly) chips.push(['bookmark', '북마크만']);
  if (_distillOnly)  chips.push(['distill', '증류만']);
  $('filter-chips').innerHTML = chips.map(([k, label]) =>
    `<button type="button" class="filter-chip" data-filter="${k}" aria-label="${attrEsc(label)} 필터 끄기"><span>${esc(label)}</span><b aria-hidden="true">✕</b></button>`).join('');
}
document.getElementById('filter-chips').addEventListener('click', e => {
  const b = e.target.closest('.filter-chip');
  if (!b) return;
  const k = b.dataset.filter;
  if (k === 'uploader') { _uploader = ''; _saveUnread(); applyFilter(); }
  else if (k === 'collection') { _collection = ''; _saveUnread(); applyFilter(); }
  else if (k === 'unread') toggleUnreadFilter();
  else if (k === 'bookmark') toggleBookmarkFilter();
  else if (k === 'distill') toggleDistillFilter();
});

/* 보기 방식 — 카드(썸네일 크게) / 목록(썸네일 작게, 한 화면에 여러 개). 저장은 localStorage ytMobileView */
let _listView = localStorage.getItem('ytMobileView') === 'list' ? 'list' : 'card';
function setListView(v) {
  _listView = v === 'list' ? 'list' : 'card';
  try { localStorage.setItem('ytMobileView', _listView); } catch { /* 저장 불가 — 이번 화면에만 적용 */ }
  _paintListView();
}
function _paintListView() {
  $('grid').classList.toggle('compact', _listView === 'list');
  $('view-card-btn').setAttribute('aria-pressed', String(_listView === 'card'));
  $('view-list-btn').setAttribute('aria-pressed', String(_listView === 'list'));
}
_paintListView();

/* ── 채널 선택(2026-10-11 사용자 지시, 데스크톱 팝업과 같은 내용): 채널마다 미읽음·전체 건수 ── */
function _paintUploaderBtnM() {
  const b = $('uploader-filter-btn');
  if (!b) return;
  const on = !!(_uploader || _collection || _bookmarkOnly || _distillOnly);   // 북마크·증류도 이 팝업 안에서 건다(2026-11-22)
  b.classList.toggle('active', on);
  b.setAttribute('aria-pressed', String(on));
  b.title = _uploader ? `채널: ${_uploader}` : '채널 · 영상 모음 · 북마크 · 증류';
  $('q').placeholder = _uploader ? `${_uploader} 안에서 검색` : '검색';
}
// 안읽은 영상의 원본 길이 합계(2026-10-15 사용자 지시) — '3시간 12분'·'45분'
function fmtUnreadDur(secs) { return YS.fmtUnreadDur(secs); }
function _uploaderStatsM() { return YS.uploaderSort.stats(ALL); }
function openUploaderM() {
  $('fs-ctrl').hidden = true;
  $('font-ctrl').hidden = true;
  openModal('채널 선택', 'utility');
  _uploaderTabM = _collection ? 'coll' : '';   // 모음을 걸어 둔 상태면 모음 탭부터
  $('modal-body').className = 'modal-body uploader-pick';
  $('modal-body').innerHTML = '<input type="search" id="uploader-q" class="uploader-q" placeholder="채널 검색" autocomplete="off">'
    // 북마크만·증류만은 검색 줄 아이콘에서 이 팝업으로(2026-11-22 사용자 지시, PC와 같다)
    + '<div class="uploader-flags" role="group" aria-label="함께 거르기">'
    + '<button type="button" class="uploader-flag" id="bookmark-filter-btn" onclick="toggleBookmarkFilter()" aria-pressed="false">북마크만</button>'
    + '<button type="button" class="uploader-flag" id="distill-filter-btn" onclick="toggleDistillFilter()" aria-pressed="false">증류만</button></div>'
    + '<div class="uploader-sort" id="uploader-sort" role="group" aria-label="채널 정렬 · 영상 모음">'
    + '<button type="button" data-sort="unread" onclick="setUploaderSortM(this.dataset.sort)">안읽은 개수</button>'
    + '<button type="button" data-sort="secs" onclick="setUploaderSortM(this.dataset.sort)">길이 합계</button>'
    + '<button type="button" data-sort="avg" onclick="setUploaderSortM(this.dataset.sort)">평균 길이</button>'
    + '<button type="button" data-sort="coll" onclick="setUploaderSortM(this.dataset.sort)">영상 모음</button></div>'
    + '<div class="uploader-list" id="uploader-list" role="listbox" aria-label="채널"></div>';
  _paintFlagBtnsM();
  $('uploader-q').addEventListener('input', _renderUploaderM);
  _renderUploaderM();
}
// 채널 정렬 기준·통계는 common.js YS.uploaderSort(PC와 같은 규칙·같은 localStorage ytUploaderSort)
function _uploaderSortModeM() { return YS.uploaderSort.mode(); }
function setUploaderSortM(mode) {
  if (mode === 'coll') { _uploaderTabM = 'coll'; _renderUploaderM(); return; }   // 영상 모음 탭
  _uploaderTabM = '';
  YS.uploaderSort.set(mode);
  _renderUploaderM();
}
function _paintFlagBtnsM() {
  [['bookmark-filter-btn', _bookmarkOnly], ['distill-filter-btn', _distillOnly]].forEach(([id, on]) => {
    const b = $(id);
    if (b) { b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); }
  });
}
/* 영상 모음 탭 — 모음 하나를 고르면 그 모음의 요약된 영상만(채널 선택은 풀린다) */
function _renderCollectionsM() {
  const list = YS.collections.list();
  const row = (slug, label, sub, n) => `<button type="button" class="uploader-item${slug === _collection ? ' selected' : ''}" role="option"
      aria-selected="${slug === _collection}" data-coll="${attrEsc(slug)}" onclick="pickCollectionM(this.dataset.coll)">
      <span class="up-name">${esc(label)}${sub ? `<small class="up-sub">${esc(sub)}</small>` : ''}</span>${n != null ? `<span class="up-total">${n}</span>` : ''}</button>`;
  $('uploader-list').innerHTML = row('', '모음 전체(해제)', '', null)
    + list.map(c => row(c.slug, c.title, c.subtitle, c.ids.size)).join('')
    + (list.length ? '' : '<div class="uploader-empty">영상 모음이 없습니다</div>');
}
function pickCollectionM(slug) {
  _collection = slug || '';
  if (_collection) _uploader = '';
  _saveUnread();
  closeModal();
  applyFilter();
  window.scrollTo({ top: 0 });
}
function _uploaderDurM(st, mode) { return YS.uploaderSort.durText(st, mode); }
function _renderUploaderM() {
  const q = ($('uploader-q').value || '').trim().toLowerCase();
  const stats = _uploaderStatsM();
  const mode = _uploaderSortModeM();
  document.querySelectorAll('#uploader-sort button').forEach(b =>
    b.setAttribute('aria-pressed', String(_uploaderTabM === 'coll' ? b.dataset.sort === 'coll' : b.dataset.sort === mode)));
  $('uploader-q').hidden = _uploaderTabM === 'coll';
  if (_uploaderTabM === 'coll') { _renderCollectionsM(); return; }
  const names = YS.uploaderSort.order(stats, mode);
  const all = { total: ALL.length, unread: [...stats.values()].reduce((n, s) => n + s.unread, 0),
                unreadSecs: [...stats.values()].reduce((n, s) => n + s.unreadSecs, 0) };
  const row = (val, label, st) => `<button type="button" class="uploader-item${val === _uploader ? ' selected' : ''}" role="option"
      aria-selected="${val === _uploader}" data-uploader="${attrEsc(val)}" onclick="pickUploaderM(this.dataset.uploader)">
      <span class="up-name">${esc(label)}</span>${st.unread ? `<span class="up-unread">${st.unread}</span>`
        + (_uploaderDurM(st, mode) ? `<span class="up-dur">${_uploaderDurM(st, mode)}</span>` : '') : ''}<span class="up-total">${st.total}</span></button>`;
  const list = names.filter(n => !q || n.toLowerCase().includes(q));
  $('uploader-list').innerHTML = (q ? '' : row('', '전체 채널', all)) + list.map(n => row(n, n, stats.get(n))).join('')
    + (list.length || !q ? '' : '<div class="uploader-empty">일치하는 채널이 없습니다</div>');
  if (!q) $('uploader-list').querySelector('.uploader-item')?.classList.add('up-all');   // '전체 채널'은 목록 위에 고정(2026-11-22)
}
function pickUploaderM(uploader) {
  _uploader = uploader || '';
  if (_uploader) _collection = '';   // 채널을 고르면 모음 필터는 푼다
  _saveUnread();
  closeModal();
  applyFilter();
  window.scrollTo({ top: 0 });
}

/* 정렬 방향 아이콘 — 화살표 + 길이가 다른 막대로 오름/내림을 나타낸다 */
const SORT_ICON = {
  desc: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 16l4 4 4-4"/><path d="M7 20V4"/><path d="M13 4h8"/><path d="M13 9h6"/><path d="M13 14h4"/></svg>',
  asc:  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8l4-4 4 4"/><path d="M7 4v16"/><path d="M13 14h8"/><path d="M13 9h6"/><path d="M13 4h4"/></svg>',
};

const SORT_LABELS = {
  date:     { key: '전사처리일', asc: '과거순',  desc: '최신순' },
  upload:   { key: '영상게시일', asc: '과거순',  desc: '최신순' },
  duration: { key: '영상길이',   asc: '짧은순', desc: '긴순' },
};

function _updateSortBtn() {
  const L = SORT_LABELS[_sortKey] || SORT_LABELS.date;
  const b = $('m-sort-btn');
  if (b) {
    const asc = _sort === 'asc';
    b.innerHTML = asc ? SORT_ICON.asc : SORT_ICON.desc;
    b.title = asc ? L.asc : L.desc;
    b.setAttribute('aria-label', asc
      ? `${L.asc} 정렬 (누르면 ${L.desc})` : `${L.desc} 정렬 (누르면 ${L.asc})`);
  }
  const k = $('m-sortkey-btn');
  if (k) {
    k.textContent = L.key;
    k.classList.toggle('active', _sortKey !== 'date');
  }
}
function toggleSort() {
  _sort = (_sort === 'asc') ? 'desc' : 'asc';
  localStorage.setItem('histSort', _sort);
  _updateSortBtn();
  applyFilter();
}
/* 정렬 기준 전환 — 카드 날짜 표기도 이 기준을 따른다 */
/* 카드의 채널명 → 그 채널로 좁힌다. 같은 채널이 이미 걸려 있으면 해제한다. */
function filterByUploader(uploader, ev) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }
  if (!uploader) return;
  _uploader = (_uploader === uploader) ? '' : uploader;   // 채널 선택과 같은 상태(같은 채널이면 해제)
  _saveUnread();
  applyFilter();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function toggleSortKey() {
  _sortKey = SORT_KEYS[(SORT_KEYS.indexOf(_sortKey) + 1) % SORT_KEYS.length];
  localStorage.setItem('histSortKey', _sortKey);
  _updateSortBtn();
  applyFilter();
}

function toggleUnreadFilter() {
  _unreadOnly = !_unreadOnly;
  $('unread-filter-btn').classList.toggle('active', _unreadOnly);
  $('unread-filter-btn').setAttribute('aria-pressed', String(_unreadOnly));
  _saveUnread();
  applyFilter();
}

/* 썸네일은 전사 때 받아둔 로컬 사본(/thumb)만 쓴다 — 조회 중 외부(i.ytimg)로 나가는
   요청이 없어 VPN 경유에서도 즉시 뜬다. 사본이 없으면 자리표시로 대체한다. */
function toggleDistillFilter() {      // 채널 팝업 안 버튼·필터 칩에서 부른다(2026-11-22)
  _distillOnly = !_distillOnly;
  _paintFlagBtnsM();
  _saveUnread();
  applyFilter();
}

function toggleBookmarkFilter() {
  _bookmarkOnly = !_bookmarkOnly;
  _paintFlagBtnsM();
  _saveUnread();
  applyFilter();
}

async function toggleBookmark(btn) {
  const itemId = Number(btn.dataset.id);
  const next = btn.dataset.on !== '1';
  try {
    const result = await YS.apiBookmark(itemId, next);
    if (!result) return;
  } catch { return; }
  const item = ALL.find(i => i.item_id === itemId);
  if (item) item.bookmark = next;
  btn.dataset.on = next ? '1' : '0';
  btn.classList.toggle('on', next);
  if (_bookmarkOnly && !next) applyFilter();
}

function _thumbFallback(img) { YS.thumbFallback(img); }

function appendPage() {
  const slice = FILTERED.slice(shownCount, shownCount + PAGE);
  if (slice.length === 0) {
    $('state').textContent = FILTERED.length === 0
      ? (ALL.length === 0 ? '이력이 없습니다.' : '검색 결과가 없습니다.')
      : '';
    return;
  }
  const html = slice.map(renderCard).join('');
  $('grid').insertAdjacentHTML('beforeend', html);
  shownCount += slice.length;
  $('state').textContent = shownCount >= FILTERED.length ? '' : '스크롤해서 더 보기…';
}

function renderCard(item) {
  const vid = ytVideoId(item.webpage_url);
  const thumb = vid
    ? `<img class="hist-thumb" src="/thumb/${encodeURIComponent(vid)}" alt="" loading="lazy" data-vid="${attrEsc(vid)}" onerror="_thumbFallback(this)">`
    : `<div class="hist-thumb-placeholder">썸네일 없음</div>`;
  const dur = item.duration
    ? `<span class="hist-thumb-dur">${YS.fmtClock(item.duration)}</span>` : '';
  // 채널명 클릭 = 같은 채널로 좁히기. 모바일은 검색창이 하나뿐이라 거기에 채널명을 넣는다
  // (미읽음·북마크 필터는 그대로 유지된다).
  const uploader = item.uploader && item.uploader !== '—'
    ? `<button type="button" class="hist-card-uploader is-filter" data-uploader="${attrEsc(item.uploader)}"
         onclick="filterByUploader(this.dataset.uploader, event)">${esc(item.uploader)}</button>`
    : '';
  // 요약 보기는 카드 자체가 링크라 버튼이 중복이었다 — 그 자리에 북마크를 둔다
  // (2026-09-04 사용자 지시). 썸네일 우상단의 북마크 버튼은 없앤다.
  const bmBtn = `<button class="hist-icon-btn hist-icon-btn-bm${item.bookmark ? ' on' : ''}"
    data-id="${item.item_id}" data-on="${item.bookmark ? '1' : '0'}"
    onclick="event.stopPropagation();toggleBookmark(this)"
    aria-label="${item.bookmark ? '북마크 해제' : '북마크'}"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg></button>`;
  const readBtn = `<button class="hist-icon-btn hist-icon-btn-read${item.is_read ? ' is-read' : ''}"
    data-id="${item.item_id}" data-read="${item.is_read ? '1' : '0'}"
    onclick="event.stopPropagation();toggleRead(this)"
    aria-label="${item.is_read ? '읽음' : '안읽음'}">${item.is_read ? SVG.eye_off : SVG.eye}</button>`;
  // 덜 쓰는 동작(전사·YouTube·삭제)은 ⋯ 메뉴로(2026-11-20) — 삭제를 자주 누르는 버튼 옆에서 뗀다
  const moreBtn = `<button class="hist-icon-btn hist-icon-btn-more" data-id="${item.item_id}"
    onclick="event.stopPropagation();openCardMenuM(this)" aria-label="더 보기 — 전사 · YouTube · 삭제" aria-haspopup="menu">${SVG.more}</button>`;
  const T = YS.cardTitles(item);       // 번역 제목을 크게, 원제는 아래 작게

  const unreadDot = !item.is_read ? '<span class="hist-unread-dot"></span>' : '';
  const unreadClass = !item.is_read ? ' hist-card-unread' : '';
  const clickable = item.has_summary
    ? ` hist-card-clickable" data-id="${item.item_id}" data-title="${attrEsc(item.title)}" onclick="openSummary(Number(this.dataset.id),this.dataset.title)`
    : '';
  return `<article class="hist-card${unreadClass}${clickable}">
    <div class="hist-thumb-wrap">${thumb}${dur}</div>
    <div class="hist-card-body">
      <h3 class="hist-card-title">${unreadDot}${esc(T.main)}</h3>
      ${T.orig ? `<div class="hist-card-orig">${esc(T.orig)}</div>` : ''}
      <div class="hist-card-meta">${uploader}</div>
      <div class="hist-card-footer">
        <span class="hist-card-left"><span class="hist-card-date">${YS.fmtDateShort(histDisplayDate(item))}</span><span class="hist-card-id" data-id="${item.item_id}" title="클릭하면 id 복사">#${item.item_id}</span></span>
        <div class="hist-card-actions">${readBtn}${bmBtn}${moreBtn}</div>
      </div>
    </div>
  </article>`;
}

/* 카드 ⋯ 메뉴 — 목록 보기에서는 북마크 버튼이 숨겨져 있어 메뉴에 함께 넣는다 */
function openCardMenuM(btn) {
  const id = Number(btn.dataset.id);
  const item = ALL.find(i => i.item_id === id);
  if (!item) return;
  const items = [
    { label: '전사 보기', disabled: !item.has_txt, onClick: () => openTranscript(id, item.title) },
    { label: 'YouTube에서 열기', disabled: !item.webpage_url,
      onClick: () => window.open(item.webpage_url, '_blank', 'noopener') },
    { label: '삭제', danger: true, onClick: () => deleteCard({ dataset: { id: String(id) } }) },
  ];
  if (_listView === 'list') {
    const bm = btn.closest('.hist-card').querySelector('.hist-icon-btn-bm');
    items.unshift({ label: item.bookmark ? '북마크 해제' : '북마크', onClick: () => toggleBookmark(bm) });
  }
  YS.openCardMenu(btn, items);
}

async function toggleRead(btn) {
  const itemId  = Number(btn.dataset.id);
  const wasRead = btn.dataset.read === '1';
  const newRead = !wasRead;
  try {
    const result = await YS.apiMarkRead(itemId, newRead);
    if (!result) return;
    if (result.revision) HISTORY_REVISION = result.revision;
  } catch { return; }

  // ALL 배열 내 항목 업데이트
  const item = ALL.find(i => i.item_id === itemId);
  if (item) item.is_read = newRead;

  // 버튼 즉시 갱신
  btn.dataset.read = newRead ? '1' : '0';
  btn.innerHTML = newRead ? SVG.eye_off : SVG.eye;
  btn.classList.toggle('is-read', newRead);

  const card = btn.closest('article');
  if (card) {
    card.classList.toggle('hist-card-unread', !newRead);
    const dot = card.querySelector('.hist-unread-dot');
    if (newRead && dot) dot.remove();
    else if (!newRead && !dot) {
      const titleEl = card.querySelector('.hist-card-title');
      if (titleEl) titleEl.insertAdjacentHTML('afterbegin', '<span class="hist-unread-dot"></span>');
    }
  }

  // 미읽음 필터 활성 중이면 읽음 처리된 카드 제거 — 토스트로 되돌리기(2026-11-03, 데스크톱과 같음)
  if (_unreadOnly && newRead) {
    applyFilter();
    YS.undoToast('읽음 처리했습니다', { onUndo: () => _undoReadM(itemId) });
  }
}

async function _undoReadM(itemId) {
  try {
    const result = await YS.apiMarkRead(itemId, false);
    if (!result) throw new Error();
    if (result.revision) HISTORY_REVISION = result.revision;
  } catch { alert('되돌리기 실패'); return; }
  const item = ALL.find(i => i.item_id === itemId);
  if (item) item.is_read = false;
  applyFilter();
}

// 확인창 없이 바로 빼고 6초 되돌리기 뒤 서버 삭제(2026-11-03, 데스크톱과 같음)
function deleteCard(btn) {
  const itemId = Number(btn.dataset.id);
  const idx = ALL.findIndex(i => i.item_id === itemId);
  if (idx === -1) return;
  const item = ALL[idx];
  YS.deleteWithUndo(itemId, {
    hide() { ALL = ALL.filter(i => i.item_id !== itemId); applyFilter(); },
    restore() {
      if (!ALL.some(i => i.item_id === itemId)) ALL.splice(Math.min(idx, ALL.length), 0, item);
      applyFilter();
    },
    committed(result) { if (result.revision) HISTORY_REVISION = result.revision; },
    failed() { loadHistory(true); },
  });
}

/* ── 모달 ── */
let _modalPushed = false;

function _updateReadProgress() {
  const el = $('modal-body');
  const max = el.scrollHeight - el.clientHeight;
  $('read-progress').style.width = max > 4 ? Math.min(100, el.scrollTop / max * 100) + '%' : '0%';
}
document.getElementById('modal-body').addEventListener('scroll', _updateReadProgress, { passive: true });

/* 요약을 아래로 읽어 내려갈 때는 하단 버튼 줄을 숨기고 제목의 원제 줄을 접는다(2026-11-20 — 고정 영역이
   화면의 4분의 1이었다). 위로 올리거나 맨 위·맨 끝에 닿으면 다시 보인다. 접고 펴면 본문 높이가 달라져
   브라우저가 스크롤 위치를 보정하면서 반대 방향 이벤트가 한 번 올 수 있어, 바꾼 직후 0.3초는 판정을 쉰다. */
let _readLastTop = 0, _readLockUntil = 0;
function _updateReadingChrome() {
  const el = $('modal-body'), m = $('modal');
  const top = el.scrollTop, d = top - _readLastTop;
  _readLastTop = top;
  if (!el.classList.contains('markdown')) { m.classList.remove('reading'); return; }
  if (performance.now() < _readLockUntil) return;
  const was = m.classList.contains('reading');
  let next = was;
  if (top < 60 || d < -8 || top + el.clientHeight >= el.scrollHeight - 40) next = false;
  else if (d > 8) next = true;
  if (next !== was) { m.classList.toggle('reading', next); _readLockUntil = performance.now() + 300; }
}
document.getElementById('modal-body').addEventListener('scroll', _updateReadingChrome, { passive: true });

function openModal(title, kind = 'content') {
  $('modal-body').textContent = '로드 중…';
  $('modal-body').className = 'modal-body';
  $('modal-body').scrollTop = 0;
  $('summary-actions').hidden = true;
  $('modal-youtube-link').hidden = true;
  $('modal-youtube-link').href = '#';
  $('modal-title').textContent = title || '';
  $('modal').classList.toggle('utility', kind === 'utility');
  $('read-progress').style.width = '0%';   // 열 때 진행바 초기화
  $('modal').classList.remove('reading');
  _readLastTop = 0;
  $('modal-bd').classList.add('open');
  $('modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  if (!_modalPushed) {
    history.pushState({ modal: true }, '');
    _modalPushed = true;
  }
}

function _closeModalUI() {
  $('modal').classList.remove('open');
  $('modal-bd').classList.remove('open');
  document.body.style.overflow = '';
}

function closeModal() {
  _closeModalUI();
  if (_modalPushed) {
    _modalPushed = false;
    history.back(); // 열 때 push한 state를 다시 pop
  }
}

/* ── 처리 큐(본편/캡처 대기 목록 · 순서 변경) ── */
const _Q_ST = {
  processing: '처리 중', pending: '대기', kf_retry: '캡처 재시도',
  deferred: '재시도 예약', done: '완료', failed: '실패',
};
// ── 각주 제외 용어(칩 나열) — 설정 팝업의 '각주 제외' 탭 ─────────────────
// 칩 툴팁: 각주에서 뺀 용어는 저장된 뜻(같은 표기의 다른 뜻은 남는다), 직접 입력한 용어는 모든 뜻
function _termSenseTitle(t) { return YS.termSenseTitle(t); }
async function renderTermsM() {
  const pane = $('modal-body').querySelector('[data-set-pane="terms"]');
  if (!pane) return;
  const terms = await YS.ensureTermExclusions(true);
  const chips = terms.length
    ? terms.map(t => `<span class="term-excl-chip" title="${YS.attrEscape(_termSenseTitle(t))}">${YS.escapeHtml(t)}`
        + `<button type="button" class="term-excl-del" data-term="${YS.attrEscape(t)}" aria-label="제거">✕</button></span>`).join('')
    : '<span class="term-excl-empty">아직 없음 — 요약 각주의 용어를 누르면 여기에 쌓입니다</span>';
  pane.innerHTML = `<p class="term-excl-note">화면·블로그·새 요약 모두에서 이 용어의 각주를 붙이지 않습니다. 각주 용어를 눌러 뺀 용어는 그 뜻일 때만 빼고(표기가 같아도 뜻이 다르면 남김), 직접 입력한 용어는 모든 뜻을 뺍니다.</p>`
    + `<div class="term-excl-list">${chips}</div>`
    + `<form class="term-excl-form" onsubmit="addTermM(event)"><input id="term-excl-input" type="text" placeholder="용어 직접 추가" maxlength="80" autocomplete="off"><button type="submit" class="term-excl-add">추가</button></form>`;
}
async function addTermM(ev) {
  ev.preventDefault();
  const term = ($('term-excl-input').value || '').trim();
  if (!term) return;
  try { await YS.apiTermExclusion(term, false); await renderTermsM(); }
  catch (e) { alert('추가 실패: ' + e.message); }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest && e.target.closest('.term-excl-del');
  if (!b) return;
  if (!confirm(`'${b.dataset.term}'을(를) 제외 목록에서 뺄까요?`)) return;
  try { await YS.apiTermExclusion(b.dataset.term, true); await renderTermsM(); }
  catch (err) { alert('제거 실패: ' + err.message); }
});

async function openQueueM() {
  $('fs-ctrl').hidden = true;
  $('font-ctrl').hidden = true;
  openModal('영상 처리 큐', 'utility');
  $('modal-body').className = 'modal-body queue';
  await loadQueueM();
}
let _mqPvTimer = null, _mqPv = null;
function mqPreviewDebounced() { clearTimeout(_mqPvTimer); _mqPvTimer = setTimeout(mqPreview, 350); }
async function mqPreview() {
  const url = $('mq-add-url').value.trim();
  const btn = $('mq-add-btn'), pv = $('mq-add-pv');
  _mqPv = null; btn.disabled = true;
  if (!url) { pv.innerHTML = ''; return; }
  pv.innerHTML = '<span class="mq-pv-wait">영상 정보 확인 중…</span>';
  try {
    const d = await (await fetch('/queue/preview?url=' + encodeURIComponent(url))).json();
    if ($('mq-add-url').value.trim() !== url) return;
    if (!d.ok) { pv.innerHTML = `<span class="mq-pv-err">${attrEsc(d.error || '조회 실패')}</span>`; return; }
    if (d.duplicate) {
      pv.innerHTML = `<span class="mq-pv-err">${d.duplicate === 'history' ? '이미 전사된 영상' : '이미 큐에 대기 중'} — ${attrEsc(d.title)}</span>`;
      return;
    }
    _mqPv = d;
    const seg = d.start_sec ? `<span class="mq-pv-seg">${attrEsc(d.start_label)}부터 전사</span>` : '';
    // 같은 채널을 지난번에 넣은 설정으로 캡처·증류 칩을 맞춘다(데스크톱과 같은 규칙).
    const applied = _mqApplyChannelPrefs(d.prefs);
    const note = applied ? `<span class="mq-pv-seg">${attrEsc(applied)}</span>` : '';
    pv.innerHTML = `<div class="mq-pv-ok"><b>${attrEsc(d.title)}</b><span>${attrEsc(d.channel || '')}</span>${seg}${note}</div>`;
    btn.disabled = false;
  } catch (e) { pv.innerHTML = `<span class="mq-pv-err">조회 실패</span>`; }
}
function _mqApplyChannelPrefs(prefs) {
  const set = (id, on) => {
    const b = $(id); if (!b) return;
    b.classList.toggle('on', !!on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  };
  set('mq-add-capture-btn', true);
  set('mq-add-distill-btn', true);
  if (!prefs || (prefs.capture === undefined && prefs.distill === undefined)) return '';
  const parts = [];
  if (prefs.capture !== undefined) { set('mq-add-capture-btn', prefs.capture); parts.push('캡처 ' + (prefs.capture ? 'ON' : 'OFF')); }
  if (prefs.distill !== undefined) { set('mq-add-distill-btn', prefs.distill); parts.push('증류 ' + (prefs.distill ? 'ON' : 'OFF')); }
  return `${prefs.source === 'channel' ? '채널 설정' : '지난 처리'} 따름 — ${parts.join(' · ')}`;
}
async function mqPaste() {
  const inp = $('mq-add-url');
  try {
    inp.value = ((await navigator.clipboard.readText()) || '').trim();
    mqPreviewDebounced();
  } catch (e) {
    inp.focus();
    $('mq-add-pv').innerHTML = '<span class="mq-pv-wait">자동 붙여넣기가 막혀 있습니다 — 직접 붙여넣어 주세요.</span>';
  }
}
async function mqAdd() {
  if (!_mqPv) return;
  $('mq-add-btn').disabled = true;
  const r = await fetch('/queue/items', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: 'https://www.youtube.com/watch?v=' + _mqPv.yt_id, title: _mqPv.title,
                          start_sec: _mqPv.start_sec || 0,
                          capture: $('mq-add-capture-btn').classList.contains('on'),
                          distill: $('mq-add-distill-btn').classList.contains('on')})});
  const d = await r.json();
  if (d.ok) { $('mq-add-url').value = ''; _mqPv = null; loadQueueM(); }
  else $('mq-add-pv').innerHTML = `<span class="mq-pv-err">${attrEsc(d.error || '등록 실패')}</span>`;
}

function mqHms(sec) { return YS.hms(sec); }

async function loadQueueM() {
  const body = $('modal-body');
  body.innerHTML = '<div class="ch-empty">불러오는 중…</div>';
  try {
    const d = await (await fetch('/queue/items')).json();
    const row = (v, i, movable, extraCtl = '') => {
      const ch = v.channel_name || '';   // 손으로 넣은 영상(channel_id 'manual')은 채널 칸을 비운다(2026-11-21 사용자 지시 — '수동' 글자 삭제)
      const fmtEta = t => { const d = new Date(t * 1000);
        return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; };
      const bits = [];
      if (v.status === 'deferred' && v.next_retry_at) bits.push(`${v.next_retry_at.slice(5, 16)} 재시도`);
      if (v.eta) bits.push(`~${fmtEta(v.eta)} 시작 예정`);
      if (v.start_sec) bits.push(`${mqHms(v.start_sec)}부터`);
      if (v.attempt_count > 1) bits.push(`시도 ${v.attempt_count}`);
      const extra = bits.join(' · ');
      const waitingRow = ['pending', 'kf_retry', 'deferred'].includes(v.status);
      // 캡처·증류 칩 — PC 큐와 같은 공용 표시(YS.queueFlag·queueResult)
      const canToggle = waitingRow && v.status !== 'kf_retry';
      const isRecent = ['done', 'failed'].includes(v.status);
      const capBtn = isRecent ? '' : YS.queueFlag('capture', v, { toggle: canToggle, cls: 'q-flag mq-flag',
        onclick: `mqToggleCapture(${v.id}, ${v.capture_on === false ? 'true' : 'false'})` });
      const dsBtn = isRecent ? '' : YS.queueFlag('distill', v, { toggle: canToggle, cls: 'q-flag mq-flag',
        onclick: `mqToggleDistill(${v.id}, ${v.distill_on === false ? 'true' : 'false'})` });
      const result = isRecent ? `<div class="q-result">${YS.queueResult(v, 'q-flag mq-flag')}</div>` : '';
      const ctl = (movable || capBtn || dsBtn || extraCtl) ? `<span class="mq-ctl">${capBtn}${dsBtn}${extraCtl}${movable ? `
          <button onclick="mqMove(${v.id},'top')" aria-label="맨 위로">⤒</button>
          <button onclick="mqMove(${v.id},'up')" aria-label="위로">↑</button>
          <button onclick="mqMove(${v.id},'down')" aria-label="아래로">↓</button>
          <button onclick="mqMove(${v.id},'bottom')" aria-label="맨 아래로">⤓</button>
          <button class="mq-del" onclick="mqCancel(${v.id})">×</button>` : ''}</span>` : '';
      return `<div class="mq-row">
        <span class="mq-st mq-${v.status}">${i ? i + ' · ' : ''}${_Q_ST[v.status] || v.status}</span>
        <div class="mq-main"><div class="mq-title">${attrEsc(v.title || v.yt_id)}</div>
          <div class="mq-sub">${attrEsc([ch, extra].filter(Boolean).join(' · '))}</div>${result}</div>${ctl}</div>`;
    };
    const main = d.waiting.filter(v => v.status !== 'kf_retry');
    const kf = d.waiting.filter(v => v.status === 'kf_retry');
    let n = 0, k = 0;
    const keepUrl = $('mq-add-url') ? $('mq-add-url').value : '';
    const keepPv = $('mq-add-pv') ? $('mq-add-pv').innerHTML : '';
    const mainRows = main.map(v => row(v, ++n, v.status === 'pending')).join('');
    const kfRows = kf.map(v => row(v, ++k, true)).join('');
    const recentRows = d.recent.map(v => row(v, '', false,
      v.status === 'failed' ? `<button class="mq-requeue" onclick="mqRequeue(${v.id})">↻</button>` : '')).join('');
    const group = (title, count, rows, empty = '') => `<section class="mq-group">`
      + `<div class="mq-sec"><span>${title}</span><span>${count}건</span></div>`
      + (rows || `<div class="ch-empty">${empty}</div>`) + `</section>`;
    body.innerHTML = `<section class="m-utility-card mq-add-card">
       <div class="m-utility-section-head"><span>새 영상 추가</span><small>YouTube URL</small></div>
       <div class="mq-add"><input type="url" id="mq-add-url" placeholder="https://youtu.be/..."
         oninput="mqPreviewDebounced()"><button class="mq-paste" onclick="mqPaste()" title="클립보드에서 붙여넣기">📋</button><button id="mq-add-btn" onclick="mqAdd()" disabled>추가</button></div>
       <div class="mq-options"><button type="button" class="mq-cap-chip on" id="mq-add-capture-btn" role="switch" aria-checked="true"
               onclick="this.classList.toggle('on'); this.setAttribute('aria-checked', this.classList.contains('on'))">캡처 포함</button>
       <button type="button" class="mq-cap-chip on" id="mq-add-distill-btn" role="switch" aria-checked="true"
               onclick="this.classList.toggle('on'); this.setAttribute('aria-checked', this.classList.contains('on'))">증류 포함</button></div>
       <div class="mq-add-pv" id="mq-add-pv"></div></section>`
      + `<p class="m-utility-note">유휴면 바로 시작하고, 이후 10분마다 본편 1건과 캡처 1건을 처리합니다.</p>`
      + group('본편 대기', main.length, mainRows, '대기 중인 영상이 없습니다.')
      + (kf.length ? group('캡처 재시도', kf.length, kfRows) : '')
      + (recentRows ? group('최근 처리', d.recent.length, recentRows) : '');
    if (keepUrl) { $('mq-add-url').value = keepUrl; $('mq-add-pv').innerHTML = keepPv;
      if (_mqPv) $('mq-add-btn').disabled = false; }
  } catch (e) {
    body.innerHTML = `<div class="ch-empty">불러오기 실패: ${attrEsc(e.message)}</div>`;
  }
}
async function mqRequeue(id) {
  await YS.queueApi.requeue(id);
  loadQueueM();
}
async function mqToggleCapture(id, enable) {
  await YS.queueApi.setFlags(id, {capture: enable});
  loadQueueM();
}
async function mqToggleDistill(id, enable) {
  await YS.queueApi.setFlags(id, {distill: enable});
  loadQueueM();
}
async function mqMove(id, direction) {
  await YS.queueApi.move(id, direction);
  loadQueueM();
}
async function mqCancel(id) {
  if (await YS.queueApi.cancel(id)) loadQueueM();
}

/* ── 채널 자동 모니터(원격 관리) ── */
/* 설정 팝업(2026-10-24 사용자 지시): 모델 / 채널 / 각주 제외 3탭 — 예전 '채널 자동 모니터' 팝업과 헤더 🏷(각주 제외)
   버튼을 합쳤다. 마지막 탭을 기억한다(localStorage ytSettingsTab, PC와 같은 키). */
const SET_TABS = [['model', '모델'], ['channels', '채널'], ['terms', '각주 제외']];
let _setTab = 'model';
try { const t = localStorage.getItem('ytSettingsTab'); if (SET_TABS.some(x => x[0] === t)) _setTab = t; } catch (e) {}
function _settingsShell() {
  const body = $('modal-body');
  if (body.querySelector('.set-tabs')) return body;
  body.innerHTML = `<div class="set-tabs" role="tablist">` + SET_TABS.map(([k, label]) =>
      `<button type="button" class="set-tab" role="tab" data-set-tab="${k}" aria-selected="false">${label}</button>`).join('') + `</div>`
    + `<section data-set-pane="model"></section><section data-set-pane="channels" hidden></section><section data-set-pane="terms" hidden></section>`;
  body.querySelectorAll('[data-set-tab]').forEach(b => b.addEventListener('click', () => switchSetTab(b.dataset.setTab)));
  return body;
}
function switchSetTab(name) {
  if (!SET_TABS.some(x => x[0] === name)) name = 'model';
  _setTab = name;
  try { localStorage.setItem('ytSettingsTab', name); } catch (e) {}
  const body = _settingsShell();
  body.querySelectorAll('[data-set-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.setTab === name)));
  body.querySelectorAll('[data-set-pane]').forEach(sec => { sec.hidden = sec.dataset.setPane !== name; });
  body.scrollTop = 0;
  if (name === 'terms') renderTermsM();
}
function openTermsM() { openChannels('terms'); }
async function openChannels(tab) {
  if (tab) _setTab = tab;
  $('fs-ctrl').hidden = true;             // 글자크기 컨트롤은 요약 전용
  $('font-ctrl').hidden = true;           // 글꼴 컨트롤도 요약 전용
  openModal('설정', 'utility');
  $('modal-body').className = 'modal-body channels';
  $('modal-body').innerHTML = '';
  switchSetTab(_setTab);
  if (_mobileChannelsCache) renderChannelsM(_mobileChannelsCache);
  else ['model', 'channels'].forEach(k => { $('modal-body').querySelector(`[data-set-pane="${k}"]`).innerHTML = '<div class="m-utility-loading"><span></span><span></span><span></span></div>'; });
  requestAnimationFrame(() => { loadChannels({ force: !!_mobileChannelsCache }); });
}
// 모델 선택기는 common.js의 YS.monitorModels가 상태·저장을, model-selector.js가 화면을 맡는다.
let _mobileChannelsCache = null;
let _mobileChannelsRequest = null;

function renderChannelsM(d) {
  const body = _settingsShell();
  YS.monitorModels.load(d);
  const q = d.queue || {}, parts = [];
  if (q.pending)    parts.push(`대기 ${q.pending}`);
  if (q.processing) parts.push(`처리중 ${q.processing}`);
  if (q.kf_retry)   parts.push(`캡처재시도 ${q.kf_retry}`);
  if (q.done)       parts.push(`완료 ${q.done}`);
  if (q.failed)     parts.push(`실패 ${q.failed}`);
  const qline = parts.length ? parts.join(' · ') : '큐 비어있음';
  const chans = d.channels || [];
  body.querySelector('[data-set-pane="model"]').innerHTML =
    `<p class="m-utility-note">새 영상의 요약은 슬롯이 돌아가며 맡고, 캡처 해설은 순서대로 폴백합니다.</p>`
    + `<div class="msel-host" id="model-selector"></div>` + `<div class="ch-queue">${esc(qline)}</div>`;
  // 채널 목록(로고·검색·자동>캡처>증류 스위치)은 PC·모바일 공용 channel-list.js가 그린다.
  // 다시 그릴 때(새로고침 응답) 검색어·포커스가 날아가지 않게 칸은 한 번만 만든다.
  const pane = body.querySelector('[data-set-pane="channels"]');
  if (!pane.querySelector('#mobile-channel-list')) {
    pane.innerHTML =
      `<p class="m-utility-note">등록 채널의 새 영상을 10분마다 확인해 전사·요약·캡처합니다.</p>`
      + `<section class="ch-list-shell chl-shell"><div class="m-utility-section-head"><span>등록 채널</span>`
      + `<small id="mobile-channels-count">${chans.filter(c => c.enabled).length} ON · 전체 ${chans.length}</small></div>`
      + `<div id="mobile-channel-list"></div></section>`;
  }
  YS.channelList.mount($('mobile-channel-list'), d, { mobile: true, countEl: $('mobile-channels-count') });
  YS.monitorModels.mount($('model-selector'), {
    onSaved: saved => { if (_mobileChannelsCache) Object.assign(_mobileChannelsCache, YS.monitorModels.fields(saved)); },
  });
}

async function _fetchChannelsM(force = false) {
  if (_mobileChannelsCache && !force) return _mobileChannelsCache;
  if (_mobileChannelsRequest) return _mobileChannelsRequest;
  _mobileChannelsRequest = fetch('/channels', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => {
      if (d.error) throw new Error(d.error);
      _mobileChannelsCache = d;
      return d;
    })
    .finally(() => { _mobileChannelsRequest = null; });
  return _mobileChannelsRequest;
}

async function loadChannels({ force = false } = {}) {
  try {
    const d = await _fetchChannelsM(force);
    if ($('modal').classList.contains('open') && $('modal-body').classList.contains('channels')) renderChannelsM(d);
  } catch (e) {
    if ($('modal-body').classList.contains('channels')) {
      _settingsShell().querySelectorAll('[data-set-pane="model"], [data-set-pane="channels"]').forEach(sec => {
        sec.innerHTML = `<div class="ch-empty">오류: ${esc(e.message)}</div>`;
      });
    }
  }
}

const _prefetchChannelsM = () => { _fetchChannelsM(false).catch(() => {}); };
if ('requestIdleCallback' in window) requestIdleCallback(_prefetchChannelsM, { timeout: 1800 });
else setTimeout(_prefetchChannelsM, 700);



/* ── 뒤로가기로 모달 닫기 ── */
window.addEventListener('popstate', () => {
  if (_modalPushed) {
    _modalPushed = false;
    _closeModalUI();
  }
});

// 요약 본문 글자크기(소제목 제외) — 로컬 저장
let _readerFs = parseInt(localStorage.getItem('sumFontSize'), 10) || 14;
const FS_MIN = 11, FS_MAX = 22;
function _applyReaderFs() { $('modal-body').style.setProperty('--reader-fs', _readerFs + 'px'); }
function adjustFs(delta) {
  _readerFs = Math.max(FS_MIN, Math.min(FS_MAX, _readerFs + delta));
  localStorage.setItem('sumFontSize', _readerFs);
  _applyReaderFs();
}

// 데스크톱과 같은 본문 글꼴 설정(sumReaderFont)을 공유하고 로컬에 기억한다. 목록·글꼴 값은 공용 fonts.js가 정본
// (2026-11-01 사용자 지시) — 공용 목록에 더한 글꼴이 그대로 나타난다. 옛 저장값 'sans'는 Noto Sans KR이었다.
const _RF = window.HermesReaderFonts
  || {list: [], get: () => null, resolve: () => null, apply() {}, fillSelect() {}, ensureWeb() {}};
let _readerFont = _RF.resolve(localStorage.getItem('sumReaderFont'), {legacy: {sans: 'noto'}, fallback: 'noto'});
function _applyReaderFont() {
  _RF.apply($('modal'), _readerFont);
  const sel = $('font-select');
  if (!sel.options.length) _RF.fillSelect(sel);
  if (sel.value !== _readerFont) sel.value = _readerFont;
}
function setReaderFont(key) {
  if (!_RF.get(key)) return;
  _readerFont = key;
  localStorage.setItem('sumReaderFont', key);
  _applyReaderFont();
}

let _summaryMd = '';   // 현재 열린 요약 원문 — 블로그 발행에 사용
let _titleKoM = '';    // 현재 열린 요약의 번역 제목(외국어 제목일 때만 값이 있다)
let _summaryReadM = false;

function _setSummaryReadUIM(isRead) {
  _summaryReadM = !!isRead;
  const btn = $('summary-read-btn');
  btn.classList.toggle('active', _summaryReadM);
  btn.querySelector('.read-label').textContent = _summaryReadM ? '읽음' : '읽음 처리';
  btn.title = _summaryReadM ? '읽음 상태입니다. 탭하면 안읽음으로 변경' : '읽음으로 표시';
}

async function toggleSummaryReadM(btn) {
  if (!_summaryItemId) return;
  const next = !_summaryReadM;
  btn.disabled = true;
  try {
    const result = await YS.apiMarkRead(_summaryItemId, next);
    if (!result) throw new Error('상태 변경 실패');
    if (result.revision) HISTORY_REVISION = result.revision;
    const item = ALL.find(i => i.item_id === _summaryItemId);
    if (item) item.is_read = next;
    _setSummaryReadUIM(next);
    applyFilter();
    // 읽음 표시 = 다 봤다는 뜻이므로 목록으로 돌아간다. 안읽음 되돌리기는 닫지 않는다.
    if (next) closeModal();
  } catch (e) {
    console.warn('[read] 상태 변경 실패', e);
  } finally {
    btn.disabled = false;
  }
}

async function openSummary(itemId, title) {
  openModal(title);
  $('fs-ctrl').hidden = false;   // 요약에서만 글자크기 조절 노출
  $('font-ctrl').hidden = false;
  _applyReaderFs();
  _applyReaderFont();
  _summaryMd = '';
  _titleKoM = '';
  _summaryItemId = itemId;
  _setSummaryReadUIM(false);
  $('summary-actions').hidden = true;
  _setDistillUI(null);
  try {
    const [d] = await Promise.all([
      YS.apiSummaryContent(itemId),
      YS.ensureReaderAssets(),
      YS.ensureTermExclusions(),      // 제외 용어를 렌더 전에 확보
    ]);
    if (d.error) {
      $('modal-body').textContent = '오류: ' + d.error;
    } else {
      _summaryMd = d.content || '';
      YS.setSummaryNotes(itemId, d.notes);
      _titleKoM = d.title_ko || '';
      $('modal-body').className = 'modal-body markdown';
      $('modal-body').innerHTML = YS.renderMarkdown(_summaryMd);
      YS.applyTitleTranslation($('modal-body'), d.title_ko);  // 제목은 번역본, 원문은 아래 병기
      YS.stripSummaryPopupChrome($('modal-body'));            // '핵심 내용' 머리말·목차는 팝업에서 생략
      YS.pinMetaToHead($('modal-body'));                      // 메타 칩 줄도 상단 고정 영역에(PC와 같다)
      YS.setupStickySummarySections($('modal-body'));         // 소제목은 해당 h3 섹션 안에서만 고정
      YS.decoratePeople($('modal-body'));                 // 인명 클릭 소개(토스트)
      YS.attachSummaryNotes($('modal-body'), itemId);
      _attachSectionEditorsM($('modal-body'));
      $('modal-youtube-link').href = d.webpage_url || '#';
      $('modal-youtube-link').hidden = !d.webpage_url;
      _setSummaryReadUIM(d.is_read);
      _setPubBtnM(d.blog_url, d.blog);
      _setDistillUI(d.distill);
      $('summary-actions').hidden = !_summaryMd;
    }
  } catch (e) {
    $('modal-body').textContent = '오류: ' + e.message;
  }
}

let _summaryItemId = 0;  // 현재 열린 요약의 DB ID — 경로를 브라우저에 노출하지 않는다

let _distill = null;   // 현재 영상의 증류 상태 {override, channel, effective}

/* 증류 토글 표시 — 채널 따름일 때는 그 채널의 실제 값(포함/제외)을 옆에 함께 보여준다 */
function _setDistillUI(d) {
  const box = $('distill-ctrl');
  if (!box) return;
  _distill = d || null;
  const ov = d ? d.override : null;
  const set = ov !== null && ov !== undefined;
  box.classList.toggle('set', set);
  if (!d) { box.textContent = '증류 —'; box.title = ''; return; }
  const chan = d.channel ? '포함' : '제외';
  // 자동수집 채널이 아니면 따를 채널 설정 자체가 없다 → '채널'이 아니라 '기본'으로 설명한다.
  const reg  = d.registered !== false;
  const src  = reg ? '채널' : '기본';
  const srcDesc = reg ? `채널 설정을 따름 → 현재 ${chan}`
                      : `자동수집 채널이 아니라 기본값 적용 → ${chan}`;
  if (!set) {
    // 좁은 헤더라 값만 남기고 '따름'은 ↪ 기호로 구분한다(전체 설명은 툴팁)
    box.textContent = `증류 ↪ ${chan}`;
    box.title = `${srcDesc}. 탭하면 이 영상만 '포함'으로`;
  } else if (ov) {
    box.textContent = '증류 ✓ 포함';
    box.title = `이 영상만 포함(${src} 설정은 ${chan}). 탭하면 '제외'로`;
  } else {
    box.textContent = '증류 ✕ 제외';
    box.title = `이 영상만 제외(${src} 설정은 ${chan}). 탭하면 '${src} 따름'으로`;
  }
}

/* 탭할 때마다 채널 따름 → 포함 → 제외 → 채널 따름 */
function cycleItemDistill() {
  const ov = _distill ? _distill.override : null;
  setItemDistill((ov === null || ov === undefined) ? true : (ov ? false : null));
}

/* 영상 단위 증류 설정 — null=미설정(채널 따름), true=포함, false=제외 */
async function setItemDistill(value) {
  if (!_summaryItemId) return;
  try {
    const r = await fetch('/history/distill', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: _summaryItemId, distill: value }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    _setDistillUI(d);
    const it = ALL.find(i => i.item_id === _summaryItemId);   // 목록의 증류 필터도 바로 맞춘다
    if (it && d.effective !== undefined) it.distill = !!d.effective;
  } catch (e) {
    alert('증류 설정 실패: ' + e.message);
  }
}

/* 블로그 발행 — '블로거용 복사'를 대체(2026-09-05). 데스크톱과 같은 엔드포인트. */
let _blogUrl = '';
let _blogStateM = { stale: false, changed_at: '', published_at: '' };
function _setPubBtnM(url, state) {
  _blogUrl = url || '';
  if (state) _blogStateM = state;
  if (!_blogUrl) _blogStateM = { stale: false, changed_at: '', published_at: '' };
  const b = $('pub-btn'); if (!b) return;
  b.querySelector('.action-label').textContent = _blogUrl ? '발행됨' : '블로그 발행';
  b.className = _blogUrl
    ? 'summary-action-btn primary done' + (_blogStateM.stale ? ' stale' : '')
    : 'summary-action-btn primary';
  const stampM = (_blogStateM.published_at || '').slice(0, 16);
  b.title = _blogUrl
    ? (stampM ? `${stampM} 게시됨` : '게시 시각 기록 없음')
      + (_blogStateM.stale ? ' — 이후 내용이 바뀜, 열기 또는 수정' : ' — 열기 또는 수정')
    : '요약을 내 블로그스팟에 바로 발행 (즉시 공개)';
  b.disabled = false;
}
/* 소제목 구간 직접 편집(✏️) — 저장되면 본문을 다시 그린다(데스크톱과 같은 엔드포인트). */
function _attachSectionEditorsM(rootEl) {
  if (!rootEl || !_summaryItemId) return;
  YS.attachSectionEditors(rootEl, {
    md: _summaryMd, itemId: _summaryItemId,
    onSaved: (content, blog) => {
      _summaryMd = content || _summaryMd;
      if (blog && _blogUrl) _setPubBtnM(_blogUrl, blog);
      const body = $('modal-body');
      const top = body.scrollTop;
      body.innerHTML = YS.renderMarkdown(_summaryMd);
      YS.applyTitleTranslation(body, _titleKoM);
      YS.stripSummaryPopupChrome(body);
      YS.pinMetaToHead(body);
      YS.setupStickySummarySections(body);
      YS.decoratePeople(body);                 // 인명 클릭 소개(토스트)
      YS.attachSummaryNotes(body, _summaryItemId);
      _attachSectionEditorsM(body);
      body.scrollTop = top;
      if (typeof loadHistory === 'function') loadHistory(true);
    },
  });
}
/* 메모 저장/삭제 즉시 발행 버튼 상태를 갱신한다(common.js가 알림). */
document.addEventListener('ys:blog-state', e => {
  const { itemId, blog } = e.detail || {};
  if (_blogUrl && String(itemId) === String(_summaryItemId)) _setPubBtnM(_blogUrl, blog);
});
/* 발행된 글을 지금 요약·메모로 덮어쓴다(데스크톱 _updateBlogPost와 같은 엔드포인트). */
async function updateBlogPostM(btn) {
  if (!_summaryMd || !_summaryItemId) return;
  if (!await YS.askConfirm({ title: '블로그 글 수정', message: '발행된 글을 지금 요약·메모로 수정할까요? (주소는 그대로입니다)', ok: '수정' })) return;
  const lbl = btn.querySelector('.action-label');
  const { html, title } = YS.mdToBloggerHtml(_summaryMd, { translatedTitle: _titleKoM, people: await YS.blogPeople(_summaryItemId) });
  btn.disabled = true; lbl.textContent = '수정 중…';
  try {
    const r = await fetch('/history/publish-blog', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item_id:_summaryItemId, mode:'update', html, title})});
    const d = await r.json();
    if (d && d.status === 'updated') {
      _setPubBtnM(d.url || _blogUrl, d.blog);
      if (typeof loadHistory === 'function') loadHistory(true);
      return;
    }
    lbl.textContent = d && d.code === 'not_configured' ? '연동 미설정' : '수정 실패';
    btn.className = 'summary-action-btn primary err'; console.warn('[blog]', d);
  } catch (e) { lbl.textContent = '수정 실패'; btn.className = 'summary-action-btn primary err'; }
  setTimeout(() => _setPubBtnM(_blogUrl, _blogStateM), 2200);
}
// 발행한 블로그 글 삭제(2026-10-19) — 지우면 '블로그 발행' 상태로 돌아간다.
async function deleteBlogPostM(btn) {
  if (!_summaryItemId || !_blogUrl) return;
  if (!await YS.askConfirm({ title: '블로그 글 삭제', message: '블로그에서 이 글을 삭제할까요? (공개된 글이 지워지고 되돌릴 수 없습니다)', ok: '삭제', danger: true })) return;
  const lbl = btn.querySelector('.action-label');
  btn.disabled = true; lbl.textContent = '삭제 중…';
  try {
    const d = await YS.apiDeleteBlog(_summaryItemId);
    if (d && d.status === 'deleted') { _setPubBtnM(''); if (typeof loadHistory === 'function') loadHistory(true); return; }
    lbl.textContent = d && d.code === 'not_configured' ? '연동 미설정' : '삭제 실패';
    btn.className = 'summary-action-btn primary err'; console.warn('[blog]', d);
  } catch (e) { lbl.textContent = '삭제 실패'; btn.className = 'summary-action-btn primary err'; }
  setTimeout(() => _setPubBtnM(_blogUrl, _blogStateM), 2200);
}
async function publishToBlog(btn) {
  // 이미 발행된 글은 '보기 / 수정' 선택 메뉴를 띄운다.
  if (_blogUrl) {
    YS.openBlogMenu(btn, {
      url: _blogUrl, stale: !!_blogStateM.stale, changedAt: _blogStateM.changed_at || '',
      publishedAt: _blogStateM.published_at || '',
      contentChanged: !!_blogStateM.content_changed, outdatedRender: !!_blogStateM.outdated_render,
      onUpdate: () => updateBlogPostM(btn),
      onDelete: () => deleteBlogPostM(btn),
    });
    return;
  }
  if (!_summaryMd || !_summaryItemId) return;
  if (!await YS.askConfirm({ title: '블로그 발행', message: '블로그스팟에 바로 발행할까요? (초안이 아니라 즉시 공개됩니다)', ok: '발행' })) return;
  const lbl = btn.querySelector('.action-label');
  const { html, title } = YS.mdToBloggerHtml(_summaryMd, { translatedTitle: _titleKoM, people: await YS.blogPeople(_summaryItemId) });
  btn.disabled = true; lbl.textContent = '…';
  try {
    const d = await YS.apiPublishBlog(_summaryItemId, _titleKoM || title, html);
    if (d && (d.status === 'ok' || d.status === 'exists')) { _setPubBtnM(d.url, d.blog); if (typeof loadHistory === 'function') loadHistory(true); return; }
    lbl.textContent = d && d.code === 'not_configured' ? '연동 미설정' : '발행 실패'; btn.className = 'summary-action-btn primary err'; console.warn('[blog]', d);
  } catch (e) { lbl.textContent = '발행 실패'; btn.className = 'summary-action-btn primary err'; }
  setTimeout(() => _setPubBtnM(''), 2200);
}

async function openTranscript(itemId, title) {
  openModal(title);
  $('fs-ctrl').hidden = true;   // 전사 보기엔 글자크기 조절 숨김(요약 전용)
  $('font-ctrl').hidden = true;
  $('summary-actions').hidden = true;
  _summaryMd = '';
  _titleKoM = '';
  try {
    const r = await fetch('/history/text', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ item_id: itemId })
    });
    const d = await r.json();
    if (d.error) {
      $('modal-body').textContent = '오류: ' + d.error;
    } else {
      $('modal-body').className = 'modal-body transcript';
      $('modal-body').textContent = d.text || '';
    }
  } catch (e) {
    $('modal-body').textContent = '오류: ' + e.message;
  }
}

/* 아주 가벼운 markdown 렌더러 (요약 표시용) */
// 마크다운 렌더링은 공유 모듈(YS.renderMarkdown, marked.js 기반)로 일원화.
// 수제 파서 제거 → index/summary와 동일 엔진으로 표·이스케이프 처리 일치.
function mdToHtml(src) { return YS.renderMarkdown(src); }

/* ── 검색 디바운스 ── */
let searchTimer;
$('q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilter, 180);
});

/* ── 무한 스크롤 ── */
const io = new IntersectionObserver(entries => {
  if (entries[0].isIntersecting && shownCount < FILTERED.length) appendPage();
}, { rootMargin: '400px' });
io.observe($('sentinel'));

/* ── Pull-to-refresh ── */
let ptrY0 = 0, ptrDy = 0, ptrActive = false;
window.addEventListener('touchstart', e => {
  if (window.scrollY > 0) return;
  ptrY0 = e.touches[0].clientY; ptrDy = 0; ptrActive = true;
}, { passive: true });
window.addEventListener('touchmove', e => {
  if (!ptrActive) return;
  ptrDy = e.touches[0].clientY - ptrY0;
  if (ptrDy > 8) {
    $('ptr').classList.add('visible');
    $('ptr-text').textContent = ptrDy > 70 ? '놓으면 새로고침' : '아래로 당겨 새로고침';
  }
}, { passive: true });
window.addEventListener('touchend', () => {
  if (!ptrActive) return;
  if (ptrDy > 70) loadHistory(true);
  $('ptr').classList.remove('visible');
  ptrActive = false; ptrDy = 0;
}, { passive: true });

/* ── ESC로 모달 닫기 ── */
window.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* ── 진입 시 자동 fetch ── */
loadHistory();

/* ── 페이지 가시화 시 자동 새로고침 (다른 탭에서 돌아올 때) ── */
let lastRefresh = Date.now();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastRefresh > 60_000) {
    lastRefresh = Date.now();
    loadHistory(false);
  }
});

/* 요약을 HTML 파일 하나로 내보내기(2026-11-17) — 아는 사람에게 보내는 용도. 만들기는 공용 YS.exportSummaryFile. */
async function exportSummaryHtmlM(btn) {
  if (!_summaryMd) return;
  const label = btn && btn.querySelector('.action-label');
  const orig = label ? label.textContent : '';
  if (btn) btn.disabled = true;
  if (label) label.textContent = '만드는 중…';
  let done = '';
  try {
    const r = await YS.exportSummaryFile({ md: _summaryMd, titleKo: _titleKoM, webpageUrl: $('modal-youtube-link').hidden ? '' : $('modal-youtube-link').href });
    done = r === 'shared' ? '보냈습니다' : r === 'downloaded' ? '저장했습니다' : '';
  } catch (e) {
    console.warn('[export]', e);
    done = '만들기 실패';
  }
  if (btn) btn.disabled = false;
  if (label) {
    label.textContent = done || orig;
    if (done) setTimeout(() => { label.textContent = orig; }, 2200);
  }
  // 좁은 폰에선 글자가 숨으므로 색으로도 알린다
  if (btn && (done === '보냈습니다' || done === '저장했습니다')) {
    btn.classList.add('done'); setTimeout(() => btn.classList.remove('done'), 2200);
  } else if (btn && done) {
    btn.classList.add('err'); setTimeout(() => btn.classList.remove('err'), 2200);
  }
}
