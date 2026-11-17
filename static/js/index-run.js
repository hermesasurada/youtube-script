/* 유튜브 스크립트 PC 화면 — 전사 시작·요약 생성·단계 표시·중지·파일 선택 창.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
/* ── Start transcription ── */
/* URL 전사는 서버 큐(watch_queue)에 줄을 선다 — 자동 모니터와 같은 파이프라인.
   유휴(최근 주기 동안 처리 없음)면 즉시 시작하고, 이후 10분에 한 건.
   입력창 URL + 사이드바 대기열의 대기 항목을 전부 적재.
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
        lines.push(`✅ 큐 등록 (대기 ${d.waiting}번째): ${d.title}`
          + (d.started ? ' — 바로 시작합니다' : ''));
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
      '\n\n최근 10분 동안 처리가 없었으면 바로 시작하고, 이후에는 10분에 한 건입니다. 우측 상단 [큐] 버튼에서 순서를 바꿀 수 있습니다.';
  }
  renderQueue();
  openQueueModal();          // 등록 직후 현재 대기 순서를 바로 보여준다
}

async function startTranscription() {
  if (currentSource === 'url') { openQueueModal(); return; }   // URL 전사는 큐 팝업에서
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
document.getElementById('url-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && currentSource === 'url') startTranscription();
});
