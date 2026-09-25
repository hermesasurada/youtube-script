/* ── 모델 선택기 — wm·yt 공통 렌더러 ──────────────────────────────────────
   두 저장소에 같은 파일이 있다(yt static/js/model-selector.js,
   wm static/model-selector.js). 한쪽을 고치면 다른 쪽도 맞춘다.
   전역 ModelSelector로 노출하고, yt에서는 YS.modelSelector로도 붙는다.

   state = {
     order:    [slot|null, ...]      요약 라운드로빈 순번(null = 사용 안 함)
     version:  {slot: value}         슬롯별로 고른 구체 모델
     effort:   {slot: value}         슬롯별 추론 수준
     next:     slot|null             다음 차례 — 번호 배지를 강조(모르면 null)
     versions: [{slot, value, label}]  모델 드롭다운 항목
     efforts:  [{value, label}]
     allowNone: bool                 요약 행에 '사용 안 함'을 둘지
     tag:      string                요약 머리 옆 꼬리표(기본 '라운드로빈')
     hint, status, notes: [{rule, detail}], extra: html
   }
   슬롯 모드(yt): order 대신 slots: [{model, effort}], nextIndex, limits: {min, max}.
     행마다 모델·추론을 따로 고르고(같은 계열 여러 번 가능) 행을 더하고 뺀다.
   handlers = { onModel(index, value), onEffort(slot, value),
                onSlotModel(i, v), onSlotEffort(i, v), onAddSlot(), onRemoveSlot(i) } */
(function (global) {
  const NONE = '__none__';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function option(value, label, selected) {
    return `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
  }

  function render(root, state, handlers) {
    if (!root) return;
    const versions = state.versions || [];
    const efforts = state.efforts || [];
    const levelsFor = (model, selected) => {
      const hit = versions.find(v => v.value === model);
      const allowed = hit && hit.reasoning;
      let result = allowed ? efforts.filter(e => allowed.includes(e.value)) : efforts;
      if (selected && !result.some(e => e.value === selected)) result = result.concat([{value:selected,label:selected+' (기존 설정)'}]);
      return result;
    };
    const slotRows = () => {
      const lim = state.limits || { min: 1, max: 5 };
      const list = state.slots || [];
      const rows = list.map((s, i) => {
        const isNext = i === state.nextIndex;
        const canRemove = list.length > lim.min;
        return `<div class="msel-row has-remove${isNext ? ' is-next' : ''}">`
          + `<span class="msel-step">${i + 1}</span>`
          + `<select data-msel="slot-model" data-index="${i}" aria-label="요약 ${i + 1}번 슬롯 모델">`
          + versions.map(v => option(v.value, v.label, v.value === s.model)).join('') + `</select>`
          + `<select data-msel="slot-effort" data-index="${i}" aria-label="요약 ${i + 1}번 슬롯 추론 수준">`
          + levelsFor(s.model, s.effort).map(e => option(e.value, e.label, e.value === s.effort)).join('') + `</select>`
          + `<button type="button" class="msel-remove" data-msel="slot-remove" data-index="${i}"`
          + `${canRemove ? '' : ' disabled'} aria-label="${i + 1}번 슬롯 빼기" title="슬롯 빼기">×</button></div>`;
      }).join('');
      const add = list.length < lim.max
        ? `<button type="button" class="msel-add" data-msel="slot-add">+ 슬롯 추가 `
          + `<span>${list.length}/${lim.max}</span></button>` : '';
      return rows + add;
    };
    const rows = state.slots ? slotRows() : (state.order || []).map((slot, i) => {
      const models = versions.map(v =>
        option(v.value, v.label, slot === v.slot && (state.version || {})[slot] === v.value)).join('')
        + (state.allowNone ? option(NONE, '사용 안 함', !slot) : '');
      const levels = levelsFor((state.version || {})[slot] || slot, (state.effort || {})[slot]).map(e =>
        option(e.value, e.label, slot && (state.effort || {})[slot] === e.value)).join('');
      const isNext = slot && slot === state.next;
      return `<div class="msel-row${isNext ? ' is-next' : ''}${slot ? '' : ' is-off'}">`
        + `<span class="msel-step">${i + 1}</span>`
        + `<select data-msel="model" data-index="${i}" aria-label="요약 ${i + 1}순번 모델">${models}</select>`
        + `<select data-msel="effort" data-slot="${esc(slot || '')}"${slot ? '' : ' disabled'} aria-label="요약 ${i + 1}순번 추론 수준">${levels}</select></div>`;
    }).join('');

    const notes = (state.notes || []).length
      ? `<div class="msel-notes"><div class="msel-notes-title">예외 규칙</div><ul>`
        + state.notes.map(n => `<li><b>${esc(n.rule)}</b>${esc(n.detail)}</li>`).join('')
        + `</ul></div>` : '';

    root.innerHTML = `<div class="msel">`
      + `<section class="msel-block"><div class="msel-head"><strong>요약</strong>`
      + `<span class="msel-tag">${esc(state.tag || '라운드로빈')}</span><span class="msel-status" data-msel-status>${esc(state.status || '')}</span></div>`
      + rows + (state.hint ? `<p class="msel-hint">${esc(state.hint)}</p>` : '') + `</section>`
      + (state.extra || '') + notes + `</div>`;

    if (!root._mselBound) {             // 이벤트 위임은 한 번만 건다(다시 그려도 유지)
      root._mselBound = true;
      root.addEventListener('change', e => {
        const el = e.target.closest('[data-msel]');
        if (!el || !root._mselHandlers) return;
        const h = root._mselHandlers, kind = el.dataset.msel, i = Number(el.dataset.index);
        if (kind === 'model' && h.onModel) h.onModel(i, el.value === NONE ? null : el.value);
        if (kind === 'effort' && h.onEffort) h.onEffort(el.dataset.slot, el.value);
        if (kind === 'slot-model' && h.onSlotModel) h.onSlotModel(i, el.value);
        if (kind === 'slot-effort' && h.onSlotEffort) h.onSlotEffort(i, el.value);
      });
      root.addEventListener('click', e => {
        const el = e.target.closest('button[data-msel]');
        if (!el || el.disabled || !root._mselHandlers) return;
        const h = root._mselHandlers;
        if (el.dataset.msel === 'slot-add' && h.onAddSlot) h.onAddSlot();
        if (el.dataset.msel === 'slot-remove' && h.onRemoveSlot) h.onRemoveSlot(Number(el.dataset.index));
      });
    }
    root._mselHandlers = handlers || {};
  }

  /** 요약 행에서 모델을 골랐을 때의 새 순번·버전. 같은 계열이 다른 행에 있으면 자리를 바꾼다. */
  function pick(state, index, value) {
    const order = (state.order || []).slice();
    const version = Object.assign({}, state.version || {});
    if (value == null) {                                   // 사용 안 함
      if (order.filter((s, i) => s && i !== index).length === 0) return null;   // 하나는 남긴다
      order[index] = null;
      return { order, version };
    }
    const hit = (state.versions || []).find(v => v.value === value);
    if (!hit) return null;
    const other = order.indexOf(hit.slot);
    if (other >= 0 && other !== index) {
      [order[index], order[other]] = [order[other], order[index]];
    } else {
      order[index] = hit.slot;
    }
    version[hit.slot] = value;
    return { order, version };
  }

  function setStatus(root, text) {
    const el = root && root.querySelector('[data-msel-status]');
    if (el) el.textContent = text || '';
  }

  const api = { render, pick, setStatus, NONE };
  if (global.YS) global.YS.modelSelector = api;
  global.ModelSelector = api;
})(typeof window !== 'undefined' ? window : globalThis);
