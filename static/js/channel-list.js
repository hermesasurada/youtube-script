/* ── 설정 > 채널 탭 — PC·모바일 공용 채널 목록 ─────────────────────────────
   (2026-11-01 사용자 지시) 채널 로고 · 키워드 실시간 필터 · 자동 > 캡처 > 증류 스위치.
   한 줄에 로고 + 이름·뱃지, 아래 줄에 @핸들 · 처리 편수 · 마지막 처리일.
   자동수집이 꺼진 채널은 줄 전체를 흐리게 하고 '수집 꺼짐'을 붙인다.

   YS.channelList.mount(host, data, {mobile, countEl})
     host    목록을 그릴 요소. 다시 부르면 검색어·포커스는 그대로 두고 줄만 바꾼다.
     data    /channels 응답(채널 객체를 그 자리에서 고쳐 캐시와 맞춘다)
     countEl 'ON n · 전체 m' 을 쓸 요소(선택) */
(function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const norm = s => String(s || '').normalize('NFC').toLowerCase();

  // 자동 > 캡처 > 증류 순서. key = PATCH 필드, 기본값은 NULL일 때의 의미.
  const SWITCHES = [
    { key: 'enabled', label: '자동', title: '자동 수집 — 새 영상을 20분마다 확인해 전사·요약', dflt: false },
    { key: 'capture', label: '캡처', title: '영상 캡처(키프레임) 포함 — 끄면 요약만', dflt: true },
    { key: 'distill', label: '증류', title: '지식증류(옵시디언 볼트) 대상 — 끄면 증류에서 제외', dflt: true },
  ];
  const isOn = (c, sw) => (c[sw.key] == null ? sw.dflt : !!c[sw.key]);

  function _style() {
    if (document.getElementById('ys-chl-style')) return;
    const css = `
.chl{--chl-sw-w:32px;--chl-sw-h:18px;--chl-col:46px;--chl-logo:28px;display:flex;flex-direction:column;min-width:0;}
.chl--m{--chl-sw-w:34px;--chl-sw-h:20px;--chl-col:36px;--chl-logo:30px;}
.chl--m .chl-head,.chl--m .chl-row{column-gap:.4rem;padding-left:.5rem;padding-right:.5rem;}
.chl-top{position:sticky;top:calc(-1 * var(--chl-stick,0px));z-index:2;background:var(--surface);padding:.5rem 0 .35rem;margin-top:-.5rem;}
.chl-search{position:relative;display:flex;align-items:center;}
.chl-search input{width:100%;height:32px;padding:0 2rem 0 2rem;border:1px solid var(--border);border-radius:8px;
  background:var(--surface2);color:var(--text);font:inherit;font-size:13px;outline:none;}
.chl--m .chl-search input{height:38px;font-size:16px;}
.chl-search input:focus{border-color:var(--highlight);background:var(--surface);}
.chl-search input::-webkit-search-cancel-button{display:none;}
.chl-search svg{position:absolute;left:.6rem;width:14px;height:14px;color:var(--muted);pointer-events:none;}
.chl-clear{position:absolute;right:.3rem;width:24px;height:24px;border:0;border-radius:50%;background:transparent;
  color:var(--muted);font-size:15px;line-height:1;cursor:pointer;}
.chl-clear:hover{color:var(--text);background:var(--surface2);}
.chl-head,.chl-row{display:grid;grid-template-columns:var(--chl-logo) minmax(0,1fr) repeat(3,var(--chl-col));align-items:center;column-gap:.55rem;}
.chl-head{margin-top:.45rem;padding:.3rem .6rem;border:1px solid var(--border);border-bottom:0;border-radius:8px 8px 0 0;
  background:var(--surface2);color:var(--muted);font-size:10.5px;font-weight:700;letter-spacing:.03em;}
.chl-head span{text-align:center;}
.chl-head span:nth-child(2){text-align:left;}
.chl-head .chl-head-q{font-weight:600;letter-spacing:0;}
.chl-rows{border:1px solid var(--border);border-radius:0 0 8px 8px;background:var(--surface);overflow:hidden;}
.chl-row{padding:.36rem .6rem;border-top:1px solid var(--border);transition:background .12s;}
.chl-row:first-child{border-top:0;}
.chl-row:hover{background:color-mix(in oklab,var(--surface2) 60%,transparent);}
.chl-row[hidden]{display:none;}
.chl-logo{position:relative;width:var(--chl-logo);height:var(--chl-logo);border-radius:50%;overflow:hidden;flex-shrink:0;
  display:grid;place-items:center;background:var(--surface2);color:var(--muted);font-size:12px;font-weight:700;}
.chl-logo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;}
.chl-meta{min-width:0;display:flex;flex-direction:column;gap:1px;}
.chl-line1{display:flex;align-items:center;gap:.3rem;min-width:0;}
.chl-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:650;color:var(--text);}
.chl--m .chl-name{font-size:14px;}
.chl-badge{flex-shrink:0;padding:0 5px;border-radius:8px;font-size:10.5px;font-weight:700;line-height:15px;cursor:help;
  color:#b0413e;background:color-mix(in srgb,#b0413e 12%,transparent);}
.chl-line2{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums;}
.chl-line2 .chl-h{font-family:var(--font-mono,ui-monospace,monospace);font-size:10.5px;}
.chl-line2 .chl-offtag{color:#b0413e;font-weight:650;}
.chl-row.is-off .chl-logo,.chl-row.is-off .chl-name{opacity:.45;filter:grayscale(1);}
.chl-sw{justify-self:center;position:relative;width:var(--chl-sw-w);height:var(--chl-sw-h);padding:0;border:0;border-radius:999px;
  background:color-mix(in oklab,var(--muted) 28%,var(--surface2));cursor:pointer;transition:background .15s;}
.chl-sw::after{content:"";position:absolute;top:2px;left:2px;width:calc(var(--chl-sw-h) - 4px);height:calc(var(--chl-sw-h) - 4px);
  border-radius:50%;background:var(--surface);box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s;}
.chl-sw.on{background:var(--highlight);}
.chl-sw.on::after{transform:translateX(calc(var(--chl-sw-w) - var(--chl-sw-h)));background:#fff;}
.chl-sw:disabled{opacity:.5;cursor:default;}
.chl-sw:focus-visible{outline:2px solid var(--highlight);outline-offset:2px;}
.chl-empty{padding:.9rem .7rem;color:var(--muted);font-size:13px;text-align:center;}
`;
    const el = document.createElement('style');
    el.id = 'ys-chl-style';
    el.textContent = css;
    document.head.appendChild(el);
  }

  function _date(stamp) {
    if (!stamp) return '';
    const m = String(stamp).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return '';
    return m[1] === String(new Date().getFullYear()) ? `${m[2]}-${m[3]}` : `${m[1].slice(2)}-${m[2]}-${m[3]}`;
  }

  function _row(c) {
    const name = c.title || c.handle || c.channel_id;
    const on = isOn(c, SWITCHES[0]);
    const badge = c.min_duration === 0
      ? '<span class="chl-badge" title="길이제한 면제 — 짧은 영상도 수집">∞</span>'
      : (c.min_duration != null
        ? `<span class="chl-badge" title="최소 길이 ${Math.round(c.min_duration / 60)}분 — 그 미만은 수집 제외">${Math.round(c.min_duration / 60)}분+</span>` : '');
    const sub = [`<span class="chl-h">${esc(c.handle ? '@' + c.handle : c.channel_id)}</span>`];
    if (!on) sub.push('<span class="chl-offtag">수집 꺼짐</span>');
    sub.push(c.done ? `처리 ${c.done}편` : '처리 없음');
    const last = _date(c.last_done);
    if (last) sub.push(`최근 ${last}`);
    const q = norm([name, c.handle, c.handle ? '@' + c.handle : '', c.channel_id].join(' '));
    const sws = SWITCHES.map(sw => {
      const v = isOn(c, sw);
      return `<button type="button" class="chl-sw${v ? ' on' : ''}" role="switch" aria-checked="${v}"
        data-key="${sw.key}" title="${esc(sw.title)}" aria-label="${esc(name)} ${sw.label}"></button>`;
    }).join('');
    return `<div class="chl-row${on ? '' : ' is-off'}" data-id="${c.id}" data-q="${esc(q)}">
      <span class="chl-logo" aria-hidden="true">${esc(Array.from(name.trim())[0] || '?')}<img src="/channels/logo/${encodeURIComponent(c.channel_id)}" alt="" loading="lazy" onerror="this.remove()"></span>
      <div class="chl-meta"><div class="chl-line1"><span class="chl-name" title="${esc(name)}">${esc(name)}</span>${badge}</div>
        <div class="chl-line2">${sub.join(' · ')}</div></div>${sws}</div>`;
  }

  function _apply(host) {
    const st = host._chl;
    const terms = norm(st.input.value).split(/\s+/).filter(Boolean);
    let shown = 0;
    st.rows.querySelectorAll('.chl-row').forEach(r => {
      const hit = terms.every(t => r.dataset.q.includes(t));
      r.hidden = !hit;
      if (hit) shown++;
    });
    const empty = st.rows.querySelector('.chl-empty-q');
    if (empty) empty.hidden = shown > 0 || !terms.length;
    st.clear.hidden = !st.input.value;
    const chans = st.data.channels || [];
    st.headQ.textContent = terms.length ? `${shown}개 일치` : '';
    if (st.countEl) st.countEl.textContent = `${chans.filter(c => isOn(c, SWITCHES[0])).length} ON · 전체 ${chans.length}`;
  }

  async function _toggle(host, btn) {
    const st = host._chl;
    const row = btn.closest('.chl-row');
    const c = (st.data.channels || []).find(x => String(x.id) === row.dataset.id);
    if (!c) return;
    const sw = SWITCHES.find(s => s.key === btn.dataset.key);
    const next = !isOn(c, sw);
    btn.disabled = true;
    try {
      const d = await (await fetch('/channels/' + c.id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [sw.key]: next }),
      })).json();
      if (d.error) throw new Error(d.error);
      c[sw.key] = !!d[sw.key];
      const fresh = document.createElement('div');
      fresh.innerHTML = _row(c);
      const nrow = fresh.firstElementChild;
      nrow.hidden = row.hidden;
      row.replaceWith(nrow);
      const nbtn = nrow.querySelector(`[data-key="${sw.key}"]`);
      if (nbtn && document.activeElement === btn) nbtn.focus();
      _apply(host);
    } catch (e) {
      alert(`${sw.label} 설정 변경 실패: ${e.message}`);
      btn.disabled = false;
    }
  }

  function mount(host, data, opts = {}) {
    if (!host) return;
    _style();
    // 불러오는 중·오류 표시가 host를 덮어썼으면 다시 짓는다.
    if (!host._chl || !host.contains(host._chl.input)) {
      host.classList.add('chl');
      host.classList.toggle('chl--m', !!opts.mobile);
      host.innerHTML = `<div class="chl-top">
          <label class="chl-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
            <input type="search" placeholder="채널 검색 — 이름·@핸들" aria-label="채널 검색" autocomplete="off" spellcheck="false">
            <button type="button" class="chl-clear" aria-label="검색어 지우기" hidden>×</button></label>
          <div class="chl-head" aria-hidden="true"><span></span><span>채널 <span class="chl-head-q"></span></span>${SWITCHES.map(s => `<span>${s.label}</span>`).join('')}</div>
        </div><div class="chl-rows"></div>`;
      const st = host._chl = {
        input: host.querySelector('.chl-search input'),
        clear: host.querySelector('.chl-clear'),
        rows: host.querySelector('.chl-rows'),
        headQ: host.querySelector('.chl-head-q'),
      };
      st.input.addEventListener('input', () => _apply(host));
      st.input.addEventListener('keydown', e => {
        if (e.key === 'Escape' && st.input.value) { e.stopPropagation(); st.input.value = ''; _apply(host); }
      });
      st.clear.addEventListener('click', () => { st.input.value = ''; _apply(host); st.input.focus(); });
      st.rows.addEventListener('click', e => {
        const btn = e.target.closest('.chl-sw');
        if (btn && !btn.disabled) _toggle(host, btn);
      });
    }
    const st = host._chl;
    // 검색줄은 스크롤 상자 맨 위에 붙는다 — 상자 안쪽 여백만큼 끌어올려야 그 틈으로 줄이 비치지 않는다.
    let sc = host.parentElement;
    while (sc && sc !== document.body && !/(auto|scroll)/.test(getComputedStyle(sc).overflowY)) sc = sc.parentElement;
    if (sc) host.style.setProperty('--chl-stick', getComputedStyle(sc).paddingTop);
    st.data = data;
    st.countEl = opts.countEl || null;
    const chans = data.channels || [];
    st.rows.innerHTML = chans.length
      ? chans.map(_row).join('') + '<div class="chl-empty chl-empty-q" hidden>일치하는 채널이 없습니다.</div>'
      : '<div class="chl-empty">등록된 채널이 없습니다.</div>';
    _apply(host);
  }

  window.YS = window.YS || {};
  window.YS.channelList = { mount };
})();
