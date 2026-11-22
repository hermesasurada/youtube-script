/* 유튜브 스크립트 PC 화면 — 이력 목록 — 불러오기·검색조건 유지·채널 선택 팝업·카드 그리기.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
/* ── History ── */
let _historyLoaded   = false;
let _historyItems    = [];
let _historyFiltered = [];
let _historySelIdx   = -1;
let _historyPage     = 1;
let _historyRevision = '';
let _histUnreadOnly  = false;
let _histBookmarkOnly = false;
let _histDistillOnly = false;   // 증류 대상만(2026-11-07) — item.distill = 증류 파이프라인과 같은 판정
let _histCollection = '';       // 영상 모음 슬러그(2026-11-22) — 채널 팝업 '영상 모음' 탭에서 고른다
let _uploaderTab = '';          // 채널 팝업에서 '영상 모음' 탭을 보는 중이면 'coll'
let _histSort        = localStorage.getItem('histSort') || 'desc';   // 'desc'=최신순(기본), 'asc'=과거순
let _histSortKey     = localStorage.getItem('histSortKey') || 'date'; // 'date'=전사 처리일(기본), 'upload'=영상 게시일, 'duration'=영상 길이
const HIST_SORT_KEYS = ['date', 'upload', 'duration'];   // 기준 버튼이 순환하는 순서
const HIST_PAGE_BASE = 20;   // 한 페이지 기본 카드 수 — 실제 수는 열 수의 배수로 올린다(histPageSize)
/* 한 페이지 카드 수를 그리드 열 수의 배수로(2026-11-21 사용자 지시 '우측 하단이 비어 보임' — 넓은 화면 8열에서
   20개면 마지막 줄이 4칸 비었다). 열 수는 .hist-grid의 실제 열(grid-template-columns 트랙 수)로 센다. */
let _histPageSizeCache = 0;
function histPageSize() {
  const grid = document.getElementById('history-grid');
  let cols = 0;
  if (grid && grid.offsetParent) {
    const t = getComputedStyle(grid).gridTemplateColumns;
    if (t && t !== 'none') cols = t.trim().split(/\s+/).length;
  }
  if (!cols) return _histPageSizeCache || HIST_PAGE_BASE;   // 그리드가 아직 안 보이면 직전 값
  _histPageSizeCache = Math.ceil(HIST_PAGE_BASE / cols) * cols;
  return _histPageSizeCache;
}

/* ── 검색조건 유지: 날짜·업로더·제목·미읽음·페이지를 localStorage에 기억해 새로고침 후 복원 ──
   (정렬 _histSort는 예전부터 histSort 키로 별도 저장 중) */
const HIST_FILTER_KEY = 'histFilter';
let _histRestoring = false;   // 복원 중엔 저장 금지(빈 입력값이 저장을 덮어쓰는 것 방지)

function _saveHistFilter() {
  if (_histRestoring) return;
  try {
    localStorage.setItem(HIST_FILTER_KEY, JSON.stringify({
      uploader: document.getElementById('hf-uploader').value,
      title:    document.getElementById('hf-title').value,
      unread:   _histUnreadOnly,
      bookmark: _histBookmarkOnly,
      distill:  _histDistillOnly,
      collection: _histCollection,
      page:     _historyPage,
    }));
  } catch { /* 사파리 프라이빗 등 스토리지 불가 — 무시 */ }
}

/* 저장된 조건을 입력 UI에 되돌린다. 업로더 옵션이 채워진 뒤 호출해야 함(없는 값이면 '전체'로 남음). */
function _restoreHistFilter() {
  let s;
  try { s = JSON.parse(localStorage.getItem(HIST_FILTER_KEY) || 'null'); } catch { s = null; }
  if (!s) return;
  _histRestoring = true;
  try {
    if (s.title)    document.getElementById('hf-title').value     = s.title;
    const usel = document.getElementById('hf-uploader');
    if (s.uploader && [...usel.options].some(o => o.value === s.uploader)) usel.value = s.uploader;
    _histUnreadOnly = !!s.unread;
    document.getElementById('hf-unread-btn').classList.toggle('active', _histUnreadOnly);
    _histBookmarkOnly = !!s.bookmark;
    _histDistillOnly = !!s.distill;
    _histCollection = String(s.collection || '');
    _histRestorePage = Number(s.page) > 1 ? Number(s.page) : 1;   // 필터 적용 후 반영
  } finally {
    _histRestoring = false;
  }
}
let _histRestorePage = 1;


async function loadHistory(force = false) {
  _historyLoaded = true;
  document.getElementById('history-count').textContent = '로드 중...';
  try {
    const url = (!force && _historyRevision)
      ? '/history?revision=' + encodeURIComponent(_historyRevision) : '/history';
    const r = await fetch(url, { cache: force ? 'no-store' : 'default' });
    const d = await r.json();
    if (!d.unchanged) _historyItems = d.items || [];
    if (d.revision) _historyRevision = d.revision;
  } catch {
    if (!_historyItems.length) _historyItems = [];
  }
  _populateUploaderOptions();
  await YS.collections.load(force);   // 영상 모음별 이력 id(모음 필터용)
  _updateSortBtn();   // 저장된 정렬값 → 토글 버튼 라벨 반영
  _restoreHistFilter();          // 저장된 검색조건 복원(업로더 옵션 채운 뒤여야 값이 붙는다)
  applyHistoryFilter();
  if (_histRestorePage > 1) {    // 보던 페이지로 복귀(필터 결과 범위 밖이면 렌더가 클램프)
    _historyPage = _histRestorePage;
    _histRestorePage = 1;
    _renderHistoryList();
  }
}

function _populateUploaderOptions() {
  const sel = document.getElementById('hf-uploader');
  const prev = sel.value;
  const uploaders = [...new Set(
    _historyItems.map(i => i.uploader).filter(u => u && u !== '—')
  )].sort((a, b) => a.localeCompare(b, 'ko'));
  sel.innerHTML = '<option value="">전체 업로더</option>' +
    uploaders.map(u => `<option value="${esc(u)}">${esc(u)}</option>`).join('');
  if (prev && uploaders.includes(prev)) sel.value = prev;
}

/* ── 채널 선택 팝업(2026-10-11 사용자 지시): 채널별 전체·미읽음 건수 표시, 누르면 그 채널로 좁힌다.
   실제 필터 상태는 숨긴 select(#hf-uploader)가 들고 있어 기존 저장·복원·초기화 경로를 그대로 쓴다. ── */
// 안읽은 영상의 원본 길이 합계(2026-10-15 사용자 지시) — '3시간 12분'·'45분'
function fmtUnreadDur(secs) { return YS.fmtUnreadDur(secs); }
// 채널 정렬 기준·통계는 common.js YS.uploaderSort(PC·모바일 공용, localStorage ytUploaderSort)
function _uploaderSortMode() { return YS.uploaderSort.mode(); }
function _sortUploaders(stats, mode) { return YS.uploaderSort.order(stats, mode); }
// 행 오른쪽 길이 표시 — 평균 기준이면 평균 길이, 아니면 합계
function _uploaderDurLabel(s, mode) {
  const text = YS.uploaderSort.durText(s, mode);
  return text ? { text, title: mode === 'avg' ? '안읽은 영상 평균 길이' : '안읽은 영상 원본 길이 합계' } : null;
}
function setUploaderSort(mode) {
  if (mode === 'coll') { _uploaderTab = 'coll'; _renderUploaderList(); return; }   // 영상 모음 탭
  _uploaderTab = '';
  YS.uploaderSort.set(mode);
  _renderUploaderList();
}
function _uploaderStats() { return YS.uploaderSort.stats(_historyItems); }
function _paintUploaderBtn() {
  const btn = document.getElementById('hf-uploader-btn');
  if (!btn) return;
  const u = document.getElementById('hf-uploader').value;
  const coll = _histCollection && YS.collections.get(_histCollection);
  // 버튼 글자: 모음 > 채널 > '전체 채널', 함께 건 북마크·증류는 뒤에 붙인다(2026-11-22 — 두 버튼이 팝업 안으로 들어가서)
  const extra = [_histBookmarkOnly && '북마크', _histDistillOnly && '증류'].filter(Boolean);
  const main = coll ? coll.title : (u || '전체 채널');
  btn.querySelector('.nm').textContent = main + (extra.length ? ' · ' + extra.join(' · ') : '');
  btn.classList.toggle('active', !!(u || coll || extra.length));
  btn.title = `${coll ? '영상 모음: ' + coll.title : u ? '채널: ' + u : '채널 선택'}${extra.length ? ' + ' + extra.join('·') + '만' : ''} — 바꾸기`;
  const bm = document.getElementById('hf-bookmark-btn'), ds = document.getElementById('hf-distill-btn');
  if (bm) { bm.classList.toggle('active', _histBookmarkOnly); bm.setAttribute('aria-pressed', String(_histBookmarkOnly)); }
  if (ds) { ds.classList.toggle('active', _histDistillOnly); ds.setAttribute('aria-pressed', String(_histDistillOnly)); }
}
/* 영상 모음 탭 — 모음 하나를 고르면 그 모음의 요약된 영상만 보인다(채널 선택은 풀린다) */
function _renderCollectionList(box) {
  const list = YS.collections.list();
  const row = (slug, label, sub, n) => `<button type="button" class="uploader-item${slug === _histCollection ? ' selected' : ''}" role="option"
      aria-selected="${slug === _histCollection}" data-coll="${_attrEsc(slug)}" onclick="pickCollection(this.dataset.coll)">
      <span class="up-name">${esc(label)}${sub ? `<small class="up-sub">${esc(sub)}</small>` : ''}</span>${n != null ? `<span class="up-total" title="요약된 영상">${n}</span>` : ''}</button>`;
  box.innerHTML = row('', '모음 전체(해제)', '', null)
    + list.map(c => row(c.slug, c.title, c.subtitle, c.ids.size)).join('')
    + (list.length ? '' : '<div class="uploader-empty">영상 모음이 없습니다</div>');
}
function pickCollection(slug) {
  _histCollection = slug || '';
  if (_histCollection) document.getElementById('hf-uploader').value = '';
  document.getElementById('uploader-modal').classList.remove('open');
  applyHistoryFilter();
}
function _renderUploaderList() {
  const box = document.getElementById('uploader-list');
  if (!box) return;
  const q = (document.getElementById('uploader-search').value || '').trim().toLowerCase();
  const cur = document.getElementById('hf-uploader').value;
  const stats = _uploaderStats();
  const mode = _uploaderSortMode();
  document.querySelectorAll('#uploader-sort button').forEach(b =>
    b.setAttribute('aria-pressed', String(_uploaderTab === 'coll' ? b.dataset.sort === 'coll' : b.dataset.sort === mode)));
  document.getElementById('uploader-search').hidden = _uploaderTab === 'coll';
  if (_uploaderTab === 'coll') { _renderCollectionList(box); return; }
  const names = _sortUploaders(stats, mode);
  const totalUnread = [...stats.values()].reduce((n, s) => n + s.unread, 0);
  const row = (val, label, s) => {
    const dur = _uploaderDurLabel(s, mode);
    const unread = s.unread ? `<span class="up-unread" title="미읽음 ${s.unread}건">${s.unread}</span>`
      + (dur ? `<span class="up-dur" title="${dur.title}">${dur.text}</span>` : '') : '';
    return `<button type="button" class="uploader-item${val === cur ? ' selected' : ''}" role="option"
        aria-selected="${val === cur}" data-uploader="${_attrEsc(val)}" onclick="pickUploader(this.dataset.uploader)">
        <span class="up-name">${esc(label)}</span>${unread}<span class="up-total" title="전체 ${s.total}건">${s.total}</span></button>`;
  };
  const all = { total: _historyItems.length, unread: totalUnread,
                unreadSecs: [...stats.values()].reduce((n, s) => n + s.unreadSecs, 0) };
  const list = names.filter(n => !q || n.toLowerCase().includes(q));
  box.innerHTML = (q ? '' : row('', '전체 채널', all))
    + list.map(n => row(n, n, stats.get(n))).join('')
    + (list.length || !q ? '' : '<div class="uploader-empty">일치하는 채널이 없습니다</div>');
}
function openUploaderModal() {
  document.getElementById('uploader-search').value = '';
  _uploaderTab = _histCollection ? 'coll' : '';   // 모음을 걸어 둔 상태면 모음 탭부터
  _renderUploaderList();
  document.getElementById('uploader-modal').classList.add('open');
  const sel = document.querySelector('#uploader-list .uploader-item.selected');
  (sel || document.getElementById('uploader-search')).focus();
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}
function closeUploaderModal() {
  document.getElementById('uploader-modal').classList.remove('open');
  document.getElementById('hf-uploader-btn')?.focus();
}
function pickUploader(uploader) {
  const sel = document.getElementById('hf-uploader');
  if (uploader && ![...sel.options].some(o => o.value === uploader)) return;
  sel.value = uploader || '';
  if (uploader) _histCollection = '';      // 채널을 고르면 모음 필터는 푼다
  document.getElementById('uploader-modal').classList.remove('open');
  applyHistoryFilter();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('uploader-modal')?.classList.contains('open')) {
    e.preventDefault();
    closeUploaderModal();
  }
});

function clearTitleSearch() {
  const inp = document.getElementById('hf-title');
  inp.value = '';
  document.getElementById('hf-clear-btn').classList.remove('visible');
  applyHistoryFilter();
  inp.focus();
}

function applyHistoryFilter(keepPage = false) {
  const uploader = document.getElementById('hf-uploader').value;
  _paintUploaderBtn();
  const titleQ   = document.getElementById('hf-title').value.trim().toLowerCase();
  document.getElementById('hf-clear-btn').classList.toggle('visible', titleQ.length > 0);

  _historyFiltered = _historyItems.filter(item => {
    if (YS.isPendingDelete(item.item_id)) return false;   // 되돌리기 대기 중인 삭제
    if (uploader && item.uploader !== uploader)  return false;
    if (_histCollection && !YS.collections.has(_histCollection, item.item_id)) return false;
    if (titleQ   && !item.title.toLowerCase().includes(titleQ)
                 && !(item.title_ko || '').toLowerCase().includes(titleQ)) return false;
    if (_histUnreadOnly && item.is_read) return false;
    if (_histBookmarkOnly && !item.bookmark) return false;
    // 증류만 = 증류 대상이면서 영상 게시일 7일 이내(2026-11-07 사용자 지시)
    if (_histDistillOnly && !(item.distill && YS.withinRecentDays(item, 7))) return false;
    return true;
  });
  // 정렬: 기준(_histSortKey)에 따라 전사 처리일 또는 영상 게시일. desc=최신순, asc=과거순.
  _historyFiltered.sort((a, b) => {
    const ka = _histSortValue(a), kb = _histSortValue(b);
    const c = ka < kb ? -1 : ka > kb ? 1 : 0;
    return _histSort === 'asc' ? c : -c;
  });
  // keepPage: 읽음/삭제 등 '항목 변경'에 의한 재적용은 현재 페이지 유지
  // (페이지 수가 줄면 _renderHistoryList의 클램프가 마지막 페이지로 보정).
  // 필터 조건 자체가 바뀐 경우(기본)는 1페이지부터.
  if (!keepPage) _historyPage = 1;
  _saveHistFilter();   // 모든 필터 변경 경로가 여기를 지나므로 한 곳에서 저장
  _renderHistoryList();
}

/* 정렬 기준값: 전사 처리일은 date+stem(같은 날 안에서도 시간순), 게시일은 upload_date.
   게시일이 없는 항목(파일 업로드 등)은 처리일로 대체해 순서가 뒤엉키지 않게 한다. */
function _histSortValue(item) {
  // 길이는 숫자로 비교한다(문자열로 두면 '9:59'가 '1:02:00'보다 뒤로 간다).
  if (_histSortKey === 'duration') return Number(item.duration) || 0;
  if (_histSortKey === 'upload') return (item.upload_date || item.date || '') + (item.stem || '');
  return (item.date || '') + (item.stem || '');
}

/* 카드·상세에 보여줄 날짜 — 선택한 정렬 기준을 따른다(게시일 없으면 처리일). */
function histDisplayDate(item) {
  return _histSortKey === 'upload' ? (item.upload_date || item.date || '') : (item.date || '');
}

/* 정렬 기준별 버튼 문구 — 방향 버튼은 기준에 맞는 말로 바뀐다(날짜는 최신/과거, 길이는 긴/짧은). */
const HIST_SORT_LABELS = {
  date:     { key: '전사처리일', keyTitle: '전사 처리일 기준 (클릭하면 영상 게시일로)', asc: '과거순 ↑',  desc: '최신순 ↓' },
  upload:   { key: '영상게시일', keyTitle: '영상 게시일 기준 (클릭하면 영상 길이로)',   asc: '과거순 ↑',  desc: '최신순 ↓' },
  duration: { key: '영상길이',   keyTitle: '영상 길이 기준 (클릭하면 전사 처리일로)',   asc: '짧은순 ↑', desc: '긴순 ↓' },
};

function _updateSortBtn() {
  const L = HIST_SORT_LABELS[_histSortKey] || HIST_SORT_LABELS.date;
  const dir = document.getElementById('hf-sort-btn');
  if (dir) {
    dir.textContent = _histSort === 'asc' ? L.asc : L.desc;
    dir.title = `정렬 순서 전환(${L.desc.replace(/\s.*$/, '')} ↔ ${L.asc.replace(/\s.*$/, '')})`;
  }
  const key = document.getElementById('hf-sortkey-btn');
  if (key) {
    key.textContent = L.key;
    key.title = L.keyTitle;
    key.classList.toggle('on', _histSortKey !== 'date');   // 기본값이 아닐 때 강조
  }
}
function toggleHistorySort() {
  _histSort = (_histSort === 'asc') ? 'desc' : 'asc';
  localStorage.setItem('histSort', _histSort);
  _updateSortBtn();
  applyHistoryFilter();   // 정렬 변경은 1페이지부터
}
/* 정렬 기준 전환 — 카드에 찍히는 날짜도 이 기준을 따라 바뀐다. */
function toggleHistorySortKey() {
  const i = HIST_SORT_KEYS.indexOf(_histSortKey);
  _histSortKey = HIST_SORT_KEYS[(i + 1) % HIST_SORT_KEYS.length];
  localStorage.setItem('histSortKey', _histSortKey);
  _updateSortBtn();
  applyHistoryFilter();
}

/* 카드의 채널명 클릭 → 그 채널로 좁힌다. 제목·미읽음·북마크 등 나머지 조건은 그대로 둔다.
   이미 그 채널로 좁혀져 있으면 해제해 전체로 돌아간다(같은 버튼을 다시 누른 경우). */
function filterByUploader(uploader, ev) {
  if (ev) ev.stopPropagation();          // 카드 클릭(요약 열기)과 분리
  const sel = document.getElementById('hf-uploader');
  if (!sel || !uploader) return;
  const next = (sel.value === uploader) ? '' : uploader;
  if (next && ![...sel.options].some(o => o.value === next)) return;   // 목록에 없는 채널은 무시
  sel.value = next;
  applyHistoryFilter();
  document.getElementById('history-grid')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function toggleDistillFilter() {      // 채널 팝업 안 버튼(2026-11-22) — 팝업은 닫지 않는다
  _histDistillOnly = !_histDistillOnly;
  applyHistoryFilter();
}

function toggleBookmarkFilter() {
  _histBookmarkOnly = !_histBookmarkOnly;
  applyHistoryFilter();
}

/* 썸네일 우측 상단 북마크 토글 — 서버 저장 후 아이콘 즉시 갱신 */
async function toggleBookmark(btn, ev) {
  if (ev) ev.stopPropagation();          // 카드 클릭(요약 열기)과 분리
  const itemId = Number(btn.dataset.id);
  const next = btn.dataset.on !== '1';
  try {
    const result = await YS.apiBookmark(itemId, next);
    if (!result) return;
    if (result.revision) _historyRevision = result.revision;
  } catch { return; }
  const item = _historyItems.find(i => i.item_id === itemId);
  if (item) item.bookmark = next;
  btn.dataset.on = next ? '1' : '0';
  btn.classList.toggle('on', next);
  btn.title = next ? '북마크 해제' : '북마크';
  // 북마크만 보기 중 해제하면 목록에서 빠진다(페이지 유지)
  if (_histBookmarkOnly && !next) applyHistoryFilter(true);
}

function toggleUnreadFilter() {
  _histUnreadOnly = !_histUnreadOnly;
  document.getElementById('hf-unread-btn').classList.toggle('active', _histUnreadOnly);
  applyHistoryFilter();
}

async function toggleRead(btn) {
  const itemId  = Number(btn.dataset.id);
  const wasRead = btn.dataset.read === '1';
  const newRead = !wasRead;

  try {
    const result = await YS.apiMarkRead(itemId, newRead);
    if (!result) return;
    if (result.revision) _historyRevision = result.revision;
  } catch { return; }

  // _historyItems 내 해당 항목 업데이트
  const item = _historyItems.find(i => i.item_id === itemId);
  if (item) item.is_read = newRead;

  // 버튼 즉시 갱신 (파란점 제거/추가)
  btn.dataset.read = newRead ? '1' : '0';
  btn.innerHTML = newRead ? ICON_EYE_OFF : ICON_EYE;
  btn.title = newRead ? '읽음 (클릭 시 안읽음으로)' : '안읽음 (클릭 시 읽음으로)';
  btn.classList.toggle('is-read', newRead);

  const card = btn.closest('.hist-card');
  if (card) {
    card.classList.toggle('hist-card-unread', !newRead);
    const dot = card.querySelector('.hist-unread-dot');
    if (newRead && dot) dot.remove();
    else if (!newRead && !dot) {
      const titleEl = card.querySelector('.hist-card-title');
      if (titleEl) titleEl.insertAdjacentHTML('afterbegin', '<span class="hist-unread-dot"></span>');
    }
  }

  // 미읽음 필터 활성 중이면 읽음 처리된 카드 제거(현재 페이지 유지) — 카드가 사라져 버튼으로
  // 되돌릴 수 없으니 토스트로 복원 기회를 준다(2026-11-03 사용자 지시, sa·wm과 같은 방식).
  if (_histUnreadOnly && newRead) {
    applyHistoryFilter(true);
    YS.undoToast('읽음 처리했습니다', { onUndo: () => _undoRead(itemId) });
  }
}

async function _undoRead(itemId) {
  try {
    const result = await YS.apiMarkRead(itemId, false);
    if (!result) throw new Error();
    if (result.revision) _historyRevision = result.revision;
  } catch { alert('되돌리기 실패'); return; }
  const item = _historyItems.find(i => i.item_id === itemId);
  if (item) item.is_read = false;
  applyHistoryFilter(true);
}

// 삭제는 확인창 없이 카드를 바로 빼고, 6초 동안 되돌리기를 준 뒤 서버에서 지운다
// (2026-11-03 사용자 지시, sa·wm과 같은 방식 — yt 삭제는 파일까지 지워 서버 복원이 없어 확정을 늦춘다).
function deleteCard(btn) {
  const itemId = Number(btn.dataset.id);
  const idx = _historyItems.findIndex(i => i.item_id === itemId);
  if (idx === -1) return;
  const item = _historyItems[idx];
  YS.deleteWithUndo(itemId, {
    hide() { _historyItems = _historyItems.filter(i => i.item_id !== itemId); applyHistoryFilter(true); },
    restore() {
      if (!_historyItems.some(i => i.item_id === itemId)) _historyItems.splice(Math.min(idx, _historyItems.length), 0, item);
      applyHistoryFilter(true);
    },
    committed(result) { if (result.revision) _historyRevision = result.revision; },
    failed() { loadHistory(true); },
  });
}

/* 카드 ⋯ 메뉴 — 전사 보기 · YouTube에서 열기 · 삭제(2026-11-20 사용자 승인: 삭제를 자주 누르는 버튼 옆에서 뗀다) */
function openHistCardMenu(btn, ev) {
  if (ev) ev.stopPropagation();
  const id = Number(btn.dataset.id);
  const item = _historyItems.find(i => i.item_id === id);
  if (!item) return;
  YS.openCardMenu(btn, [
    { label: '전사 보기', disabled: !item.has_txt, onClick: () => openTranscriptModal(id, item.title) },
    { label: 'YouTube에서 열기', disabled: !item.webpage_url,
      onClick: () => window.open(item.webpage_url, '_blank', 'noopener') },
    { label: '삭제', danger: true, onClick: () => deleteCard({ dataset: { id: String(id) } }) },
  ]);
}

/* 썸네일은 전사 때 받아둔 로컬 사본(/thumb)만 쓴다 — 조회 중 외부(i.ytimg.com)로
   나가는 요청이 없어 VPN 경유에서도 즉시 뜬다. 사본이 없으면 자리표시로 대체한다. */
function _thumbFallback(img) { YS.thumbFallback(img); }

/* 창 폭이 바뀌어 열 수가 달라지면 보던 첫 카드가 들어 있는 페이지로 다시 그린다 */
let _histResizeTimer = 0;
function _onHistGridResize() {
  clearTimeout(_histResizeTimer);
  _histResizeTimer = setTimeout(() => {
    const before = _histPageSizeCache;
    if (!before || histPageSize() === before) return;
    const firstIdx = (_historyPage - 1) * before;
    _historyPage = Math.floor(firstIdx / _histPageSizeCache) + 1;
    _renderHistoryList();
  }, 200);
}
// 창 크기 대신 그리드 자체의 폭을 본다(사이드 패널·확대 배율이 바뀌어도 잡힌다)
if (typeof ResizeObserver !== 'undefined') {
  const _hg = document.getElementById('history-grid');
  if (_hg) new ResizeObserver(_onHistGridResize).observe(_hg);
} else {
  window.addEventListener('resize', _onHistGridResize);
}

function _historyGoToPage(p) {
  const totalPages = Math.max(1, Math.ceil(_historyFiltered.length / histPageSize()));
  _historyPage = Math.min(Math.max(1, p), totalPages);
  _saveHistFilter();   // 보던 페이지도 기억(새로고침 시 그 페이지로 복귀)
  _renderHistoryList();
  const grid = document.getElementById('history-grid');
  if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function _renderPagination(totalItems) {
  const pgEl = document.getElementById('history-pagination');
  const totalPages = Math.ceil(totalItems / histPageSize());
  if (totalPages <= 1) {
    pgEl.style.display = 'none';
    pgEl.innerHTML = '';
    return;
  }
  pgEl.style.display = '';
  const cur = _historyPage;
  const parts = [];
  parts.push(`<button class="pg-btn" onclick="_historyGoToPage(1)" ${cur===1?'disabled':''} title="처음">«</button>`);
  parts.push(`<button class="pg-btn" onclick="_historyGoToPage(${cur-1})" ${cur===1?'disabled':''} title="이전">‹</button>`);

  // 슬라이딩 윈도우: 현재 ±2, 처음/끝 항상 표시, 갭은 …
  const pages = new Set([1, totalPages, cur-2, cur-1, cur, cur+1, cur+2]);
  const visible = [...pages].filter(p => p >= 1 && p <= totalPages).sort((a,b) => a-b);
  let prev = 0;
  for (const p of visible) {
    if (prev && p - prev > 1) parts.push(`<span class="pg-ellipsis">…</span>`);
    if (p === cur) parts.push(`<button class="pg-btn pg-current">${p}</button>`);
    else           parts.push(`<button class="pg-btn" onclick="_historyGoToPage(${p})">${p}</button>`);
    prev = p;
  }

  parts.push(`<button class="pg-btn" onclick="_historyGoToPage(${cur+1})" ${cur===totalPages?'disabled':''} title="다음">›</button>`);
  parts.push(`<button class="pg-btn" onclick="_historyGoToPage(${totalPages})" ${cur===totalPages?'disabled':''} title="끝">»</button>`);
  pgEl.innerHTML = parts.join('');
}

// 순수 헬퍼는 공유 모듈(common.js)로 일원화 — 여기선 위임만(함수 선언이라 호이스팅 안전).
function _ytVideoId(url) { return YS.ytVideoId(url); }
function _attrEsc(s) { return YS.attrEscape(s); }

const ICON_MORE    = '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
const ICON_BOOKMARK = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>';
const ICON_EYE     = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_EYE_OFF = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

function _onHistCardClick(e) {
  if (e.target.closest('a, button')) return;
  const card = e.currentTarget;
  const itemId = Number(card.dataset.id);
  const title   = card.dataset.title;
  if (itemId) openSummaryModal(itemId, title);
}

function _renderHistoryList() {
  const grid    = document.getElementById('history-grid');
  const empty   = document.getElementById('history-empty');
  const countEl = document.getElementById('history-count');
  const total   = _historyItems.length;
  const shown   = _historyFiltered.length;

  countEl.textContent = shown === total
    ? `총 ${total}건`
    : `${shown}건 / 전체 ${total}건`;

  if (shown === 0) {
    grid.style.display = 'none';
    empty.textContent   = total === 0 ? '이력이 없습니다.' : '검색 결과가 없습니다.';
    empty.style.display = '';
    _renderPagination(0);
    return;
  }
  empty.style.display = 'none';
  grid.style.display = '';

  const pageSize  = histPageSize();       // 그리드가 보인 뒤에 열 수를 센다
  const totalPages = Math.ceil(shown / pageSize);
  if (_historyPage > totalPages) _historyPage = totalPages;
  const start = (_historyPage - 1) * pageSize;
  const pageItems = _historyFiltered.slice(start, start + pageSize);

  grid.innerHTML = pageItems.map(item => {
    const vid       = _ytVideoId(item.webpage_url);
    const thumbUrl  = vid ? `/thumb/${encodeURIComponent(vid)}` : '';
    // 로컬 사본만 사용 — 없으면 자리표시. 비공개로 바뀐 영상도 사본으로 계속 보인다.
    const thumbInner = thumbUrl
      ? `<img class="hist-thumb" src="${_attrEsc(thumbUrl)}" alt="" loading="lazy" data-vid="${_attrEsc(vid)}" onerror="_thumbFallback(this)">`
      : `<div class="hist-thumb-placeholder">썸네일 없음</div>`;
    const durBadge  = item.duration
      ? `<span class="hist-thumb-dur">${YS.fmtClock(item.duration)}</span>` : '';

    const bmBtn = `<button class="hist-bm-btn${item.bookmark ? ' on' : ''}"
      data-id="${item.item_id}" data-on="${item.bookmark ? '1' : '0'}"
      onclick="toggleBookmark(this, event)"
      title="${item.bookmark ? '북마크 해제' : '북마크'}">${ICON_BOOKMARK}</button>`;
    const thumbBlock = `<div class="hist-thumb-wrap">${thumbInner}${durBadge}${bmBtn}</div>`;

    // 채널명 클릭 = 같은 채널로 좁히기(다른 검색조건은 그대로). 유튜브 채널로 나가는
    // 링크는 상세 화면의 '업로더' 행에 그대로 있다.
    const uploaderHtml = (item.uploader && item.uploader !== '—')
      ? `<button type="button" class="hist-card-uploader is-filter" data-uploader="${_attrEsc(item.uploader)}"
           onclick="filterByUploader(this.dataset.uploader, event)"
           title="${_attrEsc(item.uploader)} 영상만 보기">${esc(item.uploader)}</button>`
      : `<span class="hist-card-uploader" title="${_attrEsc(item.uploader || '')}">${esc(item.uploader || '')}</span>`;

    const titleAttr = _attrEsc(item.title);
    const T = YS.cardTitles(item);       // 번역 제목을 크게, 원제는 아래 작게(2026-11-20)

    const readBtn = `<button class="hist-icon-btn hist-icon-btn-read${item.is_read ? ' is-read' : ''}"
      data-id="${item.item_id}" data-read="${item.is_read ? '1' : '0'}"
      onclick="toggleRead(this)" title="${item.is_read ? '읽음 (클릭 시 안읽음으로)' : '안읽음 (클릭 시 읽음으로)'}"
      >${item.is_read ? ICON_EYE_OFF : ICON_EYE}</button>`;
    // 덜 쓰는 동작(전사·YouTube·삭제)은 ⋯ 메뉴로(2026-11-20) — 요약 보기는 카드 클릭과 같아 뺐다
    const moreBtn = `<button class="hist-icon-btn hist-icon-btn-more" data-id="${item.item_id}"
      onclick="openHistCardMenu(this, event)" title="더 보기 — 전사 · YouTube · 삭제" aria-haspopup="menu"
      >${ICON_MORE}</button>`;

    const unreadDot = !item.is_read ? '<span class="hist-unread-dot"></span>' : '';
    const clickable = item.has_summary ? ' hist-card-clickable" onclick="_onHistCardClick(event)' : '';
    const cardData  = item.has_summary
      ? ` data-id="${item.item_id}" data-title="${titleAttr}"` : '';
    const unreadClass = !item.is_read ? ' hist-card-unread' : '';

    return `<div class="hist-card${unreadClass}${clickable}"${cardData}>
      ${thumbBlock}
      <div class="hist-card-body">
        <h3 class="hist-card-title" title="${titleAttr}">${unreadDot}${esc(T.main)}</h3>
        ${T.orig ? `<div class="hist-card-orig">${esc(T.orig)}</div>` : ''}
        <div class="hist-card-meta">${uploaderHtml}</div>
        <div class="hist-card-footer">
          <span class="hist-card-left"><span class="hist-card-date" title="${_histSortKey === 'upload' ? '영상 게시일' : '전사 처리일'}">${YS.fmtDateShort(histDisplayDate(item))}</span><span class="hist-card-id" data-id="${item.item_id}" title="클릭하면 id 복사">#${item.item_id}</span></span>
          <div class="hist-card-actions">${readBtn}${moreBtn}</div>
        </div>
      </div>
    </div>`;
  }).join('');

  _renderPagination(shown);
}

function openHistoryDetail(idx) {
  _historySelIdx = idx;
  _renderHistoryList();
  const item = _historyItems[idx];

  document.getElementById('hd-title').textContent = item.title;

  let metaHtml = '';
  metaHtml += `<div class="hd-meta-row"><span class="hd-meta-key">날짜</span><span class="hd-meta-val">${fmtDate(item.date)}</span></div>`;
  if (item.uploader && item.uploader !== '—') {
    const uploaderVal = item.channel_url
      ? `<a href="${esc(item.channel_url)}" target="_blank" rel="noopener">${esc(item.uploader)}</a>`
      : esc(item.uploader);
    metaHtml += `<div class="hd-meta-row"><span class="hd-meta-key">업로더</span><span class="hd-meta-val">${uploaderVal}</span></div>`;
  }
  if (item.categories && item.categories.length)
    metaHtml += `<div class="hd-meta-row"><span class="hd-meta-key">카테고리</span><span class="hd-meta-val">${esc(item.categories.join(', '))}</span></div>`;
  if (item.duration)
    metaHtml += `<div class="hd-meta-row"><span class="hd-meta-key">길이</span><span class="hd-meta-val">${fmtDurKo(item.duration)}</span></div>`;
  if (item.webpage_url)
    metaHtml += `<div class="hd-meta-row"><span class="hd-meta-key">URL</span><span class="hd-meta-val"><a href="${esc(item.webpage_url)}" target="_blank" rel="noopener">${esc(item.webpage_url)}</a></span></div>`;
  if (item.txt_path)
    metaHtml += `<div class="hd-meta-row hd-path-row">
      <span class="hd-meta-key">파일</span>
      <span class="hd-meta-val hd-path-val" id="hd-path-text" title="${esc(item.txt_path)}">${esc(item.txt_path)}</span>
      <button class="btn-sm btn-ghost hd-path-btn" onclick="copyFilePath()">경로 복사</button>
      <button class="btn-sm btn-ghost hd-path-btn" onclick="openExplorer()">탐색기</button>
    </div>`;
  document.getElementById('hd-meta-bar').innerHTML = metaHtml;

  const tagsWrap = document.getElementById('hd-tags-wrap');
  if (item.tags && item.tags.length) {
    document.getElementById('hd-tags').innerHTML =
      item.tags.map(t => `<span class="hd-tag-chip">${esc(t)}</span>`).join('');
    tagsWrap.style.display = '';
  } else {
    tagsWrap.style.display = 'none';
  }

  const textEl = document.getElementById('hd-text');
  if (!item.has_txt) {
    textEl.textContent = '전사 파일이 없습니다.';
  } else {
    textEl.textContent = '로드 중...';
    fetch('/history/text', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({txt_path: item.txt_path}),
    }).then(r => r.json()).then(d => {
      textEl.textContent = d.error ? `오류: ${d.error}` : (d.text || '');
    }).catch(e => { textEl.textContent = '오류: ' + e.message; });
  }

  document.getElementById('hd-modal').classList.add('open');
}

function closeHistoryDetail() {
  document.getElementById('hd-modal').classList.remove('open');
}

function copyHistoryUrl(url, btn) {
  const orig = btn ? btn.textContent : '';
  const done = () => {
    if (btn) { btn.textContent = '✓'; setTimeout(() => { btn.textContent = orig; }, 1200); }
  };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    done();
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done).catch(fallback);
  } else {
    fallback();
  }
}

function copyFilePath() {
  const path = document.getElementById('hd-path-text').textContent;
  navigator.clipboard.writeText(path).then(() => {
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = '복사됨!';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}

async function openExplorer() {
  const path = document.getElementById('hd-path-text').textContent;
  try {
    const r = await fetch('/history/explore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({txt_path: path}),
    });
    const d = await r.json();
    if (d.error) alert('오류: ' + d.error);
  } catch (e) {
    alert('오류: ' + e.message);
  }
}
function closeHistoryDetailBg(e) {
  if (e.target === document.getElementById('hd-modal')) closeHistoryDetail();
}

function copyHistoryText() {
  const text = document.getElementById('hd-text').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = '복사됨!';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}

function downloadHistoryText() {
  const text = document.getElementById('hd-text').textContent;
  if (!text || !_historyItems[_historySelIdx]) return;
  const item = _historyItems[_historySelIdx];
  const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = item.stem + '.txt';
  a.click();
  URL.revokeObjectURL(a.href);
}

function showTab(name) {
  document.getElementById('tab-' + name + '-btn').classList.remove('hidden');
}
