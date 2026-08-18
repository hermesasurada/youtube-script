const DEFAULT_PROMPT = (typeof __DEFAULT_PROMPT__ !== 'undefined') ? __DEFAULT_PROMPT__ : "다음 전사 텍스트를 한국어로 핵심만 간결히 요약해 주세요.";

let currentJobId    = null;
let currentTxtPath  = null;   // 전사 md 경로(요약 호출에 사용; 잡 ID와 분리)
let activeTab       = 'history';
let saveTimer       = null;
let currentSource   = 'url';
let selectedFilePath = '';

/* ── Prompt persistence (사이드바 팝업) ── */
async function loadSavedPrompt() {
  try {
    const r = await fetch('/prompt');
    const d = await r.json();
    document.getElementById('prompt-text').value = d.prompt || DEFAULT_PROMPT;
  } catch {
    document.getElementById('prompt-text').value = DEFAULT_PROMPT;
  }
}

function openPromptModal() {
  document.getElementById('prompt-warn').hidden = true;
  document.getElementById('save-status').textContent = '';
  document.getElementById('prompt-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
  const ta = document.getElementById('prompt-text');
  ta.focus();
  // 커서를 끝으로
  ta.selectionStart = ta.selectionEnd = ta.value.length;
}

function closePromptModal() {
  document.getElementById('prompt-overlay').hidden = true;
  document.body.style.overflow = '';
}

function handlePromptOverlayClick(e) {
  if (e.target.id === 'prompt-overlay') closePromptModal();
}

/* ── 채널 자동 모니터 모달 ── */
async function openChannelsModal() {
  document.getElementById('channels-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
  await loadChannels();
}
function closeChannelsModal() {
  document.getElementById('channels-overlay').hidden = true;
  document.body.style.overflow = '';
}
function handleChannelsOverlayClick(e) {
  if (e.target.id === 'channels-overlay') closeChannelsModal();
}
const MONITOR_MODELS = ['opus', 'gpt', 'grok'];
const MONITOR_MODEL_LABELS = { opus: 'Opus 5', gpt: 'GPT-5.6 Sol', grok: 'Grok-4.5' };
let monitorModelOrders = { summary: [...MONITOR_MODELS], capture: [...MONITOR_MODELS] };

function renderMonitorModelOrders() {
  for (const kind of ['summary', 'capture']) {
    const el = document.getElementById(kind + '-model-order');
    if (!el) continue;
    const order = monitorModelOrders[kind] || MONITOR_MODELS;
    el.innerHTML = order.map((selected, index) => {
      const options = MONITOR_MODELS.map(model =>
        `<option value="${model}" ${model === selected ? 'selected' : ''}>${MONITOR_MODEL_LABELS[model]}</option>`
      ).join('');
      return `<label class="model-order-slot"><span class="model-order-rank">${index + 1}</span>`
        + `<select class="model-order-select" aria-label="${kind === 'summary' ? '요약' : '캡처'} ${index + 1}순위" `
        + `onchange="changeMonitorModelOrder('${kind}', ${index}, this.value)">${options}</select></label>`;
    }).join('');
  }
}

async function changeMonitorModelOrder(kind, index, selected) {
  const original = [...monitorModelOrders[kind]];
  const previous = [...original];
  const other = previous.indexOf(selected);
  if (other !== index) {
    [previous[index], previous[other]] = [previous[other], previous[index]];
  }
  monitorModelOrders[kind] = previous;
  renderMonitorModelOrders();
  const selects = document.querySelectorAll('.model-order-select');
  const status = document.getElementById('model-order-status');
  selects.forEach(el => { el.disabled = true; });
  status.textContent = '저장 중…';
  try {
    const d = await (await fetch('/channels/model-orders', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ [kind]: previous }),
    })).json();
    if (d.error) throw new Error(d.error);
    monitorModelOrders = d.model_orders;
    status.textContent = '저장됨';
  } catch (e) {
    monitorModelOrders[kind] = original;
    status.textContent = '저장 실패';
    alert('모델 순서 저장 실패: ' + e.message);
  } finally {
    renderMonitorModelOrders();
    window.setTimeout(() => { if (status.textContent === '저장됨') status.textContent = ''; }, 1400);
  }
}

async function loadChannels() {
  const list = document.getElementById('channels-list');
  const qEl  = document.getElementById('channels-queue');
  list.innerHTML = '<li class="channels-empty">불러오는 중…</li>';
  qEl.textContent = '';
  try {
    const d = await (await fetch('/channels')).json();
    if (d.error) throw new Error(d.error);
    monitorModelOrders = d.model_orders || monitorModelOrders;
    renderMonitorModelOrders();
    const q = d.queue || {};
    const parts = [];
    if (q.pending)    parts.push(`대기 ${q.pending}`);
    if (q.processing) parts.push(`처리중 ${q.processing}`);
    if (q.done)       parts.push(`완료 ${q.done}`);
    if (q.failed)     parts.push(`실패 ${q.failed}`);
    qEl.textContent = parts.length ? '큐: ' + parts.join(' · ') : '큐 비어있음';
    const chans = d.channels || [];
    if (!chans.length) { list.innerHTML = '<li class="channels-empty">등록된 채널이 없습니다.</li>'; return; }
    list.innerHTML = chans.map(c => {
      const on = !!c.enabled;
      const sub = (c.handle ? '@' + c.handle : c.channel_id) +
                  (c.last_checked ? ` · 확인 ${esc(c.last_checked.slice(5, 16))}` : '');
      // min_duration 뱃지: 0=면제(∞), 그 외 값=커스텀 최소길이(N분+). NULL=전역 기본(3분, 표시 없음)
      const noLimit = c.min_duration === 0
        ? ' <span class="channel-nolimit" title="길이제한 면제 — 짧은 영상도 수집">∞</span>'
        : (c.min_duration != null
          ? ` <span class="channel-nolimit" title="최소 길이 ${Math.round(c.min_duration / 60)}분 — 그 미만은 수집 제외">${Math.round(c.min_duration / 60)}분+</span>` : '');
      const dis = c.distill == null ? true : !!c.distill;   // 기본은 증류 대상
      return `<li class="channel-row">
        <div class="channel-meta">
          <span class="channel-name">${esc(c.title || c.handle || c.channel_id)}${noLimit}</span>
          <span class="channel-sub">${esc(sub)}</span>
        </div>
        <button class="channel-distill ${dis ? 'on' : ''}" role="switch" aria-checked="${dis}"
                title="지식증류(옵시디언 볼트) 대상 — 끄면 증류에서 제외"
                onclick="toggleChannelDistill(${c.id}, ${dis ? 'false' : 'true'}, this)">증류</button>
        <button class="channel-toggle ${on ? 'on' : ''}" role="switch" aria-checked="${on}"
                title="자동 수집 ON/OFF"
                onclick="toggleChannel(${c.id}, ${on ? 'false' : 'true'}, this)">
          <span class="channel-toggle-knob"></span>
        </button>
      </li>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<li class="channels-empty">오류: ${esc(e.message)}</li>`;
  }
}
async function toggleChannel(id, enable, btn) {
  btn.disabled = true;
  try {
    const d = await (await fetch('/channels/' + id, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enable}),
    })).json();
    if (d.error) throw new Error(d.error);
    btn.classList.toggle('on', d.enabled);
    btn.setAttribute('aria-checked', String(d.enabled));
    btn.setAttribute('onclick', `toggleChannel(${id}, ${d.enabled ? 'false' : 'true'}, this)`);
  } catch (e) {
    alert('토글 실패: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* 지식증류 대상 토글 — 끄면 hermes 증류 파이프라인이 그 채널 문서를 건너뛴다. */
async function toggleChannelDistill(id, enable, btn) {
  btn.disabled = true;
  try {
    const d = await (await fetch('/channels/' + id, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({distill: enable}),
    })).json();
    if (d.error) throw new Error(d.error);
    btn.classList.toggle('on', d.distill);
    btn.setAttribute('aria-checked', String(d.distill));
    btn.setAttribute('onclick', `toggleChannelDistill(${id}, ${d.distill ? 'false' : 'true'}, this)`);
  } catch (e) {
    alert('증류 설정 실패: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* {transcript} 자리표시자 검증 후 저장. 누락 시 차단(요약 시 전사 본문이 안 들어감). */
async function savePromptFromModal() {
  const text = document.getElementById('prompt-text').value;
  const warn = document.getElementById('prompt-warn');
  if (!text.includes('{transcript}')) {
    warn.hidden = false;
    warn.textContent = '⚠ {transcript} 자리표시자가 없습니다. 이 위치에 전사 본문이 삽입되므로 반드시 포함해야 합니다.';
    return;
  }
  warn.hidden = true;
  const status = document.getElementById('save-status');
  status.textContent = '저장 중...';
  try {
    const r = await fetch('/prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: text}),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      status.textContent = '';
      warn.hidden = false;
      warn.textContent = '⚠ ' + (d.error || '저장 실패');
      return;
    }
    status.textContent = '저장됨 ✓';
    setTimeout(closePromptModal, 500);
  } catch {
    status.textContent = '';
    warn.hidden = false;
    warn.textContent = '⚠ 저장 실패 (네트워크 오류)';
  }
}

const IS_REMOTE = document.body.classList.contains('remote-mode');
if (!IS_REMOTE) loadSavedPrompt();   // /prompt 는 원격 비허용

/* ── 백그라운드 키프레임 처리 상태 배지(헤더) ── */
async function _pollKfStatus() {
  const el = document.getElementById('kf-status');
  if (!el) return;
  try {
    const d = await (await fetch('/keyframes/status')).json();
    if (d.processing) {
      const t = d.processing.length > 30 ? d.processing.slice(0, 30) + '…' : d.processing;
      el.textContent = `🎞 캡처 처리 중: ${t}` + (d.queued ? `  ·  대기 ${d.queued}` : '');
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  } catch { /* 무시 */ }
}
if (!IS_REMOTE) { _pollKfStatus(); setInterval(_pollKfStatus, 4000); }

/* ── Tab switching ── */
function switchTab(name) {
  activeTab = name;
  ['log', 'result', 'summary', 'history'].forEach(t => {
    document.getElementById('tab-' + t + '-btn').classList.toggle('active', t === name);
    document.getElementById('pane-' + t).classList.toggle('active', t === name);
  });
  if (name === 'result')  document.getElementById('result-badge').classList.remove('visible');
  if (name === 'summary') document.getElementById('summary-badge').classList.remove('visible');
  if (name === 'history') loadHistory();   // revision만 확인하고 바뀐 경우에만 목록 재수신
}

/* ── History ── */
let _historyLoaded   = false;
let _historyItems    = [];
let _historyFiltered = [];
let _historySelIdx   = -1;
let _historyPage     = 1;
let _historyRevision = '';
let _histUnreadOnly  = false;
let _histSort        = localStorage.getItem('histSort') || 'desc';   // 'desc'=최신순(기본), 'asc'=과거순
let _histSortKey     = localStorage.getItem('histSortKey') || 'date'; // 'date'=전사 처리일(기본), 'upload'=영상 게시일
const HIST_PAGE_SIZE = 20;

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
    _histRestorePage = Number(s.page) > 1 ? Number(s.page) : 1;   // 필터 적용 후 반영
  } finally {
    _histRestoring = false;
  }
}
let _histRestorePage = 1;

loadHistory();

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

function clearTitleSearch() {
  const inp = document.getElementById('hf-title');
  inp.value = '';
  document.getElementById('hf-clear-btn').classList.remove('visible');
  applyHistoryFilter();
  inp.focus();
}

function applyHistoryFilter(keepPage = false) {
  const uploader = document.getElementById('hf-uploader').value;
  const titleQ   = document.getElementById('hf-title').value.trim().toLowerCase();
  document.getElementById('hf-clear-btn').classList.toggle('visible', titleQ.length > 0);

  _historyFiltered = _historyItems.filter(item => {
    if (uploader && item.uploader !== uploader)  return false;
    if (titleQ   && !item.title.toLowerCase().includes(titleQ)) return false;
    if (_histUnreadOnly && item.is_read) return false;
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
  if (_histSortKey === 'upload') return (item.upload_date || item.date || '') + (item.stem || '');
  return (item.date || '') + (item.stem || '');
}

/* 카드·상세에 보여줄 날짜 — 선택한 정렬 기준을 따른다(게시일 없으면 처리일). */
function histDisplayDate(item) {
  return _histSortKey === 'upload' ? (item.upload_date || item.date || '') : (item.date || '');
}

function _updateSortBtn() {
  const dir = document.getElementById('hf-sort-btn');
  if (dir) dir.textContent = _histSort === 'asc' ? '과거순 ↑' : '최신순 ↓';
  const key = document.getElementById('hf-sortkey-btn');
  if (key) {
    key.textContent = _histSortKey === 'upload' ? '영상게시일' : '전사처리일';
    key.title = _histSortKey === 'upload'
      ? '영상 게시일 기준 (클릭하면 전사 처리일로)'
      : '전사 처리일 기준 (클릭하면 영상 게시일로)';
    key.classList.toggle('on', _histSortKey === 'upload');   // 기본값이 아닐 때 강조
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
  _histSortKey = (_histSortKey === 'upload') ? 'date' : 'upload';
  localStorage.setItem('histSortKey', _histSortKey);
  _updateSortBtn();
  applyHistoryFilter();
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

  // 미읽음 필터 활성 중이면 읽음 처리된 카드 제거(현재 페이지 유지)
  if (_histUnreadOnly && newRead) applyHistoryFilter(true);
}

async function deleteCard(btn) {
  const itemId = Number(btn.dataset.id);
  const title  = btn.dataset.title || '이 항목';
  if (!confirm(`"${title}" 을(를) 삭제하시겠습니까?\n\n전사 파일과 요약 파일이 모두 삭제됩니다.`)) return;

  btn.disabled = true;
  try {
    const result = await YS.apiDeleteItem(itemId);
    if (!result) { btn.disabled = false; return; }
    if (result.revision) _historyRevision = result.revision;
  } catch { btn.disabled = false; return; }

  // _historyItems에서 제거
  const idx = _historyItems.findIndex(i => i.item_id === itemId);
  if (idx !== -1) _historyItems.splice(idx, 1);

  // 카드 DOM 제거 후 목록 다시 렌더(현재 페이지 유지)
  applyHistoryFilter(true);
}

/* 썸네일은 전사 때 받아둔 로컬 사본(/thumb)만 쓴다 — 조회 중 외부(i.ytimg.com)로
   나가는 요청이 없어 VPN 경유에서도 즉시 뜬다. 사본이 없으면 자리표시로 대체한다. */
function _thumbFallback(img) {
  img.onerror = null;
  const ph = document.createElement('div');
  ph.className = 'hist-thumb-placeholder';
  ph.textContent = '썸네일 없음';
  img.replaceWith(ph);
}

function _historyGoToPage(p) {
  const totalPages = Math.max(1, Math.ceil(_historyFiltered.length / HIST_PAGE_SIZE));
  _historyPage = Math.min(Math.max(1, p), totalPages);
  _saveHistFilter();   // 보던 페이지도 기억(새로고침 시 그 페이지로 복귀)
  _renderHistoryList();
  const grid = document.getElementById('history-grid');
  if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function _renderPagination(totalItems) {
  const pgEl = document.getElementById('history-pagination');
  const totalPages = Math.ceil(totalItems / HIST_PAGE_SIZE);
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

function resetHistoryFilter() {
  document.getElementById('hf-uploader').value  = '';
  document.getElementById('hf-title').value      = '';
  _histUnreadOnly = false;
  document.getElementById('hf-unread-btn').classList.remove('active');
  _histSort    = 'desc';                                // 정렬도 기본(전사처리일·최신순)으로
  _histSortKey = 'date';
  localStorage.setItem('histSort', _histSort);
  localStorage.setItem('histSortKey', _histSortKey);
  _updateSortBtn();
  applyHistoryFilter();
}

// 순수 헬퍼는 공유 모듈(common.js)로 일원화 — 여기선 위임만(함수 선언이라 호이스팅 안전).
function _ytVideoId(url) { return YS.ytVideoId(url); }
function _attrEsc(s) { return YS.attrEscape(s); }

const ICON_SUMMARY  = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2h7l3 3v9H3V2z"/><path d="M10 2v3h3"/><path d="M5.5 8h5M5.5 10.5h5M5.5 6h2.5"/></svg>';
const ICON_TRANSCRIPT = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5h11M2.5 6.5h11M2.5 9.5h11M2.5 12.5h7"/></svg>';
const ICON_YOUTUBE = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M14.7 4.4a1.85 1.85 0 0 0-1.3-1.3C12.2 2.8 8 2.8 8 2.8s-4.2 0-5.4.3A1.85 1.85 0 0 0 1.3 4.4 19.4 19.4 0 0 0 1 8a19.4 19.4 0 0 0 .3 3.6 1.85 1.85 0 0 0 1.3 1.3c1.2.3 5.4.3 5.4.3s4.2 0 5.4-.3a1.85 1.85 0 0 0 1.3-1.3A19.4 19.4 0 0 0 15 8a19.4 19.4 0 0 0-.3-3.6zM6.6 10.4V5.6L10.7 8z"/></svg>';
const ICON_DELETE  = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>';
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

  const totalPages = Math.ceil(shown / HIST_PAGE_SIZE);
  if (_historyPage > totalPages) _historyPage = totalPages;
  const start = (_historyPage - 1) * HIST_PAGE_SIZE;
  const pageItems = _historyFiltered.slice(start, start + HIST_PAGE_SIZE);

  grid.innerHTML = pageItems.map(item => {
    const vid       = _ytVideoId(item.webpage_url);
    const thumbUrl  = vid ? `/thumb/${encodeURIComponent(vid)}` : '';
    // 로컬 사본만 사용 — 없으면 자리표시. 비공개로 바뀐 영상도 사본으로 계속 보인다.
    const thumbInner = thumbUrl
      ? `<img class="hist-thumb" src="${_attrEsc(thumbUrl)}" alt="" loading="lazy" data-vid="${_attrEsc(vid)}" onerror="_thumbFallback(this)">`
      : `<div class="hist-thumb-placeholder">썸네일 없음</div>`;
    const durBadge  = item.duration
      ? `<span class="hist-thumb-dur">${fmtDurKo(item.duration)}</span>` : '';

    const thumbBlock = `<div class="hist-thumb-wrap">${thumbInner}${durBadge}</div>`;

    const uploaderHtml = item.channel_url
      ? `<a class="hist-card-uploader" href="${_attrEsc(item.channel_url)}" target="_blank" rel="noopener" title="${_attrEsc(item.uploader)}">${esc(item.uploader)}</a>`
      : `<span class="hist-card-uploader" title="${_attrEsc(item.uploader)}">${esc(item.uploader)}</span>`;

    const titleAttr = _attrEsc(item.title);
    const titleHtml = `<h3 class="hist-card-title" title="${titleAttr}">${esc(item.title)}</h3>`;

    const sumBtn  = item.has_summary
      ? `<button class="hist-icon-btn hist-icon-btn-summary" data-id="${item.item_id}" data-title="${titleAttr}" onclick="openSummaryModal(Number(this.dataset.id),this.dataset.title)" title="요약 보기">${ICON_SUMMARY}</button>`
      : `<button class="hist-icon-btn hist-icon-btn-summary" disabled title="요약 없음">${ICON_SUMMARY}</button>`;
    const txtBtn  = item.has_txt
      ? `<button class="hist-icon-btn" data-id="${item.item_id}" data-title="${titleAttr}" onclick="openTranscriptModal(Number(this.dataset.id),this.dataset.title)" title="전사 보기">${ICON_TRANSCRIPT}</button>`
      : `<button class="hist-icon-btn" disabled title="전사 없음">${ICON_TRANSCRIPT}</button>`;
    const ytBtn   = item.webpage_url
      ? `<a class="hist-icon-btn hist-icon-btn-youtube" href="${_attrEsc(item.webpage_url)}" target="_blank" rel="noopener" title="YouTube에서 열기">${ICON_YOUTUBE}</a>`
      : `<button class="hist-icon-btn hist-icon-btn-youtube" disabled title="URL 없음">${ICON_YOUTUBE}</button>`;
    const readBtn = `<button class="hist-icon-btn hist-icon-btn-read${item.is_read ? ' is-read' : ''}"
      data-id="${item.item_id}" data-read="${item.is_read ? '1' : '0'}"
      onclick="toggleRead(this)" title="${item.is_read ? '읽음 (클릭 시 안읽음으로)' : '안읽음 (클릭 시 읽음으로)'}"
      >${item.is_read ? ICON_EYE_OFF : ICON_EYE}</button>`;
    const delBtn  = `<button class="hist-icon-btn hist-icon-btn-delete"
      data-id="${item.item_id}" data-title="${titleAttr}"
      onclick="deleteCard(this)" title="삭제"
      >${ICON_DELETE}</button>`;

    const unreadDot = !item.is_read ? '<span class="hist-unread-dot"></span>' : '';
    const clickable = item.has_summary ? ' hist-card-clickable" onclick="_onHistCardClick(event)' : '';
    const cardData  = item.has_summary
      ? ` data-id="${item.item_id}" data-title="${titleAttr}"` : '';
    const unreadClass = !item.is_read ? ' hist-card-unread' : '';

    return `<div class="hist-card${unreadClass}${clickable}"${cardData}>
      ${thumbBlock}
      <div class="hist-card-body">
        <h3 class="hist-card-title" title="${titleAttr}">${unreadDot}${esc(item.title)}</h3>
        <div class="hist-card-meta">${uploaderHtml}</div>
        <div class="hist-card-footer">
          <span class="hist-card-date" title="${_histSortKey === 'upload' ? '영상 게시일' : '전사 처리일'}">${fmtDate(histDisplayDate(item))}</span>
          <div class="hist-card-actions">${readBtn}${sumBtn}${txtBtn}${ytBtn}${delBtn}</div>
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

/* ── Prompt toggle ── */
/* ── Progress bar ── */
function setProgress(pct) {
  const fill = document.getElementById('progress-fill');
  const pctEl = document.getElementById('progress-pct');
  fill.classList.remove('indeterminate');
  fill.style.width = pct + '%';
  pctEl.textContent = pct + '%';
  if (pct >= 100) {
    document.getElementById('progress-label').textContent = '전사 완료';
  } else {
    document.getElementById('progress-label').textContent = '전사 중...';
  }
}

function startIndeterminate() {
  const fill = document.getElementById('progress-fill');
  fill.classList.add('indeterminate');
  fill.style.width = '';
  document.getElementById('progress-pct').textContent = '—';
  document.getElementById('progress-label').textContent = '영상 정보 수집 중...';
}

/* ── Helpers ── */
function setRunning(on) {
  const btn  = document.getElementById('start-btn');
  const stop = document.getElementById('stop-btn');
  btn.disabled    = on;
  btn.textContent = on ? '처리 중...' : '전사 시작';
  stop.classList.toggle('visible', on);
  stop.disabled    = false;
  stop.textContent = '중지';
}

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.textContent = msg;
  el.style.display = 'block';
}

function clearError() {
  document.getElementById('error-banner').style.display = 'none';
}

function appendLog(line) {
  const el = document.getElementById('log-area');
  if (el.textContent === '대기 중...') el.textContent = '';
  el.textContent += line + '\n';
  el.scrollTop = el.scrollHeight;
}

function copyResult() {
  navigator.clipboard.writeText(document.getElementById('result-area').textContent).then(() => {
    const btn = event.target;
    btn.textContent = '복사됨!';
    setTimeout(() => { btn.textContent = '클립보드 복사'; }, 1500);
  });
}

function downloadResult() {
  if (currentJobId) window.location = '/download/' + currentJobId;
}

function copySummary() {
  navigator.clipboard.writeText(document.getElementById('summary-area').textContent).then(() => {
    const btn = document.getElementById('copy-summary-btn');
    btn.textContent = '복사됨!';
    setTimeout(() => { btn.textContent = '복사'; }, 1500);
  });
}

/* ── 처리 큐 팝업 — 대기 목록 조회·순서 변경·취소 ── */
const _Q_STATUS = {
  processing: ['처리 중', 'q-st-run'], pending: ['대기', 'q-st-wait'],
  kf_retry: ['캡처 재시도', 'q-st-wait'], deferred: ['재시도 예약', 'q-st-defer'],
  done: ['완료', 'q-st-done'], failed: ['실패', 'q-st-fail'],
};

function openQueueModal() {
  document.getElementById('queue-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
  refreshQueueModal();
}
function closeQueueModal() {
  document.getElementById('queue-overlay').hidden = true;
  document.body.style.overflow = '';
}

async function refreshQueueModal() {
  const body = document.getElementById('queue-modal-body');
  try {
    const d = await (await fetch('/queue/items')).json();
    const row = (v, i, movable) => {
      const [label, cls] = _Q_STATUS[v.status] || [v.status, ''];
      const ch = v.channel_name || (v.channel_id === 'manual' ? '수동' : '') || '';
      const extra = v.status === 'deferred' && v.next_retry_at
        ? `<span class="q-extra">${_attrEsc(v.next_retry_at.slice(5, 16))} 재시도</span>`
        : (v.attempt_count > 1 ? `<span class="q-extra">시도 ${v.attempt_count}</span>` : '');
      const ctl = movable ? `
        <span class="q-ctl">
          <button onclick="moveQueueItem(${v.id},'up')" title="위로">↑</button>
          <button onclick="moveQueueItem(${v.id},'down')" title="아래로">↓</button>
          <button class="q-del" onclick="cancelQueueItem(${v.id})" title="취소">×</button>
        </span>` : '';
      return `<div class="q-row">
        <span class="q-no">${i}</span>
        <span class="q-st ${cls}">${label}</span>
        <div class="q-main">
          <div class="q-title">${_attrEsc(v.title || v.yt_id)}</div>
          <div class="q-sub">${_attrEsc(ch)}${ch && extra ? ' · ' : ''}${extra}</div>
        </div>${ctl}</div>`;
    };
    let n = 0;
    const waiting = d.waiting.map(v => row(v, ++n,
      v.status === 'pending' || v.status === 'kf_retry')).join('');
    const recent = d.recent.map(v => row(v, '', false)).join('');
    body.innerHTML =
      `<div class="q-sec">대기 · ${d.waiting.length}건 <span class="q-hint">30분에 1건 처리</span></div>`
      + (waiting || '<div class="q-empty">대기 중인 영상이 없습니다.</div>')
      + (recent ? `<div class="q-sec">최근 처리</div>${recent}` : '');
  } catch (e) {
    body.innerHTML = `<div class="q-empty">불러오기 실패: ${_attrEsc(e.message)}</div>`;
  }
}

async function moveQueueItem(id, direction) {
  await fetch(`/queue/items/${id}/move`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({direction}),
  });
  refreshQueueModal();
}

async function cancelQueueItem(id) {
  if (!confirm('이 영상을 큐에서 취소할까요?')) return;
  await fetch(`/queue/items/${id}`, {method: 'DELETE'});
  refreshQueueModal();
}

/* ── Start transcription ── */
/* URL 전사는 서버 큐(watch_queue)에 줄을 선다 — 자동 모니터와 같은 파이프라인이
   30분에 한 건씩 처리한다. 입력창 URL + 사이드바 대기열의 대기 항목을 전부 적재.
   (파일 업로드는 유튜브와 무관하므로 종전대로 즉시 처리) */
async function enqueueUrls() {
  clearError();
  const input = document.getElementById('url-input');
  const urls = [];
  if (input.value.trim()) urls.push(input.value.trim());
  urlQueue.filter(i => i.status === 'waiting').forEach(i => {
    if (!urls.includes(i.url)) urls.push(i.url);
  });
  if (!urls.length) { showError('URL을 입력해주세요.'); return; }

  const logEl = document.getElementById('log-area');
  switchTab('log');
  const lines = [];
  for (const u of urls) {
    try {
      const r = await fetch('/queue/items', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url: u}),
      });
      const d = await r.json();
      if (d.ok) {
        lines.push(`✅ 큐 등록 (대기 ${d.waiting}번째): ${d.title}`);
        const qi = urlQueue.find(i => i.url === u);
        if (qi) { urlQueue.splice(urlQueue.indexOf(qi), 1); }
        if (input.value.trim() === u) input.value = '';
      } else {
        lines.push(`⚠️ ${d.error || '등록 실패'}: ${u}`);
      }
    } catch (e) {
      lines.push(`⚠️ 서버 연결 실패: ${e.message}`);
    }
    logEl.textContent = lines.join('\n') +
      '\n\n큐는 30분에 한 건씩 자동 처리됩니다. 우측 상단 [큐] 버튼에서 순서를 바꿀 수 있습니다.';
  }
  renderQueue();
  openQueueModal();          // 등록 직후 현재 대기 순서를 바로 보여준다
}

async function startTranscription() {
  if (currentSource === 'url') { await enqueueUrls(); return; }
  if (_queueLaunchTimer) {
    clearTimeout(_queueLaunchTimer);
    _queueLaunchTimer = null;
  }
  clearError();

  if (currentSource === 'url' && !document.getElementById('url-input').value.trim()) {
    const first = urlQueue.find(i => i.status === 'waiting');
    if (first) document.getElementById('url-input').value = first.url;
  }

  let payload;
  try { payload = buildPayload(); }
  catch (e) { showError(e.message); return; }

  if (payload.source === 'url') {
    const _url = payload.url;
    const _existing = urlQueue.find(i => i.url === _url && i.status === 'waiting');
    if (_existing) {
      _existing.status = 'running';
      _existing.error = '';
      _existing.errorStage = '';
      _existing.retryFrom = '';
    } else if (!_runningItem()) {
      urlQueue.push({url: _url, meta: null, status: 'running', error: '', errorStage: '', txtPath: ''});
    }
    renderQueue();
  }

  document.getElementById('pane-result').classList.remove('active');
  document.getElementById('pane-summary').classList.remove('active');
  document.getElementById('tab-result-btn').classList.add('hidden');
  document.getElementById('tab-summary-btn').classList.add('hidden');
  document.getElementById('log-area').textContent = '';
  document.getElementById('result-area').textContent = '';
  document.getElementById('summary-area').innerHTML = '<span class="summary-placeholder">요약이 여기에 표시됩니다.</span>';
  document.getElementById('summary-status').textContent = '';
  document.getElementById('video-info-bar').style.display = 'none';
  document.getElementById('vi-title').textContent = '';
  document.getElementById('vi-uploader').textContent = '';
  switchTab('log');
  setRunning(true);

  document.getElementById('progress-section').classList.add('visible');
  document.getElementById('duration-row').style.display = 'none';
  startIndeterminate();
  initStagebar(payload.source || 'url');

  let resp;
  try {
    resp = await fetch('/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
  } catch (e) {
    const reason = '서버 연결 실패: ' + e.message;
    showError(reason);
    setRunning(false);
    _failCurrentAndNext(reason, 'connection');
    return;
  }

  let data;
  try {
    data = await resp.json();
  } catch (e) {
    const reason = `작업 시작 응답 오류 (HTTP ${resp.status})`;
    showError(reason);
    setRunning(false);
    _failCurrentAndNext(reason, 'start');
    return;
  }
  if (data.error) {
    showError(data.error);
    setRunning(false);
    _failCurrentAndNext(data.error, 'start');
    return;
  }
  currentJobId = data.job_id;

  const es = new EventSource('/stream/' + currentJobId);
  let jobLastError = '';
  const jobLogTail = [];

  es.onmessage = (e) => {
    appendLog(e.data);
    if (e.data.trim()) {
      jobLogTail.push(e.data.trim());
      if (jobLogTail.length > 3) jobLogTail.shift();
    }
    if (/오류|error|failed|실패/i.test(e.data)) jobLastError = e.data.trim();
  };

  es.addEventListener('duration', (e) => {
    const { seconds } = JSON.parse(e.data);
    // Show duration in sidebar
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    document.getElementById('duration-val').textContent =
      [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
    document.getElementById('duration-row').style.display = '';
    // Switch from indeterminate to 0%
    const fill = document.getElementById('progress-fill');
    fill.classList.remove('indeterminate');
    fill.style.width = '0%';
    document.getElementById('progress-pct').textContent = '0%';
    document.getElementById('progress-label').textContent = '다운로드 중...';
    const _ri1 = _runningItem();
    if (_ri1) { _ri1.meta = _ri1.meta || {}; _ri1.meta.duration = seconds; renderQueue(); }
  });

  es.addEventListener('videoinfo', (e) => {
    const { title, uploader } = JSON.parse(e.data);
    const bar = document.getElementById('video-info-bar');
    document.getElementById('vi-title').textContent = title || '';
    document.getElementById('vi-uploader').textContent = (uploader && uploader !== 'N/A') ? uploader : '';
    bar.style.display = title ? '' : 'none';
    const _ri2 = _runningItem();
    if (_ri2) {
      _ri2.meta = _ri2.meta || {};
      _ri2.meta.title    = title || '';
      _ri2.meta.uploader = (uploader && uploader !== 'N/A') ? uploader : '';
      renderQueue();
    }
  });

  es.addEventListener('stage', (e) => {
    const { stage } = JSON.parse(e.data);
    setStage(stage);
    if (stage === 'transcribe') {
      document.getElementById('progress-label').textContent = '전사 중...';
    }
  });

  es.addEventListener('progress', (e) => {
    const { pct } = JSON.parse(e.data);
    setProgress(pct);
  });

  es.addEventListener('done', async () => {
    es.close();
    setRunning(false);
    setProgress(100);

    let result;
    try {
      const r = await fetch('/result/' + currentJobId);
      result = await r.json();
    } catch (e) {
      const reason = '작업 결과 조회 실패: ' + e.message;
      showError(reason);
      _failCurrentAndNext(reason, 'result');
      return;
    }

    if (result.status === 'done' && result.result) {
      currentTxtPath = result.txt_path || null;
      const running = _runningItem();
      if (running) running.txtPath = currentTxtPath || '';
      document.getElementById('result-area').textContent = result.result;
      document.getElementById('result-fname').textContent = result.filename || '';
      showTab('result');
      showTab('summary');
      document.getElementById('result-badge').classList.add('visible');
      if (document.getElementById('auto-summarize').checked) {
        switchTab('summary');
        const summarized = await generateSummary();
        if (summarized.ok) _autoStartNext();
        else _failCurrentAndNext(summarized.error, 'summary');
      } else {
        switchTab('result');
        _autoStartNext();
      }
    } else if (result.status === 'cancelled') {
      appendLog('⏹ 전사가 중지되었습니다.');
      const _ri = _runningItem();
      if (_ri) {
        _ri.status = 'cancelled';
        _ri.error = '사용자가 작업을 중지했습니다.';
        _ri.errorStage = 'cancelled';
        renderQueue();
      }
    } else {
      const logDetail = jobLogTail.length ? `마지막 로그: ${jobLogTail.join(' / ')}` : '';
      const reason = result.error_message || jobLastError || logDetail ||
        '전사 처리 중 알 수 없는 오류가 발생했습니다.';
      showError(reason);
      _failCurrentAndNext(reason, result.error_stage || 'transcribe');
    }
  });

  es.onerror = () => {
    es.close();
    setRunning(false);
    const reason = jobLastError || '서버와의 진행 상태 연결이 끊어졌습니다.';
    showError(reason);
    _failCurrentAndNext(reason, 'connection');
  };
}

/* ── Generate summary ── */
async function generateSummary() {
  if (!currentTxtPath) return {ok: false, error: '요약할 전사 파일 경로가 없습니다.'};

  const btn = document.getElementById('summarize-btn');
  const statusEl = document.getElementById('summary-status');
  const area = document.getElementById('summary-area');

  const rendered = document.getElementById('summary-rendered');
  btn.disabled = true;
  btn.textContent = '생성 중...';
  statusEl.textContent = '';
  area.textContent = '';
  // 생성 중에는 raw 스트리밍(pre) 표시, 렌더 컨테이너는 숨김/초기화
  area.style.display = '';
  rendered.style.display = 'none';
  rendered.innerHTML = '';
  document.getElementById('copy-summary-btn').style.display = 'none';
  setStage('summarize');

  const promptText = document.getElementById('prompt-text').value;
  let hasError = false;
  let errorMessage = '';

  try {
    let resp;
    try {
      resp = await fetch('/summarize', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({txt_path: currentTxtPath, prompt: promptText}),
      });
    } catch (e) {
      errorMessage = '요약 서버 연결 실패: ' + e.message;
      area.textContent = '오류: ' + errorMessage;
      hasError = true;
      return {ok: false, error: errorMessage};
    }

    if (!resp.ok) {
      try {
        const payload = await resp.json();
        errorMessage = payload.error || `요약 요청 실패 (HTTP ${resp.status})`;
      } catch (_) {
        errorMessage = `요약 요청 실패 (HTTP ${resp.status})`;
      }
      area.textContent = '오류: ' + errorMessage;
      hasError = true;
      return {ok: false, error: errorMessage};
    }

    // Stream the SSE response via ReadableStream
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let errorNext = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith('event: reset')) {
          area.textContent = '';
          rendered.innerHTML = '';
          errorNext = false;
        } else if (errorNext && line.startsWith('data: ')) {
          errorMessage = JSON.parse(line.slice(6));
          area.textContent = '오류: ' + errorMessage;
          hasError = true;
          errorNext = false;
        } else if (line.startsWith('event: error')) {
          errorNext = true;
        } else if (line.startsWith('data: ') && !line.includes('""\n')) {
          try {
            const chunk = JSON.parse(line.slice(6));
            if (chunk) {
              area.textContent += chunk;
              area.scrollTop = area.scrollHeight;
              document.getElementById('summary-badge').classList.add('visible');
            }
          } catch (_) {}
        }
      }
    }
  } catch (e) {
    errorMessage = '요약 응답 처리 실패: ' + e.message;
    area.textContent = '오류: ' + errorMessage;
    hasError = true;
  } finally {
    btn.disabled = false;
    btn.textContent = '다시 생성';
    statusEl.textContent = hasError ? '⚠ 요약 실패' : '✓ 완료';
    if (!hasError) {
      setStage('complete');
      document.getElementById('copy-summary-btn').style.display = '';
      // 완료 시 마크다운 렌더(** 굵게/표/리스트 등). raw 텍스트는 pre에 남겨 복사에 사용.
      const rendered = document.getElementById('summary-rendered');
      await YS.ensureReaderAssets();
      rendered.innerHTML = YS.renderMarkdown(area.textContent);
      area.style.display = 'none';
      rendered.style.display = '';
      // 영상 캡처 포함 ON이면 백그라운드로 키프레임 처리(큐 정체 방지).
      // await 하지 않으므로 다음 대기열 항목이 바로 시작됨. 완료 시 같은 항목이면 재렌더.
      if (document.getElementById('video-frames').checked && currentTxtPath) {
        runKeyframes(currentTxtPath, rendered);
      }
    }
  }
  return {ok: !hasError, error: errorMessage || (hasError ? '요약 생성에 실패했습니다.' : '')};
}

/* 영상 키프레임 추출→요약에 합치기(백그라운드). 실패해도 요약은 유지(폴백).
   처리 도중 대기열이 다음으로 넘어갔으면(현재 항목 불일치) UI 갱신은 생략. */
async function runKeyframes(capPath, renderedEl) {
  const statusEl = document.getElementById('summary-status');
  const here = () => currentTxtPath === capPath;   // 아직 같은 항목을 보고 있나
  if (here()) statusEl.textContent = '🎞 영상 캡처 처리 중…';
  try {
    const r = await fetch('/keyframes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({txt_path: capPath}),
    });
    const d = await r.json();
    if (!here()) return;   // 다른 항목으로 이동함 → 저장은 됐으니 이력에서 확인 가능
    if (d.ok && d.n_frames > 0 && d.summary_md) {
      await YS.ensureReaderAssets();
      renderedEl.innerHTML = YS.renderMarkdown(d.summary_md);
      statusEl.textContent = `✓ 완료 · 자료 캡처 ${d.n_frames}장`;
    } else if (d.reason === 'download_failed') {
      statusEl.textContent = '✓ 완료 (영상 다운로드 실패 — 음성 요약만 유지)';
    } else if (d.reason === 'vision_failed') {
      statusEl.textContent = '⚠ 영상 분석 실패(일시 오류) — 다시 시도해 주세요';   // '자료 없음'과 구분
    } else if (d.reason === 'no_frames') {
      statusEl.textContent = '⚠ 프레임 추출 실패 — 다시 시도해 주세요';
    } else if (d.ok) {
      statusEl.textContent = '✓ 완료 (자료성 캡처 없음)';
    } else {
      statusEl.textContent = '✓ 완료 (영상 캡처 건너뜀)';
    }
  } catch {
    if (here()) statusEl.textContent = '✓ 완료 (영상 캡처 오류 — 음성 요약만 유지)';
  }
}

/* ── Stage bar ── */
const STAGE_ORDER = ['download', 'transcribe', 'summarize', 'complete'];

function initStagebar(source) {
  document.getElementById('stage-bar').classList.add('visible');
  // Show/hide download stage based on source
  const dlItem = document.getElementById('st-download');
  const dlLine = document.getElementById('sl-1');
  const show   = source === 'url';
  dlItem.style.display = show ? '' : 'none';
  dlLine.style.display = show ? '' : 'none';
  // Reset all
  STAGE_ORDER.forEach(s => {
    const el = document.getElementById('st-' + s);
    if (el) el.classList.remove('active', 'done');
  });
  for (let i = 1; i <= 3; i++) {
    const ln = document.getElementById('sl-' + i);
    if (ln) ln.style.background = 'var(--border)';
  }
  // File mode: immediately mark download as skipped (done)
  if (source === 'file') setStage('transcribe');
}

function setStage(stageName) {
  const dlVisible = document.getElementById('st-download').style.display !== 'none';
  const stages    = dlVisible ? STAGE_ORDER : STAGE_ORDER.slice(1);
  const idx       = stages.indexOf(stageName);
  if (idx < 0) return;

  stages.forEach((s, i) => {
    const el = document.getElementById('st-' + s);
    if (!el) return;
    el.classList.remove('active', 'done');
    if (i < idx) el.classList.add('done');
    else if (i === idx) el.classList.add('active');
  });

  // Color connecting lines: sl-1 between download↔transcribe, sl-2 transcribe↔summarize, sl-3 summarize↔complete
  const lineMap = dlVisible
    ? [null, 'sl-1', 'sl-2', 'sl-3']   // index matches STAGE_ORDER
    : [null, null,   'sl-2', 'sl-3'];
  STAGE_ORDER.forEach((s, i) => {
    const ln = lineMap[i] ? document.getElementById(lineMap[i]) : null;
    if (ln) ln.style.background = i <= idx ? 'var(--success)' : 'var(--border)';
  });
}

/* ── Stop ── */
async function stopTranscription() {
  if (!currentJobId) return;
  document.getElementById('stop-btn').disabled = true;
  document.getElementById('stop-btn').textContent = '중지 중...';
  try {
    await fetch('/stop/' + currentJobId, { method: 'POST' });
  } catch {}
}

/* ── Source mode ── */
function setSource(src) {
  currentSource = src;
  document.getElementById('src-url').classList.toggle('active',  src === 'url');
  document.getElementById('src-file').classList.toggle('active', src === 'file');
  document.getElementById('url-section').style.display  = src === 'url'  ? '' : 'none';
  document.getElementById('file-section').style.display = src === 'file' ? '' : 'none';
}

/* ── File modal ── */
async function openFileModal() {
  const modal = document.getElementById('file-modal');
  modal.classList.add('open');
  document.getElementById('modal-empty').style.display = 'none';
  document.getElementById('modal-selected-label').textContent = '선택된 파일 없음';
  document.getElementById('modal-apply-btn').disabled = true;
  document.getElementById('file-list-body').innerHTML =
    '<tr><td colspan="3" style="text-align:center;padding:2rem;color:var(--muted)">로드 중...</td></tr>';

  try {
    const r = await fetch('/files');
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    document.getElementById('modal-dir').textContent = '📁 ' + d.dir;
    const tbody = document.getElementById('file-list-body');
    if (!d.files.length) {
      tbody.innerHTML = '';
      document.getElementById('modal-empty').style.display = '';
    } else {
      tbody.innerHTML = d.files.map((f, i) =>
        `<tr data-path=${JSON.stringify(f.path)} data-name=${JSON.stringify(f.name)}>
           <td>${esc(f.name)}</td>
           <td class="num-cell">${fmtDur(f.duration)}</td>
           <td class="num-cell">${fmtSize(f.size)}</td>
         </tr>`
      ).join('');
      tbody.querySelectorAll('tr').forEach(tr => {
        tr.addEventListener('click', () => selectFile(tr.dataset.path, tr.dataset.name, tr));
      });
    }
  } catch (e) {
    document.getElementById('file-list-body').innerHTML =
      `<tr><td colspan="3" style="text-align:center;color:var(--error);padding:2rem">오류: ${esc(e.message)}</td></tr>`;
  }
}

let pendingFilePath = '';
let pendingFileName = '';

function closeFileModal() {
  pendingFilePath = '';
  pendingFileName = '';
  document.getElementById('file-modal').classList.remove('open');
}

function handleModalBg(e) {
  if (e.target === document.getElementById('file-modal')) closeFileModal();
}

function selectFile(path, name, tr) {
  pendingFilePath = path;
  pendingFileName = name;
  document.querySelectorAll('#file-list-body tr').forEach(r => r.classList.remove('selected'));
  tr.classList.add('selected');
  document.getElementById('modal-selected-label').textContent = name;
  document.getElementById('modal-apply-btn').disabled = false;
}

function applyFileSelection() {
  if (!pendingFilePath) return;
  selectedFilePath = pendingFilePath;
  const el = document.getElementById('file-pick-name');
  el.textContent = pendingFileName;
  el.classList.add('selected');
  pendingFilePath = '';
  pendingFileName = '';
  document.getElementById('file-modal').classList.remove('open');
}

function fmtDur(secs) {
  if (!secs) return '—';
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = Math.floor(secs % 60);
  return [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
}
function fmtDurKo(secs) {
  if (!secs) return '—';
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = Math.floor(secs % 60);
  if (h > 0) return `${h}시간 ${m}분 ${s}초`;
  if (m > 0) return `${m}분 ${s}초`;
  return `${s}초`;
}

function fmtDate(d) {
  if (!d) return '—';
  const s = String(d);
  if (/^\d{8}$/.test(s)) return s.slice(0,4) + '/' + s.slice(4,6) + '/' + s.slice(6,8);
  return s.replace(/-/g, '/');
}

function fmtSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return bytes + ' B';
}

function esc(s) { return YS.escapeHtml(s); }   // 공유 모듈로 일원화(따옴표까지 이스케이프 — 텍스트 노드 안전)

/* ── Build payload (URL or file) ── */
function buildPayload() {
  const base = {
    language: document.getElementById('language').value,
    threads:  parseInt(document.getElementById('threads').value) || 8,
  };
  if (currentSource === 'file') {
    if (!selectedFilePath) throw new Error('파일을 선택해주세요.');
    return { ...base, source: 'file', file_path: selectedFilePath };
  }
  const url = document.getElementById('url-input').value.trim();
  if (!url) throw new Error('URL을 입력해주세요.');
  return { ...base, source: 'url', url };
}

/* ── Enter key (URL mode only) ── */
document.getElementById('url-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && currentSource === 'url') startTranscription();
});

/* ── URL Queue ── */
// 큐 아이템: {url, meta, status, error, errorStage, txtPath, retryFrom}
let urlQueue = [];
let _queueLaunchTimer = null;
renderQueue();

function _runningItem() { return urlQueue.find(i => i.status === 'running') || null; }

/* 툴팁 엘리먼트 */
const _qTip = document.createElement('div');
_qTip.className = 'queue-tooltip-popup';
_qTip.style.display = 'none';
document.body.appendChild(_qTip);

function _showQTip(e, meta, url) {
  let html = '';
  if (meta && meta.title) {
    html += `<div class="qt-title">${esc(meta.title)}</div>`;
    if (meta.uploader) html += `<div class="qt-row">${esc(meta.uploader)}</div>`;
    if (meta.duration) html += `<div class="qt-row">${fmtDur(meta.duration)}</div>`;
  } else if (meta === null) {
    html = `<div class="qt-loading">정보 로드 중...</div>`;
  } else {
    html = `<div class="qt-row" style="word-break:break-all;font-family:monospace;font-size:.7rem">${esc(url)}</div>`;
  }
  _qTip.innerHTML = html;
  _qTip.style.display = 'block';
  const r  = e.currentTarget.getBoundingClientRect();
  const tw = _qTip.offsetWidth;
  const th = _qTip.offsetHeight;
  let left = r.right + 8;
  let top  = r.top + r.height / 2 - th / 2;
  if (left + tw > window.innerWidth - 8) left = r.left - tw - 8;
  if (top < 4) top = 4;
  if (top + th > window.innerHeight - 4) top = window.innerHeight - th - 4;
  _qTip.style.left = left + 'px';
  _qTip.style.top  = top  + 'px';
}
function _hideQTip() { _qTip.style.display = 'none'; }


async function fetchVideoMeta(url) {
  try {
    const r = await fetch('/info', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    const d = await r.json();
    if (d.error) return false;
    return {title: d.title, uploader: d.uploader, duration: d.duration};
  } catch { return false; }
}

async function addToQueue() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  const vid = YS.ytVideoId(url);

  // 대기열 내 같은 영상(ID 우선, 비유튜브는 URL) → 추가 스킵
  const dupQ = urlQueue.find(i => (vid && YS.ytVideoId(i.url) === vid) || i.url === url);
  if (dupQ) { showError('이미 대기열에 있는 영상입니다.'); return; }

  // 처리 이력에 같은 영상이 있으면 확인 후 진행
  if (vid) {
    try {
      const r = await (await fetch('/history/check?yt_id=' + encodeURIComponent(vid))).json();
      if (r.exists && !confirm(`이미 처리한 영상입니다.\n(${fmtDate(r.date)} · ${r.title})\n\n다시 추가할까요?`)) return;
    } catch (_) {}   // 체크 실패는 추가를 막지 않음
  }

  clearError();
  const item = {
    url, meta: null, status: 'waiting', error: '', errorStage: '', txtPath: '', retryFrom: '',
  };
  urlQueue.push(item);
  document.getElementById('url-input').value = '';
  renderQueue();
  item.meta = await fetchVideoMeta(url);
  renderQueue();
}

function toggleAutoSummarize() {
  const cb  = document.getElementById('auto-summarize');
  const btn = document.getElementById('auto-summarize-btn');
  cb.checked = !cb.checked;
  btn.classList.toggle('on', cb.checked);
  btn.querySelector('.onoff-state').textContent = cb.checked ? 'ON' : 'OFF';
}

function toggleVideoFrames() {
  const cb  = document.getElementById('video-frames');
  const btn = document.getElementById('video-frames-btn');
  cb.checked = !cb.checked;
  btn.classList.toggle('on', cb.checked);
  btn.querySelector('.onoff-state').textContent = cb.checked ? 'ON' : 'OFF';
}

function removeFromQueue(idx) {
  urlQueue.splice(idx, 1);
  renderQueue();
}

function clearQueue() {
  // 진행중 항목은 유지, 나머지 전체 삭제
  urlQueue = urlQueue.filter(i => i.status === 'running');
  renderQueue();
}

async function copyQueue() {
  if (urlQueue.length === 0) return;
  const btn   = document.getElementById('queue-export-btn');
  const label = document.getElementById('queue-export-label');
  const text = urlQueue.map((item, i) => {
    const m     = item.meta || {};
    const stCfg = _STATUS_CFG[item.status] || _STATUS_CFG.waiting;
    const lines = [`[${i + 1}] ${m.title || '(정보 없음)'}`];
    if (m.uploader) lines.push(`업로더: ${m.uploader}`);
    if (m.duration) lines.push(`길이: ${fmtDur(m.duration)}`);
    lines.push(`상태: ${stCfg.label}`);
    if (item.error) lines.push(`오류: ${item.error}`);
    lines.push(item.url);
    return lines.join('\n');
  }).join('\n\n');

  try {
    await navigator.clipboard.writeText(text);
    label.textContent = '복사됨';
    btn.style.color = 'var(--success)';
  } catch (e) {
    label.textContent = '복사 실패';
    btn.style.color = 'var(--error)';
  }
  setTimeout(() => {
    label.textContent = '클립보드로 내보내기';
    btn.style.color = '';
  }, 1500);
}

const _STATUS_CFG = {
  running:   {cls: 'qs-running',   label: '진행중', rowCls: 'queue-item-active'},
  done:      {cls: 'qs-done',      label: '완료',   rowCls: 'queue-item-done'},
  waiting:   {cls: 'qs-waiting',   label: '대기',   rowCls: ''},
  error:     {cls: 'qs-error',     label: '오류',   rowCls: 'queue-item-error'},
  cancelled: {cls: 'qs-cancelled', label: '취소',   rowCls: 'queue-item-cancelled'},
};

const _ERROR_STAGE_LABELS = {
  metadata: '영상 정보', download: '다운로드', transcribe: '전사', summary: '요약',
  connection: '연결', result: '결과 조회', start: '작업 시작', worker: '내부 처리',
  cancelled: '중지',
};

function renderQueue() {
  const list    = document.getElementById('queue-list');
  const countEl = document.getElementById('queue-count');
  const waiting = urlQueue.filter(i => i.status === 'waiting').length;
  countEl.textContent = urlQueue.length > 0 ? (waiting > 0 ? `${waiting}` : urlQueue.length) : '0';

  const itemRows = urlQueue.map((item, i) => {
    const cfg = _STATUS_CFG[item.status] || _STATUS_CFG.waiting;
    const removeBtn = item.status !== 'running'
      ? `<button class="queue-item-remove" onclick="removeFromQueue(${i})">✕</button>`
      : '';
    const retryBtn = item.status === 'error' || item.status === 'cancelled'
      ? `<button class="queue-item-retry" onclick="retryQueueItem(${i})" title="이 영상 재시도">재시도</button>`
      : '';
    // 영상정보(제목) 확보 시 제목으로 치환 표시(URL은 내부/툴팁 보존)
    const display = (item.meta && item.meta.title) ? item.meta.title : item.url;
    const stage = _ERROR_STAGE_LABELS[item.errorStage] || '오류';
    const errorLine = item.error
      ? `<span class="queue-item-error-text" title="${_attrEsc(item.error)}">${esc(stage)} · ${esc(item.error)}</span>`
      : '';
    return `<li class="queue-item ${cfg.rowCls}">
      <span class="queue-item-idx">${i + 1}</span>
      <span class="queue-item-content">
        <span class="queue-item-url" title="${_attrEsc(item.url)}">${esc(display)}</span>
        ${errorLine}
      </span>
      <span class="queue-status ${cfg.cls}">${cfg.label}</span>
      ${retryBtn}
      ${removeBtn}
    </li>`;
  });

  const emptyRows = Array.from({ length: Math.max(0, 10 - urlQueue.length) }, (_, i) =>
    `<li class="queue-item queue-item-empty">
      <span class="queue-item-idx">${urlQueue.length + i + 1}</span>
      <span class="queue-item-url">—</span>
    </li>`
  );

  list.innerHTML = [...itemRows, ...emptyRows].join('');

  list.querySelectorAll('.queue-item:not(.queue-item-empty)').forEach((li, i) => {
    li.addEventListener('mouseenter', (e) => _showQTip(e, urlQueue[i].meta, urlQueue[i].url));
    li.addEventListener('mouseleave', _hideQTip);
  });

  const exportBtn = document.getElementById('queue-export-btn');
  if (exportBtn) exportBtn.disabled = urlQueue.length === 0;
}

function _autoStartNext() {
  const running = _runningItem();
  if (running) {
    running.status = 'done';
    running.error = '';
    running.errorStage = '';
    running.retryFrom = '';
  }

  const next = urlQueue.find(i => i.status === 'waiting');
  if (!next) { renderQueue(); return; }

  renderQueue();
  const rem = urlQueue.filter(i => i.status === 'waiting').length - 1;
  appendLog(`\n⏭ 대기열 다음 항목 자동 시작 (남은: ${rem}개)...`);
  _launchQueueItem(next, 1500);
}

async function _retrySummaryItem(item) {
  item.status = 'running';
  item.error = '';
  item.errorStage = '';
  renderQueue();
  clearError();
  setRunning(true);
  currentTxtPath = item.txtPath;
  showTab('summary');
  switchTab('summary');
  const summarized = await generateSummary();
  setRunning(false);
  if (summarized.ok) _autoStartNext();
  else _failCurrentAndNext(summarized.error, 'summary');
}

function _launchQueueItem(item, delay = 0) {
  if (_queueLaunchTimer) clearTimeout(_queueLaunchTimer);
  _queueLaunchTimer = setTimeout(() => {
    _queueLaunchTimer = null;
    if (!item || item.status !== 'waiting') return;
    if (item.retryFrom === 'summary' && item.txtPath) {
      _retrySummaryItem(item);
      return;
    }
    document.getElementById('url-input').value = item.url;
    startTranscription();
  }, delay);
}

function retryQueueItem(idx) {
  const item = urlQueue[idx];
  if (!item || !['error', 'cancelled'].includes(item.status)) return;

  const summaryOnly = item.errorStage === 'summary' && Boolean(item.txtPath);
  item.status = 'waiting';
  item.retryFrom = summaryOnly ? 'summary' : '';
  item.error = '';
  item.errorStage = '';
  renderQueue();

  if (_runningItem() || document.getElementById('start-btn').disabled) {
    appendLog(`\n↻ 재시도 대기열에 추가: ${(item.meta && item.meta.title) || item.url}`);
    return;
  }
  appendLog(`\n↻ ${summaryOnly ? '요약만 ' : ''}재시도 시작: ${(item.meta && item.meta.title) || item.url}`);
  _launchQueueItem(item);
}

/* 현재 항목의 원인을 보존하고 나머지 대기열은 계속 처리한다. */
function _failCurrentAndNext(reason = '알 수 없는 오류가 발생했습니다.', stage = 'worker') {
  const running = _runningItem();
  if (running) {
    running.status = 'error';
    running.error = reason;
    running.errorStage = stage;
    running.retryFrom = stage === 'summary' && running.txtPath ? 'summary' : '';
  }
  renderQueue();

  const next = urlQueue.find(i => i.status === 'waiting');
  if (!next) return;

  const rem = urlQueue.filter(i => i.status === 'waiting').length - 1;
  appendLog(`\n⏭ 오류 발생 — 대기열 다음 항목으로 넘어갑니다 (남은: ${rem}개)...`);
  _launchQueueItem(next, 1500);
}

/* ── Icon SVG strings ── */
const ICON_CHECK = `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
const ICON_PASTE = `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const ICON_LINK  = `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;

/* ── Paste URL from clipboard ── */
async function pasteUrl() {
  try {
    const text = await navigator.clipboard.readText();
    document.getElementById('url-input').value = text.trim();
    const btn = document.getElementById('paste-btn');
    btn.innerHTML = ICON_CHECK;
    btn.style.color = 'var(--success)';
    setTimeout(() => { btn.innerHTML = ICON_PASTE; btn.style.color = ''; }, 1200);
  } catch (e) {
    document.getElementById('url-input').focus();
  }
}

/* ── Copy URL to clipboard ── */
async function copyUrl() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  const btn = document.getElementById('copy-url-btn');
  try {
    await navigator.clipboard.writeText(url);
    btn.innerHTML = ICON_CHECK;
    btn.style.color = 'var(--success)';
    setTimeout(() => { btn.innerHTML = ICON_LINK; btn.style.color = ''; }, 1500);
  } catch (e) {}
}

/* ── Summary Modal ── (marked.js 설정은 공유 모듈 common.js에서 일원화) */
let _summaryMd = '';      // 현재 열린 요약 원문(마크다운) — 몰입형 재구성·블로거 복사에 사용
let _titleKo = '';        // 현재 열린 요약의 번역 제목(외국어 제목일 때만 값이 있다)
let _summaryItemId = 0;   // 현재 열린 요약의 DB ID — 경로를 브라우저에 노출하지 않는다

let _distill = null;      // 현재 영상의 증류 상태 {override, channel, effective}

/* 증류 토글 표시 — d = {override, channel, effective} | null(미조회).
   채널 따름일 때는 그 채널이 실제로 어떤 값인지(포함/제외) 옆에 함께 보여준다. */
function _setDistillUI(d) {
  const box = document.getElementById('sum-distill-ctrl');
  if (!box) return;
  _distill = d || null;
  const ov = d ? d.override : null;
  const set = ov !== null && ov !== undefined;
  box.classList.toggle('set', set);
  if (!d) {
    box.textContent = '증류 —';
    box.title = '';
    return;
  }
  const chan = d.channel ? '포함' : '제외';
  // 자동수집 채널이 아니면 따를 채널 설정 자체가 없다 → '채널'이 아니라 '기본'으로 표기한다.
  const reg  = d.registered !== false;
  const src  = reg ? '채널' : '기본';
  const srcDesc = reg ? `채널 설정을 따름 → 현재 ${chan}`
                      : `자동수집 채널이 아니라 기본값 적용 → ${chan}`;
  if (!set) {
    box.innerHTML = `증류 ↪ ${src} <span class="dist-eff">(${chan})</span>`;
    box.title = `${srcDesc}. 클릭하면 이 영상만 '포함'으로 지정`;
  } else if (ov) {
    box.textContent = '증류 ✓ 포함';
    box.title = `이 영상만 포함으로 지정됨(${src} 설정은 ${chan}). 클릭하면 '제외'로`;
  } else {
    box.textContent = '증류 ✕ 제외';
    box.title = `이 영상만 제외로 지정됨(${src} 설정은 ${chan}). 클릭하면 '${src} 따름'으로`;
  }
}

/* 클릭할 때마다 채널 따름 → 포함 → 제외 → 채널 따름 순으로 돈다. */
function cycleItemDistill() {
  const ov = _distill ? _distill.override : null;
  const next = (ov === null || ov === undefined) ? true : (ov ? false : null);
  setItemDistill(next);
}

/* 영상 단위 증류 설정 변경 — null=미설정(채널 따름), true=포함, false=제외 */
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
  } catch (e) {
    alert('증류 설정 실패: ' + e.message);
  }
}

/* 구글 블로거 포스팅용 복사 — 이미지 제외, 인라인 스타일 서식 HTML을 클립보드에.
   text/html + text/plain 을 함께 넣어 블로거 작성(리치텍스트)·HTML 보기 어디에 붙여도 되게 한다. */
/* 비보안 컨텍스트(http + 비 localhost, 예: Tailscale IP 접속) 대비 레거시 복사.
   navigator.clipboard는 HTTPS/localhost에서만 존재하므로 원격 접속 시엔 이 경로가 쓰인다.
   contenteditable 요소를 선택해 execCommand('copy') → 브라우저가 text/html+text/plain을
   함께 넣어주므로 서식이 유지된다. */
function _copyHtmlLegacy(html, text) {
  let ok = false;
  // copy 이벤트를 가로채 clipboardData에 html+plain을 직접 넣는다(서식 보장 + 성공 판정 확실).
  const onCopy = (e) => {
    e.clipboardData.setData('text/html', html);
    e.clipboardData.setData('text/plain', text);
    e.preventDefault();
    ok = true;
  };
  document.addEventListener('copy', onCopy, true);
  // execCommand('copy')는 선택 영역이 없으면 copy 이벤트를 안 쏘는 브라우저가 있어 임시 선택을 만든다
  const holder = document.createElement('div');
  holder.contentEditable = 'true';
  holder.textContent = ' ';
  holder.setAttribute('style', 'position:fixed;left:-99999px;top:0;opacity:0;');
  document.body.appendChild(holder);
  const sel = window.getSelection();
  const saved = sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
  const range = document.createRange();
  range.selectNodeContents(holder);
  sel.removeAllRanges();
  sel.addRange(range);
  try { document.execCommand('copy'); } catch { /* ok는 false로 남는다 */ }
  document.removeEventListener('copy', onCopy, true);
  sel.removeAllRanges();
  if (saved) sel.addRange(saved);
  holder.remove();
  return ok;
}

async function copySummaryForBlogger(btn) {
  if (!_summaryMd) return;
  const label = btn && btn.querySelector('.blg-label');
  const setLbl = (t, color) => {
    if (label) label.textContent = t;
    if (btn) btn.style.color = color || '';
  };
  const { html, text } = YS.mdToBloggerHtml(_summaryMd);
  let ok = false;
  try {
    if (navigator.clipboard && window.ClipboardItem) {      // HTTPS·localhost
      await navigator.clipboard.write([new ClipboardItem({
        'text/html':  new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain' }),
      })]);
      ok = true;
    }
  } catch (e) {
    console.warn('[blogger] clipboard API 실패 → 레거시 폴백', e);
  }
  if (!ok) ok = _copyHtmlLegacy(html, text);                // 원격(비보안 컨텍스트) 경로
  setLbl(ok ? '복사됨' : '복사 실패', ok ? 'var(--success)' : 'var(--error)');
  setTimeout(() => setLbl('블로거용 복사'), 1600);
}

/* 텔레그램용 복사 — 범위는 블로거와 동일하되 텔레그램이 살리는 서식(<b>/<i>/<a>)과
   줄바꿈만으로 구성한다. 4096자를 넘으면 라벨로 알려 나눠 보내게 한다. */
async function copySummaryForTelegram(btn) {
  if (!_summaryMd) return;
  const label = btn && btn.querySelector('.tg-label');
  const setLbl = (t, color) => {
    if (label) label.textContent = t;
    if (btn) btn.style.color = color || '';
  };
  const { html, text, length, parts, overLimit } = YS.mdToTelegram(_summaryMd);
  let ok = false;
  try {
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html':  new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain' }),
      })]);
      ok = true;
    }
  } catch (e) {
    console.warn('[telegram] clipboard API 실패 → 레거시 폴백', e);
  }
  if (!ok) ok = _copyHtmlLegacy(html, text);
  if (!ok) setLbl('복사 실패', 'var(--error)');
  else if (overLimit) setLbl(`복사됨 (${parts}개로 나눠 보내기)`, 'var(--warn, #b45309)');
  else setLbl('복사됨', 'var(--success)');
  setTimeout(() => setLbl('텔레그램용 복사'), overLimit ? 3200 : 1600);
}
let _immersive = false;

/* ── 본문 글자크기 배율(−/+) — 일반/몰입 두 본문 공통, 소제목·표는 고정. 로컬 저장 ── */
const _READER_MIN = 0.8, _READER_MAX = 1.6, _READER_STEP = 0.08;
let _readerScale = parseFloat(localStorage.getItem('sumReaderScale')) || 1;
if (!(_readerScale >= _READER_MIN && _readerScale <= _READER_MAX)) _readerScale = 1;
function _applyReaderScale() {
  const panel = document.getElementById('sum-panel');
  if (panel) panel.style.setProperty('--reader-scale', _readerScale.toFixed(3));
}
function adjustReaderFs(dir) {
  const next = Math.min(_READER_MAX, Math.max(_READER_MIN, _readerScale + dir * _READER_STEP));
  _readerScale = Math.round(next * 1000) / 1000;
  localStorage.setItem('sumReaderScale', String(_readerScale));
  _applyReaderScale();
}


/* ── 본문 글꼴 선택 — 일반/몰입 공통, localStorage 기억 ── */
const _READER_FONTS = {
  sans:       'Noto Sans KR',
  pretendard: 'Pretendard',
  chosun: '조선일보명조',
  serif:  'Noto Serif KR',
  gowun:  '고운바탕',
  nanum:  '나눔명조',
  song:   '송명',
  system: '시스템',
  mono:   '고정폭',
};
let _readerFont = localStorage.getItem('sumReaderFont') || 'sans';
if (_readerFont === 'news') _readerFont = 'gowun';   // 구 '신문 명조' → 고운바탕
if (!_READER_FONTS[_readerFont]) _readerFont = 'sans';
function _applyReaderFont() {
  const panel = document.getElementById('sum-panel');
  if (panel) panel.dataset.readerFont = _readerFont;
  const sel = document.getElementById('sum-font-select');
  if (sel && sel.value !== _readerFont) sel.value = _readerFont;
}
function setReaderFont(key) {
  if (!_READER_FONTS[key]) return;
  _readerFont = key;
  localStorage.setItem('sumReaderFont', key);
  _applyReaderFont();
}


/* 읽기 진행바: 활성 스크롤 컨테이너(일반=sum-panel-body, 몰입=imm-text) 기준 */
function _updateSumProgress(el) {
  const bar = document.getElementById('sum-progress');
  if (!bar || !el) return;
  const max = el.scrollHeight - el.clientHeight;
  bar.style.width = max > 4 ? Math.min(100, el.scrollTop / max * 100) + '%' : '0%';
}
document.getElementById('sum-panel-body').addEventListener('scroll', function () {
  if (!_immersive) _updateSumProgress(this);
}, { passive: true });
document.querySelector('#sum-immersive-body .imm-text').addEventListener('scroll', function () {
  if (_immersive) _updateSumProgress(this);
}, { passive: true });

/* h3 소제목에서 목차 라벨(제목/시각) 추출 — ▸ 아이콘·.kf-time 제거 후 텍스트 */
function _tocLabel(h3) {
  const c = h3.cloneNode(true);
  const ico = c.querySelector('.kf-ico'); if (ico) ico.remove();
  const timeEl = c.querySelector('.kf-time');
  const time = timeEl ? timeEl.textContent.trim() : '';
  if (timeEl) timeEl.remove();
  return { title: c.textContent.trim(), time };
}

/* 한눈 요약 다음(핵심 내용 머리말 뒤)에 소제목 목차 삽입 + 클릭 시 해당 위치로 스크롤.
   일반/몰입 두 본문에서 각각 호출(container 기준으로 동작). */
function _injectToc(container) {
  if (!container) return;
  const h3s = [...container.querySelectorAll('h3')];
  if (h3s.length < 2) return;                       // 소제목 2개 미만이면 목차 불필요
  const coreH2 = [...container.querySelectorAll('h2')]
    .find(h => h.textContent.replace(/\s+/g, '').includes('핵심내용'));
  const nav = document.createElement('details');    // 기본 접힘(open 미설정)
  nav.className = 'sum-toc';
  const items = h3s.map((h, i) => {
    const { title, time } = _tocLabel(h);
    return `<li><a href="#" data-toc="${i}"><span class="sum-toc-t">${esc(title)}</span>`
         + (time ? `<span class="sum-toc-time">${esc(time)}</span>` : '') + `</a></li>`;
  }).join('');
  nav.innerHTML = `<summary class="sum-toc-hd"><span class="sum-toc-chev" aria-hidden="true">▸</span>목차`
    + `<span class="sum-toc-count">${h3s.length}</span></summary>`
    + `<ol class="sum-toc-list">${items}</ol>`;
  if (coreH2) coreH2.after(nav); else h3s[0].before(nav);
  nav.querySelectorAll('a[data-toc]').forEach(a => a.addEventListener('click', ev => {
    ev.preventDefault();
    const t = container.querySelectorAll('h3')[+a.dataset.toc];
    if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
}

async function openSummaryModal(itemId, title) {
  const overlay  = document.getElementById('sum-overlay');
  const bodyEl   = document.getElementById('sum-panel-body');

  _summaryItemId = itemId;                              // 증류 설정 변경 대상
  _setDistillUI(null);                                   // 값 로드 전에는 기본 표시
  bodyEl.innerHTML    = '<p class="sum-loading">불러오는 중…</p>';
  overlay.hidden      = false;
  document.body.style.overflow = 'hidden';
  _applyReaderScale();                                   // 저장된 글자크기 배율 반영
  _applyReaderFont();                                    // 저장된 본문 글꼴 반영
  _setImmersive(false);                                  // 항상 일반 보기로 시작
  document.getElementById('sum-immersive-btn').hidden = true;

  try {
    const [data] = await Promise.all([
      YS.apiSummaryContent(itemId),
      YS.ensureReaderAssets(),
    ]);
    if (data.error) throw new Error(data.error);
    _summaryMd = data.content || '';
    _titleKo   = data.title_ko || '';                      // 외국어 제목의 한국어 번역
    _setDistillUI(data.distill);                           // 서버가 함께 준 증류 설정 반영
    bodyEl.innerHTML = YS.renderMarkdown(_summaryMd);
    YS.applyTitleTranslation(bodyEl, _titleKo);            // 제목을 번역본으로, 원문은 아래 병기
    _injectToc(bodyEl);                                    // 목차 삽입(일반 보기)
    bodyEl.scrollTop = 0;
    _updateSumProgress(bodyEl);
    // 캡처 이미지가 있을 때만 몰입형 버튼 노출
    const hasImg = !!bodyEl.querySelector('.kf-strip');
    document.getElementById('sum-immersive-btn').hidden = !hasImg;
    // 마지막으로 고른 보기 상태를 기억(기본 'immersive'). 버튼이 보이는 폭(>900px)에서만 자동 몰입(되돌리기 보장)
    const pref = localStorage.getItem('immViewMode') || 'immersive';
    if (hasImg && window.innerWidth > 900 && pref === 'immersive') _setImmersive(true);
  } catch (e) {
    _summaryMd = '';
    bodyEl.innerHTML = `<p class="sum-error">오류: ${e.message}</p>`;
  }
}

function toggleImmersive() {
  _setImmersive(!_immersive);
  localStorage.setItem('immViewMode', _immersive ? 'immersive' : 'normal');   // 사용자 선택을 로컬에 기억
}

/** 일반 ↔ 몰입형 전환. 몰입형은 좌측 이미지 갤러리 + 우측 텍스트(스트립 제거)로 재구성. */
function _setImmersive(on) {
  _immersive = on;
  const panel  = document.getElementById('sum-panel');
  const normal = document.getElementById('sum-panel-body');
  const imm    = document.getElementById('sum-immersive-body');
  const btn    = document.getElementById('sum-immersive-btn');
  panel.classList.toggle('immersive', on);
  normal.hidden = on;
  imm.hidden    = !on;
  btn.textContent = on ? '⊠ 일반 보기' : '⊟ 몰입형 읽기';
  if (!on) { _updateSumProgress(normal); return; }

  const tmp = document.createElement('div');
  tmp.innerHTML = YS.renderMarkdown(_summaryMd);
  YS.applyTitleTranslation(tmp, _titleKo);                      // 몰입형에서도 제목은 번역본
  const figs = [...tmp.querySelectorAll('.kf-strip figure')];   // 모든 캡처 수집(좌측으로)
  tmp.querySelectorAll('.kf-strip').forEach(s => s.remove());   // 본문에선 스트립 제거
  // 이미지가 모두 좌측으로 이동했으니 빈 '기타 자료 캡처' 부록 제목 제거
  [...tmp.querySelectorAll('h2')].forEach(h => {
    if (h.textContent.replace(/\s+/g, '').includes('기타자료캡처')) h.remove();
  });
  const gal = imm.querySelector('.imm-gallery');
  const txt = imm.querySelector('.imm-text');
  gal.innerHTML = figs.length
    ? `<div class="kf-strip">${figs.map(f => f.outerHTML).join('')}</div>`   // kf-strip 유지 → 라이트박스 동작
    : '<p class="imm-empty">캡처 이미지가 없습니다.</p>';
  txt.innerHTML = tmp.innerHTML;
  _injectToc(txt);                     // 목차 삽입(몰입형 우측 본문)
  gal.scrollTop = txt.scrollTop = 0;   // 몰입형 진입 시 항상 맨 위에서 시작
  _updateSumProgress(txt);
}


function closeSummaryModal() {
  document.getElementById('sum-overlay').hidden = true;
  document.body.style.overflow = '';
  _setImmersive(false);                                  // 다음 열림을 위해 초기화
}

function handleSumOverlayClick(e) {
  if (e.target === document.getElementById('sum-overlay')) closeSummaryModal();
}

/* 전사 원문과 한국어 전문 번역. 번역은 별도 작업(transcript_translator)이 만들어
   두는 것이라, 없으면 탭 자체를 감추고 원문만 보여준다. */
let _transOrig = '';
let _transKo   = null;      // {status, done, total, text} | null

async function openTranscriptModal(itemId, title) {
  const overlay  = document.getElementById('trans-overlay');
  const titleEl  = document.getElementById('trans-panel-title');
  const bodyEl   = document.getElementById('trans-panel-body');

  titleEl.textContent = title;
  bodyEl.textContent  = '불러오는 중…';
  overlay.hidden      = false;
  document.getElementById('trans-tabs').hidden = true;
  document.body.style.overflow = 'hidden';

  try {
    const res  = await fetch('/history/text', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ item_id: itemId }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    _transOrig = data.text || '(내용 없음)';
    _transKo   = data.translation || null;
    const hasKo = !!(_transKo && _transKo.text);
    document.getElementById('trans-tabs').hidden = !hasKo;
    switchTranscriptTab('orig');
  } catch (e) {
    bodyEl.textContent = '오류: ' + e.message;
  }
}

function switchTranscriptTab(which) {
  const bodyEl = document.getElementById('trans-panel-body');
  const oBtn = document.getElementById('trans-tab-orig');
  const kBtn = document.getElementById('trans-tab-ko');
  oBtn.classList.toggle('on', which === 'orig');
  kBtn.classList.toggle('on', which === 'ko');
  if (which === 'ko' && _transKo) {
    // 아직 번역 중이면 어디까지 됐는지 알려준다(청크 단위로 저장돼 부분 열람 가능)
    const partial = _transKo.status === 'processing'
      ? `⏳ 번역 중 ${_transKo.done}/${_transKo.total} — 아래는 지금까지 번역된 부분입니다.\n\n`
      : '';
    bodyEl.textContent = partial + _transKo.text;
  } else {
    bodyEl.textContent = _transOrig;
  }
  bodyEl.scrollTop = 0;
}

function closeTranscriptModal() {
  document.getElementById('trans-overlay').hidden = true;
  document.body.style.overflow = '';
}

function handleTransOverlayClick(e) {
  if (e.target === document.getElementById('trans-overlay')) closeTranscriptModal();
}

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!document.getElementById('sum-overlay').hidden) closeSummaryModal();
  else if (!document.getElementById('trans-overlay').hidden) closeTranscriptModal();
});

/* ── Theme toggle ── */
function toggleTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  document.getElementById('theme-btn').textContent = next === 'light' ? '☀' : '◐';
}

// Sync button icon with current theme
(function() {
  const theme = localStorage.getItem('theme') || 'light';
  document.getElementById('theme-btn').textContent = theme === 'light' ? '☀' : '◐';
})();

/* reader font init */
_applyReaderFont();
