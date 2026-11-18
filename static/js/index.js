/* 유튜브 스크립트 PC 화면 — 화면 상태·프롬프트·각주 제외 용어·설정 창·캡처 상태 배지·탭.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
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

// ── 각주 제외 용어(프롬프트 패널 하단 칩) ─────────────────────────────
// 칩 툴팁: 각주에서 뺀 용어는 저장된 뜻(같은 표기의 다른 뜻은 남는다), 직접 입력한 용어는 모든 뜻
function _termSenseTitle(t) { return YS.termSenseTitle(t); }
async function renderTermExclusions() {
  const list = document.getElementById('term-excl-list');
  if (!list) return;
  const terms = await YS.ensureTermExclusions(true);
  list.innerHTML = terms.length
    ? terms.map(t => `<span class="term-excl-chip" title="${YS.attrEscape(_termSenseTitle(t))}">${YS.escapeHtml(t)}`
        + `<button type="button" class="term-excl-del" data-term="${YS.attrEscape(t)}" title="목록에서 제거">✕</button></span>`).join('')
    : '<span class="term-excl-empty">아직 없음 — 요약 각주의 용어를 누르거나 아래에 입력</span>';
}
async function addTermExclusion(ev) {
  if (ev) ev.preventDefault();
  const input = document.getElementById('term-excl-input');
  const term = (input.value || '').trim();
  if (!term) return;
  try { await YS.apiTermExclusion(term, false); input.value = ''; await renderTermExclusions(); }
  catch (e) { alert('추가 실패: ' + e.message); }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest && e.target.closest('.term-excl-del');
  if (!b) return;
  if (!confirm(`'${b.dataset.term}'을(를) 제외 목록에서 뺄까요?`)) return;
  try { await YS.apiTermExclusion(b.dataset.term, true); await renderTermExclusions(); }
  catch (err) { alert('제거 실패: ' + err.message); }
});

/* 프롬프트 팝업: 요약 프롬프트 / Grok 문체 보정 2탭(각주 제외 용어는 2026-10-24 설정 팝업으로 옮김).
   둘 다 조회 전용이다 — 프롬프트는 prompt.txt를, Grok 노트는 app.py를 고친다. */
function switchPromptTab(name) {
  document.querySelectorAll('#prompt-panel .prompt-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('#prompt-panel .prompt-tabpane').forEach(sec => {
    const on = sec.dataset.pane === name;
    sec.classList.toggle('active', on);
    sec.hidden = !on;
  });
  if (name === 'terms') renderTermExclusions();
  if (name === 'grok') loadGrokNote();
}

let _grokNoteLoaded = false;
async function loadGrokNote() {
  const ta = document.getElementById('grok-note-text');
  if (!ta || _grokNoteLoaded) return;
  try {
    const d = await (await fetch('/prompt')).json();
    if (d.grok_note) {
      ta.value = d.grok_note;
      _grokNoteLoaded = true;
    } else {
      // 서버가 아직 이 필드를 내려주지 않는 경우(구버전 기동 중)
      ta.value = '이 서버에서는 Grok 문체 보정 노트를 불러올 수 없습니다.';
    }
  } catch (e) {
    ta.value = '불러오기 실패: ' + e.message;
  }
}

function openPromptModal() {
  if (!document.getElementById('prompt-text').value) loadSavedPrompt();
  document.getElementById('prompt-warn').hidden = true;
  document.getElementById('save-status').textContent = '';
  document.getElementById('prompt-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
  switchPromptTab('prompt');
}


function closePromptModal() {
  document.getElementById('prompt-overlay').hidden = true;
  document.body.style.overflow = '';
}

function handlePromptOverlayClick(e) {
  if (e.target.id === 'prompt-overlay') closePromptModal();
}

/* ── 채널 자동 모니터 모달 ── */
/* 설정 팝업 탭(2026-10-24 사용자 지시): 모델 / 채널 / 각주 제외. 마지막 탭을 기억한다(localStorage ytSettingsTab). */
const SETTINGS_TABS = ['model', 'channels', 'terms'];
function switchSettingsTab(name) {
  if (!SETTINGS_TABS.includes(name)) name = 'model';
  document.querySelectorAll('#channels-panel .prompt-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('#channels-panel .prompt-tabpane').forEach(sec => {
    const on = sec.dataset.pane === name;
    sec.classList.toggle('active', on);
    sec.hidden = !on;
  });
  try { localStorage.setItem('ytSettingsTab', name); } catch (e) {}
  if (name === 'terms') renderTermExclusions();
}
async function openChannelsModal(tab) {
  let t = tab;
  if (!t) { try { t = localStorage.getItem('ytSettingsTab'); } catch (e) {} }
  switchSettingsTab(t || 'model');
  document.getElementById('channels-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
  if (_channelsCache) renderChannels(_channelsCache);
  else document.getElementById('channels-list').innerHTML =
    '<div class="utility-loading"><span></span><span></span><span></span></div>';
  requestAnimationFrame(() => { loadChannels({force: !!_channelsCache}); });
}
function closeChannelsModal() {
  document.getElementById('channels-overlay').hidden = true;
  document.body.style.overflow = '';
}
function handleChannelsOverlayClick(e) {
  if (e.target.id === 'channels-overlay') closeChannelsModal();
}
// 모델 선택기는 common.js의 YS.monitorModels가 상태·저장을, model-selector.js가 화면을 맡는다.
let _channelsCache = null;
let _channelsRequest = null;

function renderChannels(d) {
  const list = document.getElementById('channels-list');
  const qEl  = document.getElementById('channels-queue');
  YS.monitorModels.load(d);
  YS.monitorModels.mount(document.getElementById('model-selector'), {
    onSaved: saved => { if (_channelsCache) Object.assign(_channelsCache, YS.monitorModels.fields(saved)); },
  });
  const q = d.queue || {};
  const parts = [];
  if (q.pending)    parts.push(`대기 ${q.pending}`);
  if (q.processing) parts.push(`처리중 ${q.processing}`);
  if (q.kf_retry)   parts.push(`캡처재시도 ${q.kf_retry}`);
  if (q.done)       parts.push(`완료 ${q.done}`);
  if (q.failed)     parts.push(`실패 ${q.failed}`);
  qEl.textContent = parts.length ? parts.join(' · ') : '큐 비어있음';
  // 채널 목록(로고·검색·자동>캡처>증류 스위치)은 PC·모바일 공용 channel-list.js가 그린다.
  YS.channelList.mount(list, d, {countEl: document.getElementById('channels-count')});
}

async function _fetchChannels(force = false) {
  if (_channelsCache && !force) return _channelsCache;
  if (_channelsRequest) return _channelsRequest;
  _channelsRequest = fetch('/channels', {cache: 'no-store'})
    .then(r => r.json())
    .then(d => {
      if (d.error) throw new Error(d.error);
      _channelsCache = d;
      return d;
    })
    .finally(() => { _channelsRequest = null; });
  return _channelsRequest;
}

async function loadChannels({force = false} = {}) {
  const list = document.getElementById('channels-list');
  try {
    const d = await _fetchChannels(force);
    if (!document.getElementById('channels-overlay').hidden) renderChannels(d);
  } catch (e) {
    if (!document.getElementById('channels-overlay').hidden) {
      list.innerHTML = `<div class="channels-empty">오류: ${esc(e.message)}</div>`;
    }
  }
}

/* {transcript} 자리표시자 검증 후 저장. 누락 시 차단(요약 시 전사 본문이 안 들어감). */

const IS_REMOTE = document.body.classList.contains('remote-mode');

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
