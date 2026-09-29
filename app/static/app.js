/* 會議逐字稿工具 — 前端 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  projectId: null,
  project: null,
  selectMode: false,
  picked: new Set(),
  focusSeg: null,
  projects: [],
  projTimer: null,
  dlTimer: null,
  splitSeg: null,
  reworked: new Set(),
};

/* ---------------------------------------------------------------- helpers */
const fmt = (sec) => {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
};

let toastTimer;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('err', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 5200 : 2400);
}

/* ---------------------------------------------------------------- errors */
/* 每個錯誤都完整印到瀏覽器主控台（F12 → Console）以及伺服器視窗，
   因為彈出視窗的文字沒辦法複製。 */
function logError(context, err) {
  const detail = (err && err.detail) || {};
  console.group(`%c[逐字稿工具] ${context}`, 'color:#dc2626;font-weight:bold');
  console.error(err);
  if (detail.where) console.error('端點：', detail.where);
  if (detail.traceback) console.error('伺服器 traceback：\n' + detail.traceback);
  console.groupEnd();
}

/* 顯示錯誤，並附上「複製」按鈕。 */
function showError(context, err) {
  logError(context, err);
  const detail = (err && err.detail) || {};
  const message = (err && err.message) || String(err || '未知錯誤');
  const full = [
    `${context}：${message}`,
    detail.where ? `端點：${detail.where}` : '',
    detail.traceback ? `\n${detail.traceback}` : '',
  ].filter(Boolean).join('\n');

  $('#err-title').textContent = context;
  $('#err-body').textContent = full;
  $('#err-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText(full);
      $('#err-copy').textContent = '已複製';
      setTimeout(() => { $('#err-copy').textContent = '複製錯誤訊息'; }, 1600);
    } catch {
      const range = document.createRange();
      range.selectNodeContents($('#err-body'));
      const sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(range);
      toast('已選取，請按 Ctrl+C 複製', true);
    }
  };
  if (!$('#dlg-error').open) $('#dlg-error').showModal();
}

let inFlight = 0;

function setBusy(delta) {
  inFlight = Math.max(0, inFlight + delta);
  const on = inFlight > 0;
  document.body.classList.toggle('loading', on);
  const bar = $('#topload');
  if (bar) bar.hidden = !on;
}

/* 明確等待某個動作，游標與按鈕都會反映 */
async function withBusy(el, label, fn) {
  const old = el && el.textContent;
  if (el) { el.disabled = true; el.textContent = label; }
  try {
    return await fn();
  } finally {
    if (el) { el.disabled = false; el.textContent = old; }
  }
}

async function api(url, opts = {}) {
  let res;
  setBusy(1);
  try {
    res = await fetch(url, {
      headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (netErr) {
    setBusy(-1);
    const err = new Error(`連不上伺服器（${url}）。伺服器視窗還開著嗎？`);
    err.detail = { where: url, cause: String(netErr) };
    throw err;
  }

  let data;
  try {
    const isJson = (res.headers.get('content-type') || '').includes('json');
    data = isJson ? await res.json() : await res.text();
  } finally {
    setBusy(-1);
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status} ${res.statusText}`);
    err.status = res.status;
    err.detail = typeof data === 'object' ? { where: `${opts.method || 'GET'} ${url}`, ...data }
                                          : { where: `${opts.method || 'GET'} ${url}`, body: data };
    throw err;
  }
  return data;
}

/* --------------------------------------------------------- project list */
async function loadProjects() {
  let payload;
  try {
    payload = await api('/api/projects');
  } catch (err) {
    logError('讀取專案清單失敗', err);
    return 0;
  }
  const list = payload.items || [];
  state.projects = list;

  const box = $('#project-list');
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<p class="hint" style="padding:0 6px">尚無紀錄</p>';
  }
  for (const p of list) {
    box.appendChild(projectRow(p));
  }
  $('#queue-badge').textContent = payload.active ? `${payload.active} 個進行中` : '';
  $('#queue-badge').hidden = !payload.active;
  return payload.active || 0;
}

/* 側欄的一列：每個專案都有自己的狀態與進度 */
function projectRow(p) {
  const style = STATUS_STYLE[isStalled(p) ? 'stalled' : p.status]
             || { label: p.status, cls: '' };
  const busy = isBusy(p);
  const pct = Math.round(((p.job && p.job.progress) || 0) * 100);

  const el = document.createElement('div');
  el.className = 'pitem' + (p.id === state.projectId ? ' active' : '') + (busy ? ' busy' : '');
  el.innerHTML = `
    <div class="pitem-top">
      <div class="pitem-name"></div>
      <span class="pill ${style.cls}">${style.label}</span>
    </div>
    <div class="pitem-meta">
      <span>${p.duration ? fmt(p.duration) : '—'}</span>
      ${p.num_segments ? `<span>${p.num_segments} 句</span>` : ''}
      ${p.speakers.length ? `<span>${p.speakers.length} 人</span>` : ''}
    </div>
    ${busy ? `<div class="bar thin"><div class="bar-fill" style="width:${pct}%"></div></div>
              <div class="pitem-stage"></div>` : ''}`;
  $('.pitem-name', el).textContent = p.name;
  if (busy) {
    const job = p.job || {};
    $('.pitem-stage', el).textContent =
      job.state === 'queued' && job.position > 0
        ? `排隊中（前面 ${job.position} 個）`
        : `${job.stage_label || '準備中'} ${pct}%` + (job.eta ? `　剩約 ${fmt(job.eta)}` : '');
  }
  el.onclick = () => openProject(p.id);
  return el;
}

/* ------------------------------------------------------------- rendering */
function speakerOf(id) {
  return (state.project.speakers || []).find((s) => s.id === id)
      || { id, name: id, color: '#888' };
}

function renderSpeakers() {
  const box = $('#speakers');
  box.innerHTML = '';
  const counts = {};
  for (const seg of state.project.segments) counts[seg.speaker] = (counts[seg.speaker] || 0) + 1;

  for (const spk of state.project.speakers) {
    const el = document.createElement('div');
    el.className = 'spk';
    el.innerHTML = `
      <span class="spk-swatch" style="background:${spk.color}" title="換顏色"></span>
      <input class="spk-name" value="" spellcheck="false" title="改名字">
      <span class="spk-count">${counts[spk.id] || 0} 句</span>
      <button class="spk-del" title="刪除講者">×</button>`;

    const input = $('.spk-name', el);
    input.value = spk.name;
    input.onkeydown = (e) => { if (e.key === 'Enter') input.blur(); };
    input.onblur = async () => {
      const name = input.value.trim();
      if (!name || name === spk.name) { input.value = spk.name; return; }
      await mutate(`/api/projects/${state.projectId}/speakers/${spk.id}`,
                   { method: 'PATCH', body: JSON.stringify({ name }) });
      toast(`已改名為 ${name}`);
    };

    $('.spk-swatch', el).onclick = async () => {
      const palette = ['#2563eb','#db2777','#059669','#d97706','#7c3aed',
                       '#0891b2','#dc2626','#65a30d','#c026d3','#0284c7'];
      const next = palette[(palette.indexOf(spk.color) + 1) % palette.length];
      await mutate(`/api/projects/${state.projectId}/speakers/${spk.id}`,
                   { method: 'PATCH', body: JSON.stringify({ color: next }) });
    };

    $('.spk-del', el).onclick = async () => {
      if (state.project.speakers.length <= 1) return toast('至少要有一位講者', true);
      const others = state.project.speakers.filter((s) => s.id !== spk.id);
      if (!confirm(`刪除「${spk.name}」？他的 ${counts[spk.id] || 0} 句話會改成「${others[0].name}」。`)) return;
      await mutate(`/api/projects/${state.projectId}/speakers/${spk.id}?reassign_to=${others[0].id}`,
                   { method: 'DELETE' });
    };
    box.appendChild(el);
  }

  const add = document.createElement('button');
  add.className = 'spk-add';
  add.textContent = '＋ 新增講者';
  add.onclick = async () => {
    await mutate(`/api/projects/${state.projectId}/speakers`,
                 { method: 'POST', body: JSON.stringify({ name: '' }) });
  };
  box.appendChild(add);
}

function renderTranscript() {
  const box = $('#transcript');
  box.scrollTop;
  box.innerHTML = '';
  const speakerIndex = Object.fromEntries(state.project.speakers.map((s, i) => [s.id, i]));

  for (const group of state.project.bubbles || []) {
    const spk = speakerOf(group.speaker);
    const side = (speakerIndex[group.speaker] || 0) % 2 === 0 ? 'left' : 'right';

    const row = document.createElement('div');
    row.className = `bubble-row ${side}`;
    row.innerHTML = `
      <div class="avatar" style="background:${spk.color}"></div>
      <div class="bubble">
        <div class="bubble-head">
          <span class="bubble-who" style="color:${spk.color}"></span>
          <span class="bubble-time">${fmt(group.start)} – ${fmt(group.end)}</span>
        </div>
        <div class="bubble-body"></div>
      </div>`;
    $('.avatar', row).textContent = (spk.name || '?').slice(0, 2);
    $('.bubble-who', row).textContent = spk.name;

    const body = $('.bubble-body', row);
    for (const seg of group.segments) body.appendChild(renderSentence(seg));
    box.appendChild(row);
  }

  if (!(state.project.bubbles || []).length) {
    box.innerHTML = '<p class="hint">這段錄音沒有辨識到語音內容。</p>';
  }
}

function renderSentence(seg) {
  const el = document.createElement('div');
  el.className = 'sent';
  el.dataset.id = seg.id;
  el.dataset.start = seg.start;
  el.dataset.end = seg.end;
  if (seg.edited) el.classList.add('edited');
  if (seg.speaker_edited) el.classList.add('moved');
  if (state.reworked && state.reworked.has(seg.id)) el.classList.add('reworked');
  if (state.picked.has(seg.id)) el.classList.add('picked');

  el.innerHTML = `
    <input type="checkbox" class="sent-check">
    <button class="icon-btn sent-play" title="只播這一句，播完就停（校對講者時最好用）">▶</button>
    <span class="sent-time" title="從這裡接著往下播">${fmt(seg.start)}</span>
    <div class="sent-text" contenteditable="plaintext-only" spellcheck="false"></div>
    <select class="sent-spk" title="這句是誰說的"></select>
    <div class="sent-tools">
      <button class="icon-btn" data-act="rerun" title="重新辨識這句（多人講話時可自動拆開）">🔁</button>
      <button class="icon-btn" data-act="split"  title="從游標處切成兩句">✂</button>
      <button class="icon-btn" data-act="merge"  title="與下一句合併">⭳</button>
      <button class="icon-btn warn" data-act="del" title="刪除這句">🗑</button>
    </div>`;

  const text = $('.sent-text', el);
  text.textContent = seg.text;

  const sel = $('.sent-spk', el);
  for (const s of state.project.speakers) {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    if (s.id === seg.speaker) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.style.borderColor = speakerOf(seg.speaker).color;

  /* ── 這句改成誰說的：本工具的核心動作 ── */
  sel.onchange = async () => {
    await mutate(`/api/projects/${state.projectId}/segments/${seg.id}`,
                 { method: 'PATCH', body: JSON.stringify({ speaker: sel.value }) });
    toast(`這句改成「${speakerOf(sel.value).name}」`);
  };

  text.onfocus = () => { state.focusSeg = seg.id; el.classList.add('focus'); };
  text.onblur = async () => {
    el.classList.remove('focus');
    const val = text.textContent.trim();
    if (val === seg.text) return;
    await mutate(`/api/projects/${state.projectId}/segments/${seg.id}`,
                 { method: 'PATCH', body: JSON.stringify({ text: val }) }, true);
  };
  text.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); text.blur(); }
  };

  // Two different jobs, so two different controls. The button plays exactly
  // this sentence and stops - which is the loop you are in when checking who
  // said what. The timestamp seeks here and keeps going, for reading along.
  $('.sent-play', el).onclick = (e) => {
    e.stopPropagation();
    const audio = $('#audio');
    const playingThis = !audio.paused
      && audio.currentTime >= seg.start && audio.currentTime < seg.end;
    if (playingThis) { audio.pause(); return; }
    playFrom(seg.start, seg.end);
  };

  $('.sent-time', el).onclick = () => playFrom(seg.start, null);

  $('.sent-check', el).onchange = (e) => {
    if (e.target.checked) state.picked.add(seg.id); else state.picked.delete(seg.id);
    el.classList.toggle('picked', e.target.checked);
    updateBulkBar();
  };
  $('.sent-check', el).checked = state.picked.has(seg.id);

  el.onclick = (e) => {
    if (!state.selectMode) return;
    if (e.target.closest('.sent-text, .sent-spk, .sent-tools, .sent-check')) return;
    const cb = $('.sent-check', el);
    cb.checked = !cb.checked;
    cb.dispatchEvent(new Event('change'));
  };

  // Scoped to .sent-tools on purpose: the play button is an .icon-btn too,
  // and a bare '.icon-btn' would bind this handler over its own.
  $$('.sent-tools .icon-btn', el).forEach((btn) => {
    btn.onclick = async () => {
      const act = btn.dataset.act;
      if (act === 'rerun') {
        openSplitDialog(seg);
      } else if (act === 'split') {
        const at = caretOffset(text);
        if (at <= 0 || at >= text.textContent.length) return toast('請先把游標點在要切開的位置', true);
        await mutate(`/api/projects/${state.projectId}/segments/${seg.id}/split`,
                     { method: 'POST', body: JSON.stringify({ at }) });
      } else if (act === 'merge') {
        await mutate(`/api/projects/${state.projectId}/segments/${seg.id}/merge`, { method: 'POST' });
      } else if (act === 'del') {
        if (!confirm('刪除這一句？')) return;
        await mutate(`/api/projects/${state.projectId}/segments/${seg.id}`, { method: 'DELETE' });
      }
    };
  });

  return el;
}

function caretOffset(node) {
  const sel = window.getSelection();
  if (!sel.rangeCount) return -1;
  const range = sel.getRangeAt(0);
  if (!node.contains(range.startContainer)) return -1;
  const pre = range.cloneRange();
  pre.selectNodeContents(node);
  pre.setEnd(range.startContainer, range.startOffset);
  return pre.toString().length;
}

/* ------------------------------------------------------------- mutations */
async function mutate(url, opts, keepScroll = false) {
  const y = window.scrollY;
  try {
    const proj = await api(url, opts);
    state.project = proj;
    renderSpeakers();
    renderTranscript();
    updateMeta();
    updateBulkBar();
    if (keepScroll) window.scrollTo(0, y);
    loadProjects();
  } catch (err) {
    showError('儲存變更失敗', err);
  }
}

/* ---------------------------------------------------------- bulk actions */
function updateBulkBar() {
  const bar = $('#bulkbar');
  bar.hidden = !state.selectMode;
  if (!state.selectMode) return;
  $('#bulk-count').textContent = `已選 ${state.picked.size} 句`;

  const box = $('#bulk-speakers');
  box.innerHTML = '';
  for (const s of state.project.speakers) {
    const b = document.createElement('button');
    b.className = 'btn sm';
    b.textContent = s.name;
    b.style.borderColor = s.color;
    b.onclick = async () => {
      if (!state.picked.size) return toast('請先勾選句子', true);
      const ids = [...state.picked];
      await mutate(`/api/projects/${state.projectId}/segments/bulk-speaker`, {
        method: 'POST',
        body: JSON.stringify({ segment_ids: ids, speaker: s.id }),
      });
      toast(`${ids.length} 句改成「${s.name}」`);
      state.picked.clear();
      updateBulkBar();
      renderTranscript();
    };
    box.appendChild(b);
  }
}

/* ------------------------------------------------------------ open/close */
async function openProject(id) {
  if (state.projectId !== id) {
    $('#workspace').classList.add('switching');
    $('#transcript').innerHTML = '<p class="loading-line"><span class="spinner"></span>載入中…</p>';
  }
  try {
    const proj = await api(`/api/projects/${id}`);
    state.projectId = id;
    state.project = proj;
    state.picked.clear();

    $('#empty-state').hidden = true;
    $('#workspace').hidden = false;
    $('#project-name').value = proj.name;

    const meta = { ...proj, job: proj.job, num_segments: (proj.segments || []).length };
    renderProjectStatus(meta);

    const done = proj.status === 'ready' && (proj.segments || []).length > 0;
    $('#player').hidden = !done;
    $('#speakers').hidden = !done;
    $('#btn-export').disabled = !done;
    $('#btn-select-mode').disabled = !done;
    $('#btn-rerun').disabled = isBusy(meta);
    state.reworked = new Set();

    if (done) {
      renderSpeakers();
      renderTranscript();
      const audio = $('#audio');
      audio.src = `/api/projects/${id}/audio?t=${Date.now()}`;
      audio.load();
    } else {
      $('#transcript').innerHTML = isBusy(meta)
        ? '<p class="hint">辨識完成後，逐字稿會出現在這裡。你可以先去處理別的專案。</p>'
        : '<p class="hint">這個專案還沒有逐字稿。</p>';
    }
    updateMeta();
    updateBulkBar();
    updateTidyBadge();

    loadProjects();
    if (isBusy(meta)) startProjectPolling();
    window.scrollTo(0, 0);
  } catch (err) {
    $('#transcript').innerHTML = '<p class="hint">載入失敗，詳見錯誤視窗。</p>';
    showError('開啟專案失敗', err);
  } finally {
    $('#workspace').classList.remove('switching');
  }
}

function updateMeta() {
  const p = state.project;
  const models = window.__MODELS__ || [];
  const label = (models.find((m) => m.key === p.model) || {}).label || p.model;
  const bits = [p.duration ? fmt(p.duration) : '長度未知'];
  if ((p.segments || []).length) bits.push(`${p.segments.length} 句`);
  if ((p.speakers || []).length) bits.push(`${p.speakers.length} 位講者`);
  bits.push(label);
  $('#ws-meta').textContent = bits.join(' · ');
  $('#time-total').textContent = fmt(p.duration);
}

/* A transcript is only correct for the rules that existed when it ran. The
   server counts what the current rules would still change, so an older
   project can say so instead of quietly staying wrong. */
function updateTidyBadge() {
  const btn = $('#btn-tidy');
  if (!btn) return;
  const n = state.project?.pending?.total || 0;
  btn.textContent = n ? `整理逐字稿 ${n}` : '整理逐字稿';
  btn.classList.toggle('attn', n > 0);
  btn.title = n
    ? `有 ${n} 句可以依目前的詞語修正、標點與斷句規則整理`
    : '用目前的詞語修正、標點與斷句規則整理一次，不用重跑辨識';
}

/* ---------------------------------------------------------------- player */
let stopAt = null;

function playFrom(start, end) {
  const audio = $('#audio');
  audio.currentTime = start;
  stopAt = end ?? null;
  audio.play().catch(() => {});
}

function initPlayer() {
  const audio = $('#audio');
  const seek = $('#seek');

  $('#play-toggle').onclick = () => (audio.paused ? audio.play() : audio.pause());
  audio.onplay  = () => { $('#play-toggle').textContent = '⏸'; };
  audio.onpause = () => {
    $('#play-toggle').textContent = '▶';
    for (const b of $$('.sent-play')) b.textContent = '▶';
  };

  audio.ontimeupdate = () => {
    const t = audio.currentTime;
    const d = audio.duration || state.project?.duration || 1;
    $('#time-now').textContent = fmt(t);
    if (!seek.dragging) seek.value = Math.round((t / d) * 1000);

    if (stopAt !== null && t >= stopAt) { audio.pause(); stopAt = null; }

    let active = null;
    for (const el of $$('.sent')) {
      const on = t >= +el.dataset.start && t < +el.dataset.end;
      el.classList.toggle('playing', on);
      const btn = el.querySelector('.sent-play');
      if (btn) btn.textContent = on && !audio.paused ? '⏸' : '▶';
      if (on) active = el;
    }
    if (active && $('#follow').checked) {
      const r = active.getBoundingClientRect();
      if (r.top < 90 || r.bottom > window.innerHeight - 120) {
        active.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  };

  seek.oninput = () => {
    seek.dragging = true;
    const d = audio.duration || state.project?.duration || 1;
    audio.currentTime = (seek.value / 1000) * d;
    stopAt = null;
  };
  seek.onchange = () => { seek.dragging = false; };
  $('#rate').onchange = (e) => { audio.playbackRate = +e.target.value; };

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea, [contenteditable]')) return;
    if (e.key === ' ') { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
    if (e.key === 'ArrowLeft')  { audio.currentTime -= e.shiftKey ? 10 : 3; stopAt = null; }
    if (e.key === 'ArrowRight') { audio.currentTime += e.shiftKey ? 10 : 3; stopAt = null; }
  });
}

/* ------------------------------------------------------- new meeting (queue) */
function initNewDialog() {
  const dlg = $('#dlg-new');

  const open = async (ev) => {
    const btn = ev && ev.currentTarget;
    await withBusy(btn, '載入中…', async () => {
      await refreshModelOptions();   // await: a late refresh would reset the picker
      updateLanguageField();
    });
    dlg.showModal();
  };
  $('#btn-new').onclick = open;
  $('#btn-new-2').onclick = open;
  $('#new-cancel').onclick = () => dlg.close();
  $('#f-model').onchange = () => { updateLanguageField(); updateNewHint(); };
  $('#f-threads').onchange = () => updateThreadNote();

  /* 多檔案：一次選好幾個錄音，全部排進佇列。 */
  $('#new-start').onclick = async () => {
    const files = [...$('#f-audio').files];
    if (!files.length) return toast('請先選擇音檔', true);

    const shared = {
      model: $('#f-model').value,
      num_speakers: $('#f-speakers').value,
      zh_mode: $('#f-zh').value,
      threads: $('#f-threads').value,
    };
    const nameOverride = $('#f-name').value.trim();
    dlg.close();

    let first = null;
    for (const [i, file] of files.entries()) {
      const fd = new FormData();
      fd.append('audio', file);
      // A custom name only makes sense for a single file; otherwise use filenames.
      fd.append('name', files.length === 1 ? nameOverride : '');
      fd.append('language', $('#f-language').value || '');
      for (const [k, v] of Object.entries(shared)) fd.append(k, v);
      try {
        const res = await api('/api/projects', { method: 'POST', body: fd });
        if (i === 0) first = res.project;
      } catch (err) {
        showError(`「${file.name}」上傳失敗`, err);
      }
    }

    $('#f-audio').value = '';
    $('#f-name').value = '';
    await loadProjects();
    if (first) openProject(first);
    toast(files.length > 1 ? `已排入 ${files.length} 個錄音` : '已開始辨識');
    startProjectPolling();
  };
}

/* 有些模型需要指定語言（Cohere 沒指定就會整份空白），有些完全不吃這個參數。 */
function updateLanguageField() {
  const info = (window.__MODELS__ || []).find((m) => m.key === $('#f-model').value);
  const field = $('#lang-field');
  const sel = $('#f-language');
  const opts = (info && info.language_options) || [];

  if (!opts.length) { field.hidden = true; sel.innerHTML = ''; return; }
  field.hidden = false;

  const keep = sel.value;
  sel.innerHTML = '';
  for (const o of opts) {
    const el = document.createElement('option');
    el.value = o.code;
    el.textContent = o.label;
    sel.appendChild(el);
  }
  sel.value = opts.some((o) => o.code === keep) ? keep : (info.default_language || opts[0].code);
  $('#lang-note').textContent = info.language_required
    ? '這個模型必須指定語言，選錯會辨識不出內容。'
    : '留「自動偵測」通常就可以。';
}

/* 執行緒開太多反而變慢：句子很短，ONNX 的 intra-op 平行化吃不滿，
   實測 20 執行緒比 6 慢。真正的加速來自同時處理多個專案。 */
function updateThreadNote() {
  const cap = window.__CAP__;
  const note = $('#thread-note');
  if (!cap || !note) return;
  const n = +$('#f-threads').value;
  if (n > cap.default_threads * 1.5) {
    note.textContent = `⚠ ${n} 執行緒通常不會更快（實測 20 比 6 慢）。`
                     + `建議維持 ${cap.default_threads}，想更快就同時多跑幾個專案。`;
    note.className = 'warn-note';
  } else {
    note.textContent = `你的 CPU 有 ${cap.cpu_count} 核心，最多同時處理 ${cap.max_jobs} 個專案`
                     + `（${cap.max_jobs} × ${cap.default_threads} ≈ ${cap.cores_in_use} 核心）。`;
    note.className = '';
  }
}

function updateNewHint() {
  const model = $('#f-model').value;
  const info = (window.__MODELS__ || []).find((m) => m.key === model);
  const hint = $('#new-hint');
  if (!info) return;
  if (info.downloaded) {
    hint.textContent = '知道人數就直接選，分辨講者會準很多。可以一次選多個檔案。';
  } else if (info.busy) {
    const d = info.download || {};
    hint.textContent = `這個模型${d.state_label || '下載中'}（${Math.round((d.progress || 0) * 100)}%）。`
                     + '可以直接開始，辨識會等它下載完再跑。';
  } else {
    hint.textContent = `這個模型尚未下載（${info.size_mb} MB）。可以直接開始，`
                     + '系統會自動下載後再辨識。';
  }
}

/* -------------------------------------------------------- project polling */
/* 只要還有專案在跑就持續輪詢；全部完成就停下來。 */
function startProjectPolling() {
  if (state.projTimer) return;
  state.projTimer = setInterval(async () => {
    const active = await loadProjects();
    if (state.projectId) {
      const meta = (state.projects || []).find((p) => p.id === state.projectId);
      if (meta) renderProjectStatus(meta);
      // 這個專案剛跑完 -> 載入結果
      if (meta && meta.status === 'ready' && state.project
          && (state.project.segments || []).length === 0) {
        openProject(state.projectId);
      }
    }
    if (!active) stopProjectPolling();
  }, 1000);
}

function stopProjectPolling() {
  clearInterval(state.projTimer);
  state.projTimer = null;
}

/* 完整流程，讓使用者看得到自己在第幾步 */
const STEPS = [
  { key: 'wait_model', label: '等模型' },
  { key: 'decode',  label: '讀音檔' },
  { key: 'load',    label: '載入模型' },
  { key: 'diarize', label: '分辨講者' },
  { key: 'vad',     label: '切句' },
  { key: 'asr',     label: '辨識文字' },
  { key: 'save',    label: '儲存' },
];

const STATUS_STYLE = {
  queued:      { label: '排隊中',  cls: 'wait' },
  processing:  { label: '辨識中',  cls: 'run' },
  stalled:     { label: '已中斷',  cls: 'err' },
  ready:       { label: '完成',    cls: 'ok' },
  error:       { label: '失敗',    cls: 'err' },
  cancelled:   { label: '已取消',  cls: 'off' },
  interrupted: { label: '中斷',    cls: 'err' },
};

/* 真正在跑 = 狀態是進行中，而且伺服器上確實有一個工作在背後。
   只有狀態沒有工作，代表那個工作已經不在了（例如伺服器重開過）。 */
function isBusy(meta) {
  const active = meta.job && (meta.job.state === 'queued' || meta.job.state === 'running');
  return (meta.status === 'queued' || meta.status === 'processing') && !!active;
}

function isStalled(meta) {
  return (meta.status === 'queued' || meta.status === 'processing') && !isBusy(meta);
}

/* 專案工作區上方的狀態面板（取代原本的彈出視窗）。 */
function renderProjectStatus(meta) {
  const panel = $('#proj-status');
  const job = meta.job;
  const busy = isBusy(meta);
  const stalled = isStalled(meta);
  const failed = meta.status === 'error' || meta.status === 'interrupted' || stalled;
  const cancelled = meta.status === 'cancelled';

  if (!busy && !failed && !cancelled) { panel.hidden = true; return; }
  panel.hidden = false;
  panel.className = 'proj-status ' + (STATUS_STYLE[meta.status] || {}).cls;

  if (busy) {
    const pct = Math.round(((job && job.progress) || 0) * 100);
    const stage = (job && job.stage_label) || '排隊中';
    const detail = (job && job.detail) || '';
    const queued = job && job.state === 'queued' && job.position > 0;
    panel.innerHTML = `
      <div class="ps-row">
        <span class="spinner"></span>
        <b class="ps-stage"></b>
        <span class="ps-detail"></span>
        <button class="btn ghost sm" id="ps-cancel">取消</button>
      </div>
      <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="ps-foot">
        <span class="ps-steps"></span>
        <span class="ps-times"></span>
      </div>`;
    $('.ps-stage', panel).textContent =
      queued ? `排隊中（前面還有 ${job.position} 個）` : `${stage} ${pct}%`;
    $('.ps-detail', panel).textContent = detail;

    // 每一步都列出來並標記目前在哪一步，這樣一眼就知道進行到哪裡
    $('.ps-steps', panel).innerHTML = queued ? ''
      : STEPS.map((st) => {
          const cur = st.key === job.stage;
          const past = STEPS.findIndex((x) => x.key === job.stage) > STEPS.indexOf(st);
          return `<span class="step ${cur ? 'now' : past ? 'past' : ''}">${st.label}</span>`;
        }).join('<span class="step-sep">›</span>');

    const bits = [];
    if (job && job.audio_seconds) bits.push(`錄音 ${fmt(job.audio_seconds)}`);
    if (job && job.elapsed) bits.push(`已跑 ${fmt(job.elapsed)}`);
    bits.push(job && job.eta ? `剩約 ${fmt(job.eta)}` : (queued ? '' : '剩餘時間估算中…'));
    $('.ps-times', panel).textContent = bits.filter(Boolean).join('　·　');
    $('#ps-cancel', panel).onclick = async () => {
      try { await api(`/api/projects/${meta.id}/cancel`, { method: 'POST' }); }
      catch (err) { showError('取消失敗', err); }
      loadProjects();
    };
  } else {
    const msg = stalled
      ? '這個專案的辨識工作已經不在執行了（多半是伺服器重新啟動過）。按「重新辨識」即可從頭跑一次。'
      : failed ? (meta.error || (job && job.error) || '辨識失敗')
               : '這個專案的辨識已取消。';
    panel.innerHTML = `
      <div class="ps-row">
        <b class="ps-stage">${stalled ? '辨識已中斷' : failed ? '辨識失敗' : '已取消'}</b>
        <button class="btn ghost sm" id="ps-retry">重新辨識</button>
        <button class="btn ghost sm" id="ps-detail-btn">查看詳細</button>
      </div>
      <div class="ps-msg"></div>`;
    $('.ps-msg', panel).textContent = msg;
    $('#ps-retry', panel).onclick = async () => {
      try {
        await api(`/api/projects/${meta.id}/retry`, { method: 'POST' });
        toast('已重新排入佇列');
        loadProjects();
        startProjectPolling();
      } catch (err) { showError('無法重新辨識', err); }
    };
    $('#ps-detail-btn', panel).onclick = () => showError('辨識失敗', { message: msg });
  }
}

/* --------------------------------------------------------------- models */
async function refreshModelOptions() {
  let models;
  try {
    const res = await api('/api/models');
    models = res.items;
    window.__LB__ = res.leaderboard;
  } catch (err) {
    logError('讀取模型清單失敗', err);
    return window.__MODELS__ || [];
  }
  window.__MODELS__ = models;
  // The catalogue also carries add-ons (the punctuation model). They download
  // through the same list but cannot transcribe, so they never reach a picker.
  models = models.filter((m) => (m.role || 'asr') === 'asr');

  const sel = $('#f-model');
  const keep = sel.value;
  sel.innerHTML = '';
  for (const m of models) {
    const opt = document.createElement('option');
    let suffix = '';
    if (m.busy) suffix = `　— ${m.download.state_label} ${Math.round(m.download.progress * 100)}%`;
    else if (!m.downloaded && m.files_ready) suffix = `　— 不完整 ${m.files_ready}/${m.files_total}`;
    else if (!m.downloaded) suffix = `　— 需下載 ${m.size_mb} MB`;
    opt.value = m.key;
    opt.textContent = m.label + suffix;
    sel.appendChild(opt);
  }
  // Restore the user's pick; only fall back when it no longer exists.
  sel.value = models.some((m) => m.key === keep) ? keep
            : (models.find((m) => m.downloaded) || models[0]).key;
  updateLanguageField();
  updateNewHint();
  return models;
}

/* 下載佇列：伺服器是唯一真相來源。畫面重繪不會弄丟進度，
   重複點下載也只會得到同一個工作。 */
async function renderModelList() {
  let models;
  try {
    const res = await api('/api/models');
    models = res.items;
    window.__LB__ = res.leaderboard;
  } catch (err) {
    $('#model-list').innerHTML = '<p class="hint">讀取模型清單失敗，詳見主控台。</p>';
    logError('讀取模型清單失敗', err);
    return;
  }
  window.__MODELS__ = models;

  renderLeaderboardSource();
  const box = $('#model-list');
  box.innerHTML = '';
  for (const m of models) box.appendChild(modelCard(m));
  syncDownloadPolling();
}

/* 明講數字是哪來的、什麼時候抓的 */
function renderLeaderboardSource() {
  const lb = window.__LB__ || {};
  const el = $('#lb-source');
  if (!lb.available) {
    el.innerHTML = '排名資料取得失敗（離線時只顯示本機資訊）。'
                 + ' <button class="linkish" id="lb-refresh">重新取得</button>';
  } else {
    const age = lb.age_hours < 1 ? '剛剛' : `${Math.round(lb.age_hours)} 小時前`;
    el.innerHTML = `排名與 WER 取自 <a href="${lb.source}" target="_blank" rel="noopener">`
      + `Hugging Face Open ASR Leaderboard</a>（${lb.board}，共 ${lb.total} 個模型，`
      + `${age}更新）。 <button class="linkish" id="lb-refresh">立即更新</button>`;
  }
  const btn = $('#lb-refresh');
  if (btn) btn.onclick = async () => {
    btn.disabled = true; btn.textContent = '更新中…';
    try { await api('/api/leaderboard/refresh', { method: 'POST' }); }
    catch (err) { showError('無法更新排行榜', err); }
    renderModelList();
  };
}

const nfmt = (n) => (n === null || n === undefined) ? '—' : n.toLocaleString('en-US');

function daysAgo(iso) {
  if (!iso) return '';
  const d = (Date.now() - new Date(iso).getTime()) / 86400000;
  if (d < 1) return '今天更新';
  if (d < 30) return `${Math.round(d)} 天前更新`;
  if (d < 365) return `${Math.round(d / 30)} 個月前更新`;
  return `${(d / 365).toFixed(1)} 年前更新`;
}

function modelCard(m) {
  const d = m.download;
  const lb = m.leaderboard;
  const up = m.hf_upstream_stats;
  const lic = (up && up.license) || (m.hf_repo_stats && m.hf_repo_stats.license) || '';
  const busy = m.busy;
  const partial = !m.downloaded && !busy && m.files_ready > 0;

  const card = document.createElement('div');
  card.className = 'mcard' + (m.downloaded ? ' ready' : '') + (busy ? ' busy' : '');
  card.dataset.key = m.key;
  card.innerHTML = `
    <div class="mcard-main">
      <div class="mcard-title">
        <span class="mcard-label"></span>
        ${lb ? `<span class="tag rank">Open ASR #${lb.rank}</span>` : ''}
        ${m.downloaded ? '<span class="tag ok">可用</span>' : ''}
        ${partial ? '<span class="tag warn">不完整</span>' : ''}
        ${busy ? `<span class="tag busy">${d.state_label}</span>` : ''}
      </div>
      <p class="mcard-note"></p>
      ${lb ? `
        <div class="mcard-lb">
          <span><b>WER ${lb.wer}%</b></span>
          ${lb.rtfx ? `<span>RTFx ${nfmt(Math.round(lb.rtfx))}</span>` : ''}
          <span>總榜 #${lb.rank}${lb.open_rank ? `　開源第 ${lb.open_rank}/${lb.open_total}` : ''}</span>
        </div>` : ''}
      <div class="mcard-meta">
        <span>語言：${m.languages}</span>
        ${m.arch ? `<span>${m.arch}</span>` : ''}
        <span>${m.size_mb} MB</span>
        <span>${m.files_ready}/${m.files_total} 個檔案</span>
        ${lic ? `<span>授權 ${lic}</span>` : ''}
      </div>
      <div class="mcard-meta">
        ${m.repo ? `<span>🤗 <a href="https://huggingface.co/${m.repo}" target="_blank" rel="noopener">${m.repo}</a></span>`
                 : '<span>本機內建</span>'}
        ${up ? `<span>上游 <a href="${up.url}" target="_blank" rel="noopener">${up.id}</a>
                 ・${nfmt(up.downloads)} 次下載・${up.likes} 讚・${daysAgo(up.last_modified)}</span>` : ''}
      </div>
      ${busy ? `
        <div class="mini-bar"><div class="mini-fill" style="width:${Math.round(d.progress * 100)}%"></div></div>
        <div class="mini-text">${d.state === 'queued'
            ? `排隊中，前面還有 ${d.position} 個`
            : `${Math.round(d.progress * 100)}%　${d.detail || ''}${d.eta ? `　剩約 ${fmt(d.eta)}` : ''}`}</div>` : ''}
      ${partial ? `<div class="mini-text warn-text">缺少：${m.missing.slice(0, 3).join('、')}${m.missing.length > 3 ? ' …' : ''}</div>` : ''}
      ${d && d.state === 'error' ? '<div class="mini-text err-text"></div>' : ''}
    </div>
    <div class="mcard-action"></div>`;

  $('.mcard-label', card).textContent = m.label;
  $('.mcard-note', card).textContent = m.note;
  if (d && d.state === 'error') $('.err-text', card).textContent = d.error || '下載失敗';

  const action = $('.mcard-action', card);
  if (busy) {
    const cancel = document.createElement('button');
    cancel.className = 'btn ghost sm';
    cancel.textContent = '取消';
    cancel.onclick = async () => {
      cancel.disabled = true;
      try { await api(`/api/models/${m.key}/download`, { method: 'DELETE' }); }
      catch (err) { showError('取消下載失敗', err); }
      renderModelList();
    };
    action.appendChild(cancel);
  } else if (m.downloaded) {
    if (!m.builtin) {
      const del = document.createElement('button');
      del.className = 'btn ghost sm';
      del.textContent = '刪除';
      del.onclick = async () => {
        if (!confirm(`刪除「${m.label}」的模型檔案？`)) return;
        try { await api(`/api/models/${m.key}`, { method: 'DELETE' }); }
        catch (err) { showError('刪除失敗', err); }
        renderModelList();
        refreshModelOptions();
      };
      action.appendChild(del);
    }
  } else {
    const btn = document.createElement('button');
    btn.className = 'btn primary sm';
    btn.textContent = partial ? '補齊下載' : '下載';
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = '排入佇列…';
      try { await api(`/api/models/${m.key}/download`, { method: 'POST' }); }
      catch (err) { showError('無法開始下載', err); }
      renderModelList();
    };
    action.appendChild(btn);
  }
  return card;
}

/* 只要還有下載在進行就持續輪詢；全部結束就停下來。 */
function syncDownloadPolling() {
  const anyBusy = (window.__MODELS__ || []).some((m) => m.busy);
  if (anyBusy && !state.dlTimer) {
    state.dlTimer = setInterval(() => {
      if ($('#dlg-models').open) renderModelList();
    }, 900);
  } else if (!anyBusy && state.dlTimer) {
    clearInterval(state.dlTimer);
    state.dlTimer = null;
    refreshModelOptions();
  }
}

/* --------------------------------------------------- 重新辨識（整份 / 單句） */
function initRerun() {
  const dlg = $('#dlg-rerun');

  $('#btn-rerun').onclick = async () => {
    if (!state.project) return;
    const models = await refreshModelOptions();
    const cur = state.project.options || {};

    const sel = $('#r-model');
    sel.innerHTML = '';
    for (const m of models) {
      const o = document.createElement('option');
      o.value = m.key;
      o.textContent = m.label + (m.downloaded ? '' : `　— 需下載 ${m.size_mb} MB`);
      sel.appendChild(o);
    }
    sel.value = cur.model || state.project.model || models[0].key;
    $('#r-speakers').value = String(cur.num_speakers ?? -1);
    $('#r-zh').value = cur.zh_mode || state.project.zh_mode || 's2twp';
    updateRerunLanguage();
    dlg.showModal();
  };

  $('#r-model').onchange = () => updateRerunLanguage();
  $('#r-cancel').onclick = () => dlg.close();

  $('#r-start').onclick = async () => {
    const n = (state.project.segments || []).length;
    if (!confirm(`確定要清除目前的 ${n} 句逐字稿與所有修改，重新辨識一次嗎？`)) return;
    dlg.close();
    try {
      await api(`/api/projects/${state.projectId}/reprocess`, {
        method: 'POST',
        body: JSON.stringify({
          model: $('#r-model').value,
          num_speakers: $('#r-speakers').value,
          zh_mode: $('#r-zh').value,
          language: $('#r-language').value || '',
        }),
      });
      toast('已重新排入佇列');
      await loadProjects();
      openProject(state.projectId);
      startProjectPolling();
    } catch (err) {
      showError('無法重新辨識', err);
    }
  };

  /* 單句 */
  $('#s-cancel').onclick = () => $('#dlg-split').close();
  $('#s-start').onclick = async () => {
    const segId = state.splitSeg;
    if (!segId) return;
    const btn = $('#s-start');
    btn.disabled = true;
    btn.textContent = '辨識中…';
    $('#s-status').textContent = '正在重新切分並比對聲紋，請稍候…';
    try {
      const res = await api(
        `/api/projects/${state.projectId}/segments/${segId}/reprocess`,
        { method: 'POST', body: JSON.stringify({ num_speakers: $('#s-speakers').value }) });

      state.project = res.project;
      state.reworked = new Set([...(state.reworked || [])]);
      for (const s of res.project.segments) {
        if (s.id === segId || s.id.startsWith(segId + '-r')) state.reworked.add(s.id);
      }
      renderSpeakers();
      renderTranscript();
      updateMeta();
      $('#dlg-split').close();

      const sum = res.summary;
      const names = sum.speakers.map((id) => speakerOf(id).name).join('、');
      toast(sum.pieces > 1
        ? `拆成 ${sum.pieces} 句：${names}`
        : '已重新辨識（仍是一句）');
      if (sum.new_speakers.length) {
        toast(`新增了 ${sum.new_speakers.length} 位講者，請確認名字`);
      }
    } catch (err) {
      $('#s-status').textContent = '';
      showError('重新辨識這句失敗', err);
    } finally {
      btn.disabled = false;
      btn.textContent = '重新辨識';
    }
  };
}

function updateRerunLanguage() {
  const info = (window.__MODELS__ || []).find((m) => m.key === $('#r-model').value);
  const field = $('#r-lang-field');
  const sel = $('#r-language');
  const opts = (info && info.language_options) || [];
  if (!opts.length) { field.hidden = true; sel.innerHTML = ''; return; }
  field.hidden = false;
  sel.innerHTML = '';
  for (const o of opts) {
    const el = document.createElement('option');
    el.value = o.code;
    el.textContent = o.label;
    sel.appendChild(el);
  }
  const cur = (state.project && state.project.options) || {};
  sel.value = opts.some((o) => o.code === cur.language)
    ? cur.language : (info.default_language || opts[0].code);
}

function openSplitDialog(seg) {
  state.splitSeg = seg.id;
  $('#s-text').textContent = seg.text;
  $('#s-status').textContent = '';
  $('#s-speakers').value = '2';
  $('#dlg-split').showModal();
}

/* ---------------------------------------------------------- 詞語修正 */
function initVocabulary() {
  const dlg = $('#dlg-vocab');

  const toText = (rules) => rules
    .map((r) => `${r.wrong}=${r.right}` + (r.note ? `   # ${r.note}` : ''))
    .join('\n');

  const parse = (text) => text.split('\n').map((line) => {
    const body = line.split('#')[0].trim();
    if (!body || !body.includes('=')) return null;
    const [wrong, ...rest] = body.split('=');
    const note = (line.split('#')[1] || '').trim();
    return { wrong: wrong.trim(), right: rest.join('=').trim(), note };
  }).filter((r) => r && r.wrong);

  $('#btn-vocab').onclick = async () => {
    $('#vocab-status').textContent = '';
    try {
      const rules = await api('/api/vocabulary');
      $('#vocab-text').value = toText(rules);
    } catch (err) { showError('讀取詞語修正表失敗', err); }
    $('#vocab-apply').disabled = !state.projectId;
    dlg.showModal();
  };
  $('#vocab-close').onclick = () => dlg.close();

  $('#vocab-save').onclick = async () => {
    const entries = parse($('#vocab-text').value);
    try {
      const saved = await api('/api/vocabulary', {
        method: 'PUT', body: JSON.stringify({ entries }) });
      $('#vocab-text').value = toText(saved);
      $('#vocab-status').textContent = `已儲存 ${saved.length} 條，之後辨識會自動套用。`;
    } catch (err) { showError('儲存失敗', err); }
  };

  $('#btn-tidy').onclick = async () => {
    if (!state.projectId) return;
    // Works on the saved text - a few seconds, no audio is decoded.
    const btn = $('#btn-tidy');
    btn.disabled = true;
    btn.textContent = '整理中…';
    try {
      const res = await api(`/api/projects/${state.projectId}/tidy`,
                            { method: 'POST' });
      state.project = res.project;
      renderSpeakers();
      renderTranscript();
      const c = res.counts;
      toast(c.total
        ? `已整理：詞語 ${c.vocabulary} 句、標點 ${c.punctuation} 句、切句 ${c.split} 句`
        : '已經是最新的了，沒有需要修改的地方');
    } catch (err) {
      showError('整理逐字稿失敗', err);
    } finally {
      btn.disabled = false;
      updateTidyBadge();
    }
  };

  $('#vocab-apply').onclick = async () => {
    const entries = parse($('#vocab-text').value);
    try {
      await api('/api/vocabulary', { method: 'PUT', body: JSON.stringify({ entries }) });
      const res = await api(`/api/projects/${state.projectId}/apply-vocabulary`,
                            { method: 'POST' });
      state.project = res.project;
      renderSpeakers();
      renderTranscript();
      $('#vocab-status').textContent =
        `修正了 ${res.changed} 句、共 ${res.replacements} 處`
        + (res.skipped ? `（跳過 ${res.skipped} 句你手動改過的）` : '');
      toast(`已修正 ${res.replacements} 處`);
    } catch (err) { showError('套用失敗', err); }
  };
}

/* --------------------------------------------------------------- export */
function initExport() {
  const dlg = $('#dlg-export');
  const refresh = async () => {
    const fmtVal = $('#x-format').value;
    const q = new URLSearchParams({
      format: fmtVal, inline: '1',
      timestamps: $('#x-timestamps').checked ? '1' : '0',
      merge: $('#x-merge').checked ? '1' : '0',
    });
    try {
      const res = await fetch(`/api/projects/${state.projectId}/export?${q}`);
      $('#x-preview').textContent = await res.text();
    } catch (err) { $('#x-preview').textContent = String(err); }
  };

  $('#btn-export').onclick = () => { dlg.showModal(); refresh(); };
  $('#x-format').onchange = refresh;
  $('#x-timestamps').onchange = refresh;
  $('#x-merge').onchange = refresh;
  $('#x-close').onclick = () => dlg.close();

  $('#x-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText($('#x-preview').textContent);
      toast('已複製到剪貼簿');
    } catch { toast('複製失敗，請手動選取', true); }
  };

  $('#x-download').onclick = () => {
    const q = new URLSearchParams({
      format: $('#x-format').value,
      timestamps: $('#x-timestamps').checked ? '1' : '0',
      merge: $('#x-merge').checked ? '1' : '0',
    });
    window.location = `/api/projects/${state.projectId}/export?${q}`;
  };
}

/* ------------------------------------------------------------------ init */
function init() {
  initPlayer();
  initNewDialog();
  initExport();
  initRerun();
  initVocabulary();

  $('#btn-models').onclick = async (ev) => {
    $('#model-list').innerHTML = '<p class="loading-line"><span class="spinner"></span>讀取模型清單…</p>';
    $('#dlg-models').showModal();
    await renderModelList();
  };
  $('#models-close').onclick = () => $('#dlg-models').close();
  $('#err-close').onclick = () => $('#dlg-error').close();

  $('#btn-select-mode').onclick = (e) => {
    state.selectMode = !state.selectMode;
    document.body.classList.toggle('select-mode', state.selectMode);
    e.target.classList.toggle('on', state.selectMode);
    e.target.textContent = state.selectMode ? '離開選取' : '選取模式';
    if (!state.selectMode) state.picked.clear();
    updateBulkBar();
    renderTranscript();
  };
  $('#bulk-clear').onclick = () => { state.picked.clear(); updateBulkBar(); renderTranscript(); };

  $('#project-name').onblur = async () => {
    const name = $('#project-name').value.trim();
    if (!name || name === state.project?.name) return;
    await mutate(`/api/projects/${state.projectId}`,
                 { method: 'PATCH', body: JSON.stringify({ name }) });
  };
  $('#project-name').onkeydown = (e) => { if (e.key === 'Enter') e.target.blur(); };

  $('#btn-delete-project').onclick = async () => {
    if (!confirm(`刪除「${state.project.name}」？此動作無法復原。`)) return;
    await api(`/api/projects/${state.projectId}`, { method: 'DELETE' });
    state.projectId = null; state.project = null;
    $('#proj-status').hidden = true;
    $('#workspace').hidden = true;
    $('#player').hidden = true;
    $('#empty-state').hidden = false;
    $('#audio').pause();
    loadProjects();
    toast('已刪除');
  };

  api('/api/capacity').then((cap) => { window.__CAP__ = cap; updateThreadNote(); })
                     .catch((err) => logError('讀取 CPU 設定失敗', err));
  refreshModelOptions();
  loadProjects().then((active) => {
    if (state.projects.length) openProject(state.projects[0].id);
    if (active) startProjectPolling();
  });
}

window.addEventListener('error', (e) => logError('未攔截的錯誤', e.error || e.message));
window.addEventListener('unhandledrejection', (e) => logError('未處理的 Promise 拒絕', e.reason));

document.addEventListener('DOMContentLoaded', init);
