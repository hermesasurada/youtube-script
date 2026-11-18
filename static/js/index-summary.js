/* 유튜브 스크립트 PC 화면 — 요약 창 — 본문·블로그·몰입 보기·글자 크기·글꼴·전사 번역.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
/* ── Summary Modal ── (marked.js 설정은 공유 모듈 common.js에서 일원화) */
let _summaryMd = '';      // 현재 열린 요약 원문(마크다운) — 몰입형 재구성·블로거 복사에 사용
let _titleKo = '';        // 현재 열린 요약의 번역 제목(외국어 제목일 때만 값이 있다)
let _summaryItemId = 0;   // 현재 열린 요약의 DB ID — 경로를 브라우저에 노출하지 않는다
let _summaryRead = false;

function _setSummaryVideoLinks(url) {
  const link = document.getElementById('sum-youtube-link');
  if (!link) return;
  const href = String(url || '').trim();
  link.hidden = !href;
  link.href = href || '#';
}

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
    box.textContent = `증류 ↪ ${chan}`;      // 모바일과 같은 짧은 표기('따름'은 ↪, 근거는 툴팁) — 2026-11-18 하단 버튼 정리
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
    // 목록의 증류 대상 표시·필터도 바로 맞춘다
    const it = _historyItems.find(i => i.item_id === _summaryItemId);
    if (it && d.effective !== undefined) { it.distill = !!d.effective; if (_histDistillOnly) applyHistoryFilter(true); }
  } catch (e) {
    alert('증류 설정 실패: ' + e.message);
  }
}

/* 블로그 발행 — '블로거용 복사'를 대체(2026-09-05). 같은 렌더러(mdToBloggerHtml)로 만든
   HTML을 서버에 보내 hermes_blogger로 바로 공개 발행한다(wm과 토큰 공유). 발행된 영상은
   버튼이 '🔗 블로그에서 보기'가 되고 누르면 글을 연다. */
let _blogUrl = '';
let _blogState = { stale: false, changed_at: '', published_at: '' };

function _setPubBtn(url, state) {
  _blogUrl = url || '';
  if (state) _blogState = state;
  if (!_blogUrl) _blogState = { stale: false, changed_at: '', published_at: '' };
  const b = document.getElementById('sum-publish-btn');
  if (!b) return;
  const l = b.querySelector('.pub-label');
  if (l) l.textContent = _blogUrl ? '발행됨' : '블로그 발행';
  const stamp = (_blogState.published_at || '').slice(0, 16);
  b.title = _blogUrl
    ? (stamp ? `${stamp} 게시됨` : '게시 시각 기록 없음')
      + (_blogState.stale ? ' — 이후 내용이 바뀜, 열기 또는 수정' : ' — 열기 또는 수정')
    : '요약을 내 블로그스팟에 바로 발행 (즉시 공개)';
  b.classList.toggle('published', !!_blogUrl);
  b.classList.toggle('stale', !!(_blogUrl && _blogState.stale));
  b.style.color = '';
  b.disabled = false;
}

/* 소제목 구간 직접 편집(✏️). 저장되면 화면을 지금 내용으로 다시 그린다. */
function _attachSectionEditors(rootEl) {
  if (!rootEl || !_summaryItemId) return;
  YS.attachSectionEditors(rootEl, {
    md: _summaryMd, itemId: _summaryItemId,
    onSaved: (content, blog) => {
      _summaryMd = content || _summaryMd;
      if (blog && _blogUrl) _setPubBtn(_blogUrl, blog);
      const imm = document.getElementById('sum-immersive-body');
      if (imm && !imm.hidden) { _setImmersive(true); return; }
      const body = document.getElementById('sum-panel-body');
      if (!body) return;
      const top = body.scrollTop;
      body.innerHTML = YS.renderMarkdown(_summaryMd);
      YS.applyTitleTranslation(body, _titleKo);
      YS.stripSummaryPopupChrome(body);
      YS.setupStickySummarySections(body);
      YS.decoratePeople(body);                 // 인명 클릭 소개(토스트)
      YS.attachSummaryNotes(body, _summaryItemId);
      _attachSectionEditors(body);
      body.scrollTop = top;
      _updateSumProgress(body);
      if (typeof loadHistory === 'function') loadHistory(true);
    },
  });
}

/* 메모 저장/삭제 즉시 발행 버튼 상태를 갱신한다(common.js가 알림). */
document.addEventListener('ys:blog-state', e => {
  const { itemId, blog } = e.detail || {};
  if (_blogUrl && String(itemId) === String(_summaryItemId)) _setPubBtn(_blogUrl, blog);
});

/* 발행된 글을 지금 요약·메모로 덮어쓴다. URL은 그대로고 발행시각만 갱신된다. */
async function _updateBlogPost(btn) {
  if (!_summaryMd || !_summaryItemId) return;
  if (!await YS.askConfirm({ title: '블로그 글 수정', message: '발행된 글을 지금 요약·메모로 수정할까요? (주소는 그대로입니다)', ok: '수정' })) return;
  const label = btn && btn.querySelector('.pub-label');
  const setLbl = (t, color) => { if (label) label.textContent = t; if (btn) btn.style.color = color || ''; };
  const { html, title } = YS.mdToBloggerHtml(_summaryMd, { translatedTitle: _titleKo, people: await YS.blogPeople(_summaryItemId) });
  if (btn) btn.disabled = true;
  setLbl('수정 중…');
  try {
    const r = await fetch('/history/publish-blog', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: _summaryItemId, mode: 'update', html, title }),
    });
    const d = await r.json();
    if (d && d.status === 'updated') {
      _setPubBtn(d.url || _blogUrl, d.blog);
      if (typeof loadHistory === 'function') loadHistory(true);
      return;
    }
    setLbl(d && d.code === 'not_configured' ? '연동 미설정' : '수정 실패', 'var(--error)');
    console.warn('[blog]', d);
  } catch (e) {
    setLbl('수정 실패', 'var(--error)'); console.warn('[blog]', e);
  }
  setTimeout(() => _setPubBtn(_blogUrl, _blogState), 2200);
}

/* 발행한 블로그 글을 지우고 '블로그 발행' 상태로 되돌린다(2026-10-19). */
async function _deleteBlogPost(btn) {
  if (!_summaryItemId || !_blogUrl) return;
  if (!await YS.askConfirm({ title: '블로그 글 삭제', message: '블로그에서 이 글을 삭제할까요? (공개된 글이 지워지고 되돌릴 수 없습니다)', ok: '삭제', danger: true })) return;
  const label = btn && btn.querySelector('.pub-label');
  if (btn) btn.disabled = true;
  if (label) label.textContent = '삭제 중…';
  try {
    const d = await YS.apiDeleteBlog(_summaryItemId);
    if (d && d.status === 'deleted') {
      _setPubBtn('');
      if (typeof loadHistory === 'function') loadHistory(true);
      return;
    }
    if (label) label.textContent = d && d.code === 'not_configured' ? '연동 미설정' : '삭제 실패';
    if (btn) btn.style.color = 'var(--error)';
    console.warn('[blog]', d);
  } catch (e) {
    if (label) label.textContent = '삭제 실패';
    if (btn) btn.style.color = 'var(--error)';
    console.warn('[blog]', e);
  }
  setTimeout(() => _setPubBtn(_blogUrl, _blogState), 2200);
}

async function publishSummaryToBlog(btn) {
  // 이미 발행된 글은 바로 열지 않고 '보기 / 수정' 중에서 고르게 한다.
  if (_blogUrl) {
    YS.openBlogMenu(btn, {
      url: _blogUrl, stale: !!_blogState.stale, changedAt: _blogState.changed_at || '',
      publishedAt: _blogState.published_at || '',
      contentChanged: !!_blogState.content_changed, outdatedRender: !!_blogState.outdated_render,
      onUpdate: () => _updateBlogPost(btn),
      onDelete: () => _deleteBlogPost(btn),
    });
    return;
  }
  if (!_summaryMd || !_summaryItemId) return;
  const label = btn && btn.querySelector('.pub-label');
  const setLbl = (t, color) => { if (label) label.textContent = t; if (btn) btn.style.color = color || ''; };
  if (!await YS.askConfirm({ title: '블로그 발행', message: '블로그스팟에 바로 발행할까요? (초안이 아니라 즉시 공개됩니다)', ok: '발행' })) return;
  const { html, title } = YS.mdToBloggerHtml(_summaryMd, { translatedTitle: _titleKo, people: await YS.blogPeople(_summaryItemId) });
  btn.disabled = true; setLbl('발행 중…');
  try {
    const d = await YS.apiPublishBlog(_summaryItemId, _titleKo || title, html);
    if (d && (d.status === 'ok' || d.status === 'exists')) {
      _setPubBtn(d.url, d.blog);
      if (typeof loadHistory === 'function') loadHistory(true);
    } else {
      setLbl(d && d.code === 'not_configured' ? '연동 미설정' : '발행 실패', 'var(--error)');
      console.warn('[blog]', d);
      setTimeout(() => _setPubBtn(''), 2200);
    }
  } catch (e) {
    setLbl('발행 실패', 'var(--error)'); console.warn('[blog]', e);
    setTimeout(() => _setPubBtn(''), 2200);
  }
}

function _setSummaryReadUI(isRead) {
  _summaryRead = !!isRead;
  const btn = document.getElementById('sum-read-btn');
  if (!btn) return;
  btn.classList.toggle('active', _summaryRead);
  btn.querySelector('.read-label').textContent = _summaryRead ? '읽음' : '읽음 처리';
  btn.title = _summaryRead ? '읽음 상태입니다. 클릭하면 안읽음으로 변경' : '읽음으로 표시';
}

async function toggleSummaryRead(btn) {
  if (!_summaryItemId) return;
  const next = !_summaryRead;
  btn.disabled = true;
  try {
    const result = await YS.apiMarkRead(_summaryItemId, next);
    if (!result) throw new Error('상태 변경 실패');
    if (result.revision) _historyRevision = result.revision;
    const item = _historyItems.find(i => i.item_id === _summaryItemId);
    if (item) item.is_read = next;
    _setSummaryReadUI(next);
    applyHistoryFilter(true);
    // 다 읽었다는 표시이므로 목록으로 돌려보낸다. 안읽음으로 되돌리는 경우는
    // 계속 보려는 것이라 닫지 않는다(2026-09-20 사용자 지시).
    if (next) closeSummaryModal();
  } catch (e) {
    console.warn('[read] 상태 변경 실패', e);
  } finally {
    btn.disabled = false;
  }
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
// 목록·글꼴 값은 공용 hermes-reader-fonts/fonts.js가 정본(2026-11-01 사용자 지시) — 공용 목록에 글꼴을 더하면
// 여기 고칠 것 없이 나타난다. 옛 저장값 'sans'는 Noto Sans KR이었다. 목록에 없는 값(지운 글꼴)은 기본값으로.
const _RF = window.HermesReaderFonts
  || {list: [], get: () => null, resolve: () => null, apply() {}, fillSelect() {}, ensureWeb() {}};
let _readerFont = _RF.resolve(localStorage.getItem('sumReaderFont'), {legacy: {sans: 'noto'}, fallback: 'noto'});
function _applyReaderFont() {
  _RF.apply(document.getElementById('sum-panel'), _readerFont);
  const sel = document.getElementById('sum-font-select');
  if (sel && !sel.options.length) _RF.fillSelect(sel);
  if (sel && sel.value !== _readerFont) sel.value = _readerFont;
}
function setReaderFont(key) {
  if (!_RF.get(key)) return;
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

async function openSummaryModal(itemId, title) {
  const overlay  = document.getElementById('sum-overlay');
  const bodyEl   = document.getElementById('sum-panel-body');

  _summaryItemId = itemId;                              // 증류 설정 변경 대상
  _setSummaryReadUI(false);
  _setSummaryVideoLinks('');
  _setDistillUI(null);                                   // 값 로드 전에는 기본 표시
  bodyEl.innerHTML    = '<p class="sum-loading">불러오는 중…</p>';
  document.getElementById('sum-panel-footer').hidden = true;
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
      YS.ensureTermExclusions(),                           // 제외 용어를 렌더 전에 확보
    ]);
    if (data.error) throw new Error(data.error);
    _summaryMd = data.content || '';
    YS.setSummaryNotes(itemId, data.notes);
    _titleKo   = data.title_ko || '';                      // 외국어 제목의 한국어 번역
    _setPubBtn(data.blog_url, data.blog);                               // 발행 여부에 따라 📤 / 🔗
    _setSummaryReadUI(data.is_read);
    _setDistillUI(data.distill);                           // 서버가 함께 준 증류 설정 반영
    _setSummaryVideoLinks(data.webpage_url);
    bodyEl.innerHTML = YS.renderMarkdown(_summaryMd);
    YS.applyTitleTranslation(bodyEl, _titleKo);            // 제목을 번역본으로, 원문은 아래 병기
    YS.stripSummaryPopupChrome(bodyEl);                    // '핵심 내용' 머리말·목차는 팝업에서 생략
    YS.setupStickySummarySections(bodyEl);                 // 소제목은 해당 h3 섹션 안에서만 고정
    YS.decoratePeople(bodyEl);                 // 인명 클릭 소개(토스트)
    YS.attachSummaryNotes(bodyEl, itemId);
    _attachSectionEditors(bodyEl);
    bodyEl.scrollTop = 0;
    _updateSumProgress(bodyEl);
    // 캡처 이미지가 있을 때만 몰입형 버튼 노출
    const hasImg = !!bodyEl.querySelector('.kf-strip');
    document.getElementById('sum-immersive-btn').hidden = !hasImg;
    document.getElementById('sum-panel-footer').hidden = false;
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
  if (!on) { YS.attachSummaryNotes(normal, _summaryItemId); _updateSumProgress(normal); return; }

  const tmp = document.createElement('div');
  tmp.innerHTML = YS.renderMarkdown(_summaryMd);
  YS.applyTitleTranslation(tmp, _titleKo);                      // 몰입형에서도 제목은 번역본
  YS.stripSummaryPopupChrome(tmp);
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
  YS.setupStickySummarySections(txt);                      // 몰입형 본문도 같은 섹션 경계 적용
  YS.decoratePeople(txt);                 // 인명 클릭 소개(토스트)
  YS.attachSummaryNotes(txt, _summaryItemId);
  _attachSectionEditors(txt);
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

/* 요약을 HTML 파일 하나로 내보내기(2026-11-17) — 아는 사람에게 보내는 용도. 만들기는 공용 YS.exportSummaryFile. */
async function exportSummaryHtml(btn) {
  if (!_summaryMd) return;
  const label = btn && btn.querySelector('.exp-label');
  const orig = label ? label.textContent : '';
  if (btn) btn.disabled = true;
  if (label) label.textContent = '만드는 중…';
  let done = '';
  try {
    const r = await YS.exportSummaryFile({ md: _summaryMd, titleKo: _titleKo, webpageUrl: (document.getElementById('sum-youtube-link') || {}).hidden ? '' : (document.getElementById('sum-youtube-link') || {}).href });
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
}
