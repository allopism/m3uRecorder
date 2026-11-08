'use strict';

// ════════════════════════════════════════════
//  Base: fetch con sesión, utilidades y avisos
// ════════════════════════════════════════════

// Si la sesión caduca, cualquier llamada a la API devuelve 401: volver al login
const _nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _nativeFetch(...args);
  if (res.status === 401) window.location.href = '/login';
  return res;
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const M3U_FILE = document.body.dataset.m3uFile || 'lista.m3u';

const state = {
  view: 'channels',
  status: null,
  channels: [],
  group: '',
  query: '',
  history: [],
  schedules: [],
  vpnCountries: null,
  wasRunning: false,
  sheet: null,
};

function esc(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const pad2 = n => String(n).padStart(2, '0');

function hms(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  return `${pad2(Math.floor(s / 3600))}:${pad2(Math.floor((s % 3600) / 60))}:${pad2(s % 60)}`;
}

function shortDuration(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(sec)}` : `${m}:${pad2(sec)}`;
}

function formatBytes(bytes) {
  let b = Number(bytes) || 0, i = 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${b.toLocaleString('es-ES', { maximumFractionDigits: i >= 3 ? 1 : 0 })} ${units[i]}`;
}

const fmtDay = d => d.toLocaleDateString('es-ES', { weekday: 'short', day: 'numeric', month: 'short' });
const fmtTime = d => d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
const stripExt = name => String(name || '').replace(/\.[a-z0-9]{2,4}$/i, '');

function countdownText(target) {
  const secs = Math.round((target - Date.now()) / 1000);
  if (secs <= 0) return 'Empezando…';
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `en ${d} d ${h} h`;
  if (h > 0) return `en ${h} h ${m} min`;
  return `en ${m}:${pad2(secs % 60)}`;
}

function icon(name, cls = '') {
  return `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = icon(type === 'ok' ? 'check' : type === 'error' ? 'alert' : 'clock') + `<span>${esc(msg)}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), type === 'error' ? 6000 : 3500);
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* respuesta sin JSON */ }
  if (!res.ok && !data.error) data.error = `Error ${res.status}`;
  return data;
}

function postForm(url, fields) {
  const fd = new FormData();
  Object.entries(fields || {}).forEach(([k, v]) => fd.append(k, v ?? ''));
  return api(url, { method: 'POST', body: fd });
}

function copyText(text, label) {
  const done = () => toast(label ? `Enlace copiado: ${label}` : 'Enlace copiado', 'ok');
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => toast('No se pudo copiar', 'error'));
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('No se pudo copiar', 'error'); }
  ta.remove();
}

// ════════════════════════════════════════════
//  Logos y monogramas
// ════════════════════════════════════════════
const MONO_HUES = [215, 165, 22, 275, 340, 42, 190, 125, 250, 5];
const SKIP_WORDS = /^(hd|sd|fhd|uhd|4k|the|la|el|los|las|de|del|tv)$/i;

function hueFor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return MONO_HUES[h % MONO_HUES.length];
}

function initials(name) {
  const words = String(name || '?').replace(/[([].*?[)\]]/g, ' ').split(/[\s\-_.·|/]+/).filter(Boolean);
  const useful = words.filter(w => !SKIP_WORDS.test(w));
  // "Radio Jazz 101" -> RJ, pero "Ciencia TV" -> CT (si quitar palabras deja solo una, no se quitan)
  const picked = (useful.length >= 2 ? useful : words).slice(0, 2);
  return picked.map(w => /^\d+$/.test(w) ? w.slice(0, 2) : w[0]).join('').toUpperCase().slice(0, 3) || '?';
}

function monoTile(name) {
  return `<div class="logo-tile mono-tile" style="--h:${hueFor(name)}">${esc(initials(name))}</div>`;
}

function logoTile(logoId, name) {
  if (!logoId) return monoTile(name);
  return `<div class="logo-tile"><img src="/logo/${encodeURIComponent(logoId)}" alt="" loading="lazy" decoding="async" data-logo data-name="${esc(name)}"></div>`;
}

// Logos o miniaturas que fallan al cargar: sustituirlos sin dejar el icono de imagen rota
document.addEventListener('error', e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  if (img.hasAttribute('data-logo')) {
    img.closest('.logo-tile').outerHTML = monoTile(img.dataset.name || '?');
  } else if (img.hasAttribute('data-thumb')) {
    const fallback = img.parentElement.querySelector('.thumb-fallback');
    img.remove();
    if (fallback) fallback.hidden = false;
  }
}, true);

// ════════════════════════════════════════════
//  Tema claro / oscuro
// ════════════════════════════════════════════
const THEMES = ['auto', 'light', 'dark'];
const THEME_INFO = { auto: ['auto', 'Tema: automático'], light: ['sun', 'Tema: claro'], dark: ['moon', 'Tema: oscuro'] };

function applyTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const btn = $('#themeBtn');
  btn.innerHTML = icon(THEME_INFO[theme][0]);
  btn.title = THEME_INFO[theme][1];
  btn.dataset.theme = theme;
}

function currentTheme() {
  try { return localStorage.getItem('theme') || 'auto'; } catch (e) { return 'auto'; }
}

$('#themeBtn').addEventListener('click', () => {
  const next = THEMES[(THEMES.indexOf($('#themeBtn').dataset.theme || 'auto') + 1) % THEMES.length];
  try { localStorage.setItem('theme', next); } catch (e) { /* sin almacenamiento: solo esta visita */ }
  applyTheme(next);
});
applyTheme(currentTheme());

// ════════════════════════════════════════════
//  Navegación
// ════════════════════════════════════════════
const HASH_VIEWS = { '#canales': 'channels', '#grabaciones': 'recordings' };

function showView(view) {
  state.view = view;
  $$('.view').forEach(v => { v.hidden = v.dataset.view !== view; });
  $$('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.nav === view));
  if (view === 'recordings') loadRecordings();
  if (view === 'channels' && !state.channels.length) loadChannels();
}

window.addEventListener('hashchange', () => showView(HASH_VIEWS[location.hash] || 'channels'));

// ════════════════════════════════════════════
//  Estado del servidor y barra de grabación
// ════════════════════════════════════════════
async function refreshStatus() {
  let data;
  try { data = await api('/status'); } catch (e) { return; }
  if (data.error) return;
  const prev = state.status;
  state.status = data;
  renderVpn(data.vpn);
  renderLiveBar(data);

  const badge = $('#upcomingBadge');
  badge.textContent = data.schedules_pending;
  badge.hidden = !data.schedules_pending;

  if (data.running) {
    state.wasRunning = true;
  } else if (state.wasRunning) {
    state.wasRunning = false;
    const r = data.last_result;
    if (r && r.status === 'ok') toast('Grabación terminada', 'ok');
    else if (r && r.status === 'stopped') toast('Grabación detenida', 'info');
    else if (r) toast(`La grabación falló: ${r.reason || 'motivo desconocido'}`, 'error');
    if (state.view === 'recordings') loadRecordings();
  }
  if (prev && state.view === 'recordings' &&
      (prev.history_count !== data.history_count || prev.schedules_pending !== data.schedules_pending)) {
    loadRecordings();
  }
}

function renderLiveBar(s) {
  const bar = $('#liveBar');
  bar.hidden = !s.running;
  if (!s.running) return;

  const title = s.channel || stripExt(s.filename) || 'Grabación';
  $('#liveTitle').textContent = `Grabando · ${title}`;
  $('#liveThumb').innerHTML = logoTile(s.logo_id, title);

  const progress = String(s.progress || '');
  const phase = /^(\[Programada\] )?(Guardando|Preparando|Reconectando|🌍)/.test(progress)
    ? progress.replace('[Programada] ', '') : '';
  let pct = 0, timeText = '';
  if (s.start_time && s.duration_requested) {
    const elapsed = Math.max(0, (Date.now() - new Date(s.start_time).getTime()) / 1000);
    const total = Math.max(1, Number(s.duration_requested));
    pct = Math.min(100, (elapsed / total) * 100);
    timeText = `${hms(elapsed)} / ${hms(total)}`;
  }
  const parts = [s.mode === 'scheduled' ? 'Programada' : 'En directo'];
  if (phase) parts.push(phase); else if (timeText) parts.push(timeText);
  if (s.vpn_country) parts.push(s.vpn_country);
  $('#liveSub').textContent = parts.join(' · ');
  $('#liveProgress').style.width = `${pct.toFixed(1)}%`;
}

$('#stopBtn').addEventListener('click', async () => {
  const data = await postForm('/stop');
  toast(data.message || 'Deteniendo…', 'info');
  setTimeout(refreshStatus, 800);
});

// ════════════════════════════════════════════
//  VPN
// ════════════════════════════════════════════
function fillCountrySelect(select, firstLabel) {
  const data = state.vpnCountries || { featured: [], others: [] };
  select.innerHTML = `<option value="">${esc(firstLabel)}</option>`;
  const fav = document.createElement('optgroup');
  fav.label = 'Frecuentes';
  data.featured.forEach(c => fav.appendChild(new Option(c.label, c.value)));
  select.appendChild(fav);
  if (data.others.length) {
    const all = document.createElement('optgroup');
    all.label = 'Todos';
    data.others.forEach(c => all.appendChild(new Option(c, c)));
    select.appendChild(all);
  }
}

async function loadVpnCountries() {
  state.vpnCountries = await api('/api/vpn/countries');
  fillCountrySelect($('#vpnGlobalSelect'), 'Elegir país…');
  fillCountrySelect($('#fVpn'), 'El país actual de la VPN');
}

function renderVpn(vpn) {
  const enabled = !!(vpn && vpn.enabled !== false);
  $('#vpnBox').hidden = !enabled;
  $('#vpnField').hidden = !enabled;
  if (!enabled) return;
  const country = vpn.configured_country || vpn.ip_country || 'VPN';
  const dot = $('#vpnDot');
  dot.className = 'dot' + (vpn.switching ? ' busy' : vpn.public_ip ? ' on' : '');
  $('#vpnLabel').textContent = vpn.switching ? `${country}…` : country;
  $('#vpnDetails').textContent = vpn.switching
    ? `Conectando a ${country}…`
    : vpn.public_ip
      ? `Conectada · IP ${vpn.public_ip}${vpn.ip_city ? ` (${vpn.ip_city})` : ''}`
      : vpn.available ? 'Gluetun responde, pero aún no hay conexión.' : 'No hay conexión con Gluetun.';
}

$('#vpnBtn').addEventListener('click', e => {
  e.stopPropagation();
  const pop = $('#vpnPopover');
  pop.hidden = !pop.hidden;
  $('#vpnBtn').setAttribute('aria-expanded', String(!pop.hidden));
});
document.addEventListener('click', e => {
  if (!e.target.closest('#vpnBox')) { $('#vpnPopover').hidden = true; $('#vpnBtn').setAttribute('aria-expanded', 'false'); }
});

$('#vpnGlobalSelect').addEventListener('change', async e => {
  const country = e.target.value;
  if (!country) return;
  toast(`Cambiando la VPN a ${country}…`, 'info');
  const data = await postForm('/api/vpn/country', { country });
  if (data.error) toast(data.error, 'error');
  else toast(data.message || `Conectada a ${country}`, 'ok');
  e.target.value = '';
  $('#vpnPopover').hidden = true;
  refreshStatus();
});

// ════════════════════════════════════════════
//  Canales
// ════════════════════════════════════════════
async function loadGroups() {
  const groups = await api('/api/channels/groups');
  const chips = ['<button type="button" class="chip active" data-group="">Todos</button>']
    .concat((Array.isArray(groups) ? groups : []).map(g => `<button type="button" class="chip" data-group="${esc(g)}">${esc(g)}</button>`));
  $('#groupChips').innerHTML = chips.join('');
  $('#groupChips').hidden = !groups.length;
}

async function loadChannels() {
  const params = new URLSearchParams({ q: state.query, group: state.group });
  const data = await api('/api/channels?' + params);
  state.channels = Array.isArray(data) ? data : [];
  renderChannels();
}

function renderChannels() {
  const grid = $('#channelGrid');
  const list = state.channels;
  const filtered = state.query || state.group;

  if (!list.length) {
    $('#channelsInfo').textContent = '';
    grid.innerHTML = filtered
      ? `<div class="empty">${icon('search')}<strong>Sin resultados</strong><span>Prueba con otro nombre o categoría.</span></div>`
      : `<div class="empty">${icon('tv')}<strong>No hay lista de canales</strong>
           <span>Coloca un archivo M3U en <code>${esc(M3U_FILE)}</code> y pulsa «Recargar lista», o graba pegando una URL.</span>
           <button type="button" class="btn btn-primary" data-action="manual">${icon('link')}<span>Pegar URL</span></button></div>`;
    return;
  }
  $('#channelsInfo').textContent = list.length >= 100 && !filtered
    ? `Mostrando ${list.length} canales · usa el buscador para encontrar el resto`
    : `${list.length} ${list.length === 1 ? 'canal' : 'canales'}`;
  grid.innerHTML = list.map((ch, i) => `
    <div class="channel" role="button" tabindex="0" data-channel="${i}" aria-label="Grabar ${esc(ch.name)}">
      ${logoTile(ch.logo_id, ch.name)}
      <span class="channel-name">${esc(ch.name)}</span>
      ${ch.group ? `<span class="channel-group">${esc(ch.group)}</span>` : ''}
      <button type="button" class="channel-copy" data-copy="${i}" title="Copiar enlace">${icon('copy')}</button>
    </div>`).join('');
}

let searchTimer = null;
$('#channelSearch').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.query = e.target.value.trim(); loadChannels(); }, 250);
});

$('#groupChips').addEventListener('click', e => {
  const chip = e.target.closest('[data-group]');
  if (!chip) return;
  state.group = chip.dataset.group;
  $$('#groupChips .chip').forEach(c => c.classList.toggle('active', c === chip));
  loadChannels();
});

$('#channelGrid').addEventListener('click', e => {
  const copy = e.target.closest('[data-copy]');
  if (copy) {
    e.stopPropagation();
    const ch = state.channels[+copy.dataset.copy];
    if (ch) copyText(ch.url, ch.name);
    return;
  }
  if (e.target.closest('[data-action="manual"]')) { openSheet(null); return; }
  const card = e.target.closest('[data-channel]');
  if (card) openSheet(state.channels[+card.dataset.channel]);
});
$('#channelGrid').addEventListener('keydown', e => {
  const card = e.target.closest('[data-channel]');
  if (card && (e.key === 'Enter' || e.key === ' ') && e.target === card) {
    e.preventDefault();
    openSheet(state.channels[+card.dataset.channel]);
  }
});

$('#manualUrlBtn').addEventListener('click', () => openSheet(null));

$('#reloadListBtn').addEventListener('click', async () => {
  const data = await postForm('/api/channels/reload');
  if (data.error) { toast(data.error, 'error'); return; }
  toast(data.count ? `Lista recargada: ${data.count} canales` : 'No se encontró la lista o está vacía', data.count ? 'ok' : 'error');
  state.group = '';
  await loadGroups();
  loadChannels();
});

// ════════════════════════════════════════════
//  Fechas locales y cambio de hora
// ════════════════════════════════════════════
// Una hora local puede corresponder a 0 instantes (la hora que se salta en marzo)
// o a 2 (la hora que se repite en octubre). Devuelve todos los instantes posibles.
function localCandidates(localStr) {
  if (!localStr || !localStr.includes('T')) return [];
  const [d, t] = localStr.split('T');
  const [Y, M, D] = d.split('-').map(Number);
  const [h, m] = t.split(':').map(Number);
  const base = new Date(Y, M - 1, D, h, m, 0, 0).getTime();
  const found = [];
  for (const delta of [-3600000, 0, 3600000]) {
    const dt = new Date(base + delta);
    if (dt.getFullYear() === Y && dt.getMonth() === M - 1 && dt.getDate() === D &&
        dt.getHours() === h && dt.getMinutes() === m && !found.some(c => c.getTime() === dt.getTime())) {
      found.push(dt);
    }
  }
  return found.sort((a, b) => a - b);
}

function isoWithOffset(dt) {
  const off = -dt.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  const p = n => pad2(Math.floor(Math.abs(n)));
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}T${p(dt.getHours())}:${p(dt.getMinutes())}:00` +
    `${sign}${p(off / 60)}:${p(off % 60)}`;
}

const toLocalInput = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

function resolveDate(which) {
  const value = $(which === 'start' ? '#fStart' : '#fEnd').value;
  const cands = localCandidates(value);
  const ambiguous = cands.length > 1;
  $(which === 'start' ? '#foldStart' : '#foldEnd').hidden = !ambiguous;
  if (ambiguous) return { date: cands[state.sheet.folds[which] === 1 ? 1 : 0], ambiguous, cands };
  return { date: cands[0] || new Date(value), ambiguous, cands };
}

function setFold(which, val) {
  state.sheet.folds[which] = val;
  $$(`[data-fold^="${which}:"]`).forEach(c => c.classList.toggle('active', c.dataset.fold === `${which}:${val}`));
}

function updateScheduleSummary() {
  const out = $('#scheduleSummary');
  if (!$('#fStart').value || !$('#fEnd').value) {
    out.className = 'summary';
    out.textContent = 'Elige cuándo empieza y cuándo termina.';
    return null;
  }
  const start = resolveDate('start');
  let end = resolveDate('end');
  // Fin en la hora repetida que quedaría antes del inicio: usar su segunda aparición
  if (end.ambiguous && end.date <= start.date && end.cands[1] > start.date && state.sheet.folds.end === 0) {
    setFold('end', 1);
    end = resolveDate('end');
  }
  setFold('start', state.sheet.folds.start);
  setFold('end', state.sheet.folds.end);

  const secs = Math.round((end.date - start.date) / 1000);
  if (!(secs > 0)) {
    out.className = 'summary warn';
    out.textContent = 'La hora de fin tiene que ser posterior a la de inicio.';
    return null;
  }
  const dstChange = start.date.getTimezoneOffset() !== end.date.getTimezoneOffset();
  const startsNow = start.date < new Date(Date.now() - 30000);
  out.className = 'summary' + (dstChange ? ' warn' : '');
  out.innerHTML = `Duración real <strong>${hms(secs)}</strong>` +
    (dstChange ? ' · incluye el cambio de hora, ya está tenido en cuenta' : '') +
    (startsNow ? ' · la hora de inicio ya pasó: empezará en cuanto la guardes' : '');
  return { start: start.date, end: end.date };
}

// ════════════════════════════════════════════
//  Panel para grabar o programar
// ════════════════════════════════════════════
function defaultName(channel) {
  const d = new Date();
  const stamp = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}h${pad2(d.getMinutes())}`;
  return `${channel ? channel.name : 'Grabación'} ${stamp}`;
}

function openSheet(channel, mode = 'now') {
  state.sheet = { channel, mode, duration: '01:00:00', folds: { start: 0, end: 0 } };
  const manual = !channel;
  $('#sheetLogo').outerHTML = manual
    ? `<div class="logo-tile icon-tile" id="sheetLogo">${icon('link')}</div>`
    : logoTile(channel.logo_id, channel.name).replace('class="logo-tile', 'id="sheetLogo" class="logo-tile');
  $('#sheetTitle').textContent = manual ? 'Grabar un enlace' : channel.name;
  $('#sheetSubtitle').textContent = manual ? 'M3U8, TS, RTMP, RTSP, UDP o SRT' : (channel.group || 'Canal de tu lista');
  $('#urlField').hidden = !manual;
  $('#sheetCopy').hidden = manual;
  $('#fUrl').value = '';
  $('#fName').value = defaultName(channel);
  $('#fDuration').hidden = true;
  $('#fDuration').value = '';
  $$('#durationChips .chip').forEach(c => c.classList.toggle('active', c.dataset.dur === '01:00:00'));

  const start = new Date(Date.now() + 5 * 60000);
  $('#fStart').value = toLocalInput(start);
  $('#fEnd').value = toLocalInput(new Date(start.getTime() + 3600000));
  $('#sheetError').hidden = true;
  updateOptionsSummary();
  setMode(mode);
  $('#sheetBackdrop').hidden = false;
  document.body.style.overflow = 'hidden';
  (manual ? $('#fUrl') : $('#sheetSubmit')).focus({ preventScroll: true });
}

function closeSheet() {
  $('#sheetBackdrop').hidden = true;
  document.body.style.overflow = '';
  state.sheet = null;
}

function setMode(mode) {
  state.sheet.mode = mode;
  $$('.seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
    b.setAttribute('aria-selected', String(b.dataset.mode === mode));
  });
  $('.mode-now').hidden = mode !== 'now';
  $('.mode-schedule').hidden = mode !== 'schedule';
  $('#sheetSubmit').textContent = mode === 'now' ? 'Empezar a grabar' : 'Programar grabación';
  if (mode === 'schedule') updateScheduleSummary();
  showSheetError(mode === 'now' && state.status && state.status.running
    ? 'Ya hay una grabación en curso: la nueva no podrá empezar hasta que termine.' : '');
}

function showSheetError(msg) {
  $('#sheetError').textContent = msg;
  $('#sheetError').hidden = !msg;
}

function updateOptionsSummary() {
  const fmt = $('#fFormat').value.toUpperCase();
  const vpnOn = !$('#vpnField').hidden;
  const country = $('#fVpn').value;
  $('#optionsSummary').textContent = `Opciones · ${fmt}` + (vpnOn ? ` · ${country ? `VPN en ${country}` : 'VPN actual'}` : '');
}

function parseDuration(text) {
  text = text.trim();
  if (/^\d+$/.test(text)) return hms(Number(text) * 60);         // "90" = 90 minutos
  const m = text.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return null;
  const secs = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3] || 0);
  return secs > 0 ? hms(secs) : null;
}

$('#sheetBackdrop').addEventListener('click', e => { if (e.target === e.currentTarget) closeSheet(); });
$('#recordSheet').addEventListener('click', e => {
  const t = e.target;
  if (t.closest('[data-close-sheet]')) { closeSheet(); return; }
  if (t.closest('#sheetCopy') && state.sheet.channel) { copyText(state.sheet.channel.url, state.sheet.channel.name); return; }
  const modeBtn = t.closest('[data-mode]');
  if (modeBtn) { setMode(modeBtn.dataset.mode); return; }

  const dur = t.closest('[data-dur]');
  if (dur) {
    $$('#durationChips .chip').forEach(c => c.classList.toggle('active', c === dur));
    const custom = dur.dataset.dur === 'custom';
    $('#fDuration').hidden = !custom;
    if (custom) { $('#fDuration').value = state.sheet.duration; $('#fDuration').focus(); }
    else state.sheet.duration = dur.dataset.dur;
    return;
  }
  const startIn = t.closest('[data-start-in]');
  if (startIn) {
    const prevLen = updateScheduleSummary();
    const length = prevLen ? prevLen.end - prevLen.start : 3600000;
    const start = new Date(Date.now() + Number(startIn.dataset.startIn) * 60000);
    $('#fStart').value = toLocalInput(start);
    $('#fEnd').value = toLocalInput(new Date(start.getTime() + length));
    state.sheet.folds = { start: 0, end: 0 };
    updateScheduleSummary();
    return;
  }
  const len = t.closest('[data-length]');
  if (len) {
    const start = resolveDate('start').date;
    if (isNaN(start)) return;
    const end = new Date(start.getTime() + Number(len.dataset.length) * 60000);
    $('#fEnd').value = toLocalInput(end);
    const cands = localCandidates($('#fEnd').value);
    state.sheet.folds.end = cands.length > 1 && Math.abs(cands[1] - end) < Math.abs(cands[0] - end) ? 1 : 0;
    updateScheduleSummary();
    return;
  }
  const fold = t.closest('[data-fold]');
  if (fold) {
    const [which, val] = fold.dataset.fold.split(':');
    setFold(which, Number(val));
    updateScheduleSummary();
  }
});
['#fStart', '#fEnd'].forEach(sel => $(sel).addEventListener('input', updateScheduleSummary));
['#fFormat', '#fVpn'].forEach(sel => $(sel).addEventListener('change', updateOptionsSummary));

const URL_RE = /^(https?|rtmps?|rtsps?|udp|rtp|srt|mmsh?):\/\/\S+$/i;

$('#recordSheet').addEventListener('submit', async e => {
  e.preventDefault();
  const sh = state.sheet;
  const url = sh.channel ? sh.channel.url : $('#fUrl').value.trim();
  if (!URL_RE.test(url)) { showSheetError('Pega un enlace válido (http, https, rtmp, rtsp, udp o srt).'); $('#fUrl').focus(); return; }
  const name = $('#fName').value.trim();
  if (!name) { showSheetError('Ponle un nombre a la grabación.'); $('#fName').focus(); return; }
  const common = { url, filename: name, format: $('#fFormat').value, vpn_country: $('#fVpn').value };
  const btn = $('#sheetSubmit');

  let data;
  btn.disabled = true;
  try {
    if (sh.mode === 'now') {
      const duration = $('#fDuration').hidden ? sh.duration : parseDuration($('#fDuration').value);
      if (!duration) { showSheetError('Escribe la duración como HH:MM:SS o en minutos.'); $('#fDuration').focus(); return; }
      data = await postForm('/record', { ...common, duration });
    } else {
      const range = updateScheduleSummary();
      if (!range) { showSheetError('Revisa las horas de inicio y fin.'); return; }
      data = await postForm('/schedule', {
        ...common, start: isoWithOffset(range.start), end: isoWithOffset(range.end),
        start_fold: sh.folds.start, end_fold: sh.folds.end,
      });
    }
  } finally {
    btn.disabled = false;
  }
  if (data.error) { showSheetError(data.error); return; }
  toast(data.message || 'Hecho', 'ok');
  closeSheet();
  refreshStatus();
  if (state.view === 'recordings') loadRecordings();
});

// ════════════════════════════════════════════
//  Grabaciones: próximas, biblioteca y anteriores
// ════════════════════════════════════════════
async function loadRecordings() {
  const [history, schedules] = await Promise.all([api('/history'), api('/schedules')]);
  state.history = Array.isArray(history) ? history : [];
  state.schedules = Array.isArray(schedules) ? schedules : [];
  renderUpcoming();
  renderLibrary();
  renderPast();
}

const jobTitle = j => j.channel || stripExt(j.filename);

function renderUpcoming() {
  const jobs = state.schedules.filter(j => j.status === 'pending' || j.status === 'running')
    .sort((a, b) => new Date(a.start_time) - new Date(b.start_time));
  $('#upcomingSection').hidden = !jobs.length;
  $('#upcomingList').innerHTML = jobs.map(j => {
    const start = new Date(j.start_time), end = new Date(j.end_time);
    const running = j.status === 'running';
    return `
      <div class="row">
        ${logoTile(j.logo_id, jobTitle(j))}
        <div class="row-main">
          <p class="row-title">${esc(jobTitle(j))}</p>
          <p class="row-sub"><span>${esc(fmtDay(start))} · ${esc(fmtTime(start))}–${esc(fmtTime(end))}</span>
            ${j.vpn_country ? `<span>${icon('shield', 'tiny')} ${esc(j.vpn_country)}</span>` : ''}
            <span>${esc((j.format || '').toUpperCase())}</span></p>
        </div>
        <div class="row-side">
          ${running
            ? '<span class="tag tag-rec">Grabando</span>'
            : `<span class="countdown" data-countdown="${esc(j.start_time)}">${esc(countdownText(start))}</span>`}
          ${running
            ? `<button type="button" class="btn btn-sm" data-action="stop">${icon('stop')}<span>Detener</span></button>`
            : `<button type="button" class="btn btn-sm btn-ghost" data-cancel="${esc(j.id)}">Cancelar</button>`}
        </div>
      </div>`;
  }).join('');
}

function renderLibrary() {
  const items = state.history.slice().reverse();
  const total = items.reduce((acc, x) => acc + (Number(x.filesize_bytes) || 0), 0);
  $('#librarySummary').textContent = items.length
    ? `${items.length} ${items.length === 1 ? 'grabación' : 'grabaciones'} · ${formatBytes(total)}` : '';

  if (!items.length) {
    $('#libraryGrid').innerHTML = `<div class="empty">${icon('film')}<strong>Todavía no hay grabaciones</strong>
      <span>Elige un canal para grabarlo ahora o programarlo.</span>
      <a class="btn btn-primary" href="#canales">${icon('tv')}<span>Ver canales</span></a></div>`;
    return;
  }

  $('#libraryGrid').innerHTML = items.map(item => {
    const file = item.filename;
    const hasFile = (Number(item.filesize_bytes) || 0) > 0;
    const title = stripExt(file);
    const dur = item.media_duration_seconds ?? item.duration_seconds;
    const when = item.start ? new Date(item.start) : null;
    const meta = [when ? `${fmtDay(when)}, ${fmtTime(when)}` : '', hasFile ? formatBytes(item.filesize_bytes) : '',
      (item.format || '').toUpperCase(), item.vpn_country || ''].filter(Boolean).join(' · ');
    let status = '';
    if (item.status === 'failed') {
      status = `<div class="card-status"><span class="tag ${hasFile ? 'tag-warn' : 'tag-rec'}">${hasFile ? 'Incompleta' : 'Fallida'}</span>` +
        (item.status_reason ? `<span class="card-reason">${esc(item.status_reason)}</span>` : '') + '</div>';
    } else if (item.status === 'stopped') {
      status = '<div class="card-status"><span class="tag">Detenida antes de tiempo</span></div>';
    }
    const fallback = `<div class="thumb-fallback" ${hasFile ? 'hidden' : ''}>${
      item.channel ? logoTile(item.logo_id, item.channel) : `<div class="logo-tile icon-tile">${icon(hasFile ? 'film' : 'alert')}</div>`}</div>`;
    const thumbInner = `
      ${hasFile ? `<img src="/thumb/${encodeURIComponent(file)}" alt="" loading="lazy" decoding="async" data-thumb>` : ''}
      ${fallback}
      ${hasFile ? `<span class="play-hint">${icon('play')}</span>` : ''}
      ${item.logo_id && hasFile ? `<div class="thumb-logo">${logoTile(item.logo_id, item.channel || title)}</div>` : ''}
      ${hasFile && dur ? `<span class="thumb-badge">${shortDuration(dur)}</span>` : ''}`;
    return `
      <article class="card">
        ${hasFile
          ? `<button type="button" class="thumb" data-play="${esc(file)}" aria-label="Ver ${esc(title)}">${thumbInner}</button>`
          : `<div class="thumb">${thumbInner}</div>`}
        <div class="card-body">
          <div class="card-text">
            <p class="card-title" title="${esc(file)}">${esc(title)}</p>
            <p class="card-meta">${esc(meta)}</p>
            ${status}
          </div>
          <div class="card-actions">
            ${hasFile ? `<a class="btn btn-icon btn-ghost" href="/download/${encodeURIComponent(file)}" title="Descargar">${icon('download')}</a>` : ''}
            <button type="button" class="btn btn-icon btn-ghost" data-delete="${esc(file)}" title="Eliminar">${icon('trash')}</button>
          </div>
        </div>
      </article>`;
  }).join('');
}

const STATUS_TAGS = {
  ok: ['Completada', ''], stopped: ['Detenida', ''], failed: ['Fallida', 'tag-rec'], cancelled: ['Cancelada', ''],
};

function renderPast() {
  const jobs = state.schedules.filter(j => j.status !== 'pending' && j.status !== 'running').reverse();
  $('#pastSection').hidden = !jobs.length && !state.history.length;
  $('#pastCount').textContent = jobs.length ? `(${jobs.length})` : '';
  $('#pastList').hidden = !jobs.length;
  $('#clearPastBtn').hidden = !jobs.length;
  $('#pastList').innerHTML = jobs.map(j => {
    const start = new Date(j.start_time), end = new Date(j.end_time);
    const [label, cls] = STATUS_TAGS[j.status] || [j.status, ''];
    return `
      <div class="row">
        ${logoTile(j.logo_id, jobTitle(j))}
        <div class="row-main">
          <p class="row-title">${esc(jobTitle(j))}</p>
          <p class="row-sub"><span>${esc(fmtDay(start))} · ${esc(fmtTime(start))}–${esc(fmtTime(end))}</span>
            ${j.status === 'failed' && j.status_reason ? `<span>${esc(j.status_reason)}</span>` : ''}</p>
        </div>
        <div class="row-side">
          <span class="tag ${cls}">${esc(label)}</span>
          <button type="button" class="btn btn-icon btn-ghost btn-sm" data-delete-job="${esc(j.id)}" title="Quitar de la lista">${icon('x')}</button>
        </div>
      </div>`;
  }).join('');
}

$('#view-recordings').addEventListener('click', async e => {
  const t = e.target;
  const play = t.closest('[data-play]');
  if (play) { openPlayer(play.dataset.play); return; }

  if (t.closest('[data-action="stop"]')) { $('#stopBtn').click(); return; }

  const del = t.closest('[data-delete]');
  if (del) {
    const file = del.dataset.delete;
    if (!confirm(`¿Eliminar «${file}»? Se borra el archivo y no se puede deshacer.`)) return;
    const data = await postForm('/history/delete/' + encodeURIComponent(file));
    toast(data.error || 'Grabación eliminada', data.error ? 'error' : 'ok');
    loadRecordings();
    return;
  }
  const cancel = t.closest('[data-cancel]');
  if (cancel) {
    if (!confirm('¿Cancelar esta grabación programada?')) return;
    const data = await postForm('/schedules/cancel/' + encodeURIComponent(cancel.dataset.cancel));
    toast(data.error || 'Programación cancelada', data.error ? 'error' : 'info');
    loadRecordings();
    refreshStatus();
    return;
  }
  const delJob = t.closest('[data-delete-job]');
  if (delJob) {
    await postForm('/schedules/delete/' + encodeURIComponent(delJob.dataset.deleteJob));
    loadRecordings();
  }
});

$('#clearPastBtn').addEventListener('click', async () => {
  if (!confirm('¿Quitar de la lista las programaciones terminadas o canceladas? Las grabaciones no se borran.')) return;
  await postForm('/schedules/clear');
  loadRecordings();
});

$('#clearHistoryBtn').addEventListener('click', async () => {
  if (!confirm('¿Vaciar el historial? Los archivos de vídeo no se borran, pero dejarán de aparecer en la biblioteca.')) return;
  await postForm('/history/clear');
  toast('Historial vaciado', 'info');
  loadRecordings();
});

function updateCountdowns() {
  $$('[data-countdown]').forEach(el => { el.textContent = countdownText(new Date(el.dataset.countdown)); });
}

// ════════════════════════════════════════════
//  Reproductor
// ════════════════════════════════════════════
const video = $('#proVideo');
const player = { open: false, file: '', duration: 0, tracks: [], track: 0, pipe: false, offset: 0, shareUrl: '', idleTimer: null };

async function openPlayer(file) {
  Object.assign(player, { open: true, file, duration: 0, tracks: [], track: 0, pipe: false, offset: 0 });
  $('#playerTitle').textContent = stripExt(file);
  $('#playerCodec').hidden = true;
  player.shareUrl = `${location.origin}/stream/${encodeURIComponent(file)}`;
  $('#playerVlc').href = 'vlc://' + player.shareUrl;
  $('#playerDownload').href = '/download/' + encodeURIComponent(file);
  $('#playerBackdrop').hidden = false;
  document.body.style.overflow = 'hidden';

  try {
    const info = await api('/api/media-info/' + encodeURIComponent(file));
    player.duration = Number(info.duration) || 0;
    player.tracks = info.audio_tracks || [];
    if (info.sig) {   // con login activo, VLC necesita un enlace firmado
      player.shareUrl += '?sig=' + encodeURIComponent(info.sig);
      $('#playerVlc').href = 'vlc://' + player.shareUrl;
    }
    const parts = [];
    if (info.video_codec) parts.push(info.video_codec.toUpperCase());
    if (player.tracks.length > 1) parts.push(`${player.tracks.length} pistas de audio`);
    if (parts.length) { $('#playerCodec').textContent = parts.join(' · '); $('#playerCodec').hidden = false; }
  } catch (e) {
    player.tracks = [];
  }
  renderAudioMenu();
  loadSource(0, 0);
}

function renderAudioMenu() {
  const tracks = player.tracks.length ? player.tracks : [{ label: 'Pista 1 · Principal' }];
  $('#vAudioLabel').textContent = String(player.track + 1);
  $('#vAudioBtn').hidden = tracks.length < 2;
  $('#vAudioMenu').innerHTML = tracks.map((t, i) =>
    `<button type="button" class="v-menu-item ${i === player.track ? 'active' : ''}" data-track="${i}">${esc(t.label)}</button>`).join('');
}

// Pista 1 de un MP4/MKV: lectura directa del archivo (permite saltar sin coste).
// Otras pistas o archivos .ts: el servidor los prepara al vuelo sin recodificar.
function loadSource(track, seek = 0) {
  player.track = track;
  const isTs = player.file.toLowerCase().endsWith('.ts');
  if (track === 0 && !isTs) {
    player.pipe = false;
    player.offset = 0;
    video.src = '/stream/' + encodeURIComponent(player.file);
    video.currentTime = Math.max(0, seek);
    $('#playerMode').textContent = '';
  } else {
    player.pipe = true;
    player.offset = Math.max(0, seek);
    const params = new URLSearchParams({ track: String(track), start: String(Math.floor(player.offset)) });
    video.src = `/stream-track/${encodeURIComponent(player.file)}?${params}`;
    $('#playerMode').textContent = isTs && track === 0 ? 'Archivo TS: preparado al vuelo para el navegador' : `Pista de audio ${track + 1}`;
  }
  renderAudioMenu();
  video.play().catch(() => {});
}

const currentTime = () => (player.pipe ? player.offset : 0) + (Number(video.currentTime) || 0);
const totalTime = () => player.duration || (isFinite(video.duration) ? video.duration : 0);

function seekTo(sec) {
  const dur = totalTime();
  sec = dur > 0 ? Math.max(0, Math.min(dur - 1, sec)) : Math.max(0, sec);
  if (player.pipe) loadSource(player.track, sec); else video.currentTime = sec;
}

function closeMenus() { $('#vAudioMenu').hidden = true; $('#vSpeedMenu').hidden = true; }

function togglePlay() {
  closeMenus();
  if (video.paused) video.play().catch(() => {}); else video.pause();
}

function wakeControls() {
  const stage = $('#videoStage');
  stage.classList.remove('idle');
  clearTimeout(player.idleTimer);
  if (!video.paused) player.idleTimer = setTimeout(() => { closeMenus(); stage.classList.add('idle'); }, 2600);
}

function toggleFullscreen() {
  if (!document.fullscreenElement) $('#videoStage').requestFullscreen?.();
  else document.exitFullscreen?.();
}

function setMuteIcon() { $('#vMute').innerHTML = icon(video.muted || video.volume === 0 ? 'mute' : 'volume'); }

function closePlayer() {
  player.open = false;
  clearTimeout(player.idleTimer);
  closeMenus();
  video.pause();
  video.removeAttribute('src');
  video.load();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  $('#playerBackdrop').hidden = true;
  document.body.style.overflow = '';
}

video.addEventListener('click', togglePlay);
video.addEventListener('play', () => { $('#vPlay').innerHTML = icon('pause'); wakeControls(); });
video.addEventListener('pause', () => { $('#vPlay').innerHTML = icon('play'); $('#videoStage').classList.remove('idle'); });
video.addEventListener('timeupdate', () => {
  const cur = currentTime(), dur = totalTime();
  $('#vTime').textContent = `${hms(cur)} / ${hms(dur)}`;
  $('#vTimelineFill').style.width = `${dur > 0 ? Math.min(100, (cur / dur) * 100).toFixed(2) : 0}%`;
});
$('#videoStage').addEventListener('mousemove', wakeControls);
$('#videoStage').addEventListener('touchstart', wakeControls, { passive: true });
$('#videoStage').addEventListener('dblclick', e => { if (e.target === video) toggleFullscreen(); });

const timelineRatio = e => {
  const r = $('#vTimeline').getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
};
$('#vTimeline').addEventListener('mousemove', e => {
  const ratio = timelineRatio(e);
  const tip = $('#vTimelineTooltip');
  tip.style.left = `${ratio * 100}%`;
  tip.textContent = hms(ratio * totalTime());
});
$('#vTimeline').addEventListener('click', e => { const d = totalTime(); if (d > 0) seekTo(timelineRatio(e) * d); });

$('#vPlay').addEventListener('click', togglePlay);
$('#vBack').addEventListener('click', () => seekTo(currentTime() - 10));
$('#vFwd').addEventListener('click', () => seekTo(currentTime() + 10));
$('#vMute').addEventListener('click', () => { video.muted = !video.muted; setMuteIcon(); });
$('#vVol').addEventListener('input', e => { video.volume = Number(e.target.value); video.muted = video.volume === 0; setMuteIcon(); });
$('#vFull').addEventListener('click', toggleFullscreen);
$('#vAudioBtn').addEventListener('click', e => { e.stopPropagation(); $('#vSpeedMenu').hidden = true; $('#vAudioMenu').hidden = !$('#vAudioMenu').hidden; });
$('#vSpeedBtn').addEventListener('click', e => { e.stopPropagation(); $('#vAudioMenu').hidden = true; $('#vSpeedMenu').hidden = !$('#vSpeedMenu').hidden; });

$('#vAudioMenu').addEventListener('click', e => {
  const item = e.target.closest('[data-track]');
  if (!item) return;
  closeMenus();
  const track = Number(item.dataset.track);
  if (track === player.track) return;
  // Si el navegador permite cambiar de pista sin recargar, mejor
  if (!player.pipe && video.audioTracks && video.audioTracks.length > track) {
    for (let i = 0; i < video.audioTracks.length; i++) video.audioTracks[i].enabled = i === track;
    player.track = track;
    renderAudioMenu();
  } else {
    loadSource(track, currentTime());
  }
});
$('#vSpeedMenu').addEventListener('click', e => {
  const item = e.target.closest('[data-speed]');
  if (!item) return;
  video.playbackRate = Number(item.dataset.speed);
  $('#vSpeedLabel').textContent = `${item.dataset.speed}x`;
  $$('#vSpeedMenu .v-menu-item').forEach(b => b.classList.toggle('active', b === item));
  closeMenus();
});

$('#playerClose').addEventListener('click', closePlayer);
$('#playerBackdrop').addEventListener('click', e => { if (e.target === e.currentTarget) closePlayer(); });
$('#playerCopy').addEventListener('click', () => copyText(player.shareUrl, stripExt(player.file)));

// ════════════════════════════════════════════
//  Teclado y arranque
// ════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (player.open) {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === 'Escape') closePlayer();
    else if (typing) return;
    else if (e.key === ' ' && document.activeElement.tagName !== 'BUTTON') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); seekTo(currentTime() - 10); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); seekTo(currentTime() + 10); }
    else if (e.key.toLowerCase() === 'f') { e.preventDefault(); toggleFullscreen(); }
    else if (e.key.toLowerCase() === 'm') { e.preventDefault(); video.muted = !video.muted; setMuteIcon(); }
    return;
  }
  if (e.key === 'Escape' && state.sheet) closeSheet();
});

setInterval(refreshStatus, 2000);
setInterval(updateCountdowns, 1000);
setInterval(() => { if (state.view === 'recordings') loadRecordings(); }, 20000);

(async function init() {
  refreshStatus();
  loadVpnCountries().catch(() => {});
  await loadGroups().catch(() => {});
  showView(HASH_VIEWS[location.hash] || 'channels');
})();
