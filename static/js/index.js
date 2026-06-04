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
  if (name === 'history') loadHistory();   // 이력 탭 진입 시 매번 자동 새로고침
}

/* ── History ── */
let _historyLoaded   = false;
let _historyItems    = [];
let _historyFiltered = [];
let _historySelIdx   = -1;
let _historyPage     = 1;
let _histUnreadOnly  = false;
const HIST_PAGE_SIZE = 20;

loadHistory();

async function loadHistory() {
  _historyLoaded = true;
  document.getElementById('history-count').textContent = '로드 중...';
  try {
    const r = await fetch('/history');
    const d = await r.json();
    _historyItems = d.items || [];
  } catch {
    _historyItems = [];
  }
  _populateUploaderOptions();
  applyHistoryFilter();
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

function applyHistoryFilter() {
  const dateFrom = document.getElementById('hf-date-from').value;
  const dateTo   = document.getElementById('hf-date-to').value;
  const uploader = document.getElementById('hf-uploader').value;
  const titleQ   = document.getElementById('hf-title').value.trim().toLowerCase();
  document.getElementById('hf-clear-btn').classList.toggle('visible', titleQ.length > 0);

  const toYMD = d => d.replace(/-/g, '');  // "YYYY-MM-DD" → "YYYYMMDD"
  _historyFiltered = _historyItems.filter(item => {
    if (dateFrom && item.date < toYMD(dateFrom)) return false;
    if (dateTo   && item.date > toYMD(dateTo))   return false;
    if (uploader && item.uploader !== uploader)  return false;
    if (titleQ   && !item.title.toLowerCase().includes(titleQ)) return false;
    if (_histUnreadOnly && item.is_read) return false;
    return true;
  });
  _historyPage = 1;
  _renderHistoryList();
}

function toggleUnreadFilter() {
  _histUnreadOnly = !_histUnreadOnly;
  document.getElementById('hf-unread-btn').classList.toggle('active', _histUnreadOnly);
  applyHistoryFilter();
}

async function toggleRead(btn) {
  const mdPath  = btn.dataset.path;
  const wasRead = btn.dataset.read === '1';
  const newRead = !wasRead;

  try {
    if (!(await YS.apiMarkRead(mdPath, newRead))) return;
  } catch { return; }

  // _historyItems 내 해당 항목 업데이트
  const item = _historyItems.find(i => i.txt_path === mdPath);
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

  // 미읽음 필터 활성 중이면 읽음 처리된 카드 제거
  if (_histUnreadOnly && newRead) applyHistoryFilter();
}

async function deleteCard(btn) {
  const mdPath = btn.dataset.path;
  const title  = btn.dataset.title || '이 항목';
  if (!confirm(`"${title}" 을(를) 삭제하시겠습니까?\n\n전사 파일과 요약 파일이 모두 삭제됩니다.`)) return;

  btn.disabled = true;
  try {
    if (!(await YS.apiDeleteItem(mdPath))) { btn.disabled = false; return; }
  } catch { btn.disabled = false; return; }

  // _historyItems에서 제거
  const idx = _historyItems.findIndex(i => i.txt_path === mdPath);
  if (idx !== -1) _historyItems.splice(idx, 1);

  // 카드 DOM 제거 후 목록 다시 렌더
  applyHistoryFilter();
}

function _historyGoToPage(p) {
  const totalPages = Math.max(1, Math.ceil(_historyFiltered.length / HIST_PAGE_SIZE));
  _historyPage = Math.min(Math.max(1, p), totalPages);
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
  document.getElementById('hf-date-from').value = '';
  document.getElementById('hf-date-to').value   = '';
  document.getElementById('hf-uploader').value  = '';
  document.getElementById('hf-title').value      = '';
  _histUnreadOnly = false;
  document.getElementById('hf-unread-btn').classList.remove('active');
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
  const sumPath = card.dataset.sumPath;
  const title   = card.dataset.title;
  if (sumPath) openSummaryModal(sumPath, title);
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
    const thumbUrl  = vid ? `https://i.ytimg.com/vi/${vid}/mqdefault.jpg` : '';
    const thumbInner = thumbUrl
      ? `<img class="hist-thumb" src="${_attrEsc(thumbUrl)}" alt="" loading="lazy" onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/${vid}/hqdefault.jpg'">`
      : `<div class="hist-thumb-placeholder">썸네일 없음</div>`;
    const durBadge  = item.duration
      ? `<span class="hist-thumb-dur">${fmtDurKo(item.duration)}</span>` : '';

    const thumbBlock = `<div class="hist-thumb-wrap">${thumbInner}${durBadge}</div>`;

    const uploaderHtml = item.channel_url
      ? `<a class="hist-card-uploader" href="${_attrEsc(item.channel_url)}" target="_blank" rel="noopener" title="${_attrEsc(item.uploader)}">${esc(item.uploader)}</a>`
      : `<span class="hist-card-uploader" title="${_attrEsc(item.uploader)}">${esc(item.uploader)}</span>`;

    const titleAttr = _attrEsc(item.title);
    const titleHtml = `<h3 class="hist-card-title" title="${titleAttr}">${esc(item.title)}</h3>`;

    const sumBtn  = item.summary_path
      ? `<button class="hist-icon-btn hist-icon-btn-summary" data-path="${_attrEsc(item.summary_path)}" data-title="${titleAttr}" onclick="openSummaryModal(this.dataset.path,this.dataset.title)" title="요약 보기">${ICON_SUMMARY}</button>`
      : `<button class="hist-icon-btn hist-icon-btn-summary" disabled title="요약 없음">${ICON_SUMMARY}</button>`;
    const txtBtn  = item.has_txt
      ? `<button class="hist-icon-btn" data-path="${_attrEsc(item.txt_path)}" data-title="${titleAttr}" onclick="openTranscriptModal(this.dataset.path,this.dataset.title)" title="전사 보기">${ICON_TRANSCRIPT}</button>`
      : `<button class="hist-icon-btn" disabled title="전사 없음">${ICON_TRANSCRIPT}</button>`;
    const ytBtn   = item.webpage_url
      ? `<a class="hist-icon-btn hist-icon-btn-youtube" href="${_attrEsc(item.webpage_url)}" target="_blank" rel="noopener" title="YouTube에서 열기">${ICON_YOUTUBE}</a>`
      : `<button class="hist-icon-btn hist-icon-btn-youtube" disabled title="URL 없음">${ICON_YOUTUBE}</button>`;
    const readBtn = `<button class="hist-icon-btn hist-icon-btn-read${item.is_read ? ' is-read' : ''}"
      data-path="${_attrEsc(item.txt_path)}" data-read="${item.is_read ? '1' : '0'}"
      onclick="toggleRead(this)" title="${item.is_read ? '읽음 (클릭 시 안읽음으로)' : '안읽음 (클릭 시 읽음으로)'}"
      >${item.is_read ? ICON_EYE_OFF : ICON_EYE}</button>`;
    const delBtn  = `<button class="hist-icon-btn hist-icon-btn-delete"
      data-path="${_attrEsc(item.txt_path)}" data-title="${titleAttr}"
      onclick="deleteCard(this)" title="삭제"
      >${ICON_DELETE}</button>`;

    const unreadDot = !item.is_read ? '<span class="hist-unread-dot"></span>' : '';
    const clickable = item.summary_path ? ' hist-card-clickable" onclick="_onHistCardClick(event)' : '';
    const cardData  = item.summary_path
      ? ` data-sum-path="${_attrEsc(item.summary_path)}" data-title="${titleAttr}"` : '';
    const unreadClass = !item.is_read ? ' hist-card-unread' : '';

    return `<div class="hist-card${unreadClass}${clickable}"${cardData}>
      ${thumbBlock}
      <div class="hist-card-body">
        <h3 class="hist-card-title" title="${titleAttr}">${unreadDot}${esc(item.title)}</h3>
        <div class="hist-card-meta">${uploaderHtml}</div>
        <div class="hist-card-footer">
          <span class="hist-card-date">${fmtDate(item.date)}</span>
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

/* ── Start transcription ── */
async function startTranscription() {
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
    } else if (!_runningItem()) {
      urlQueue.push({url: _url, meta: null, status: 'running'});
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
    showError('서버 연결 실패: ' + e.message);
    setRunning(false);
    _failCurrentAndNext();
    return;
  }

  const data = await resp.json();
  if (data.error) { showError(data.error); setRunning(false); _failCurrentAndNext(); return; }
  currentJobId = data.job_id;

  const es = new EventSource('/stream/' + currentJobId);

  es.onmessage = (e) => appendLog(e.data);

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

    const r = await fetch('/result/' + currentJobId);
    const result = await r.json();

    if (result.status === 'done' && result.result) {
      currentTxtPath = result.txt_path || null;
      document.getElementById('result-area').textContent = result.result;
      document.getElementById('result-fname').textContent = result.filename || '';
      showTab('result');
      showTab('summary');
      document.getElementById('result-badge').classList.add('visible');
      if (document.getElementById('auto-summarize').checked) {
        switchTab('summary');
        await generateSummary();   // 요약 완료 후 다음 항목 시작
        _autoStartNext();
      } else {
        switchTab('result');
        _autoStartNext();
      }
    } else if (result.status === 'cancelled') {
      appendLog('⏹ 전사가 중지되었습니다.');
      const _ri = _runningItem(); if (_ri) { _ri.status = 'cancelled'; renderQueue(); }
    } else {
      showError('전사 실패. 위의 로그를 확인해주세요.');
      _failCurrentAndNext();
    }
  });

  es.onerror = () => {
    es.close();
    setRunning(false);
    showError('스트리밍 연결이 끊어졌습니다.');
    _failCurrentAndNext();
  };
}

/* ── Generate summary ── */
async function generateSummary() {
  if (!currentTxtPath) return;

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

  try {
    let resp;
    try {
      resp = await fetch('/summarize', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({txt_path: currentTxtPath, prompt: promptText}),
      });
    } catch (e) {
      area.textContent = '오류: ' + e.message;
      hasError = true;
      return;
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
        if (errorNext && line.startsWith('data: ')) {
          area.textContent = '오류: ' + JSON.parse(line.slice(6));
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
  } finally {
    btn.disabled = false;
    btn.textContent = '다시 생성';
    statusEl.textContent = hasError ? '' : '✓ 완료';
    if (!hasError) {
      setStage('complete');
      document.getElementById('copy-summary-btn').style.display = '';
      // 완료 시 마크다운 렌더(** 굵게/표/리스트 등). raw 텍스트는 pre에 남겨 복사에 사용.
      const rendered = document.getElementById('summary-rendered');
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
// 큐 아이템: {url, meta, status: 'waiting'|'running'|'done'|'error'|'cancelled'}
let urlQueue = [];
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
  const item = {url, meta: null, status: 'waiting'};
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
    // 영상정보(제목) 확보 시 제목으로 치환 표시(URL은 내부/툴팁 보존)
    const display = (item.meta && item.meta.title) ? item.meta.title : item.url;
    return `<li class="queue-item ${cfg.rowCls}">
      <span class="queue-item-idx">${i + 1}</span>
      <span class="queue-item-url" title="${_attrEsc(item.url)}">${esc(display)}</span>
      <span class="queue-status ${cfg.cls}">${cfg.label}</span>
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
  if (running) running.status = 'done';

  const next = urlQueue.find(i => i.status === 'waiting');
  if (!next) { renderQueue(); return; }

  renderQueue();
  const rem = urlQueue.filter(i => i.status === 'waiting').length - 1;
  appendLog(`\n⏭ 대기열 다음 항목 자동 시작 (남은: ${rem}개)...`);
  setTimeout(() => {
    document.getElementById('url-input').value = next.url;
    startTranscription();
  }, 1500);
}

/* 현재 항목을 오류로 표기하고 다음 대기열로 넘어간다 */
function _failCurrentAndNext() {
  const running = _runningItem();
  if (running) running.status = 'error';
  renderQueue();

  const next = urlQueue.find(i => i.status === 'waiting');
  if (!next) return;

  const rem = urlQueue.filter(i => i.status === 'waiting').length - 1;
  appendLog(`\n⏭ 오류 발생 — 대기열 다음 항목으로 넘어갑니다 (남은: ${rem}개)...`);
  setTimeout(() => {
    document.getElementById('url-input').value = next.url;
    startTranscription();
  }, 1500);
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
let _summaryMd = '';      // 현재 열린 요약 원문(마크다운) — 몰입형 재구성에 사용
let _immersive = false;

async function openSummaryModal(summaryPath, title) {
  const overlay  = document.getElementById('sum-overlay');
  const titleEl  = document.getElementById('sum-panel-title');
  const bodyEl   = document.getElementById('sum-panel-body');

  titleEl.textContent = title;
  bodyEl.innerHTML    = '<p class="sum-loading">불러오는 중…</p>';
  overlay.hidden      = false;
  document.body.style.overflow = 'hidden';
  _setImmersive(false);                                  // 항상 일반 보기로 시작
  document.getElementById('sum-immersive-btn').hidden = true;

  try {
    const data = await YS.apiSummaryContent(summaryPath);
    if (data.error) throw new Error(data.error);
    _summaryMd = data.content || '';
    bodyEl.innerHTML = YS.renderMarkdown(_summaryMd);
    // 캡처 이미지가 있을 때만 몰입형 버튼 노출
    document.getElementById('sum-immersive-btn').hidden = !bodyEl.querySelector('.kf-strip');
  } catch (e) {
    _summaryMd = '';
    bodyEl.innerHTML = `<p class="sum-error">오류: ${e.message}</p>`;
  }
}

function toggleImmersive() { _setImmersive(!_immersive); }

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
  if (!on) return;

  const tmp = document.createElement('div');
  tmp.innerHTML = YS.renderMarkdown(_summaryMd);
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
  gal.scrollTop = txt.scrollTop = 0;   // 몰입형 진입 시 항상 맨 위에서 시작
}

function closeSummaryModal() {
  document.getElementById('sum-overlay').hidden = true;
  document.body.style.overflow = '';
  _setImmersive(false);                                  // 다음 열림을 위해 초기화
}

function handleSumOverlayClick(e) {
  if (e.target === document.getElementById('sum-overlay')) closeSummaryModal();
}

async function openTranscriptModal(txtPath, title) {
  const overlay  = document.getElementById('trans-overlay');
  const titleEl  = document.getElementById('trans-panel-title');
  const bodyEl   = document.getElementById('trans-panel-body');

  titleEl.textContent = title;
  bodyEl.textContent  = '불러오는 중…';
  overlay.hidden      = false;
  document.body.style.overflow = 'hidden';

  try {
    const res  = await fetch('/history/text', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ txt_path: txtPath }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    bodyEl.textContent = data.text || '(내용 없음)';
  } catch (e) {
    bodyEl.textContent = '오류: ' + e.message;
  }
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
