/* Data Transformer - wizard front-end */
'use strict';

const ROLES = [
  { key: 'new',        title: 'New Data',        desc: 'The transactions you just collected. Contains gaps.' },
  { key: 'master',     title: 'Master Data',     desc: 'Every valid city/product combination, standard labels and reference prices.' },
  { key: 'historical', title: 'Template + Past Data', desc: 'A previous output file. It does two jobs: its columns define the layout of your output, and its values fill the gaps.' },
];

const S = {
  files: {}, columns: {}, config: null,
  templateColumns: [], measures: [], stats: {}, refs: [],
  builderParts: [], lastRun: null, step: 1,
};

/* ---------------- helpers ---------------- */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const el = (tag, cls, html) => { const n = document.createElement(tag);
  if (cls) n.className = cls; if (html !== undefined) n.innerHTML = html; return n; };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = n => (n === null || n === undefined || isNaN(n)) ? '-' : Number(n).toLocaleString();

function toast(msg, kind = '') {
  const t = el('div', 'toast ' + kind, esc(msg));
  $('#toast').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 250); }, 3200);
}
function busy(on, text = 'Working...') {
  $('#overlay').hidden = !on;
  $('#overlay-text').textContent = text;
}
async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    return res;
  }
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || `Server error ${res.status}`);
  return data;
}
function selectOf(options, value, { blank = null, cls = '' } = {}) {
  const s = el('select', cls);
  if (blank !== null) s.appendChild(new Option(blank, ''));
  options.forEach(o => {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    s.appendChild(new Option(label, v));
  });
  s.value = value ?? '';
  if (value && s.value !== String(value)) { s.appendChild(new Option(value, value)); s.value = value; }
  return s;
}

/* ---------------- navigation ---------------- */
function goto(n) {
  S.step = n;
  $$('.panel').forEach(p => p.classList.toggle('active', +p.dataset.panel === n));
  $$('.step').forEach(b => {
    const i = +b.dataset.step;
    b.classList.toggle('active', i === n);
    b.classList.toggle('done', i < n && allUploaded());
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (n === 2) renderStep2();
  if (n === 3) renderStep3();
  if (n === 4) renderStep4();
  // Step 5 has nothing to draw, but it still needs the config to exist -
  // arriving here directly from step 1 would otherwise post a null config.
  if (n === 5 && !S.config && allUploaded()) ensureConfig().catch(e => toast(e.message, 'err'));
}
const allUploaded = () => ROLES.every(r => S.files[r.key]);

function lockSteps() {
  const ok = allUploaded();
  $$('.step').forEach(b => { if (+b.dataset.step > 1) b.disabled = !ok; });
  $('#to-2').disabled = !ok;
  $('#upload-hint').textContent = ok
    ? 'All three files loaded.'
    : `Upload all three files to continue (${ROLES.filter(r => S.files[r.key]).length} of 3 done).`;
}

/* ================= STEP 1 : upload ================= */
function buildUploads() {
  const wrap = $('#uploads');
  wrap.innerHTML = '';
  ROLES.forEach(r => {
    const card = el('div', 'drop');
    card.dataset.role = r.key;
    // A real <label for> is the most reliable way to open the picker; a hidden
    // input driven by JS click() is the part that tends to fail. No 'accept'
    // filter either - on a mapped network/Drive letter it can hide every file
    // in the dialog, which looks like nothing happening.
    const inputId = `file-${r.key}`;
    card.innerHTML = `
      <h3><span class="tick" hidden>&#10003;</span>${esc(r.title)}</h3>
      <p class="role-desc">${esc(r.desc)}</p>
      <label class="drop-zone" for="${inputId}">Drop a file here, or click to browse</label>
      <input id="${inputId}" type="file" class="visually-hidden">
      <div class="loaded-box" hidden></div>`;
    const input = $('input[type=file]', card);
    const handle = () => {
      const f = input.files && input.files[0];
      if (f) upload(r.key, f);
      input.value = '';   // so picking the same file again still fires
    };
    input.addEventListener('change', handle);
    ['dragenter', 'dragover'].forEach(e => card.addEventListener(e, ev => {
      ev.preventDefault(); card.classList.add('dragover');
    }));
    ['dragleave', 'drop'].forEach(e => card.addEventListener(e, ev => {
      ev.preventDefault(); if (e === 'dragleave' && card.contains(ev.relatedTarget)) return;
      card.classList.remove('dragover');
    }));
    card.addEventListener('drop', ev => {
      const f = ev.dataTransfer.files[0]; if (f) upload(r.key, f);
    });
    wrap.appendChild(card);
  });
  lockSteps();
}

/* Files sitting in the app's own folder - avoids the file dialog completely. */
async function loadLocalList() {
  let data;
  try { data = await api('/api/local-files'); } catch { return; }
  if (!data.files.length) return;

  $('#localfiles-card').hidden = false;
  $('#localfiles-path').textContent = data.folder;
  const host = $('#localfiles');
  host.innerHTML = '';
  const picks = {};

  data.files.forEach(f => {
    const row = el('div', 'local-row');
    row.appendChild(el('div', 'fname', `${esc(f.name)} <span class="tag">${f.size_kb} KB</span>`));
    const sel = selectOf(ROLES.map(r => [r.key, r.title]), f.role, { blank: '(do not load)' });
    sel.onchange = () => { picks[f.name] = sel.value; };
    picks[f.name] = f.role || '';
    const btn = el('button', 'btn ghost sm', 'Load');
    btn.onclick = async () => {
      if (!picks[f.name]) return toast('Choose which file this is', 'err');
      await loadLocal(picks[f.name], f.name);
    };
    row.append(sel, btn);
    host.appendChild(row);
  });

  const haveAll = ROLES.every(r => data.files.some(f => f.role === r.key));
  const all = $('#load-all-local');
  all.hidden = !haveAll;
  all.onclick = async () => {
    for (const r of ROLES) {
      const f = data.files.find(x => x.role === r.key);
      if (f) await loadLocal(r.key, f.name);
    }
  };
}

async function loadLocal(role, name) {
  busy(true, `Reading ${name}...`);
  try {
    const d = await api('/api/load-local', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, name }),
    });
    S.files[role] = d;
    S.columns[role] = d.preview.columns.map(c => c.name);
    S.config = null;
    renderLoaded(role, d);
    toast(`${name} loaded as ${role}`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(false); lockSteps(); }
}

async function upload(role, file) {
  const fd = new FormData();
  fd.append('role', role); fd.append('file', file);
  busy(true, `Reading ${file.name}...`);
  try {
    const data = await api('/api/upload', { method: 'POST', body: fd });
    S.files[role] = data;
    S.columns[role] = data.preview.columns.map(c => c.name);
    renderLoaded(role, data);
    S.config = null;
    toast(`${file.name} loaded`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(false); lockSteps(); }
}

function renderLoaded(role, data) {
  const card = $(`.drop[data-role="${role}"]`);
  card.classList.add('loaded');
  $('.tick', card).hidden = false;
  $('.drop-zone', card).textContent = 'Replace file';
  const box = $('.loaded-box', card);
  box.hidden = false;
  box.innerHTML = '';

  const info = el('div', 'file-info',
    `<strong title="${esc(data.name)}">${esc(data.name)}</strong>
     <span class="tag">${num(data.preview.row_count)} rows &times; ${data.preview.col_count} cols</span>`);
  box.appendChild(info);

  const row = el('div', 'sheet-row');
  const sheetSel = selectOf(data.sheets || [], data.sheet);
  const hdr = el('input'); hdr.type = 'number'; hdr.min = '0'; hdr.value = data.header_row;
  hdr.title = 'Header row (0 = first row of the sheet)';
  const relo = async () => {
    busy(true, 'Re-reading sheet...');
    try {
      const d = await api('/api/sheet', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role, sheet: sheetSel.value, header_row: +hdr.value }),
      });
      S.files[role] = d; S.columns[role] = d.preview.columns.map(c => c.name);
      S.config = null; renderLoaded(role, d);
    } catch (e) { toast(e.message, 'err'); }
    finally { busy(false); }
  };
  sheetSel.onchange = () => { hdr.value = ''; relo(); };
  hdr.onchange = relo;

  const lab1 = el('label', 'field'); lab1.append(el('span', '', 'Sheet'), sheetSel);
  const lab2 = el('label', 'field'); lab2.append(el('span', '', 'Header row'), hdr);
  row.append(lab1, lab2);
  box.appendChild(row);
  box.appendChild(dataTable(data.preview, 6));
}

function dataTable(preview, maxRows = 8, flagIdx = -1) {
  const wrap = el('div', 'table-wrap');
  const t = el('table', 'data-table');
  const thead = el('thead');
  const hr = el('tr');
  preview.columns.forEach(c => {
    const th = el('th'); th.textContent = c.name;
    th.title = `${c.dtype} - ${num(c.unique)} distinct, ${num(c.null)} blank`;
    hr.appendChild(th);
  });
  thead.appendChild(hr); t.appendChild(thead);
  const tb = el('tbody');
  preview.rows.slice(0, maxRows).forEach(r => {
    const tr = el('tr');
    if (flagIdx >= 0 && r[flagIdx] === 'FILLED') tr.className = 'filled';
    r.forEach(v => { const td = el('td');
      td.textContent = v === null ? '' : v; td.title = td.textContent; tr.appendChild(td); });
    tb.appendChild(tr);
  });
  t.appendChild(tb); wrap.appendChild(t);
  return wrap;
}

/* ================= STEP 2 : mapping ================= */
async function ensureConfig() {
  if (S.config) return;
  busy(true, 'Analysing your files and suggesting a mapping...');
  try {
    const d = await api('/api/suggest');
    S.config = d.config;
    S.templateColumns = d.template_columns;
    S.measures = d.measures;
    S.stats = d.stats || {};
    S.columns = d.columns;
    const r = await api('/api/refs');
    S.refs = r.refs;
  } finally { busy(false); }
}

/* ---- STEP 2 : column roles, then standardise labels against Master Data ---- */
const ROLE_OPTIONS = [
  ['dimension', 'Dimension (label)'],
  ['measure', 'Measure (number)'],
  ['date', 'Date'],
  ['ignore', 'Ignore'],
];

async function renderStep2() {
  try { await ensureConfig(); } catch (e) { return toast(e.message, 'err'); }
  if (!S.roleInfo) {
    try { S.roleInfo = (await api('/api/column-roles')).files; }
    catch (e) { return toast(e.message, 'err'); }
  }
  S.roleFile ||= 'new';
  renderRoleTabs(); renderRolesTable(); renderAttrMap();
  await refreshStandardise();
}

function renderRoleTabs() {
  const host = $('#role-tabs'); host.innerHTML = '';
  ROLES.forEach(r => {
    if (!S.roleInfo[r.key]) return;
    const b = el('button', 'tab' + (S.roleFile === r.key ? ' active' : ''), esc(r.title));
    b.onclick = () => { S.roleFile = r.key; renderRoleTabs(); renderRolesTable(); };
    host.appendChild(b);
  });
}

function roleOf(file, col) {
  const cfg = S.config.column_roles ||= {};
  const bucket = cfg[file] ||= {};
  if (!bucket[col]) {
    const found = (S.roleInfo[file] || []).find(c => c.name === col);
    bucket[col] = found ? found.detected : 'dimension';
  }
  return bucket[col];
}

function renderRolesTable() {
  const file = S.roleFile;
  const cols = S.roleInfo[file] || [];
  const filter = ($('#role-search').value || '').toLowerCase();
  const tb = $('#roles-table tbody'); tb.innerHTML = '';

  cols.forEach(c => {
    if (filter && !c.name.toLowerCase().includes(filter)) return;
    const tr = el('tr');
    tr.appendChild(el('td', '', esc(c.name)));
    tr.appendChild(el('td', '', `<span class="pill ${c.detected}">${esc(c.detected)}</span>`));
    tr.appendChild(el('td', 'muted', num(c.unique)));
    tr.appendChild(el('td', 'why', esc((c.samples || []).join(', ').slice(0, 60))));

    const td = el('td');
    const sel = selectOf(ROLE_OPTIONS, roleOf(file, c.name));
    sel.onchange = () => {
      S.config.column_roles[file][c.name] = sel.value;
      // The attribute map may only offer dimensions, so it has to be rebuilt.
      pruneAttrMap();
      renderRolesTable(); renderAttrMap(); refreshStandardise();
    };
    td.appendChild(sel);
    tr.appendChild(td);
    tb.appendChild(tr);
  });
  renderRoleSummary();
}

function renderRoleSummary() {
  const file = S.roleFile;
  const cols = S.roleInfo[file] || [];
  const tally = { dimension: 0, measure: 0, date: 0, ignore: 0 };
  cols.forEach(c => { tally[roleOf(file, c.name)]++; });
  $('#role-summary').innerHTML = `
    <div class="stat"><b>${tally.dimension}</b><span>Dimensions</span></div>
    <div class="stat"><b>${tally.measure}</b><span>Measures</span></div>
    <div class="stat"><b>${tally.date}</b><span>Dates</span></div>
    <div class="stat"><b>${tally.ignore}</b><span>Ignored</span></div>`;
}

/* Keep only pairs where both sides are still dimensions. */
function pruneAttrMap() {
  S.config.attribute_map = (S.config.attribute_map || []).filter(a =>
    (!a.new || roleOf('new', a.new) === 'dimension') &&
    (!a.master || roleOf('master', a.master) === 'dimension'));
}

const dimsOf = file => (S.roleInfo?.[file] || [])
  .filter(c => roleOf(file, c.name) === 'dimension').map(c => c.name);

function renderAttrMap() {
  const host = $('#attrmap'); host.innerHTML = '';
  const list = S.config.attribute_map ||= [];
  if (!list.length) host.appendChild(el('div', 'empty-note',
    'No attributes paired yet - add one below.'));

  list.forEach((a, i) => {
    const row = el('div', 'attr-row');
    const mk = (label, opts, val, key) => {
      const box = el('div');
      box.appendChild(el('div', 'lbl', label));
      const sel = selectOf(opts, val, { blank: '(none)' });
      sel.onchange = () => { a[key] = sel.value || null; };
      box.appendChild(sel);
      return box;
    };
    row.append(
      mk('Master Data attribute', dimsOf('master'), a.master, 'master'),
      el('div', 'arrow', '&larr;'),
      mk('New Data column', dimsOf('new'), a.new, 'new'));
    const del = el('button', 'btn ghost xs', '&times;');
    del.title = 'Stop standardising this attribute';
    del.onclick = () => { list.splice(i, 1); renderAttrMap(); };
    const dw = el('div'); dw.appendChild(el('div', 'lbl', '&nbsp;')); dw.appendChild(del);
    row.appendChild(dw);
    host.appendChild(row);
  });
}

async function refreshStandardise() {
  const list = (S.config.attribute_map || []).filter(a => a.new && a.master);
  if (!list.length) {
    $('#std-report').innerHTML = '';
    $('#std-summary').innerHTML = '';
    return;
  }
  busy(true, 'Comparing New Data values with Master Data...');
  try {
    const d = await api('/api/standardise', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dims: list, decisions: S.config.standardise_decisions || {},
                             column_roles: S.config.column_roles || {} }),
    });
    S.stdReport = d.dimensions;
    S.stdSkipped = d.skipped || [];
    seedDecisions(d.dimensions);
    renderStdReport();
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(false); }
}

/* Adopt the server's suggestion for anything the user has not decided yet. */
function seedDecisions(dims) {
  const dec = S.config.standardise_decisions ||= {};
  dims.forEach(d => {
    const bucket = dec[d.new_column] ||= {};
    d.unmatched.forEach(u => {
      if (!bucket[u.value]) bucket[u.value] = { action: u.action, to: u.to, why: u.why };
    });
    // Drop decisions for values that no longer need one.
    const live = new Set(d.unmatched.map(u => u.value));
    Object.keys(bucket).forEach(k => { if (!live.has(k)) delete bucket[k]; });
  });
}

const STD_ACTIONS = [
  ['map', 'Convert to Master Data value'],
  ['add', 'Add to Master Data'],
  ['exclude', 'Leave out of the output'],
];

function renderStdReport() {
  const host = $('#std-report'); host.innerHTML = '';
  const dims = S.stdReport || [];
  let totalUn = 0, totalRows = 0, clean = 0;

  dims.forEach(d => {
    if (!d.unmatched.length) { clean++; return; }
    totalUn += d.unmatched.length;
    totalRows += d.unmatched.reduce((s, u) => s + u.rows, 0);

    const block = el('div', 'std-block');
    block.appendChild(el('div', 'std-head',
      `<strong>${esc(d.new_column)}</strong> <span class="muted">&rarr; Master Data
       ${esc(d.master_column)}</span>
       <span class="tag">${d.matched} of ${d.new_value_count} values already match</span>`));

    const tbl = el('table', 'grid-table');
    tbl.innerHTML = `<thead><tr><th>New Data value</th><th>Rows</th>
      <th>What to do</th><th>Master Data value</th><th>Why</th></tr></thead>`;
    const tb = el('tbody');

    d.unmatched.forEach(u => {
      const dec = S.config.standardise_decisions[d.new_column][u.value];
      const tr = el('tr');
      tr.appendChild(el('td', '', esc(u.value)));
      tr.appendChild(el('td', 'muted', String(u.rows)));

      const tdAct = el('td'), tdTo = el('td');
      const act = selectOf(STD_ACTIONS, dec.action);
      const paint = () => {
        tdTo.innerHTML = '';
        if (act.value === 'map') {
          const sel = selectOf(d.master_values, dec.to, { blank: '(choose)' });
          sel.onchange = () => { dec.to = sel.value; updateStdHint(); };
          tdTo.appendChild(sel);
        } else if (act.value === 'add') {
          tdTo.appendChild(el('span', 'muted', `added as "${esc(u.value)}"`));
        } else {
          tdTo.appendChild(el('span', 'muted', 'rows excluded'));
        }
      };
      act.onchange = () => { dec.action = act.value; if (act.value !== 'map') dec.to = null;
        paint(); updateStdHint(); };
      paint();
      tdAct.appendChild(act);
      tr.append(tdAct, tdTo, el('td', 'why', esc(u.why || '')));
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    const wrap = el('div', 'table-wrap'); wrap.appendChild(tbl);
    block.appendChild(wrap);
    host.appendChild(block);
  });

  if (!totalUn) {
    host.appendChild(el('div', 'msg ok',
      'Every New Data value is already recognised by Master Data. Nothing to standardise.'));
  }
  (S.stdSkipped || []).forEach(s => host.appendChild(el('div', 'msg warn',
    `${esc(s.new_column)} &rarr; ${esc(s.master_column)} was not checked: ${esc(s.reason)}.`)));
  $('#std-summary').innerHTML = `
    <div class="stat"><b>${num(dims.length)}</b><span>Attributes checked</span></div>
    <div class="stat ${clean === dims.length ? 'good' : ''}"><b>${num(clean)}</b><span>Fully matched</span></div>
    <div class="stat ${totalUn ? 'warn' : ''}"><b>${num(totalUn)}</b><span>Values needing a decision</span></div>
    <div class="stat"><b>${num(totalRows)}</b><span>New Data rows affected</span></div>`;
  updateStdHint();
}

function updateStdHint() {
  const dec = S.config.standardise_decisions || {};
  let unset = 0, add = 0, drop = 0;
  Object.values(dec).forEach(b => Object.values(b).forEach(d => {
    if (d.action === 'map' && !d.to) unset++;
    if (d.action === 'add') add++;
    if (d.action === 'exclude') drop++;
  }));
  const bits = [];
  if (unset) bits.push(`${unset} value(s) still need a Master Data value chosen`);
  if (add) bits.push(`${add} will be added to Master Data`);
  if (drop) bits.push(`${drop} will be left out`);
  $('#std-hint').textContent = bits.join(' · ') || 'All values resolved.';
  $('#std-hint').className = 'hint' + (unset ? ' warn-text' : '');
}

function renderDims() {
  const host = $('#dims'); host.innerHTML = '';
  const dims = S.config.grid.master_dims;
  dims.forEach((d, i) => {
    const row = el('div', 'dim-row');
    const mk = (label, opts, val, key) => {
      const box = el('div');
      box.appendChild(el('div', 'lbl', label));
      const sel = selectOf(opts, val, { blank: '(not matched)' });
      sel.onchange = () => { d[key] = sel.value || null; renderGridStats(); };
      box.appendChild(sel);
      return box;
    };
    row.append(
      mk('Master Data', S.columns.master || [], d.master, 'master'),
      mk('New Data', S.columns.new || [], d.new, 'new'),
      mk('Template + Past Data', S.columns.historical || [], d.hist, 'hist'));
    const del = el('button', 'btn ghost xs', '&times;');
    del.title = 'Remove this dimension';
    del.onclick = () => { dims.splice(i, 1); renderDims(); renderGridStats(); };
    const dwrap = el('div'); dwrap.appendChild(el('div', 'lbl', '&nbsp;')); dwrap.appendChild(del);
    row.appendChild(dwrap);
    host.appendChild(row);
  });
  const add = el('button', 'btn ghost sm', '+ Add dimension');
  add.onclick = () => {
    dims.push({ master: (S.columns.master || [])[0] || null, new: null, hist: null });
    renderDims();
  };
  host.appendChild(add);
}

function renderGridOptions() {
  const nd = selectOf(S.columns.new || [], S.config.grid.new_date_column, { blank: '(none)' });
  nd.id = 'new-date';
  nd.onchange = () => { S.config.grid.new_date_column = nd.value || null; renderGridStats(); };
  $('#new-date').replaceWith(nd);
  const dd = $('#dedupe');
  dd.value = S.config.grid.dedupe?.strategy || 'first';
  dd.onchange = () => { S.config.grid.dedupe = { ...(S.config.grid.dedupe || {}), strategy: dd.value }; };
}

function renderGridStats() {
  const s = S.stats || {};
  const host = $('#grid-stats');
  const combos = s.combinations || 0, dates = s.dates || 0;
  host.innerHTML = `
    <div class="stat"><b>${num(combos)}</b><span>Master combinations</span></div>
    <div class="stat"><b>${num(dates)}</b><span>Reporting dates</span></div>
    <div class="stat good"><b>${num(combos * Math.max(dates, 1))}</b><span>Rows the output will have</span></div>
    <div class="stat"><b>${num(s.new_rows)}</b><span>Rows in New Data</span></div>`;
}

/* ---------- output column model ----------
   A column is described by three user-facing choices:
     source    which file the value comes from
     operation how it is filled
     what      the column name / formula / fixed value
   These map onto the engine's source spec below. */
const SOURCE_FILES = [
  ['new', 'New Data'],
  ['master', 'Master Data'],
  ['donor', 'Template + Past Data'],
];

const OPERATIONS = [
  ['column', 'Take the column'],
  ['grid', 'Grid / date'],
  ['formula', 'Formula'],
  ['prompt', 'Prompt (ask AI)'],
  ['fixed', 'Fixed value'],
  ['blank', 'Blank'],
];

function specToUi(spec) {
  const s = spec || { type: 'blank' };
  switch (s.type) {
    case 'new': case 'master': case 'donor':
      return { source: s.type, operation: 'column', what: s.column || '' };
    case 'grid':
      return { source: s.source_file || 'new', operation: 'grid', what: s.column || 'DATE' };
    case 'formula':
      return { source: s.source_file || 'new',
               operation: s.prompt ? 'prompt' : 'formula',
               what: s.expr || '', prompt: s.prompt || '' };
    case 'const':
      return { source: s.source_file || 'new', operation: 'fixed', what: s.value ?? '' };
    default:
      return { source: s.source_file || 'new', operation: 'blank', what: '' };
  }
}

function uiToSpec(ui) {
  const base = { source_file: ui.source };
  switch (ui.operation) {
    case 'column': return { type: ui.source, column: ui.what };
    case 'grid': return { ...base, type: 'grid', column: ui.what || 'DATE' };
    case 'formula': return { ...base, type: 'formula', expr: ui.what };
    case 'prompt': return { ...base, type: 'formula', expr: ui.what, prompt: ui.prompt || '' };
    case 'fixed': return { ...base, type: 'const', value: ui.what };
    default: return { ...base, type: 'blank' };
  }
}

function columnsForSource(source) {
  if (source === 'new') return S.columns.new || [];
  if (source === 'master') return S.columns.master || [];
  if (source === 'donor') return S.columns.historical || [];
  return [];
}

function gridColumns() {
  return ['DATE', ...((S.config?.grid?.master_dims || []).map(d => d.master).filter(Boolean))];
}

/* ---------- column autocomplete over all three files ---------- */
function attachAutocomplete(input, onCommit) {
  let box = null;
  const close = () => { if (box) { box.remove(); box = null; } };

  const partialAt = () => {
    const upto = input.value.slice(0, input.selectionStart ?? input.value.length);
    const m = upto.match(/(?:^|[^\w.\]])([\w.][\w. \/%-]{0,39})$/);
    return m ? { text: m[1], start: upto.length - m[1].length } : null;
  };

  const insert = (ref) => {
    const p = partialAt();
    const caret = input.selectionStart ?? input.value.length;
    const before = p ? input.value.slice(0, p.start) : input.value.slice(0, caret);
    const after = input.value.slice(caret);
    input.value = `${before}[${ref}]${after}`;
    const pos = before.length + ref.length + 2;
    input.setSelectionRange(pos, pos);
    close();
    input.focus();
    onCommit && onCommit();
  };

  const show = () => {
    const p = partialAt();
    close();
    if (!p || p.text.length < 1) return;
    const q = p.text.toLowerCase();
    const hits = (S.colIndex || []).filter(c =>
      c.column.toLowerCase().includes(q) || c.ref.toLowerCase().includes(q)).slice(0, 12);
    if (!hits.length) return;

    box = el('div', 'ac-box');
    hits.forEach((c, i) => {
      const item = el('div', 'ac-item' + (i === 0 ? ' active' : ''),
        `<span class="ac-col">${esc(c.column)}</span>
         <span class="ac-file">${esc(c.file)}</span>
         <span class="tag">${esc(c.dtype)}</span>`);
      item.onmousedown = ev => { ev.preventDefault(); insert(c.ref); };
      box.appendChild(item);
    });
    const r = input.getBoundingClientRect();
    box.style.left = `${r.left + window.scrollX}px`;
    box.style.top = `${r.bottom + window.scrollY + 2}px`;
    box.style.width = `${Math.max(r.width, 320)}px`;
    document.body.appendChild(box);
  };

  input.addEventListener('input', show);
  input.addEventListener('blur', () => setTimeout(close, 150));
  input.addEventListener('keydown', ev => {
    if (!box) return;
    const items = $$('.ac-item', box);
    const cur = items.findIndex(x => x.classList.contains('active'));
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const next = ev.key === 'ArrowDown'
        ? Math.min(cur + 1, items.length - 1) : Math.max(cur - 1, 0);
      items.forEach(x => x.classList.remove('active'));
      items[next].classList.add('active');
      items[next].scrollIntoView({ block: 'nearest' });
    } else if (ev.key === 'Enter' || ev.key === 'Tab') {
      ev.preventDefault();
      items[Math.max(cur, 0)].dispatchEvent(new MouseEvent('mousedown'));
    } else if (ev.key === 'Escape') { close(); }
  });
}

/* ---------- the output columns table ---------- */
function renderMapTable() {
  const tb = $('#map-table tbody'); tb.innerHTML = '';
  const filter = ($('#map-search').value || '').toLowerCase();
  const onlyUn = $('#only-unmapped').checked;

  S.config.output_columns.forEach((oc, idx) => {
    if (filter && !oc.name.toLowerCase().includes(filter)) return;
    const ui = specToUi(oc.source);
    if (onlyUn && ui.operation !== 'blank') return;

    const tr = el('tr');
    tr.dataset.idx = idx;          // position in output_columns, not on screen

    // drag handle. The row is only made draggable while the grip is held, so
    // text selection inside the inputs keeps working.
    const tdGrip = el('td', 'grip-cell', '<span class="grip" title="Drag to reorder">&#8942;&#8942;</span>');
    const grip = $('.grip', tdGrip);
    grip.onmousedown = () => { tr.draggable = true; };
    grip.onmouseup = () => { tr.draggable = false; };
    tr.addEventListener('dragstart', ev => {
      S.dragFrom = idx;
      tr.classList.add('dragging');
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', String(idx));
    });
    tr.addEventListener('dragend', () => {
      tr.draggable = false;
      tr.classList.remove('dragging');
      $$('#map-table tbody tr').forEach(x => x.classList.remove('drop-above', 'drop-below'));
    });
    tr.addEventListener('dragover', ev => {
      if (S.dragFrom === undefined || S.dragFrom === idx) return;
      ev.preventDefault();
      const r = tr.getBoundingClientRect();
      const below = ev.clientY > r.top + r.height / 2;
      tr.classList.toggle('drop-below', below);
      tr.classList.toggle('drop-above', !below);
    });
    tr.addEventListener('dragleave', () => tr.classList.remove('drop-above', 'drop-below'));
    tr.addEventListener('drop', ev => {
      ev.preventDefault();
      const from = S.dragFrom;
      S.dragFrom = undefined;
      if (from === undefined || from === idx) return;
      const r = tr.getBoundingClientRect();
      const below = ev.clientY > r.top + r.height / 2;
      moveOutputColumn(from, below ? idx + 1 : idx);
    });
    tr.appendChild(tdGrip);

    // Every column can be renamed - the output header is the user's to choose,
    // not something the template dictates.
    const tdName = el('td');
    const nm = el('input'); nm.type = 'text'; nm.value = oc.name;
    nm.title = 'Rename this output column';
    nm.onchange = () => renameOutputColumn(idx, nm.value);
    tdName.appendChild(nm);
    tr.appendChild(tdName);

    const tdSource = el('td'), tdOp = el('td'), tdWhat = el('td'),
          tdPrev = el('td', 'preview-cell'), tdDel = el('td');

    const srcSel = selectOf(SOURCE_FILES, ui.source);
    const opSel = selectOf(OPERATIONS, ui.operation);
    tdSource.appendChild(srcSel);
    tdOp.appendChild(opSel);

    const commit = () => {
      oc.source = uiToSpec(ui);
      oc.suggestion = 'set by you';
      markPreviewStale();
    };

    const paintWhat = () => {
      tdWhat.innerHTML = '';
      if (ui.operation === 'blank') {
        tdWhat.appendChild(el('span', 'muted', 'left empty'));
        return;
      }
      if (ui.operation === 'column' || ui.operation === 'grid') {
        const opts = ui.operation === 'grid' ? gridColumns() : columnsForSource(ui.source);
        const sel = selectOf(opts, ui.what, { blank: '(choose a column)' });
        sel.onchange = () => { ui.what = sel.value; commit(); };
        tdWhat.appendChild(sel);
        return;
      }
      if (ui.operation === 'fixed') {
        const inp = el('input'); inp.type = 'text'; inp.value = ui.what ?? '';
        inp.onchange = () => { ui.what = inp.value; commit(); };
        tdWhat.appendChild(inp);
        return;
      }
      // formula / prompt both end up as a formula the user can see and edit
      const wrap = el('div', 'what-formula');
      if (ui.operation === 'prompt') {
        const pr = el('input'); pr.type = 'text'; pr.className = 'prompt-input';
        pr.placeholder = 'Describe it, e.g. "NTP/6 divided by PKG"';
        pr.value = ui.prompt || '';
        const go = el('button', 'btn ghost xs', 'Ask');
        go.onclick = async () => {
          ui.prompt = pr.value;
          const got = await askAiForFormula(oc.name, pr.value);
          if (got) { ui.what = got; commit(); paintWhat(); refreshColumnPreview(); }
        };
        pr.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); go.click(); } };
        const row = el('div', 'prompt-row'); row.append(pr, go);
        wrap.appendChild(row);
      }
      const ta = el('input');
      ta.type = 'text';
      ta.className = 'formula-input';
      ta.value = ui.what || '';
      ta.placeholder = 'Type a column name to search all three files...';
      const sync = () => { ui.what = ta.value; commit(); checkFormula(ta, wrap); };
      ta.onchange = sync;
      attachAutocomplete(ta, sync);
      wrap.appendChild(ta);
      tdWhat.appendChild(wrap);
    };

    srcSel.onchange = () => {
      ui.source = srcSel.value;
      if (ui.operation === 'column') ui.what = '';
      commit(); paintWhat();
    };
    opSel.onchange = () => {
      ui.operation = opSel.value;
      if (ui.operation === 'grid') ui.what = 'DATE';
      else if (ui.operation === 'blank') ui.what = '';
      else if (ui.operation === 'column') ui.what = '';
      commit(); paintWhat();
    };
    paintWhat();

    // preview from the first row of real data
    const vals = (S.colPreview || {})[oc.name];
    if (vals && vals.length) {
      tdPrev.innerHTML = vals.slice(0, 2).map(v =>
        `<span class="pv">${v === null ? '<i>blank</i>' : esc(String(v))}</span>`).join('');
    } else {
      tdPrev.appendChild(el('span', 'muted', S.previewStale ? 'refresh to see' : '-'));
    }

    const last = S.config.output_columns.length - 1;
    const up = el('button', 'btn ghost xs', '&uarr;');
    up.title = 'Move up';
    up.disabled = idx === 0;
    up.onclick = () => moveOutputColumn(idx, idx - 1);
    const down = el('button', 'btn ghost xs', '&darr;');
    down.title = 'Move down';
    down.disabled = idx === last;
    down.onclick = () => moveOutputColumn(idx, idx + 2);
    tdDel.append(up, down);

    const del = el('button', 'btn ghost xs danger', '&times;');
    del.title = 'Remove this column from the output';
    del.onclick = () => removeOutputColumn(idx);
    tdDel.appendChild(del);

    tr.append(tdSource, tdOp, tdWhat, tdPrev, tdDel);
    tb.appendChild(tr);
  });
}

/* Rename an output column, repointing every [out.OLD] that referred to it.
   Without the repoint a rename silently breaks the formulas downstream. */
function renameOutputColumn(idx, raw) {
  const cols = S.config.output_columns;
  const oc = cols[idx];
  const name = String(raw || '').trim();
  if (!name || name === oc.name) { renderMapTable(); return; }
  if (cols.some((c, i) => i !== idx && c.name === name)) {
    toast(`There is already a column called ${name}`, 'err');
    renderMapTable();
    return;
  }

  const old = oc.name;
  oc.name = name;
  let repointed = 0;
  cols.forEach(c => {
    const expr = (c.source || {}).expr;
    if (!expr || !expr.includes(`[out.${old}]`)) return;
    c.source.expr = expr.split(`[out.${old}]`).join(`[out.${name}]`);
    repointed++;
  });
  (S.config.gapfill?.rules || []).forEach(r => {
    if (r.column === old) r.column = name;
    if (r.expr) r.expr = r.expr.split(`[out.${old}]`).join(`[out.${name}]`);
  });
  if (S.config.gapfill?.id_policy?.column === old) S.config.gapfill.id_policy.column = name;
  (S.config.validations || []).forEach(v => { if (v.column === old) v.column = name; });

  refreshRefs(); renderMapTable(); markPreviewStale();
  toast(repointed ? `Renamed to ${name}; updated ${repointed} formula(s)`
                  : `Renamed to ${name}`, 'ok');
}

/* Remove an output column, warning if anything still refers to it. */
function removeOutputColumn(idx) {
  const cols = S.config.output_columns;
  const gone = cols[idx].name;
  const users = cols
    .filter((c, i) => i !== idx && ((c.source || {}).expr || '').includes(`[out.${gone}]`))
    .map(c => c.name);
  if (users.length && !confirm(
      `${gone} is used by ${users.join(', ')}.\nRemove it anyway?`)) return;

  cols.splice(idx, 1);
  S.config.gapfill.rules = (S.config.gapfill?.rules || []).filter(r => r.column !== gone);
  S.config.validations = (S.config.validations || []).filter(v => v.column !== gone);
  if (S.config.gapfill?.id_policy?.column === gone) S.config.gapfill.id_policy.column = null;

  refreshRefs(); renderMapTable(); markPreviewStale();
  toast(`Removed ${gone}`, 'ok');
}

/* Move an output column. `to` is the slot it should land in, measured before
   the item is removed - so moving down needs idx+2, not idx+1. */
function moveOutputColumn(from, to) {
  const cols = S.config.output_columns;
  if (from < 0 || from >= cols.length) return;
  to = Math.max(0, Math.min(to, cols.length));
  if (to === from || to === from + 1) return;
  const [item] = cols.splice(from, 1);
  cols.splice(to > from ? to - 1 : to, 0, item);
  refreshRefs();
  renderMapTable();
  warnBrokenOrderRefs();
  markPreviewStale();
}

/* Formulas are resolved in dependency order, so position no longer constrains
   what a formula may reference. Only a genuine cycle is a problem. */
function warnBrokenOrderRefs() {
  const cols = S.config.output_columns;
  const deps = new Map(cols.map(c => [c.name,
    [...(((c.source || {}).expr) || '').matchAll(/\[out\.([^\]]+)\]/g)]
      .map(m => m[1]).filter(n => n !== c.name)]));
  const state = new Map();
  const cycles = [];
  const walk = (n, trail) => {
    if (state.get(n) === 'done') return;
    if (state.get(n) === 'open') { cycles.push([...trail, n].join(' -> ')); return; }
    state.set(n, 'open');
    (deps.get(n) || []).forEach(d => { if (deps.has(d)) walk(d, [...trail, n]); });
    state.set(n, 'done');
  };
  cols.forEach(c => walk(c.name, []));
  if (cycles.length) toast(`Circular reference: ${cycles[0]}`, 'err');
  return cycles;
}

function markPreviewStale() {
  S.previewStale = true;
  const note = $('#preview-note');
  if (note) note.textContent = 'Preview is out of date - click Refresh preview.';
}

async function refreshColumnPreview() {
  const note = $('#preview-note');
  if (note) note.textContent = 'Calculating preview...';
  try {
    const d = await api('/api/column-preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: S.config, limit: 25, rows: 2 }),
    });
    S.colPreview = d.values;
    S.previewInputs = d.inputs;
    S.previewStale = false;
    if (note) note.textContent = `Preview from the first ${d.rows} row(s) of real data.`;
    renderMapTable();
  } catch (e) {
    // Clear the old numbers and re-render. Leaving the previous run's values on
    // screen after a failed refresh is how a broken formula looks like a
    // formula that did nothing.
    S.colPreview = null;
    S.previewStale = true;
    renderMapTable();
    if (note) {
      note.textContent = e.message;
      note.className = 'warn-text';
    }
    toast(e.message, 'err');
  }
}

async function refreshRefs() {
  try {
    S.colIndex = (await api('/api/columns-index')).columns;
    S.refs = S.colIndex.map(c => c.ref);
  } catch { /* autocomplete is a convenience */ }
}

async function checkFormula(input, host) {
  $$('.formula-err', host).forEach(n => n.remove());
  if (!input.value.trim()) return;
  try {
    const d = await api('/api/validate-formula', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expr: input.value, available: S.refs }),
    });
    if (!d.ok) host.appendChild(el('div', 'formula-err', esc(d.problems.join('; '))));
  } catch (e) { /* validation is advisory */ }
}

/* ---------- AI-assisted formulas ---------- */
async function aiStatus() {
  try { return await api('/api/ai/status'); }
  catch { return { installed: false, configured: false }; }
}

function aiKeyDialog(onDone) {
  showModal('Connect Claude', `
    <p class="muted">A prompt is turned into a formula by Claude. Paste an API key
      from console.anthropic.com. It is kept in this app's memory only — never
      written to disk, never saved into a mapping profile.</p>
    <label class="field" style="margin-top:10px"><span>API key</span>
      <input type="password" id="ai-key" placeholder="sk-ant-..." autocomplete="off"></label>
    <div id="ai-key-msg"></div>`,
    async () => {
      const key = $('#ai-key').value.trim();
      if (!key) { toast('Paste a key first', 'err'); return false; }
      busy(true, 'Checking the key...');
      try {
        await api('/api/ai/key', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key }),
        });
        toast('Claude connected', 'ok');
        onDone && onDone();
        return true;
      } catch (e) {
        $('#ai-key-msg').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
        return false;
      } finally { busy(false); }
    });
}

async function askAiForFormula(targetColumn, prompt) {
  if (!String(prompt || '').trim()) { toast('Describe what you want first', 'err'); return null; }
  const st = await aiStatus();
  if (!st.installed) { toast('The anthropic package is not installed.', 'err'); return null; }
  if (!st.configured) {
    return new Promise(resolve => {
      aiKeyDialog(async () => resolve(await askAiForFormula(targetColumn, prompt)));
    });
  }
  busy(true, 'Asking Claude...');
  try {
    const samples = {};
    Object.entries(S.previewInputs || {}).forEach(([k, v]) => {
      if (v && v.length) samples[k] = v[0];
    });
    const d = await api('/api/ai/formula', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt, target_column: targetColumn, samples,
        output_columns: S.config.output_columns,
      }),
    });
    if (d.problems && d.problems.length) {
      toast(`Suggested with warnings: ${d.problems.join('; ')}`, 'err');
    } else {
      toast(d.explanation || 'Formula suggested', 'ok');
    }
    return d.formula;
  } catch (e) { toast(e.message, 'err'); return null; }
  finally { busy(false); }
}

async function renderStep3() {
  try { await ensureConfig(); } catch (e) { return toast(e.message, 'err'); }
  await refreshRefs();
  renderDims(); renderGridOptions(); renderGridStats(); renderMapTable();
  if (!S.colPreview) refreshColumnPreview();
}

function allRefOptions() {
  const opts = [];
  (S.columns.new || []).forEach(c => opts.push([`new.${c}`, `New Data - ${c}`]));
  (S.columns.master || []).forEach(c => opts.push([`master.${c}`, `Master Data - ${c}`]));
  (S.columns.historical || []).forEach(c => opts.push([`donor.${c}`, `Template + Past - ${c}`]));
  opts.push(['grid.DATE', 'Grid - DATE']);
  (S.config?.output_columns || []).forEach(c => opts.push([`out.${c.name}`, `Output - ${c.name}`]));
  return opts;
}




function formulaDialog(existing, done) {
  const item = existing || { name: '', expr: '' };
  const opts = allRefOptions().map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  const helpRows = FUNCS_HELP.map(f =>
    `<tr><td><code>${esc(f.signature)}</code></td><td class="muted">${esc(f.help)}</td></tr>`).join('');
  showModal(existing ? 'Edit computed column' : 'New computed column', `
    <label class="field"><span>Column name</span>
      <input type="text" id="f-name" value="${esc(item.name)}"></label>
    <label class="field" style="margin-top:10px"><span>Formula</span>
      <textarea id="f-expr" style="min-height:70px">${esc(item.expr)}</textarea></label>
    <div id="f-problem"></div>
    <label class="field" style="margin-top:10px"><span>Insert a column reference</span>
      <select id="f-insert"><option value="">(choose)</option>${opts}</select></label>
    <details style="margin-top:12px"><summary class="muted">Available functions</summary>
      <div class="table-wrap" style="max-height:220px;margin-top:8px">
        <table class="data-table"><tbody>${helpRows}</tbody></table></div></details>`,
    async () => {
      const name = $('#f-name').value.trim(), expr = $('#f-expr').value.trim();
      if (!name) { toast('Give the column a name', 'err'); return false; }
      if (!expr) { toast('Enter a formula', 'err'); return false; }
      const d = await api('/api/validate-formula', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expr, available: S.refs.concat(S.templateColumns) }),
      }).catch(() => ({ ok: true }));
      if (!d.ok) { $('#f-problem').innerHTML =
        `<div class="msg err">${esc(d.problems.join('; '))}</div>`; return false; }
      item.name = name; item.expr = expr;
      if (!existing) (S.config.derived_columns ||= []).push(item);
      done && done();
      return true;
    });
  $('#f-insert').onchange = e => {
    if (!e.target.value) return;
    const ta = $('#f-expr');
    const at = ta.selectionStart ?? ta.value.length;
    ta.value = ta.value.slice(0, at) + `[${e.target.value}]` + ta.value.slice(at);
    e.target.value = '';
    ta.focus();
  };
}

/* ================= STEP 4 : gap-fill ================= */
async function renderStep4() {
  // The config is built lazily, so jumping straight here from step 1 must
  // build it too - otherwise the whole panel renders empty.
  try { await ensureConfig(); } catch (e) { return toast(e.message, 'err'); }
  const g = S.config.gapfill;

  $('#gap-enabled').checked = g.enabled !== false;
  $('#gap-enabled').onchange = e => { g.enabled = e.target.checked; };

  const ds = $('#donor-strategy'); ds.value = g.donor_strategy || 'last';
  ds.onchange = () => { g.donor_strategy = ds.value; };

  const dp = $('#donor-prefer'); dp.value = g.prefer || 'historical';
  dp.onchange = () => { g.prefer = dp.value; renderFillTable(); };

  const hd = selectOf(S.columns.historical || [], g.hist_date_column, { blank: '(none)' });
  hd.id = 'hist-date';
  hd.onchange = () => { g.hist_date_column = hd.value || null; };
  $('#hist-date').replaceWith(hd);

  renderChain(); renderFillTable(); renderIdPolicy(); renderValidations();
}

function renderChain() {
  const g = S.config.gapfill;
  const host = $('#match-chain'); host.innerHTML = '';
  if (!g.match_chain || !g.match_chain.length) {
    host.appendChild(el('div', 'empty-note', 'No matching keys - filled rows will use Master Data only.'));
  }
  (g.match_chain || []).forEach((keyset, i) => {
    const row = el('div', 'chain-row');
    row.appendChild(el('span', 'order', String(i + 1)));
    row.appendChild(el('span', '', keyset.map(k =>
      `<span class="tag">${esc(k.grid)} = ${esc(k.hist)}</span>`).join(' + ')));
    const del = el('button', 'btn ghost xs', '&times;');
    del.onclick = () => { g.match_chain.splice(i, 1); renderChain(); };
    row.appendChild(del);
    host.appendChild(row);
  });
}

const FILL_RULES = [
  ['default', 'Copy from the preferred pool (default)'],
  ['donor', 'Copy from Template + Past Data'],
  ['new_donor', 'Copy from New Data'],
  ['master', 'Take from Master Data'],
  ['formula', 'Calculate with a formula'],
  ['const', 'Fixed value'],
  ['blank', 'Leave blank'],
];

function fillPresets(col) {
  // COALESCE keeps a preset working when only one pool has a donor for a row.
  const prefer = (S.config?.gapfill?.prefer === 'new')
    ? [`[new_donor.${col}]`, `[donor.${col}]`]
    : [`[donor.${col}]`, `[new_donor.${col}]`];
  const best = `COALESCE(${prefer.join(', ')})`;
  return {
    asis:     { type: 'default' },
    fromnew:  { type: 'new_donor', column: col },
    fromhist: { type: 'donor', column: col },
    jitter1:  { type: 'formula', expr: `${best} * RANDBETWEEN(99,101) / 100` },
    jitter5:  { type: 'formula', expr: `ROUND(${best} * RANDBETWEEN(95,105) / 100, 2)` },
    master:   { type: 'master', column: col },
    zero:     { type: 'const', value: 0 },
  };
}

function renderFillTable() {
  const g = S.config.gapfill;
  g.rules ||= [];
  const byCol = Object.fromEntries(g.rules.map(r => [r.column, r]));
  const tb = $('#fill-table tbody'); tb.innerHTML = '';

  // Columns the grid owns cannot be filled from a donor - that would stamp the
  // donor's own date/city on the row. Shown, but locked.
  const gridOwned = new Set(['DATE', S.config.gapfill?.hist_date_column,
    ...(S.config.grid?.master_dims || []).flatMap(d => [d.master, d.hist])].filter(Boolean));

  S.config.output_columns.forEach(oc => {
    const name = oc.name;
    if (gridOwned.has(name)) {
      const tr = el('tr', 'locked-row');
      tr.appendChild(el('td', '', ''));
      tr.appendChild(el('td', '', esc(name)));
      tr.appendChild(el('td', 'muted', "Always the row's own value"));
      tr.appendChild(el('td', 'muted',
        name === 'DATE' || name === S.config.gapfill?.hist_date_column
          ? "the reporting date from New Data's range"
          : 'the Master Data combination for this row'));
      tb.appendChild(tr);
      return;
    }
    const tr = el('tr');
    const cb = el('input'); cb.type = 'checkbox';
    cb.title = 'Select for a preset';
    cb.dataset.col = name;
    const tdCb = el('td'); tdCb.appendChild(cb); tr.appendChild(tdCb);
    tr.appendChild(el('td', '', esc(name)));

    const rule = byCol[name];
    const kind = !rule ? 'default' : rule.type;
    const tdRule = el('td'), tdVal = el('td');
    const sel = selectOf(FILL_RULES, kind);
    tdRule.appendChild(sel);

    const paint = () => {
      tdVal.innerHTML = '';
      const t = sel.value;
      if (t === 'default') { tdVal.appendChild(el('span', 'muted', 'template + past value, else Master Data')); return; }
      if (t === 'blank') { tdVal.appendChild(el('span', 'muted', 'empty')); return; }
      if (t === 'formula') {
        const ta = el('textarea');
        ta.value = (byCol[name] || {}).expr || `[donor.${name}]`;
        ta.onchange = () => { setRule(name, { type: 'formula', expr: ta.value }); checkFormula(ta, tdVal); };
        tdVal.appendChild(ta);
      } else if (t === 'const') {
        const inp = el('input'); inp.type = 'text';
        inp.value = (byCol[name] || {}).value ?? '';
        inp.onchange = () => setRule(name, { type: 'const', value: inp.value });
        tdVal.appendChild(inp);
      } else {
        const cols = t === 'master' ? (S.columns.master || [])
                   : t === 'new_donor' ? (S.columns.new || [])
                   : (S.columns.historical || []);
        const s2 = selectOf(cols, (byCol[name] || {}).column || name, { blank: '(choose)' });
        s2.onchange = () => setRule(name, { type: t, column: s2.value });
        tdVal.appendChild(s2);
      }
    };
    sel.onchange = () => {
      const t = sel.value;
      if (t === 'default') removeRule(name);
      else if (t === 'blank') setRule(name, { type: 'blank' });
      else if (t === 'formula') setRule(name, { type: 'formula', expr: `[donor.${name}]` });
      else if (t === 'const') setRule(name, { type: 'const', value: '' });
      else setRule(name, { type: t, column: name });
      paint();
    };
    paint();
    tr.append(tdRule, tdVal);
    tb.appendChild(tr);
  });
}

function setRule(col, spec) {
  const g = S.config.gapfill; g.rules ||= [];
  const i = g.rules.findIndex(r => r.column === col);
  const rule = { column: col, ...spec };
  if (i >= 0) g.rules[i] = rule; else g.rules.push(rule);
}
function removeRule(col) {
  const g = S.config.gapfill; g.rules = (g.rules || []).filter(r => r.column !== col);
}

function applyPreset(kind) {
  const cols = $$('#fill-table tbody input[type=checkbox]:checked').map(c => c.dataset.col);
  if (!cols.length) return toast('Tick the columns to apply this to first');
  cols.forEach(c => {
    if (kind === 'asis') removeRule(c);
    else setRule(c, fillPresets(c)[kind]);
  });
  renderFillTable();
  renderFillTable();
  toast(`Applied to ${cols.length} column(s)`, 'ok');
}

function renderIdPolicy() {
  const g = S.config.gapfill;
  g.id_policy ||= { column: null, mode: 'blank' };
  const sel = selectOf(S.config.output_columns.map(c => c.name), g.id_policy.column, { blank: '(none)' });
  sel.id = 'id-col';
  sel.onchange = () => { g.id_policy.column = sel.value || null; };
  $('#id-col').replaceWith(sel);

  const mode = $('#id-mode'); mode.value = g.id_policy.mode || 'blank';
  const extra = $('#id-extra'), wrap = $('#id-extra-wrap'), label = $('#id-extra-label');
  const paint = () => {
    const m = mode.value;
    wrap.hidden = (m === 'blank' || m === 'donor');
    label.textContent = m === 'constant' ? 'Value to use' : 'Prefix (optional)';
    extra.value = m === 'constant' ? (g.id_policy.value ?? '') : (g.id_policy.prefix ?? '');
  };
  mode.onchange = () => { g.id_policy.mode = mode.value; paint(); };
  extra.onchange = () => {
    if (mode.value === 'constant') g.id_policy.value = extra.value;
    else g.id_policy.prefix = extra.value;
  };
  paint();
}

const VALIDATION_TYPES = [
  ['non_negative', 'Cannot be negative'], ['positive', 'Must be greater than zero'],
  ['not_blank', 'Cannot be blank'], ['min', 'At least'], ['max', 'At most'],
  ['between', 'Between'], ['range_pct', 'Within % of a reference'],
  ['in_master', 'Must exist in Master Data'], ['unique', 'Must be unique'],
];

function renderValidations() {
  const host = $('#validations'); host.innerHTML = '';
  const list = S.config.validations ||= [];
  if (!list.length) host.appendChild(el('div', 'empty-note', 'No validation rules.'));
  list.forEach((v, i) => {
    const row = el('div', 'validation-row');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = v.enabled !== false;
    cb.onchange = () => { v.enabled = cb.checked; };

    const colSel = selectOf(S.config.output_columns.map(c => c.name), v.column, { blank: '(column)' });
    colSel.onchange = () => { v.column = colSel.value; };

    const typeSel = selectOf(VALIDATION_TYPES, v.type);
    const argBox = el('div');
    const paintArgs = () => {
      argBox.innerHTML = '';
      const t = typeSel.value;
      if (t === 'range_pct') {
        const ref = selectOf(allRefOptions(), v.reference, { blank: '(reference column)' });
        ref.onchange = () => { v.reference = ref.value; };
        const pct = el('input'); pct.type = 'number'; pct.value = v.pct ?? 10; pct.style.maxWidth = '80px';
        pct.onchange = () => { v.pct = +pct.value; };
        const g = el('div', 'row-2'); g.style.margin = '0'; g.append(ref, pct);
        argBox.appendChild(g);
      } else if (t === 'in_master') {
        const ref = selectOf((S.columns.master || []).map(c => [`master.${c}`, c]), v.reference, { blank: '(master column)' });
        ref.onchange = () => { v.reference = ref.value; };
        argBox.appendChild(ref);
      } else if (t === 'min' || t === 'max') {
        const inp = el('input'); inp.type = 'number'; inp.value = v.value ?? 0;
        inp.onchange = () => { v.value = +inp.value; };
        argBox.appendChild(inp);
      } else if (t === 'between') {
        const lo = el('input'); lo.type = 'number'; lo.value = v.min ?? 0;
        const hi = el('input'); hi.type = 'number'; hi.value = v.max ?? 0;
        lo.onchange = () => { v.min = +lo.value; }; hi.onchange = () => { v.max = +hi.value; };
        const g = el('div', 'row-2'); g.style.margin = '0'; g.append(lo, hi);
        argBox.appendChild(g);
      }
    };
    typeSel.onchange = () => { v.type = typeSel.value; paintArgs(); };
    paintArgs();

    const del = el('button', 'btn ghost xs danger', '&times;');
    del.onclick = () => { list.splice(i, 1); renderValidations(); };
    row.append(cb, colSel, typeSel, argBox, del);
    host.appendChild(row);
  });
}

/* ================= STEP 5 : run ================= */
async function runPreview() {
  busy(true, 'Building the output...');
  $('#run-status').innerHTML = '';
  try {
    const d = await api('/api/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: S.config }),
    });
    S.lastRun = d;
    showReport(d.report);
    showResults(d);
    toast('Preview ready', 'ok');
  } catch (e) {
    $('#run-status').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    toast(e.message, 'err');
  } finally { busy(false); }
}

async function runGenerate() {
  const alsoAppend = $('#also-append').checked;
  const payload = { config: S.config, filename: $('#out-name').value };
  if (alsoAppend) {
    const file = appendTarget();
    if (!file) { toast('Choose or enter the file to append to', 'err'); return; }
    payload.also_append = true;
    payload.append_to = file;
    payload.append_sheet = $('#append-sheet').value || null;
    payload.skip_existing_dates = $('#skip-existing-dates').checked;
    payload.append_template_columns = $('#append-template-cols').checked;
  }
  busy(true, alsoAppend ? 'Generating and appending...' : 'Generating the workbook...');
  $('#run-status').innerHTML = '';
  try {
    const d = await api('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showReport(d.report);

    // The new file is always produced.
    let html = `<div class="msg ok"><strong>${esc(d.filename)}</strong> is ready
      (${d.size_kb} KB). If the download did not start,
      <a href="/api/download/${d.token}">click here</a>.</div>`;

    // The append, when asked for, is reported separately - it can fail on its
    // own without affecting the file that was just generated.
    if (d.append_error) {
      html += `<div class="msg err">The output file was created, but appending
        failed: ${esc(d.append_error)}</div>`;
    } else if (d.appended_to) {
      const skipNote = d.skipped
        ? `<br><span class="muted">Skipped ${num(d.skipped)} rows for
           ${d.skipped_dates.length} date(s) already there:
           ${d.skipped_dates.map(esc).join(', ')}.</span>`
        : '';
      const dropped = (d.append_columns_dropped || []).length
        ? `<br><span class="muted">Kept the template's ${num(d.append_columns_kept)}
           columns; left out ${esc(d.append_columns_dropped.join(', '))}.</span>`
        : '';
      html += d.appended
        ? `<div class="msg ok"><strong>${num(d.appended)}</strong> rows also appended to
           <strong>${esc(d.appended_to)}</strong> (sheet “${esc(d.append_sheet)}”,
           now ${d.append_size_kb} KB).
           <a href="/api/download/${d.append_token}">Download it</a>.${skipNote}${dropped}</div>`
        : `<div class="msg warn">Nothing appended to
           <strong>${esc(d.appended_to)}</strong> — every date was already there,
           so it was left unchanged.${skipNote}</div>`;
    }
    $('#run-status').innerHTML = html;
    window.location = `/api/download/${d.token}`;
    toast(d.append_error ? 'Generated, but append failed'
          : d.appended_to ? 'Generated and appended' : 'Output generated',
          d.append_error ? 'err' : 'ok');
  } catch (e) {
    $('#run-status').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    toast(e.message, 'err');
  } finally { busy(false); }
}

// The file to append to: a folder file picked from the list, or - when
// "Another file…" is chosen - whatever full path was typed in.
function appendTarget() {
  const sel = $('#append-file').value;
  if (sel === '__path__') return ($('#append-path').value || '').trim();
  return sel;
}

// Step 5: the new output file is always written; appending is an optional extra.
function setupOutputMode() {
  $('#also-append').onchange = () => {
    const on = $('#also-append').checked;
    $('#append-opts').hidden = !on;
    if (on) populateAppendFiles();
  };
  $('#append-file').onchange = () => {
    const custom = $('#append-file').value === '__path__';
    $('#append-path-row').hidden = !custom;
    $('#append-sheet').innerHTML = `<option value="">(first sheet)</option>`;
    if (custom) $('#append-path').focus();
    else populateAppendSheets();
  };
  $('#append-path-load').onclick = () => populateAppendSheets();
  $('#append-path').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); populateAppendSheets(); }
  });
}

async function populateAppendFiles() {
  const sel = $('#append-file');
  if (sel.dataset.loaded) return;           // fill once; keep the user's choice
  try {
    const d = await api('/api/local-files');
    const opts = (d.files || [])
      .filter(f => /\.xlsx?$|\.xlsm$/i.test(f.name))
      .map(f => `<option value="${esc(f.name)}">${esc(f.name)} (${f.size_kb} KB)</option>`);
    sel.innerHTML = `<option value="">Choose a file…</option>` + opts.join('') +
      `<option value="__path__">Another file — enter full path…</option>`;
    sel.dataset.loaded = '1';
  } catch (e) {
    sel.innerHTML = `<option value="">Could not list files</option>` +
      `<option value="__path__">Another file — enter full path…</option>`;
    toast(e.message, 'err');
  }
}

async function populateAppendSheets() {
  const file = appendTarget();
  const sel = $('#append-sheet');
  const note = $('#append-note');
  sel.innerHTML = `<option value="">(first sheet)</option>`;
  if (!file) return;
  try {
    const d = await api('/api/target-sheets?name=' + encodeURIComponent(file));
    sel.innerHTML = `<option value="">(first sheet)</option>` +
      (d.sheets || []).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    if (!d.writable) {
      note.innerHTML = `<span style="color:var(--bad)">“${esc(file)}” is a ${esc(d.ext)}
        file and cannot be appended to. Only .xlsx / .xlsm can. Pick another file
        or use “New file”.</span>`;
    } else {
      note.innerHTML = `Rows are added to the bottom, matched to the sheet's own
        column headers. The rest of the workbook is left as-is.`;
    }
  } catch (e) { toast(e.message, 'err'); }
}

function showReport(rep) {
  if (!rep) return;
  $('#report-card').hidden = false;
  $('#report-stats').innerHTML = `
    <div class="stat"><b>${num(rep.row_count)}</b><span>Output rows</span></div>
    <div class="stat good"><b>${num(rep.actual_rows)}</b><span>From New Data</span></div>
    <div class="stat warn"><b>${num(rep.filled_rows)}</b><span>Gap-filled</span></div>
    <div class="stat ${rep.issue_count ? 'bad' : ''}"><b>${num(rep.issue_count)}</b><span>Validation issues</span></div>`;
  const steps = (rep.steps || []).map(s => `<li>${esc(s)}</li>`).join('');
  const warns = (rep.warnings || []).map(w => `<li>${esc(w)}</li>`).join('');

  // Which columns came out empty, and on which kind of row - the fastest way
  // to see whether a blank points at step 3 (real rows) or step 4 (filled).
  const bl = rep.blank_columns || [];
  const blankTable = !bl.length ? '' : `
    <div class="msg warn"><strong>Columns with empty cells</strong>
      <div class="table-wrap" style="max-height:220px;margin-top:8px">
      <table class="data-table"><thead><tr>
        <th>Column</th><th>Empty</th><th>on New Data rows</th><th>on gap-filled rows</th>
        <th>Likely cause</th></tr></thead><tbody>
      ${bl.map(b => {
        const onlyFilled = b.actual === 0 && b.filled > 0;
        const onlyActual = b.filled === 0 && b.actual > 0;
        const why = b.cause || (onlyFilled ? 'its step 4 gap-fill rule'
                  : onlyActual ? 'its Source/Operation in step 3'
                  : 'no source, or a formula returning nothing');
        return `<tr><td>${esc(b.column)}</td><td>${num(b.blank)} (${b.pct}%)</td>
                <td>${num(b.actual ?? 0)}</td><td>${num(b.filled ?? 0)}</td>
                <td>${esc(why)}</td></tr>`;
      }).join('')}
      </tbody></table></div></div>`;

  $('#report-detail').innerHTML =
    (steps ? `<div class="msg ok"><strong>What happened</strong><ul>${steps}</ul></div>` : '') +
    (warns ? `<div class="msg warn"><strong>Check these</strong><ul>${warns}</ul></div>` : '') +
    blankTable;
}

function showResults(d) {
  $('#result-card').hidden = false;
  const paint = tab => {
    const body = $('#result-body'); body.innerHTML = '';
    if (tab === 'all') {
      const idx = d.preview.columns.findIndex(c => c.name === 'SOURCE');
      body.appendChild(dataTable(d.preview, 40, idx));
    } else if (tab === 'filled') {
      if (!d.filled_preview) body.appendChild(el('div', 'empty-note', 'No gap-filled rows.'));
      else body.appendChild(dataTable(d.filled_preview, 20));
    } else if (tab === 'issues') {
      if (!d.issues) body.appendChild(el('div', 'empty-note', 'No validation issues.'));
      else body.appendChild(dataTable(d.issues, 25));
    } else {
      if (!d.excluded) {
        body.appendChild(el('div', 'empty-note',
          'Every New Data row matched a Master Data combination.'));
      } else {
        body.appendChild(el('div', 'msg warn',
          'These New Data rows have no matching combination in Master Data, so they are ' +
          'not in the output. Add them to Master Data, or adjust the dimension mapping.'));
        body.appendChild(dataTable(d.excluded, 25));
      }
    }
  };
  $$('#result-tabs .tab').forEach(t => {
    t.onclick = () => {
      $$('#result-tabs .tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active'); paint(t.dataset.tab);
    };
  });
  $$('#result-tabs .tab').forEach(t => t.classList.remove('active'));
  $('#result-tabs .tab').classList.add('active');
  paint('all');
}

/* ================= modal ================= */
function showModal(title, html, onOk) {
  const back = el('div', 'overlay');
  back.style.background = 'rgba(20,26,36,.45)';
  const box = el('div', 'card');
  box.style.cssText = 'max-width:640px;width:92vw;max-height:84vh;overflow:auto;margin:0';
  box.innerHTML = `<div class="card-head"><h3>${title}</h3></div><div class="modal-body">${html}</div>`;
  const foot = el('div', 'panel-foot');
  const cancel = el('button', 'btn ghost', 'Cancel');
  const ok = el('button', 'btn primary', onOk ? 'Save' : 'Close');
  cancel.onclick = () => back.remove();
  ok.onclick = async () => { if (!onOk) return back.remove();
    const r = await onOk(); if (r !== false) back.remove(); };
  foot.append(cancel, ok);
  if (!onOk) cancel.remove();
  box.appendChild(foot);
  back.appendChild(box);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

/* ================= wiring ================= */
let FUNCS_HELP = [];

document.addEventListener('DOMContentLoaded', async () => {
  buildUploads();
  loadLocalList();
  goto(1);

  $$('.step').forEach(b => b.onclick = () => { if (!b.disabled) goto(+b.dataset.step); });
  $$('[data-goto]').forEach(b => b.onclick = () => goto(+b.dataset.goto));
  $('#to-2').onclick = () => goto(2);

  $('#map-search').oninput = () => renderMapTable();
  $('#only-unmapped').onchange = () => renderMapTable();
  $('#refresh-preview').onclick = () => refreshColumnPreview();
  $('#reset-order').onclick = () => {
    // The template file defines the natural layout; added columns keep their
    // relative order and go to the end.
    const tpl = S.templateColumns || [];
    const rank = n => { const i = tpl.indexOf(n); return i === -1 ? tpl.length : i; };
    S.config.output_columns = S.config.output_columns
      .map((c, i) => ({ c, i }))
      .sort((a, b) => rank(a.c.name) - rank(b.c.name) || a.i - b.i)
      .map(x => x.c);
    refreshRefs(); renderMapTable(); markPreviewStale();
    toast('Order reset to the template layout', 'ok');
  };
  $('#add-output-col').onclick = () => {
    // The app's own modal rather than window.prompt - native dialogs are
    // suppressed in some embedded browsers, which looks like a dead button.
    showModal('Add an output column', `
      <label class="field"><span>Column name</span>
        <input type="text" id="newcol-name" placeholder="e.g. UNIT TP"></label>
      <p class="muted" style="margin-top:8px">It is added at the end of the list.
        Set its Source, Operation and What in the table.</p>`,
      () => {
        const name = ($('#newcol-name').value || '').trim();
        if (!name) { toast('Give the column a name', 'err'); return false; }
        if (S.config.output_columns.some(c => c.name === name)) {
          toast('That column already exists', 'err'); return false;
        }
        S.config.output_columns.push({
          name, added: true, source: { type: 'blank' }, suggestion: 'added by you' });
        refreshRefs(); renderMapTable();
        toast(`Added ${name}`, 'ok');
        return true;
      });
  };

  $('#add-attr').onclick = () => {
    (S.config.attribute_map ||= []).push({ master: null, new: null });
    renderAttrMap();
  };
  $('#recheck').onclick = () => refreshStandardise();
  $('#role-search').oninput = () => renderRolesTable();
  $('#roles-reset').onclick = () => {
    const file = S.roleFile;
    (S.roleInfo[file] || []).forEach(c => {
      S.config.column_roles[file][c.name] = c.detected;
    });
    pruneAttrMap();
    renderRolesTable(); renderAttrMap(); refreshStandardise();
    toast('Roles reset to detected', 'ok');
  };

  $('#add-validation').onclick = () => {
    (S.config.validations ||= []).push({ column: '', type: 'non_negative', enabled: true });
    renderValidations();
  };
  $$('[data-preset]').forEach(b => b.onclick = () => applyPreset(b.dataset.preset));

  $('#run-preview').onclick = runPreview;
  $('#run-generate').onclick = runGenerate;
  setupOutputMode();

  $('#restart').onclick = async () => {
    if (!confirm('Clear the uploaded files and start again?')) return;
    await api('/api/reset', { method: 'POST' }).catch(() => {});
    location.reload();
  };
  $('#profile-out').onclick = async () => {
    if (!S.config) return toast('Nothing to save yet');
    const res = await fetch('/api/config/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: S.config }),
    });
    const blob = await res.blob();
    const a = el('a'); a.href = URL.createObjectURL(blob);
    a.download = 'mapping_profile.json'; a.click();
  };
  $('#profile-in').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    try {
      const d = await api('/api/config/import', { method: 'POST', body: fd });
      S.config = d.config;
      toast('Profile loaded', 'ok');
      if (S.step >= 2) goto(S.step);
    } catch (err) { toast(err.message, 'err'); }
  };

  try { FUNCS_HELP = (await api('/api/functions')).functions; } catch { /* optional */ }
});
