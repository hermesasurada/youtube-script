/* 유튜브 스크립트 PC 화면 — 진행 표시·공용 도우미·처리 큐 팝업.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
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

/* URL 추가 폼 — 메타(제목·채널)가 확인돼야 추가 버튼이 활성화된다 */
let _qPreviewTimer = null;
let _qPreview = null;          // {yt_id, title, channel} | null

function qPreviewDebounced() {
  clearTimeout(_qPreviewTimer);
  _qPreviewTimer = setTimeout(qPreview, 350);
}

async function qPreview() {
  const url = document.getElementById('q-add-url').value.trim();
  const btn = document.getElementById('q-add-btn');
  const pv  = document.getElementById('q-add-preview');
  _qPreview = null;
  btn.disabled = true;
  if (!url) { pv.innerHTML = ''; return; }
  pv.innerHTML = '<span class="q-pv-wait">영상 정보 확인 중…</span>';
  try {
    const d = await (await fetch('/queue/preview?url=' + encodeURIComponent(url))).json();
    // 확인하는 사이 입력이 바뀌었으면 결과를 버린다
    if (document.getElementById('q-add-url').value.trim() !== url) return;
    if (!d.ok) { pv.innerHTML = `<span class="q-pv-err">${_attrEsc(d.error || '조회 실패')}</span>`; return; }
    if (d.duplicate === 'history') {
      pv.innerHTML = `<span class="q-pv-err">이미 전사된 영상입니다 — ${_attrEsc(d.title)}</span>`;
      return;
    }
    if (d.duplicate === 'queue') {
      pv.innerHTML = `<span class="q-pv-err">이미 큐에 대기 중입니다 — ${_attrEsc(d.title)}</span>`;
      return;
    }
    _qPreview = d;
    const seg = d.start_sec
      ? `<span class="q-pv-seg">${_attrEsc(d.start_label)}부터 전사</span>` : '';
    // 같은 채널을 지난번에 넣은 설정으로 캡처·증류 칩을 맞춰 둔다(그대로 바꿔도 됨).
    const applied = _qApplyChannelPrefs(d.prefs);
    const note = applied
      ? `<span class="q-pv-seg">${_attrEsc(applied)}</span>` : '';
    pv.innerHTML = `<div class="q-pv-ok"><b>${_attrEsc(d.title)}</b>`
                 + `<span>${_attrEsc(d.channel || '')}</span>${seg}${note}</div>`;
    btn.disabled = false;
  } catch (e) {
    pv.innerHTML = `<span class="q-pv-err">조회 실패: ${_attrEsc(e.message)}</span>`;
  }
}

function qToggleAddCapture(btn) {
  const on = !btn.classList.contains('on');
  btn.classList.toggle('on', on);
  btn.setAttribute('aria-checked', String(on));
}

async function qPasteUrl() {
  const inp = document.getElementById('q-add-url');
  try {
    const text = await navigator.clipboard.readText();
    inp.value = (text || '').trim();
    qPreviewDebounced();
  } catch (e) {
    // 원격(http)에서는 clipboard 읽기가 막힌다 — 입력창에 직접 붙여넣도록 포커스만
    inp.focus();
    document.getElementById('q-add-preview').innerHTML =
      '<span class="q-pv-wait">이 환경에선 자동 붙여넣기가 막혀 있습니다 — 입력창에 직접 붙여넣어 주세요.</span>';
  }
}

/* 미리보기로 받은 채널 기본값을 캡처·증류 칩에 반영한다. 반환: 안내 문구('' = 없음) */
function _qApplyChannelPrefs(prefs) {
  const set = (id, on) => {
    const b = document.getElementById(id);
    if (!b) return;
    b.classList.toggle('on', !!on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  };
  // 기본은 둘 다 켜짐 — 채널 이력이 없으면 종전과 같다.
  set('q-add-capture-btn', true);
  set('q-add-distill-btn', true);
  if (!prefs || (prefs.capture === undefined && prefs.distill === undefined)) return '';
  const parts = [];
  if (prefs.capture !== undefined) { set('q-add-capture-btn', prefs.capture); parts.push('캡처 ' + (prefs.capture ? 'ON' : 'OFF')); }
  if (prefs.distill !== undefined) { set('q-add-distill-btn', prefs.distill); parts.push('증류 ' + (prefs.distill ? 'ON' : 'OFF')); }
  const from = prefs.source === 'channel' ? '채널 설정' : '지난 처리';
  return `${from} 따름 — ${parts.join(' · ')}`;
}

async function qAddToQueue() {
  if (!_qPreview) return;
  const btn = document.getElementById('q-add-btn');
  btn.disabled = true;
  const r = await fetch('/queue/items', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: 'https://www.youtube.com/watch?v=' + _qPreview.yt_id,
                          title: _qPreview.title,
                          start_sec: _qPreview.start_sec || 0,
                          capture: document.getElementById('q-add-capture-btn').classList.contains('on'),
                          distill: document.getElementById('q-add-distill-btn').classList.contains('on')}),
  });
  const d = await r.json();
  const pv = document.getElementById('q-add-preview');
  if (d.ok) {
    pv.innerHTML = `<span class="q-pv-done">✅ 큐 등록 (대기 ${d.waiting}번째) — ${_attrEsc(_qPreview.title)}</span>`;
    document.getElementById('q-add-url').value = '';
    _qPreview = null;
    refreshQueueModal();
  } else {
    pv.innerHTML = `<span class="q-pv-err">${_attrEsc(d.error || '등록 실패')}</span>`;
  }
}
function closeQueueModal() {
  document.getElementById('queue-overlay').hidden = true;
  document.body.style.overflow = '';
}

function _qhms(sec) { return YS.hms(sec); }

async function refreshQueueModal() {
  const body = document.getElementById('queue-modal-body');
  try {
    const d = await (await fetch('/queue/items')).json();
    const row = (v, i, movable, extraCtl = '') => {
      const [label, cls] = _Q_STATUS[v.status] || [v.status, ''];
      const ch = v.channel_name || (v.channel_id === 'manual' ? '수동' : '') || '';
      const fmtEta = t => { const d = new Date(t * 1000);
        return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; };
      const bits = [];
      if (v.status === 'deferred' && v.next_retry_at) bits.push(`${v.next_retry_at.slice(5, 16)} 재시도`);
      if (v.eta) bits.push(`~${fmtEta(v.eta)} 시작 예정`);
      if (v.start_sec) bits.push(`${_qhms(v.start_sec)}부터`);
      if (v.capture === 0) bits.push('캡처 제외');
      if (v.distill === 0) bits.push('증류 제외');
      if (v.attempt_count > 1) bits.push(`시도 ${v.attempt_count}`);
      const extra = bits.length ? `<span class="q-extra">${_attrEsc(bits.join(' · '))}</span>` : '';
      const waiting = ['pending', 'kf_retry', 'deferred'].includes(v.status);
      const capOn = v.capture !== 0;      // NULL=채널/기본 따름(사실상 포함), 0=제외
      const capBtn = waiting && v.status !== 'kf_retry'
        ? `<button class="q-cap-row ${capOn ? 'on' : ''}" role="switch" aria-checked="${capOn}"
                   onclick="toggleQueueCapture(${v.id}, ${capOn ? 'false' : 'true'})"
                   title="이 영상의 캡처 포함/제외">캡처</button>` : '';
      const dsOn = v.distill !== 0;       // NULL=채널/기본 따름(포함 취급), 0=제외
      const dsBtn = waiting && v.status !== 'kf_retry'
        ? `<button class="q-cap-row ${dsOn ? 'on' : ''}" role="switch" aria-checked="${dsOn}"
                   onclick="toggleQueueDistill(${v.id}, ${dsOn ? 'false' : 'true'})"
                   title="이 영상의 지식증류 포함/제외">증류</button>` : '';
      const ctl = (movable || capBtn || dsBtn || extraCtl) ? `
        <span class="q-ctl">${capBtn}${dsBtn}${extraCtl}${movable ? `
          <button onclick="moveQueueItem(${v.id},'up')" title="위로">↑</button>
          <button onclick="moveQueueItem(${v.id},'down')" title="아래로">↓</button>
          <button class="q-del" onclick="cancelQueueItem(${v.id})" title="취소">×</button>` : ''}
        </span>` : '';
      return `<div class="q-row">
        <span class="q-no">${i}</span>
        <span class="q-st ${cls}">${label}</span>
        <div class="q-main">
          <div class="q-title">${_attrEsc(v.title || v.yt_id)}</div>
          <div class="q-sub">${_attrEsc(ch)}${ch && extra ? ' · ' : ''}${extra}</div>
        </div>${ctl}</div>`;
    };
    // 본편 큐와 캡처 재시도 큐는 분리돼 있다(주기당 본편 1건 + 캡처 1건)
    const main = d.waiting.filter(v => v.status !== 'kf_retry');
    const kf   = d.waiting.filter(v => v.status === 'kf_retry');
    let n = 0, k = 0;
    const mainRows = main.map(v => row(v, ++n, v.status === 'pending')).join('');
    const kfRows   = kf.map(v => row(v, ++k, true)).join('');
    // 최종 실패 건은 재시도 초기화 후 큐 맨 뒤로 복귀시킬 수 있다
    const recent = d.recent.map(v => row(v, '', false,
      v.status === 'failed'
        ? `<button class="q-requeue" title="재시도 초기화 후 큐 복귀" onclick="requeueItem(${v.id})">↻ 복귀</button>`
        : '')).join('');
    const group = (title, count, hint, rows, empty = '') => `<section class="q-group">`
      + `<div class="q-sec"><span>${title} · ${count}건</span>${hint ? `<span class="q-hint">${hint}</span>` : ''}</div>`
      + (rows || `<div class="q-empty">${empty}</div>`) + `</section>`;
    body.innerHTML = group('본편 대기', main.length, '유휴면 바로, 이후 10분에 1건', mainRows, '대기 중인 영상이 없습니다.')
      + (kf.length ? group('캡처 재시도', kf.length, '본편과 별도 슬롯', kfRows) : '')
      + (recent ? group('최근 처리', d.recent.length, '', recent) : '');
  } catch (e) {
    body.innerHTML = `<div class="q-empty">불러오기 실패: ${_attrEsc(e.message)}</div>`;
  }
}

async function requeueItem(id) {
  await YS.queueApi.requeue(id);
  refreshQueueModal();
}

async function toggleQueueDistill(id, enable) {
  await YS.queueApi.setFlags(id, {distill: enable});
  refreshQueueModal();
}

async function toggleQueueCapture(id, enable) {
  await YS.queueApi.setFlags(id, {capture: enable});
  refreshQueueModal();
}

async function moveQueueItem(id, direction) {
  await YS.queueApi.move(id, direction);
  refreshQueueModal();
}

async function cancelQueueItem(id) {
  if (await YS.queueApi.cancel(id)) refreshQueueModal();
}
