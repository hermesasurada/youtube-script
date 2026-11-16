/* 유튜브 스크립트 PC 화면 — 주소 대기열·붙여넣기·복사.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
/* ── URL Queue ── */
// 큐 아이템: {url, meta, status, error, errorStage, txtPath, retryFrom}
let urlQueue = [];
let _queueLaunchTimer = null;

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
  if (!document.getElementById('queue-list')) return;   // 사이드바 대기열 UI 제거됨(서버 큐로 일원화)
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
