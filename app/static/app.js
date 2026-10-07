'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const RANGES = ['grow_mm', 'simplify_mm', 'spread_mm', 'thickness_mm', 'bridge_mm', 'base_mm',
  'relief_mm', 'base_margin_mm', 'corner_r_mm', 'min_island_mm2', 'hole_d_mm',
  'hole_rim_mm', 'hole_x', 'hole_y', 'threshold'];
const PARAM_KEYS = ['mode', 'fit', 'size_mm', 'mirror', 'grow_mm', 'simplify_mm',
  'nozzle_mm', 'thickness_mm', 'bridges', 'bridge_mm', 'base_mm', 'relief_mm', 'base_shape',
  'base_margin_mm', 'corner_r_mm', 'min_island_mm2', 'hole', 'hole_d_mm',
  'hole_x', 'hole_y', 'hole_rim_mm'];

const V_RANGES = ['v_threshold', 'v_blur', 'v_smooth', 'v_tolerance', 'v_despeckle',
  'v_upscale', 'v_colors', 'v_overlap'];
const V_KEYS = ['threshold', 'invert', 'blur', 'smooth', 'tolerance', 'despeckle',
  'upscale', 'colors', 'overlap'];

const LANGS = ['ar', 'zh', 'en', 'fr', 'ru', 'es'];
const LANG_KEY = 'lineart2print.lang';

const state = { job: null, srcJob: null, srcKind: null, traced: false,
  islands: 0, tracedIslands: 0, lang: 'en',
  mode: 'patch', tab: 'model',
  timer: null, busy: false, again: false,
  vTimer: null, vBusy: false, vAgain: false, vView: 'both',
  tracing: false, preparing: false, vSaving: false,
  lastStats: null, lastWarnings: null, lastErr: null,
  lastVStats: null, lastVErr: null, dl: null, vdl: null,
  vMode: 'mono', palette: null, paletteCustom: false, swatch: 0, picking: false,
  sampleCanvas: null, view: { z: 1, x: 0, y: 0 },
  islandMode: false, islandSel: new Set(), islandHover: null,
  islandIndex: null, islandStamp: '', vEdited: false, islandClickTimer: null };

/* ------------------------------------------------------------------ i18n */
function matchLang(raw) {
  const base = String(raw || '').toLowerCase().split('-')[0];
  if (base === 'zh') return 'zh';
  return LANGS.includes(base) ? base : '';
}

function detectLang() {
  const prefs = navigator.languages && navigator.languages.length
    ? navigator.languages : [navigator.language || 'en'];
  for (const raw of prefs) {
    const hit = matchLang(raw);
    if (hit) return hit;
  }
  return 'en';
}

function bootLang() {
  let saved = '';
  try { saved = localStorage.getItem(LANG_KEY) || ''; } catch (e) { /* private mode */ }
  if (LANGS.includes(saved)) return saved;
  const lang = detectLang();
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* private mode */ }
  return lang;
}

function t(key, vars) {
  const pack = I18N[state.lang] || I18N.en;
  let s = Object.prototype.hasOwnProperty.call(pack, key) ? pack[key]
    : (Object.prototype.hasOwnProperty.call(I18N.en, key) ? I18N.en[key] : key);
  if (vars) {
    s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] == null ? m : String(vars[k])));
  }
  return s;
}

function units() {
  return {
    mm: t('unit.mm'), cm2: t('unit.cm2'), cm3: t('unit.cm3'), g: t('unit.g'),
  };
}

function formatErr(err) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  const key = 'err.' + (err.error || '');
  const s = t(key, err.detail != null ? { detail: err.detail } : {});
  if (s === key) return err.detail ? `${err.error}: ${err.detail}` : String(err.error || '');
  return s;
}

function numText(n) {
  let s = Number(n).toFixed(2).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
  if (state.lang === 'ru' || state.lang === 'fr' || state.lang === 'es')
    s = s.replace('.', ',');
  return s;
}

function warnText(w) {
  if (!w || typeof w !== 'object' || !w.code) return String(w ?? '');
  const vars = Object.assign({}, w);
  if (typeof vars.area === 'number') vars.area = vars.area.toFixed(1);
  for (const k of ['mm', 'bridge']) {
    if (typeof vars[k] === 'number') vars[k] = numText(vars[k]);
  }
  const key = 'warn.' + w.code;
  const s = t(key, vars);
  return s === key ? w.code : s;
}

function applyLang(lang) {
  state.lang = LANGS.includes(lang) ? lang : 'en';
  document.documentElement.lang = state.lang;
  document.documentElement.dir = state.lang === 'ar' ? 'rtl' : 'ltr';
  const sel = $('#lang');
  if (sel && sel.value !== state.lang) sel.value = state.lang;
  $$('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
  if (state.tracing) $('#v_use').textContent = t('tracing');
  if (state.preparing) $('#download').textContent = t('preparing');
  if (state.srcKind) fileMetaText();
  if (state.lastStats) showStats(state.lastStats);
  if (state.lastWarnings || state.lastErr) showWarnings(state.lastWarnings, state.lastErr);
  showVStats(state.lastVStats);
  showVError(state.lastVErr);
  showDlHint();
  showVHint();
  syncUI();
  paintQueue();
}

async function readJson(r) {
  try { return await r.json(); } catch (e) { return {}; }
}

function asErr(d, fallback) {
  if (d && typeof d.error === 'string') return d;
  return { error: fallback };
}

const QUEUE_POLL_MS = 700;
const queueUi = {};

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function paintQueue() {
  const el = $('#qstatus');
  if (!el) return;
  const items = Object.values(queueUi);
  if (!items.length) {
    el.hidden = true;
    el.textContent = '';
    return;
  }
  const cur = items.find((x) => x.mode === 'queued') || items[items.length - 1];
  el.hidden = false;
  if (cur.mode === 'running') el.textContent = t('q.working');
  else if (!cur.ahead) el.textContent = t('q.next');
  else el.textContent = t('q.ahead', { n: cur.ahead });
}

function setQueue(channel, ticket, mode, ahead) {
  if (!mode) {
    if (queueUi[channel] && queueUi[channel].ticket === ticket) delete queueUi[channel];
  } else {
    queueUi[channel] = { ticket, mode, ahead: ahead || 0 };
  }
  paintQueue();
}

function clearQueue() {
  for (const k of Object.keys(queueUi)) delete queueUi[k];
  paintQueue();
}

async function pollQueue(ticket, channel, current) {
  try {
    for (;;) {
      if (!current()) return { ignore: true };
      const r = await fetch('/api/queue/' + encodeURIComponent(ticket));
      const d = await readJson(r);
      if (!current()) return { ignore: true };
      if (r.status === 404) throw asErr(d, 'ticket_missing');
      if (!r.ok) throw asErr(d, 'server_error');
      if (d.status === 'queued') {
        setQueue(channel, ticket, 'queued', +d.ahead || 0);
        await sleep(QUEUE_POLL_MS);
        continue;
      }
      if (d.status === 'running') {
        setQueue(channel, ticket, 'running', 0);
        await sleep(QUEUE_POLL_MS);
        continue;
      }
      if (d.status === 'done') {
        setQueue(channel, ticket, null);
        if (!current()) return { ignore: true };
        if (d.download) {
          const fr = await fetch('/api/queue/' + encodeURIComponent(ticket) + '/file');
          if (!current()) return { ignore: true };
          return { response: fr };
        }
        return { json: d.result };
      }
      if (d.status === 'error') {
        setQueue(channel, ticket, null);
        if (!current() || d.error === 'superseded') return { ignore: true };
        throw { error: d.error || 'server_error', detail: d.detail };
      }
      await sleep(QUEUE_POLL_MS);
    }
  } catch (e) {
    if (current()) setQueue(channel, ticket, null);
    throw e;
  }
}

async function callHeavy(url, body, channel, current) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (r.status === 202) {
    const q = await readJson(r);
    if (!q || !q.ticket) throw asErr(q, 'server_error');
    const ahead = Number.isFinite(+q.ahead) ? +q.ahead : Math.max(0, (+q.position || 1) - 1);
    if (current()) setQueue(channel, q.ticket, 'queued', ahead);
    return pollQueue(q.ticket, channel, current);
  }
  if (!current()) return { ignore: true };
  return { response: r };
}

async function readHeavyJson(out, fallback) {
  if (!out || out.ignore) return null;
  if (Object.prototype.hasOwnProperty.call(out, 'json')) return out.json;
  const d = await readJson(out.response);
  if (!out.response.ok) throw asErr(d, fallback);
  return d;
}

/* ---------------------------------------------------------------- params */
function params() {
  const p = {};
  for (const k of PARAM_KEYS) {
    const el = $('#' + k);
    if (!el) { if (k === 'mode') p.mode = state.mode; continue; }
    p[k] = el.type === 'checkbox' ? el.checked : el.value;
  }
  p.mode = state.mode;
  return p;
}

function payload(extra) {
  return Object.assign({
    job: state.job, params: params(),
    threshold: +$('#threshold').value, invert: $('#invert').checked,
    show_bridges: $('#show_bridges').checked,
    detail_thin: $('#detail_thin').checked,
    detail_press: $('#detail_press').checked,
    spread_mm: +$('#spread_mm').value,
  }, extra || {});
}

function vparams() {
  const p = { mode: state.vMode };
  for (const k of V_KEYS) {
    const el = $('#v_' + k);
    if (!el) continue;
    p[k] = el.type === 'checkbox' ? el.checked : el.value;
  }
  if (state.vMode === 'color' && state.paletteCustom && state.palette && state.palette.length) {
    p.palette = state.palette.map((s) => s.hex);
    p.skip = state.palette.filter((s) => !s.on).map((s) => s.hex);
  }
  return p;
}

function syncUI() {
  for (const k of RANGES.concat(V_RANGES)) {
    const el = $('#' + k), out = $('#' + k + 'O');
    if (!el || !out) continue;
    const int = k === 'threshold' || k === 'v_threshold' || k === 'v_despeckle'
      || k === 'v_colors' || k === 'v_overlap';
    out.textContent = (+el.value).toFixed(int ? 0 : 2);
    const min = +el.min, max = +el.max, val = +el.value;
    const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
    el.style.setProperty('--p', pct.toFixed(2) + '%');
  }
  $$('[data-tab]').forEach((el) => { el.hidden = el.dataset.tab !== state.tab; });
  $('#stage').hidden = state.tab !== 'model';
  $('#stage-vector').hidden = state.tab !== 'vector';
  $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === state.tab));
  $('#vstack').className = (state.srcKind === 'bitmap' ? 'on ' : '') +
    'm-' + state.vView + (state.vMode === 'color' ? ' color' : '');
  $('#stage-vector').classList.toggle('color', state.vMode === 'color');
  $$('#vmode button').forEach((b) => b.classList.toggle('on', b.dataset.v === state.vMode));
  $$('[data-vmode]').forEach((el) => { el.hidden = el.dataset.vmode !== state.vMode; });
  const blurLabel = document.querySelector('[data-i18n="v.blur"]');
  if (blurLabel) blurLabel.textContent = t(state.vMode === 'color' ? 'v.blur_color' : 'v.blur');
  const blurHint = document.querySelector('[data-i18n="v.blur_hint"]');
  if (blurHint) blurHint.textContent = t(state.vMode === 'color' ? 'v.blur_hint_color' : 'v.blur_hint');
  syncPaletteChrome();
  syncIslandChrome();
  $('#vplaceholder').hidden = state.srcKind === 'bitmap';
  $$('[data-for]').forEach((el) => {
    el.hidden = el.dataset.for !== state.mode || state.tab !== 'model';
  });
  $('#bitmapBox').hidden = state.tab !== 'model' || state.traced
    || state.srcKind !== 'bitmap';
  const raster = state.srcKind === 'bitmap';
  $('#v_use').disabled = !raster || state.tracing;
  $('#v_download').disabled = !raster || state.vSaving;
  $$('[data-tab="vector"]').forEach((el) => el.classList.toggle('off', !raster));
  $('#vplaceholderText').textContent = state.srcKind === 'svg'
    ? t('ph.vector_svg') : t('ph.vector');
  $$('[data-need]').forEach((el) => {
    const n = el.dataset.need;
    const on = n === 'rect' ? $('#base_shape').value === 'rect' : $('#' + n).checked;
    el.classList.toggle('off', !on);
  });
  $('#modeHint').textContent = t(state.mode === 'plaque' ? 'hint.plaque' : 'hint.patch');
  const nz = $('#nozzle_mm');
  if (nz) $('#nozzleHint').textContent = t('nozzle.hint', { mm: numText(nz.value) });
  const spreadBox = $('#spreadBox');
  if (spreadBox) spreadBox.hidden = !$('#detail_press').checked;
  const spreadOut = $('#spread_mmO');
  if (spreadOut && $('#spread_mm') && $('#thickness_mm')) {
    const edge = +$('#spread_mm').value * (+$('#thickness_mm').value / 0.4);
    spreadOut.textContent = edge.toFixed(2);
  }
  const spreadHint = $('#spreadHint');
  if (spreadHint && spreadOut) {
    spreadHint.textContent = t('spread.hint', {
      mm: numText(spreadOut.textContent),
      pupil: numText(2.1),
      close: numText(1.1),
    });
  }
  syncHist();
}

/* --------------------------------------------------------------- preview */
function schedule(delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(render, delay === undefined ? 220 : delay);
}

let previewToken = 0;

function cancelPreview() {
  previewToken++;
  state.again = false;
}

async function render() {
  if (!state.job) return;
  const token = ++previewToken;
  const current = () => token === previewToken && !!state.job;
  $('#spinner').hidden = false;
  try {
    const out = await callHeavy('/api/preview', payload(), 'preview', current);
    if (!current() || !out || out.ignore) return;
    const d = await readHeavyJson(out, 'server_error');
    if (!current() || !d) return;
    $('#canvas').innerHTML = d.svg;
    $('#canvas').classList.add('on');
    $('#placeholder').hidden = true;
    $('#download').disabled = false;
    showStats(d.stats);
    showWarnings(d.warnings);
  } catch (e) {
    if (!current()) return;
    showWarnings([], e && e.error ? e : (e && e.message) || { error: 'server_error' });
    $('#download').disabled = true;
  } finally {
    if (token === previewToken) $('#spinner').hidden = true;
  }
}

function showStats(s) {
  state.lastStats = s || null;
  if (!s) { $('#stats').innerHTML = ''; return; }
  const u = units();
  const tiles = [
    [t('stat.size'), t('stat.size_v', { w: s.width_mm, h: s.height_mm, mm: u.mm })],
    [t('stat.height'), t('stat.height_v', { h: s.total_h_mm, mm: u.mm })],
    [t('stat.pieces'), String(s.pieces)],
  ];
  if (s.bridges) {
    tiles.push([t('stat.bridges'), t('stat.bridges_v', {
      n: s.bridges, gap: s.longest_gap_mm, mm: u.mm,
    })]);
  }
  tiles.push([t('stat.area'), t('stat.area_v', { a: s.area_cm2, cm2: u.cm2 })]);
  tiles.push([t('stat.volume'), t('stat.volume_v', {
    vol: s.volume_cm3, cm3: u.cm3, w: s.weight_g, g: u.g,
  })]);
  if (s.thin_pct > 3) tiles.push([
    t('stat.thin', { mm: numText(s.nozzle_mm == null ? 0.4 : s.nozzle_mm) }),
    `${s.thin_pct}%`, 'hot']);
  $('#stats').innerHTML = tiles
    .map(([k, v, kind]) => `<div class="s${kind ? ' ' + kind : ''}">${k}<b>${v}</b></div>`).join('');
}

function showWarnings(list, err) {
  state.lastWarnings = list || [];
  state.lastErr = err || null;
  const w = $('#warnings');
  w.innerHTML = (state.lastErr ? `<div class="warn err">${esc(formatErr(state.lastErr))}</div>` : '') +
    state.lastWarnings.map((item) => `<div class="warn">${esc(warnText(item))}</div>`).join('');
}

const esc = (s) => String(s).replace(/[<>&]/g, (c) =>
  ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));

/* ------------------------------------------------------------- vectorize */
function normHex(s) {
  const m = String(s || '').trim().match(/^#?([0-9a-fA-F]{6})$/);
  return m ? '#' + m[1].toLowerCase() : '';
}

function hideLoupe() {
  const loupe = $('#loupe');
  if (loupe) loupe.hidden = true;
}

function applyView() {
  const s = state.view;
  const el = $('#vstack');
  if (!el) return;
  if (s.z <= 1) {
    s.z = 1; s.x = 0; s.y = 0;
    el.style.transform = '';
    refreshIslandScale();
    return;
  }
  const vp = $('#vviewport');
  const limit = Math.max(vp.clientWidth, vp.clientHeight) * s.z;
  s.x = Math.max(-limit, Math.min(limit, s.x));
  s.y = Math.max(-limit, Math.min(limit, s.y));
  el.style.transform = `translate(${s.x}px, ${s.y}px) scale(${s.z})`;
  refreshIslandScale();
}

function zoomAt(clientX, clientY, nz) {
  const vp = $('#vviewport').getBoundingClientRect();
  const cx = clientX - (vp.left + vp.width / 2);
  const cy = clientY - (vp.top + vp.height / 2);
  const z = state.view.z;
  const k = nz / z;
  state.view.x = cx - (cx - state.view.x) * k;
  state.view.y = cy - (cy - state.view.y) * k;
  state.view.z = nz;
  applyView();
}

function resetTraceState() {
  state.palette = null;
  state.paletteCustom = false;
  state.swatch = 0;
  state.picking = false;
  state.sampleCanvas = null;
  state.view = { z: 1, x: 0, y: 0 };
  clearIslandEdit();
  hideLoupe();
  applyView();
  renderPalette();
  resetHist();
}

function syncPaletteChrome() {
  const hasPal = !!(state.palette && state.palette.length);
  const raster = state.srcKind === 'bitmap';
  const color = $('#v_swatch_color');
  const hide = $('#v_swatch_hide');
  if (color) color.disabled = !hasPal;
  if (hide) hide.disabled = !hasPal;
  const eye = $('#v_eyedrop');
  if (eye) {
    eye.disabled = !raster || !hasPal;
    eye.classList.toggle('on', state.picking);
    eye.setAttribute('aria-pressed', state.picking ? 'true' : 'false');
  }
  const auto = $('#v_palette_auto');
  if (auto) auto.disabled = !raster;
  const vp = $('#vviewport');
  if (vp) vp.classList.toggle('picking', state.picking);
  const hint = $('#v_pick_hint');
  if (hint) hint.textContent = t(state.picking ? 'v.picking' : 'v.palette_hint');
}

function renderPalette() {
  const box = $('#palette');
  if (!box) return;
  const pal = state.palette || [];
  if (state.swatch >= pal.length) state.swatch = 0;
  box.innerHTML = pal.map((s, i) => {
    const hex = normHex(s.hex) || '#000000';
    const pct = s.pct == null ? '' : ` · ${s.pct}%`;
    return `<button type="button" class="swatch${i === state.swatch ? ' sel' : ''}${s.on ? '' : ' off'}" data-i="${i}" style="--c:${hex}" title="${hex}${pct}"></button>`;
  }).join('');
  const cur = pal[state.swatch];
  const color = $('#v_swatch_color');
  const hide = $('#v_swatch_hide');
  const meta = $('#v_swatch_meta');
  if (color && cur) color.value = normHex(cur.hex) || '#000000';
  if (hide && cur) hide.checked = !cur.on;
  if (meta) {
    meta.textContent = cur
      ? `${normHex(cur.hex) || cur.hex} · ${cur.pct == null ? '…' : cur.pct + '%'}`
      : '';
  }
  syncPaletteChrome();
}

function adoptPalette(list) {
  if (!Array.isArray(list) || !list.length) return;
  const clean = list.map((c) => ({
    hex: normHex(c.hex) || '#000000',
    on: c.on !== false,
    pct: c.pct,
  }));
  if (state.paletteCustom && state.palette && state.palette.length === clean.length) {
    clean.forEach((c, i) => { state.palette[i].pct = c.pct; });
  } else if (!state.paletteCustom) {
    state.palette = clean;
  }
  renderPalette();
}

function hiColor(i) {
  const svg = $('#vcanvas svg');
  if (svg) {
    svg.classList.toggle('hi', i != null);
    svg.querySelectorAll('[data-i]').forEach((el) => {
      el.classList.toggle('hot', i != null && +el.dataset.i === i);
    });
  }
  $$('#palette .swatch').forEach((s) => {
    s.classList.toggle('hot', i != null && +s.dataset.i === i);
  });
}

function rasterPoint(ev) {
  const img = $('#vraster');
  if (!img || !img.naturalWidth) return null;
  const r = img.getBoundingClientRect();
  const nw = img.naturalWidth, nh = img.naturalHeight;
  const scale = Math.min(r.width / nw, r.height / nh);
  if (!(scale > 0)) return null;
  const dw = nw * scale, dh = nh * scale;
  const x = (ev.clientX - (r.left + (r.width - dw) / 2)) / scale;
  const y = (ev.clientY - (r.top + (r.height - dh) / 2)) / scale;
  if (x < 0 || y < 0 || x >= nw || y >= nh) return null;
  return { x: Math.floor(x), y: Math.floor(y), img };
}

function ensureSample(img) {
  if (state.sampleCanvas && state.sampleCanvas.width === img.naturalWidth
      && state.sampleCanvas.height === img.naturalHeight) {
    return state.sampleCanvas;
  }
  const c = document.createElement('canvas');
  c.width = img.naturalWidth;
  c.height = img.naturalHeight;
  c.getContext('2d', { willReadFrequently: true }).drawImage(img, 0, 0);
  state.sampleCanvas = c;
  return c;
}

function hexAt(pt) {
  const c = ensureSample(pt.img);
  const px = c.getContext('2d', { willReadFrequently: true })
    .getImageData(pt.x, pt.y, 1, 1).data;
  return '#' + [px[0], px[1], px[2]].map((v) => v.toString(16).padStart(2, '0')).join('');
}

function moveLoupe(ev) {
  const loupe = $('#loupe');
  const pt = rasterPoint(ev);
  if (!pt) { loupe.hidden = true; return; }
  const src = ensureSample(pt.img);
  const canvas = $('#loupeCanvas');
  const ctx = canvas.getContext('2d');
  const n = 11;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(src, pt.x - (n >> 1), pt.y - (n >> 1), n, n, 0, 0, canvas.width, canvas.height);
  const cell = canvas.width / n;
  ctx.strokeStyle = 'rgba(255,255,255,.95)';
  ctx.lineWidth = 1;
  ctx.strokeRect((n >> 1) * cell + 0.5, (n >> 1) * cell + 0.5, cell - 1, cell - 1);
  $('#loupeHex').textContent = hexAt(pt);
  const pad = 16;
  let left = ev.clientX + pad;
  let top = ev.clientY + pad;
  if (left + 100 > window.innerWidth) left = ev.clientX - 104;
  if (top + 120 > window.innerHeight) top = ev.clientY - 120;
  loupe.style.left = left + 'px';
  loupe.style.top = top + 'px';
  loupe.hidden = false;
}

function stopPicking() {
  state.picking = false;
  hideLoupe();
  syncPaletteChrome();
}

function clearIslandMarks() {
  clearTimeout(state.islandClickTimer);
  state.islandClickTimer = null;
  state.islandSel = new Set();
  state.islandHover = null;
  state.islandIndex = null;
  state.islandStamp = '';
  state.islandPx = 0;
  state.vEdited = false;
}

function clearIslandEdit() {
  clearIslandMarks();
  state.islandMode = false;
}

function syncIslandChrome() {
  const n = state.islandSel ? state.islandSel.size : 0;
  const btn = $('#v_islands');
  if (btn) {
    btn.disabled = state.srcKind !== 'bitmap';
    btn.classList.toggle('on', state.islandMode);
    btn.setAttribute('aria-pressed', state.islandMode ? 'true' : 'false');
  }
  const keep = $('#v_keep');
  const drop = $('#v_drop_islands');
  if (keep) keep.disabled = !n;
  if (drop) drop.disabled = !n;
  const vp = $('#vviewport');
  if (vp) vp.classList.toggle('islands', state.islandMode);
  const hint = $('#v_island_hint');
  if (hint) {
    hint.textContent = n
      ? t('v.island_n', { n })
      : t(state.islandMode ? 'v.island_pick' : 'v.island_hint');
  }
}

let _hitCtx = null;
function hitCtx() {
  if (!_hitCtx) _hitCtx = document.createElement('canvas').getContext('2d');
  return _hitCtx;
}

function inPath(d, x, y, rule) {
  if (!d) return false;
  return hitCtx().isPointInPath(new Path2D(d), x, y, rule || 'nonzero');
}

function polyOf(frag) {
  const tok = frag.match(/[MLCZ]|-?\d*\.?\d+(?:e[-+]?\d+)?/gi) || [];
  const poly = [];
  let i = 0, cx = 0, cy = 0;
  const line = (x, y) => { poly.push([x, y]); cx = x; cy = y; };
  while (i < tok.length) {
    const c = tok[i];
    if (c === 'M' || c === 'L') {
      line(+tok[++i], +tok[++i]);
      i++;
    } else if (c === 'C') {
      const x1 = +tok[++i], y1 = +tok[++i];
      const x2 = +tok[++i], y2 = +tok[++i];
      const x = +tok[++i], y = +tok[++i];
      const steps = 6;
      for (let s = 1; s <= steps; s++) {
        const t0 = s / steps, u = 1 - t0;
        poly.push([
          u * u * u * cx + 3 * u * u * t0 * x1 + 3 * u * t0 * t0 * x2 + t0 * t0 * t0 * x,
          u * u * u * cy + 3 * u * u * t0 * y1 + 3 * u * t0 * t0 * y2 + t0 * t0 * t0 * y,
        ]);
      }
      cx = x;
      cy = y;
      i++;
    } else i++;
  }
  if (poly.length > 1) {
    const a = poly[0], b = poly[poly.length - 1];
    if (Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-3) poly.pop();
  }
  return poly;
}

function polyArea(poly) {
  let a = 0;
  for (let i = 0, n = poly.length; i < n; i++) {
    const p = poly[i], q = poly[(i + 1) % n];
    a += p[0] * q[1] - q[0] * p[1];
  }
  return a / 2;
}

function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1];
    const xj = poly[j][0], yj = poly[j][1];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi))
      inside = !inside;
  }
  return inside;
}

function insidePoint(poly) {
  if (!poly.length) return null;
  let i = 0;
  for (let k = 1; k < poly.length; k++) {
    if (poly[k][0] < poly[i][0] || (poly[k][0] === poly[i][0] && poly[k][1] < poly[i][1])) i = k;
  }
  const a = poly[(i - 1 + poly.length) % poly.length];
  const b = poly[i];
  const c = poly[(i + 1) % poly.length];
  let vx = a[0] + c[0] - 2 * b[0];
  let vy = a[1] + c[1] - 2 * b[1];
  const len = Math.hypot(vx, vy) || 1;
  vx /= len;
  vy /= len;
  for (const eps of [0.4, 1.5, 4]) {
    for (const s of [1, -1]) {
      const x = b[0] + s * vx * eps;
      const y = b[1] + s * vy * eps;
      if (pointInPoly(x, y, poly)) return [x, y];
    }
  }
  let sx = 0, sy = 0;
  poly.forEach((p) => { sx += p[0]; sy += p[1]; });
  return [sx / poly.length, sy / poly.length];
}

function probeOf(frag, poly) {
  const cands = [];
  const ip = insidePoint(poly);
  if (ip) cands.push(ip);
  if (!poly.length) return null;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  poly.forEach((p) => {
    minx = Math.min(minx, p[0]); miny = Math.min(miny, p[1]);
    maxx = Math.max(maxx, p[0]); maxy = Math.max(maxy, p[1]);
  });
  cands.push([(minx + maxx) / 2, (miny + maxy) / 2]);
  for (const c of cands) {
    if (inPath(frag, c[0], c[1])) return c;
  }
  for (let gy = 1; gy <= 5; gy++) {
    for (let gx = 1; gx <= 5; gx++) {
      const x = minx + (maxx - minx) * gx / 6;
      const y = miny + (maxy - miny) * gy / 6;
      if (inPath(frag, x, y)) return [x, y];
    }
  }
  return null;
}

function sourcePaths() {
  const svg = $('#vcanvas svg');
  if (!svg) return [];
  return [...svg.querySelectorAll('path')].filter((p) => !p.closest('#islandOverlay'));
}

function islandsOf(el, pi) {
  const frags = (el.getAttribute('d') || '').trim().split(/(?=M)/).filter((s) => s.trim());
  const subs = frags.map((frag, si) => {
    const poly = polyOf(frag);
    return {
      si, frag, poly,
      area: Math.abs(polyArea(poly)),
      probe: null, depth: 0, parent: -1,
    };
  });
  subs.forEach((s) => { s.probe = s.poly.length >= 3 ? probeOf(s.frag, s.poly) : null; });
  const order = subs.map((_, i) => i).sort((a, b) => subs[b].area - subs[a].area);
  order.forEach((i) => {
    const s = subs[i];
    if (!s.probe) return;
    let best = -1, bestArea = Infinity;
    subs.forEach((o, j) => {
      if (j === i || o.area <= s.area + 1e-4 || o.area >= bestArea) return;
      if (inPath(o.frag, s.probe[0], s.probe[1])) { best = j; bestArea = o.area; }
    });
    s.parent = best;
    s.depth = best < 0 ? 0 : subs[best].depth + 1;
  });
  const islands = [];
  subs.forEach((s, i) => {
    if ((s.depth % 2) !== 0 || s.poly.length < 3) return;
    const holes = subs.filter((h) => h.parent === i && (h.depth % 2) === 1);
    const d = s.frag + holes.map((h) => h.frag).join('');
    islands.push({
      key: pi + ':' + i,
      pi, si: i,
      holeSis: holes.map((h) => h.si),
      d,
      label: labelPoint(d, s.poly),
    });
  });
  return islands;
}

function ensureIslands() {
  const paths = sourcePaths();
  const stamp = paths.map((p) => (p.getAttribute('d') || '').length).join(',');
  if (state.islandIndex && state.islandStamp === stamp) return state.islandIndex;
  const islands = [];
  paths.forEach((el, pi) => { islands.push(...islandsOf(el, pi)); });
  state.islandIndex = islands;
  state.islandStamp = stamp;
  return islands;
}

function svgPoint(clientX, clientY) {
  const svg = $('#vcanvas svg');
  if (!svg || !svg.createSVGPoint) return null;
  const pt = svg.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const p = pt.matrixTransform(ctm.inverse());
  return [p.x, p.y];
}

function islandAt(clientX, clientY) {
  const p = svgPoint(clientX, clientY);
  if (!p) return null;
  const islands = ensureIslands();
  for (let i = islands.length - 1; i >= 0; i--) {
    if (inPath(islands[i].d, p[0], p[1], 'evenodd')) return islands[i];
  }
  return null;
}

function labelPoint(d, poly) {
  if (!poly.length) return null;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  poly.forEach((p) => {
    minx = Math.min(minx, p[0]); miny = Math.min(miny, p[1]);
    maxx = Math.max(maxx, p[0]); maxy = Math.max(maxy, p[1]);
  });
  const cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
  if (inPath(d, cx, cy, 'evenodd')) return [cx, cy];
  let best = null, bestD = Infinity;
  const n = 8;
  for (let gy = 0; gy <= n; gy++) {
    for (let gx = 0; gx <= n; gx++) {
      const x = minx + (maxx - minx) * gx / n;
      const y = miny + (maxy - miny) * gy / n;
      if (!inPath(d, x, y, 'evenodd')) continue;
      const dist = Math.hypot(x - cx, y - cy);
      if (dist < bestD) { bestD = dist; best = [x, y]; }
    }
  }
  return best || [cx, cy];
}

function screenPx(svg) {
  const ctm = svg.getScreenCTM();
  if (!ctm) return 1;
  return 1 / (Math.hypot(ctm.a, ctm.b) || 1);
}

function refreshIslandScale() {
  if ((!state.islandSel || !state.islandSel.size) && !state.islandHover) return;
  const svg = $('#vcanvas svg');
  if (!svg) return;
  const px = screenPx(svg);
  if (state.islandPx && Math.abs(px - state.islandPx) / px < 0.04) return;
  paintIslandOverlay();
}

function paintIslandOverlay() {
  const svg = $('#vcanvas svg');
  if (!svg) return;
  const prev = svg.querySelector('#islandOverlay');
  if (prev) prev.remove();
  const islands = state.islandIndex || [];
  const hover = state.islandHover;
  const sel = state.islandSel;
  state.islandPx = screenPx(svg);
  if ((!sel || !sel.size) && !hover) return;
  const g = document.createElementNS(svg.namespaceURI, 'g');
  g.id = 'islandOverlay';
  g.setAttribute('pointer-events', 'none');
  const byKey = new Map(islands.map((isl) => [isl.key, isl]));
  const addPath = (isl, cls) => {
    const p = document.createElementNS(svg.namespaceURI, 'path');
    p.setAttribute('d', isl.d);
    p.setAttribute('fill-rule', 'evenodd');
    p.setAttribute('class', cls);
    g.appendChild(p);
  };
  let n = 0;
  if (sel) sel.forEach((key) => {
    const isl = byKey.get(key);
    if (!isl) return;
    n += 1;
    addPath(isl, 'island-sel');
    if (isl.label) addIslandBadge(g, svg, isl.label[0], isl.label[1], String(n));
  });
  if (hover && byKey.has(hover)) addPath(byKey.get(hover), 'island-hot');
  svg.appendChild(g);
}

function addIslandBadge(g, svg, x, y, text) {
  const px = state.islandPx || 1;
  const badge = document.createElementNS(svg.namespaceURI, 'g');
  badge.setAttribute('class', 'island-badge');
  badge.setAttribute('transform', `translate(${x} ${y}) scale(${px})`);
  const wide = text.length > 1;
  const c = document.createElementNS(svg.namespaceURI, 'circle');
  c.setAttribute('class', 'island-num');
  c.setAttribute('r', wide ? 15 : 11);
  badge.appendChild(c);
  const t = document.createElementNS(svg.namespaceURI, 'text');
  t.setAttribute('class', 'island-num-t');
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('dominant-baseline', 'central');
  t.textContent = text;
  badge.appendChild(t);
  g.appendChild(badge);
}

function hoverIsland(x, y) {
  if (!state.islandMode) return;
  const isl = islandAt(x, y);
  const key = isl && !state.islandSel.has(isl.key) ? isl.key : null;
  if (key === state.islandHover) return;
  state.islandHover = key;
  paintIslandOverlay();
}

function toggleIslandAt(x, y) {
  const isl = islandAt(x, y);
  if (!isl) return;
  if (state.islandSel.has(isl.key)) state.islandSel.delete(isl.key);
  else state.islandSel.add(isl.key);
  if (state.islandHover === isl.key) state.islandHover = null;
  paintIslandOverlay();
  syncIslandChrome();
}

function queueIslandClick(ev) {
  const x = ev.clientX, y = ev.clientY;
  clearTimeout(state.islandClickTimer);
  state.islandClickTimer = setTimeout(() => {
    state.islandClickTimer = null;
    if (!state.islandMode) return;
    toggleIslandAt(x, y);
  }, 250);
}

function applyIslandEdit(mode) {
  if (!state.islandSel.size) return;
  const islands = ensureIslands();
  const paths = sourcePaths();
  const byPath = new Map();
  islands.forEach((isl) => {
    if (!byPath.has(isl.pi)) byPath.set(isl.pi, []);
    byPath.get(isl.pi).push(isl);
  });
  let changed = false;
  byPath.forEach((group, pi) => {
    if (!group.some((isl) => state.islandSel.has(isl.key))) return;
    const el = paths[pi];
    if (!el) return;
    const frags = (el.getAttribute('d') || '').trim().split(/(?=M)/).filter((s) => s.trim());
    const drop = new Set();
    group.forEach((isl) => {
      const marked = state.islandSel.has(isl.key);
      const kill = mode === 'keep' ? !marked : marked;
      if (!kill) return;
      drop.add(isl.si);
      isl.holeSis.forEach((h) => drop.add(h));
    });
    if (!drop.size) return;
    const next = frags.filter((_, si) => !drop.has(si));
    changed = true;
    if (!next.length) el.remove();
    else el.setAttribute('d', next.join(''));
  });
  state.islandSel = new Set();
  state.islandHover = null;
  state.islandIndex = null;
  state.islandStamp = '';
  if (changed) state.vEdited = true;
  paintIslandOverlay();
  syncUI();
}

function vschedule(delay) {
  clearTimeout(state.vTimer);
  state.vTimer = setTimeout(vrender, delay === undefined ? 350 : delay);
}

function showVStats(s) {
  state.lastVStats = s || null;
  if (!s) { $('#vstats').innerHTML = ''; return; }
  const tiles = [
    [t('vstat.size'), t('vstat.size_v', { w: s.width_px, h: s.height_px })],
    [t('vstat.paths'), String(s.paths)],
    [t('vstat.segments'), String(s.segments)],
  ];
  if (s.colors != null) tiles.splice(1, 0, [t('vstat.colors'), String(s.colors)]);
  tiles.push([s.palette ? t('vstat.coverage') : t('vstat.ink'), `${s.ink_pct}%`]);
  tiles.push([t('vstat.svg'), t('vstat.svg_v', { kb: s.svg_kb, unit: t('unit.kb') })]);
  $('#vstats').innerHTML = tiles
    .map(([k, v]) => `<div class="s">${k}<b>${v}</b></div>`).join('');
}

function showVError(err) {
  state.lastVErr = err || null;
  $('#vwarnings').innerHTML = state.lastVErr
    ? `<div class="warn err">${esc(formatErr(state.lastVErr))}</div>` : '';
}

function showVHint() {
  const h = $('#v_hint');
  if (!state.vdl) { h.textContent = ''; return; }
  h.textContent = `${state.vdl.name} · ${(state.vdl.bytes / 1024).toFixed(1)} ${t('unit.kb')}`;
}

function showDlHint() {
  const h = $('#dlHint');
  if (!state.dl) { h.textContent = ''; return; }
  h.textContent = `${state.dl.name} · ${(state.dl.bytes / 1048576).toFixed(1)} ${t('unit.mb')}`;
}

function fileMetaText() {
  if (!state.srcKind) return;
  const kind = t(state.srcKind === 'svg' ? 'kind.svg' : 'kind.bitmap');
  let s = `${kind} · ${t('meta.islands', { n: state.islands })}`;
  if (state.traced) s = t('meta.traced', { src: s, n: state.tracedIslands });
  $('#fileMeta').textContent = s;
}

let traceToken = 0;

function cancelTrace() {
  traceToken++;
  state.vAgain = false;
}

async function vrender() {
  if (!state.srcJob || state.srcKind !== 'bitmap') return;
  const token = ++traceToken;
  const current = () => token === traceToken && state.srcKind === 'bitmap';
  $('#vspinner').hidden = false;
  try {
    const out = await callHeavy('/api/vectorize', {
      job: state.srcJob, params: vparams(),
    }, 'vector', current);
    if (!current() || !out || out.ignore) return;
    const d = await readHeavyJson(out, 'server_error');
    if (!current() || !d) return;
    $('#vcanvas').innerHTML = d.svg;
    clearIslandMarks();
    syncIslandChrome();
    if (d.palette) adoptPalette(d.palette);
    showVStats(d.stats);
    showVError(null);
  } catch (e) {
    if (!current()) return;
    showVError(e && e.error ? e : (e && e.message) || { error: 'server_error' });
    showVStats(null);
  } finally {
    if (token === traceToken) $('#vspinner').hidden = true;
  }
}

function editedSvgText() {
  const svg = $('#vcanvas svg');
  if (!svg) return '';
  const clone = svg.cloneNode(true);
  const over = clone.querySelector('#islandOverlay');
  if (over) over.remove();
  const body = new XMLSerializer().serializeToString(clone);
  return body.startsWith('<?xml') ? body : '<?xml version="1.0" encoding="UTF-8"?>\n' + body;
}

function saveBlob(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  state.vdl = { name, bytes: blob.size };
  showVHint();
}

let vdlToken = 0;

async function vdownload() {
  const btn = $('#v_download');
  const token = ++vdlToken;
  const current = () => token === vdlToken;
  state.vSaving = true;
  btn.disabled = true;
  try {
    if (state.vEdited) {
      const stem = ($('#fileName').textContent || 'art').replace(/\.[^.]+$/, '') || 'art';
      const text = editedSvgText();
      saveBlob(new Blob([text], { type: 'image/svg+xml' }), stem + '_edited.svg');
      return;
    }
    const out = await callHeavy('/api/vectorize/download', {
      job: state.srcJob, params: vparams(),
    }, 'vdownload', current);
    if (!current() || !out || out.ignore) return;
    const r = out.response;
    if (!r || !r.ok) throw asErr(r ? await readJson(r) : {}, 'server_error');
    const name = (r.headers.get('Content-Disposition') || '')
      .match(/filename="(.+?)"/)?.[1] || 'traced.svg';
    saveBlob(await r.blob(), name);
  } catch (e) {
    if (!current()) return;
    showVError(e && e.error ? e : (e && e.message) || { error: 'server_error' });
  } finally {
    if (current()) {
      state.vSaving = false;
      syncUI();
    }
  }
}

let vuseToken = 0;

async function vuse() {
  const btn = $('#v_use');
  const token = ++vuseToken;
  const current = () => token === vuseToken;
  state.tracing = true;
  btn.disabled = true;
  btn.textContent = t('tracing');
  try {
    if (state.vEdited) {
      const stem = ($('#fileName').textContent || 'art').replace(/\.[^.]+$/, '') || 'art';
      const fd = new FormData();
      fd.append('file', new File([editedSvgText()], stem + '_edited.svg', { type: 'image/svg+xml' }));
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const d = await readJson(r);
      if (!r.ok) throw asErr(d, 'server_error');
      state.job = d.job;
      state.traced = true;
      state.tracedIslands = d.islands;
      fileMetaText();
      setTab('model');
      render();
      return;
    }
    const out = await callHeavy('/api/vectorize/use', {
      job: state.srcJob, params: vparams(),
    }, 'vuse', current);
    if (!current() || !out || out.ignore) return;
    const d = await readHeavyJson(out, 'server_error');
    if (!current() || !d) return;
    state.job = d.job;          // the original raster stays in state.srcJob
    state.traced = true;
    state.tracedIslands = d.islands;
    fileMetaText();
    setTab('model');
    render();
  } catch (e) {
    if (!current()) return;
    showVError(e && e.error ? e : (e && e.message) || { error: 'server_error' });
  } finally {
    if (current()) {
      state.tracing = false;
      btn.textContent = t('v.use');
      syncUI();
    }
  }
}

function setTab(t) {
  state.tab = t;
  if (t !== 'vector') stopPicking();
  syncUI();
  if (t === 'vector') vschedule(0);
}

/* ---------------------------------------------------------------- upload */
function adoptJob(d) {
  cancelPreview();
  cancelTrace();
  clearQueue();
  state.job = d.job;
  state.srcJob = d.job;
  state.srcKind = d.kind;
  state.traced = false;
  state.islands = d.islands;
  state.tracedIslands = 0;
  state.lastVStats = null;
  state.lastVErr = null;
  state.vdl = null;
  resetTraceState();
  $('#dropInner').hidden = true;
  $('#fileInfo').hidden = false;
  $('#fileName').textContent = d.name;
  fileMetaText();
  if (d.kind === 'bitmap') {
    const th = d.threshold == null ? 128 : d.threshold;
    const inv = !!d.invert;
    $('#threshold').value = th;
    $('#v_threshold').value = th;
    $('#invert').checked = inv;
    $('#v_invert').checked = inv;
  }
  $('#vraster').src = d.kind === 'bitmap' ? `/api/source/${d.job}` : '';
  $('#vcanvas').innerHTML = '';
  showVStats(null);
  showVError(null);
  showVHint();
  syncUI();
}

async function upload(file) {
  if (!file) return;
  showWarnings([]);
  $('#spinner').hidden = false;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const d = await readJson(r);
    if (!r.ok) throw asErr(d, 'upload');
    adoptJob(d);
    setTab(d.kind === 'bitmap' ? 'vector' : 'model');
    if (state.tab === 'model') render();
    else $('#spinner').hidden = true;
  } catch (e) {
    showWarnings([], e && e.error ? e : (e && e.message) || { error: 'upload' });
    $('#spinner').hidden = true;
  }
}

/* -------------------------------------------------------------- download */
let exportToken = 0;

async function download() {
  const btn = $('#download');
  const token = ++exportToken;
  const current = () => token === exportToken;
  state.preparing = true;
  btn.disabled = true;
  btn.textContent = t('preparing');
  try {
    const out = await callHeavy('/api/export', payload({
      format: $('#format').value,
      part: state.mode === 'plaque' ? $('#part').value : 'all',
      combine: state.mode === 'plaque' && $('#combine').checked,
    }), 'export', current);
    if (!current() || !out || out.ignore) return;
    const r = out.response;
    if (!r || !r.ok) throw asErr(r ? await readJson(r) : {}, 'export_failed');
    const cd = r.headers.get('Content-Disposition') || '';
    const name = (cd.match(/filename="(.+?)"/) || [, 'model'])[1];
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    state.dl = { name, bytes: blob.size };
    showDlHint();
  } catch (e) {
    if (!current()) return;
    showWarnings(state.lastWarnings, e && e.error ? e : (e && e.message) || { error: 'export_failed' });
  } finally {
    if (current()) {
      state.preparing = false;
      btn.textContent = t('dl.button');
      btn.disabled = false;
    }
  }
}

/* ---------------------------------------------------------------- history */
const HIST_MAX = 40;
const HIST_SKIP = new Set([
  'format', 'part', 'combine', 'file', 'lang',
  'pick', 'reset', 'undo', 'redo',
  'v_use', 'v_download', 'v_eyedrop', 'v_islands',
]);
const hist = { past: [], future: [], pending: null, gid: null, lock: false };

function histId(el) {
  if (el.id) return el.id;
  const host = el.parentElement && el.parentElement.id;
  return (host || 'act') + ':' + (el.dataset.v || '');
}

function histTrack(el) {
  if (!el || el.disabled || el.classList.contains('swatch')) return false;
  if (el.type === 'file' || HIST_SKIP.has(el.id)) return false;
  return el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'BUTTON';
}

function traceSvg() {
  const svg = $('#vcanvas svg');
  if (!svg) return '';
  const clone = svg.cloneNode(true);
  const over = clone.querySelector('#islandOverlay');
  if (over) over.remove();
  clone.classList.remove('hi');
  clone.querySelectorAll('.hot').forEach((el) => el.classList.remove('hot'));
  return clone.outerHTML;
}

function captureEdit() {
  const controls = {};
  $$('#panel input, #panel select').forEach((el) => {
    if (!el.id || el.type === 'file' || HIST_SKIP.has(el.id)) return;
    controls[el.id] = el.type === 'checkbox' ? el.checked : el.value;
  });
  return {
    controls,
    mode: state.mode,
    vMode: state.vMode,
    palette: state.palette
      ? state.palette.map((s) => ({ hex: s.hex, on: s.on, pct: s.pct })) : null,
    paletteCustom: state.paletteCustom,
    swatch: state.swatch,
    vEdited: !!state.vEdited,
    svg: traceSvg(),
  };
}

function editSig(part, s) {
  const controls = {};
  Object.keys(s.controls).forEach((k) => {
    if (part === 'vector' ? k.startsWith('v_') : !k.startsWith('v_')) controls[k] = s.controls[k];
  });
  if (part === 'vector') {
    return JSON.stringify({
      controls, vMode: s.vMode, palette: s.palette, paletteCustom: s.paletteCustom,
      vEdited: s.vEdited, svg: s.vEdited ? s.svg : '',
    });
  }
  return JSON.stringify({ controls, mode: s.mode });
}

function sameEdit(a, b) {
  return editSig('vector', a) === editSig('vector', b)
    && editSig('model', a) === editSig('model', b);
}

function syncHist() {
  const undo = $('#undo');
  const redo = $('#redo');
  if (!undo || !redo) return;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  undo.disabled = !hist.past.length;
  redo.disabled = !hist.future.length;
  undo.title = t('edit.undo') + (mac ? ' (⌘Z)' : ' (Ctrl+Z)');
  redo.title = t('edit.redo') + (mac ? ' (⇧⌘Z)' : ' (Ctrl+Y)');
  undo.setAttribute('aria-label', undo.title);
  redo.setAttribute('aria-label', redo.title);
}

function resetHist() {
  hist.past = [];
  hist.future = [];
  hist.pending = null;
  hist.gid = null;
  syncHist();
}

function beginHist(id) {
  if (hist.lock || !id) return;
  if (hist.gid === id && hist.pending) return;
  if (hist.pending) sealHist();
  hist.pending = captureEdit();
  hist.gid = id;
}

function sealHist() {
  if (hist.lock || !hist.pending) {
    hist.pending = null;
    hist.gid = null;
    return;
  }
  const now = captureEdit();
  if (!sameEdit(hist.pending, now)) {
    hist.past.push(hist.pending);
    if (hist.past.length > HIST_MAX) hist.past.shift();
    hist.future = [];
  }
  hist.pending = null;
  hist.gid = null;
  syncHist();
}

function restoreEdit(snap) {
  const cur = captureEdit();
  const vectorChanged = editSig('vector', cur) !== editSig('vector', snap);
  const modelChanged = editSig('model', cur) !== editSig('model', snap);
  hist.lock = true;
  stopPicking();
  try {
    Object.keys(snap.controls).forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!snap.controls[id];
      else el.value = snap.controls[id];
    });
    state.mode = snap.mode;
    state.vMode = snap.vMode;
    state.palette = snap.palette
      ? snap.palette.map((s) => ({ hex: s.hex, on: s.on, pct: s.pct })) : null;
    state.paletteCustom = snap.paletteCustom;
    state.swatch = snap.swatch;
    state.islandSel = new Set();
    state.islandHover = null;
    state.islandIndex = null;
    state.islandStamp = '';
    state.islandPx = 0;
    state.vEdited = !!snap.vEdited;
    if (vectorChanged && snap.svg) $('#vcanvas').innerHTML = snap.svg;
    renderPalette();
    paintIslandOverlay();
    syncUI();
  } finally {
    hist.lock = false;
  }
  if (vectorChanged) {
    cancelTrace();
    if (!(snap.vEdited && snap.svg)) vschedule(0);
  }
  if (modelChanged) {
    cancelPreview();
    schedule(0);
  }
  syncHist();
}

function undoEdit() {
  sealHist();
  if (!hist.past.length) return;
  hist.future.push(captureEdit());
  restoreEdit(hist.past.pop());
}

function redoEdit() {
  sealHist();
  if (!hist.future.length) return;
  hist.past.push(captureEdit());
  restoreEdit(hist.future.pop());
}

/* ------------------------------------------------------------------ wire */
$('#pick').onclick = () => $('#file').click();
$('#dropInner').addEventListener('click', (e) => {
  if (e.target.closest('button, a, input')) return;
  $('#file').click();
});
const loadSample = async (name, type) => {
  const r = await fetch('samples/' + name);
  upload(new File([await r.blob()], name, { type }));
};
$('#sample').onclick = (e) => { e.preventDefault(); loadSample('cats.svg', 'image/svg+xml'); };
$('#samplePng').onclick = (e) => { e.preventDefault(); loadSample('cats.png', 'image/png'); };
$('#file').onchange = (e) => upload(e.target.files[0]);
$('#reset').onclick = () => {
  cancelPreview();
  cancelTrace();
  clearQueue();
  $('#spinner').hidden = true;
  $('#vspinner').hidden = true;
  state.job = state.srcJob = null;
  state.srcKind = null;
  state.traced = false;
  state.islands = 0;
  state.tracedIslands = 0;
  state.lastStats = null;
  state.lastVStats = null;
  state.dl = null;
  state.vdl = null;
  resetTraceState();
  $('#file').value = '';
  $('#vcanvas').innerHTML = '';
  $('#vraster').src = '';
  syncUI();
  $('#dropInner').hidden = false;
  $('#fileInfo').hidden = true;
  $('#bitmapBox').hidden = true;
  $('#canvas').classList.remove('on');
  $('#placeholder').hidden = false;
  $('#download').disabled = true;
  showStats(null);
  showWarnings([]);
  showVStats(null);
  showVError(null);
  showVHint();
  showDlHint();
};

const drop = $('#drop');
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', (e) => upload(e.dataTransfer.files[0]));
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

$$('#mode button').forEach((b) => {
  b.onclick = () => {
    $$('#mode button').forEach((x) => x.classList.toggle('on', x === b));
    state.mode = b.dataset.v;
    syncUI();
    schedule(0);
  };
});

$$('#tabs button').forEach((b) => { b.onclick = () => setTab(b.dataset.t); });
$$('#vview button').forEach((b) => {
  b.onclick = () => {
    $$('#vview button').forEach((x) => x.classList.toggle('on', x === b));
    state.vView = b.dataset.v;
    syncUI();
  };
});

const NO_RERENDER = new Set(['format', 'part', 'combine']);
$$('#panel input, #panel select').forEach((el) => {
  el.addEventListener('input', () => {
    if (el.id === 'v_colors') state.paletteCustom = false;
    else if (el.id === 'v_swatch_color' && state.palette && state.palette[state.swatch]) {
      const s = state.palette[state.swatch];
      s.hex = normHex(el.value) || s.hex;
      state.paletteCustom = true;
      const b = document.querySelector('#palette .swatch.sel');
      if (b) b.style.setProperty('--c', s.hex);
      const meta = $('#v_swatch_meta');
      if (meta) meta.textContent = `${s.hex} · ${s.pct == null ? '…' : s.pct + '%'}`;
    } else if (el.id === 'v_swatch_hide' && state.palette && state.palette[state.swatch]) {
      const s = state.palette[state.swatch];
      s.on = !el.checked;
      state.paletteCustom = true;
      const b = document.querySelector('#palette .swatch.sel');
      if (b) b.classList.toggle('off', !s.on);
    }
    syncUI();
    if (NO_RERENDER.has(el.id)) return;
    const live = el.type === 'range' || el.type === 'color';
    if (el.id.startsWith('v_')) vschedule(live ? 400 : 0);
    else schedule(live ? 260 : 0);
  });
});

$$('#vmode button').forEach((b) => {
  b.onclick = () => {
    state.vMode = b.dataset.v;
    if (state.vMode !== 'color') stopPicking();
    syncUI();
    vschedule(0);
  };
});

$('#palette').addEventListener('click', (e) => {
  const b = e.target.closest('.swatch');
  if (!b) return;
  state.swatch = +b.dataset.i;
  renderPalette();
});
$('#palette').addEventListener('pointerover', (e) => {
  const b = e.target.closest('.swatch');
  hiColor(b ? +b.dataset.i : null);
});
$('#palette').addEventListener('pointerleave', () => hiColor(null));

$('#v_eyedrop').onclick = () => {
  if (!state.palette || !state.palette.length) return;
  state.picking = !state.picking;
  if (state.picking) {
    state.islandMode = false;
    state.islandHover = null;
    paintIslandOverlay();
  }
  if (!state.picking) hideLoupe();
  else if (state.vView === 'vector') {
    state.vView = 'raster';
    $$('#vview button').forEach((x) => x.classList.toggle('on', x.dataset.v === state.vView));
  }
  syncUI();
};
$('#v_palette_auto').onclick = () => {
  state.paletteCustom = false;
  vschedule(0);
};
$('#v_islands').onclick = () => {
  state.islandMode = !state.islandMode;
  if (state.islandMode) {
    stopPicking();
    if (state.vView === 'raster') state.vView = 'both';
    ensureIslands();
  } else {
    state.islandHover = null;
  }
  paintIslandOverlay();
  syncUI();
};
$('#v_keep').onclick = () => applyIslandEdit('keep');
$('#v_drop_islands').onclick = () => applyIslandEdit('drop');
$('#v_reset').onclick = () => {
  stopPicking();
  clearIslandEdit();
  state.vMode = 'mono';
  state.palette = null;
  state.paletteCustom = false;
  state.swatch = 0;
  state.view = { z: 1, x: 0, y: 0 };
  applyView();
  $$('[data-tab="vector"] input').forEach((el) => {
    if (el.type === 'color' || el.type === 'file') return;
    if (el.type === 'checkbox') el.checked = el.defaultChecked;
    else el.value = el.defaultValue;
  });
  renderPalette();
  syncUI();
  vschedule(0);
};
const vp = $('#vviewport');
let ptr = null;
vp.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  ptr = {
    x: e.clientX, y: e.clientY, ox: state.view.x, oy: state.view.y,
    moved: false, id: e.pointerId,
  };
  vp.setPointerCapture(e.pointerId);
  e.preventDefault();
});
vp.addEventListener('pointermove', (e) => {
  if (state.picking) moveLoupe(e);
  if (state.islandMode && !(ptr && ptr.moved)) hoverIsland(e.clientX, e.clientY);
  if (!ptr || ptr.id !== e.pointerId) return;
  const dx = e.clientX - ptr.x;
  const dy = e.clientY - ptr.y;
  if (Math.hypot(dx, dy) > 4) ptr.moved = true;
  if (ptr.moved && !state.picking) {
    state.view.x = ptr.ox + dx;
    state.view.y = ptr.oy + dy;
    vp.classList.add('panning');
    applyView();
  }
});
vp.addEventListener('pointerup', (e) => {
  if (!ptr || ptr.id !== e.pointerId) return;
  const moved = ptr.moved;
  ptr = null;
  vp.classList.remove('panning');
  if (moved) return;
  if (state.picking) {
    const pt = rasterPoint(e);
    if (!pt || !state.palette || !state.palette.length) return;
    beginHist('eyedrop');
    const i = Math.min(state.swatch, state.palette.length - 1);
    state.swatch = i;
    state.palette[i].hex = hexAt(pt);
    state.palette[i].on = true;
    state.paletteCustom = true;
    stopPicking();
    renderPalette();
    sealHist();
    vschedule(0);
    return;
  }
  if (state.islandMode) {
    queueIslandClick(e);
    return;
  }
  const hit = e.target.closest && e.target.closest('[data-i]');
  if (hit && state.vMode === 'color') {
    state.swatch = +hit.dataset.i;
    renderPalette();
  }
});
vp.addEventListener('pointercancel', () => {
  ptr = null;
  vp.classList.remove('panning');
});
vp.addEventListener('pointerleave', () => {
  if (state.picking) hideLoupe();
  if (state.islandHover) {
    state.islandHover = null;
    paintIslandOverlay();
  }
});
vp.addEventListener('dblclick', () => {
  if (state.islandMode) {
    clearTimeout(state.islandClickTimer);
    state.islandClickTimer = null;
    return;
  }
  state.view = { z: 1, x: 0, y: 0 };
  applyView();
});
vp.addEventListener('wheel', (e) => {
  e.preventDefault();
  let nz = state.view.z * (e.deltaY < 0 ? 1.12 : 1 / 1.12);
  nz = Math.max(1, Math.min(8, nz));
  if (nz < 1.04) nz = 1;
  if (nz === 1) {
    state.view = { z: 1, x: 0, y: 0 };
    applyView();
    return;
  }
  zoomAt(e.clientX, e.clientY, nz);
}, { passive: false });
$('#vcanvas').addEventListener('pointerover', (e) => {
  if (state.picking || state.islandMode) return;
  const hit = e.target.closest && e.target.closest('[data-i]');
  hiColor(hit ? +hit.dataset.i : null);
});
$('#vcanvas').addEventListener('pointerleave', () => hiColor(null));
window.addEventListener('keydown', (e) => {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && !e.altKey && (e.key === 'z' || e.key === 'Z')) {
    e.preventDefault();
    if (e.shiftKey) redoEdit();
    else undoEdit();
    return;
  }
  if (mod && !e.shiftKey && (e.key === 'y' || e.key === 'Y')) {
    e.preventDefault();
    redoEdit();
    return;
  }
  if (e.key !== 'Escape') return;
  if (state.picking) stopPicking();
  else if (state.islandMode) {
    state.islandMode = false;
    state.islandHover = null;
    paintIslandOverlay();
    syncUI();
  }
});
$('#undo').onclick = () => undoEdit();
$('#redo').onclick = () => redoEdit();
$('#panel').addEventListener('pointerdown', (e) => {
  const el = e.target.closest('input, select, button');
  if (!histTrack(el)) return;
  beginHist(histId(el));
}, true);
$('#panel').addEventListener('focusin', (e) => {
  const el = e.target;
  if (!histTrack(el) || el.tagName === 'BUTTON') return;
  beginHist(el.id);
});
$('#panel').addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const el = e.target;
  if (el && el.tagName === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) {
    if (histTrack(el)) beginHist(histId(el));
    return;
  }
  const nudge = e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp'
    || e.key === 'ArrowDown' || e.key === ' ' || e.key === 'PageUp' || e.key === 'PageDown'
    || e.key === 'Home' || e.key === 'End';
  if (!nudge || !el || !el.id || !histTrack(el)) return;
  beginHist(el.id);
}, true);
$('#panel').addEventListener('change', (e) => {
  if (e.target && e.target.id === 'file') return;
  sealHist();
});
$('#panel').addEventListener('focusout', (e) => {
  const el = e.target;
  if (!el || el.type === 'color') return;
  if (hist.gid && hist.gid === (el.id || histId(el))) sealHist();
});
$('#panel').addEventListener('click', (e) => {
  const el = e.target.closest('button');
  if (!el || !hist.gid || hist.gid !== histId(el)) return;
  sealHist();
});
$('#vraster').addEventListener('load', () => { state.sampleCanvas = null; });

$('#download').onclick = download;
$('#v_download').onclick = vdownload;
$('#v_use').onclick = vuse;
$('#lang').onchange = () => {
  const v = $('#lang').value;
  try { localStorage.setItem(LANG_KEY, v); } catch (e) { /* private mode */ }
  applyLang(v);
};
applyLang(bootLang());
