// Si la sesión caduca, cualquier llamada a la API devuelve 401: volver al login
const _nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _nativeFetch(...args);
  if (res.status === 401) window.location.href = '/login';
  return res;
};
// ── Utilidades generales y Toasts ──────────────────────────────────
function showToast(msg, type = 'info') {
  const c = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast';
  if (type === 'success') t.style.borderColor = '#10b981';
  if (type === 'error')   t.style.borderColor = '#f43f5e';
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.remove(); }, 3500);
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function pad2(n) { return String(n).padStart(2, '0'); }

function secsToHHMMSS(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  return pad2(Math.floor(s / 3600)) + ':' + pad2(Math.floor((s % 3600) / 60)) + ':' + pad2(s % 60);
}

function formatBytes(bytes) {
  let b = Number(bytes) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return b.toFixed(i === 0 ? 0 : 2) + ' ' + units[i];
}

// ── Reloj en vivo con indicador de zona horaria (Verano/Invierno) ──
function updateClock() {
  const now = new Date();
  const offsetMin = -now.getTimezoneOffset();
  const sign = offsetMin >= 0 ? '+' : '-';
  const absH = Math.floor(Math.abs(offsetMin) / 60);
  const tzLabel = `UTC${sign}${absH}`;
  document.getElementById('serverClock').textContent =
    now.toLocaleTimeString('es-ES', { hour12: false }) + ' · ' + tzLabel;
}
setInterval(updateClock, 1000);
updateClock();

// ── Carga y Control de Países VPN (Gluetun) ────────────────────────
async function initVpnCountries() {
  try {
    const res = await fetch('/api/vpn/countries');
    const data = await res.json();
    const selects = [
      { el: document.getElementById('topbarVpnSelect'), defaultLabel: '🌍 País...' },
      { el: document.getElementById('rVpnCountry'),     defaultLabel: '🛡️ Usar país actual de la VPN (Sin cambiar)' },
      { el: document.getElementById('sVpnCountry'),     defaultLabel: '🛡️ Mantener país actual de la VPN' }
    ];

    selects.forEach(({ el, defaultLabel }) => {
      if (!el) return;
      el.innerHTML = `<option value="">${defaultLabel}</option>`;
      const grpFav = document.createElement('optgroup');
      grpFav.label = '⭐ Países frecuentes';
      (data.featured || []).forEach(item => {
        const opt = document.createElement('option');
        opt.value = item.value;
        opt.textContent = item.label;
        grpFav.appendChild(opt);
      });
      el.appendChild(grpFav);

      if ((data.others || []).length > 0) {
        const grpAll = document.createElement('optgroup');
        grpAll.label = '🌐 Todos los países ProtonVPN';
        data.others.forEach(c => {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c;
          grpAll.appendChild(opt);
        });
        el.appendChild(grpAll);
      }
    });
  } catch (e) {}
}
initVpnCountries();

async function changeGlobalVpnCountry(country) {
  const sel = document.getElementById('topbarVpnSelect');
  if (!country) return;
  const vDot = document.getElementById('vpnDot');
  const vTxt = document.getElementById('vpnStatusText');
  vDot.className = 'dot switching';
  vTxt.textContent = `⏳ Conectando a ${country}...`;
  showToast(`Cambiando VPN a ${country}... (tarda ~5-8s)`, 'info');

  try {
    const fd = new FormData();
    fd.append('country', country);
    const res = await fetch('/api/vpn/country', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) {
      showToast('❌ ' + data.error, 'error');
    } else {
      showToast('✅ ' + (data.message || `Conectado a ${country}`), 'success');
      log('🌍 VPN: ' + (data.message || country));
    }
  } catch (e) {
    showToast('Error al cambiar el país de la VPN', 'error');
  } finally {
    sel.value = '';
    checkServerStatus();
  }
}

// ── Gestión de Pestañas ────────────────────────────────────────────
function showTab(tab) {
  ['record', 'schedule', 'channels', 'history'].forEach(t => {
    document.getElementById(t + 'View').style.display = t === tab ? 'block' : 'none';
    const id = 'tab' + t.charAt(0).toUpperCase() + t.slice(1);
    const btn = document.getElementById(id);
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (window.innerWidth <= 768) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  if (tab === 'history')  loadHistory();
  if (tab === 'schedule') { initScheduleDefaults(); loadSchedules(); }
  if (tab === 'channels') {
    loadChannelGroups();
    if (document.getElementById('searchInput').value === '' && document.getElementById('channelsBody').children.length <= 1) {
      searchChannels();
    }
  }
}

function log(msg) {
  const d = document.getElementById('log');
  const time = new Date().toLocaleTimeString('es-ES', { hour12: false });
  d.innerHTML += `<span style="color:#64748b">[${time}]</span> ${escapeHtml(msg)}<br>`;
  d.scrollTop = d.scrollHeight;
}

// ── Motor de Fechas y Cambios de Hora (DST Verano/Invierno) ────────
let scheduleFolds = { start: 0, end: 0 };

function parseLocalCandidates(localStr) {
  if (!localStr || !localStr.includes('T')) return [];
  const [dPart, tPart] = localStr.split('T');
  const [Y, M, D] = dPart.split('-').map(Number);
  const [h, m] = tPart.split(':').map(Number);

  const base = new Date(Y, M - 1, D, h, m, 0, 0).getTime();
  const candidates = [];
  [-3600000, 0, 3600000].forEach(delta => {
    const dt = new Date(base + delta);
    if (
      dt.getFullYear() === Y &&
      dt.getMonth() === M - 1 &&
      dt.getDate() === D &&
      dt.getHours() === h &&
      dt.getMinutes() === m
    ) {
      if (!candidates.some(c => c.getTime() === dt.getTime())) {
        candidates.push(dt);
      }
    }
  });
  candidates.sort((a, b) => a.getTime() - b.getTime());
  return candidates;
}

function toIsoWithOffset(dateObj) {
  const tzOffsetMin = -dateObj.getTimezoneOffset();
  const sign = tzOffsetMin >= 0 ? '+' : '-';
  const pad = n => String(Math.floor(Math.abs(n))).padStart(2, '0');
  return dateObj.getFullYear() +
    '-' + pad(dateObj.getMonth() + 1) +
    '-' + pad(dateObj.getDate()) +
    'T' + pad(dateObj.getHours()) +
    ':' + pad(dateObj.getMinutes()) +
    ':' + pad(dateObj.getSeconds()) +
    sign + pad(tzOffsetMin / 60) + ':' + pad(tzOffsetMin % 60);
}

function formatOffsetBadge(dateObj) {
  if (!dateObj || isNaN(dateObj.getTime())) return '';
  const offH = -dateObj.getTimezoneOffset() / 60;
  const sign = offH >= 0 ? '+' : '';
  const season = offH === 2 ? 'Horario verano' : (offH === 1 ? 'Horario invierno' : '');
  return `UTC${sign}${offH}${season ? ' · ' + season : ''}`;
}

function setFold(which, foldVal) {
  scheduleFolds[which] = foldVal;
  document.getElementById(which === 'start' ? 'sStartFold0' : 'sEndFold0').classList.toggle('active', foldVal === 0);
  document.getElementById(which === 'start' ? 'sStartFold1' : 'sEndFold1').classList.toggle('active', foldVal === 1);
  onScheduleDatesChanged();
}

function resolveInputDate(localStr, which) {
  const cands = parseLocalCandidates(localStr);
  const ambigBox = document.getElementById(which === 'start' ? 'sStartAmbiguousBox' : 'sEndAmbiguousBox');
  if (cands.length > 1) {
    ambigBox.style.display = 'block';
    const idx = scheduleFolds[which] === 1 ? 1 : 0;
    return { date: cands[idx], ambiguous: true, candidates: cands };
  } else {
    ambigBox.style.display = 'none';
    if (cands.length === 1) {
      return { date: cands[0], ambiguous: false, candidates: cands };
    }
    const fallback = new Date(localStr);
    return { date: fallback, ambiguous: false, candidates: [fallback] };
  }
}

function onScheduleDatesChanged() {
  const sStartStr = document.getElementById('sStart').value;
  const sEndStr   = document.getElementById('sEnd').value;
  const preview   = document.getElementById('schedulePreview');
  const pText     = document.getElementById('schedulePreviewText');
  const pDur      = document.getElementById('schedulePreviewDuration');

  if (!sStartStr || !sEndStr) {
    pText.textContent = 'Selecciona fecha de inicio y fin.';
    pDur.textContent = '--:--:--';
    return;
  }

  const startRes = resolveInputDate(sStartStr, 'start');
  let endRes     = resolveInputDate(sEndStr, 'end');

  if (endRes.ambiguous && endRes.date <= startRes.date && endRes.candidates[1] > startRes.date && scheduleFolds.end === 0) {
    scheduleFolds.end = 1;
    document.getElementById('sEndFold0').classList.remove('active');
    document.getElementById('sEndFold1').classList.add('active');
    endRes = resolveInputDate(sEndStr, 'end');
  }

  document.getElementById('sStartTzBadge').textContent = formatOffsetBadge(startRes.date);
  document.getElementById('sEndTzBadge').textContent   = formatOffsetBadge(endRes.date);

  const diffSecs = Math.round((endRes.date.getTime() - startRes.date.getTime()) / 1000);
  if (diffSecs <= 0) {
    preview.classList.remove('has-dst');
    pText.innerHTML = '<span style="color:#fda4af">⚠️ La hora de fin debe ser posterior a la hora de inicio.</span>';
    pDur.textContent = '00:00:00';
    return;
  }

  const startOff = -startRes.date.getTimezoneOffset();
  const endOff   = -endRes.date.getTimezoneOffset();
  pDur.textContent = secsToHHMMSS(diffSecs);

  if (startOff !== endOff) {
    preview.classList.add('has-dst');
    const diffHours = (startOff - endOff) / 60;
    const msgExtra = diffHours > 0
      ? `Incluye cambio al horario de invierno (+${diffHours}h real de grabación garantizada)`
      : `Incluye cambio al horario de verano (${diffHours}h real)`;
    pText.innerHTML = `<strong style="color:#fde68a">🌙 ¡Cambio de hora detectado!</strong> ${msgExtra}`;
  } else {
    preview.classList.remove('has-dst');
    pText.innerHTML = `✅ Grabación programada lista · Duración total en tiempo real:`;
  }
}

function fmtDatetimeLocal(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function setScheduleStartOffset(minutesFromNow) {
  const start = new Date(Date.now() + minutesFromNow * 60000);
  document.getElementById('sStart').value = fmtDatetimeLocal(start);
  onScheduleDatesChanged();
}

function addScheduleDuration(realMinutes) {
  const sStartStr = document.getElementById('sStart').value;
  const startRes = sStartStr ? resolveInputDate(sStartStr, 'start') : { date: new Date(Date.now() + 300000) };
  if (!sStartStr) {
    document.getElementById('sStart').value = fmtDatetimeLocal(startRes.date);
  }
  const targetEnd = new Date(startRes.date.getTime() + realMinutes * 60000);
  document.getElementById('sEnd').value = fmtDatetimeLocal(targetEnd);

  const endCands = parseLocalCandidates(document.getElementById('sEnd').value);
  if (endCands.length > 1) {
    const isSecond = Math.abs(endCands[1].getTime() - targetEnd.getTime()) < Math.abs(endCands[0].getTime() - targetEnd.getTime());
    setFold('end', isSecond ? 1 : 0);
  } else {
    onScheduleDatesChanged();
  }
}

// ── Buscador de Canales M3U ────────────────────────────────────────
let searchTimer = null;
function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(searchChannels, 260);
}

async function loadChannelGroups() {
  const sel = document.getElementById('groupFilter');
  if (sel.options.length > 1) return;
  try {
    const res = await fetch('/api/channels/groups');
    const groups = await res.json();
    groups.forEach(g => {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = '📂 ' + g;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

async function reloadM3uServer() {
  try {
    const res = await fetch('/api/channels/reload', { method: 'POST' });
    const data = await res.json();
    document.getElementById('badgeChannelsCount').textContent = data.count || 0;
    document.getElementById('m3uStatusInfo').innerHTML = data.count > 0
      ? `<strong style="color:#6ee7b7">✅ Lista recargada con ${data.count} canales.</strong>`
      : `<strong style="color:#fda4af">⚠️ No se encontró lista.m3u o está vacía.</strong>`;
    showToast(`Lista M3U actualizada (${data.count} canales)`, 'success');
    const sel = document.getElementById('groupFilter');
    sel.innerHTML = '<option value="">📂 Todas las categorías</option>';
    await loadChannelGroups();
    searchChannels();
  } catch (e) {
    showToast('Error al recargar lista M3U', 'error');
  }
}

let lastChannelsData = [];
async function searchChannels() {
  const query = document.getElementById('searchInput').value.trim();
  const group = document.getElementById('groupFilter').value;
  const body  = document.getElementById('channelsBody');
  const info  = document.getElementById('resultsInfo');

  body.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:30px">⏳ Buscando canales...</td></tr>';

  try {
    const params = new URLSearchParams({ q: query, group: group });
    const res = await fetch('/api/channels?' + params.toString());
    const data = await res.json();
    lastChannelsData = data;
    body.innerHTML = '';

    if (data.length === 0) {
      body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:34px">No se encontraron canales con ese criterio</td></tr>';
      info.textContent = '0 resultados';
      return;
    }

    info.textContent = `Mostrando ${data.length} canales`;
    data.forEach((ch, idx) => {
      const tr = document.createElement('tr');
      const groupBadge = ch.group
        ? `<span class="badge badge-scheduled">${escapeHtml(ch.group)}</span>`
        : '<span style="color:var(--text-muted);font-size:12px;">General</span>';
      tr.innerHTML = `
        <td class="cell-main">
          <div style="font-weight:600;color:#f8fafc;">${escapeHtml(ch.name)}</div>
          <div style="font-size:11.5px;color:var(--text-muted);word-break:break-all;margin-top:2px;">${escapeHtml(ch.url)}</div>
        </td>
        <td data-label="Categoría">${groupBadge}</td>
        <td class="cell-actions" style="text-align:right;white-space:nowrap;">
          <button class="btn-success btn-sm" onclick="copyChannel(${idx})">📋 Copiar</button>
          <button class="btn-primary btn-sm" onclick="recordChannel(${idx})">▶️ Grabar</button>
          <button class="btn-warning btn-sm" onclick="scheduleChannel(${idx})">📅 Programar</button>
        </td>
      `;
      body.appendChild(tr);
    });
  } catch (e) {
    body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f43f5e;padding:30px">Error de conexión</td></tr>';
  }
}

function copyChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  copyToClipboard(ch.url, ch.name);
}

function recordChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  document.getElementById('url').value = ch.url;
  document.getElementById('filename').value = formatFilenameForChannel(ch.name);
  showTab('record');
  showToast(`Canal "${ch.name}" cargado en el grabador`, 'info');
}

function scheduleChannel(idx) {
  const ch = lastChannelsData[idx];
  if (!ch) return;
  document.getElementById('sUrl').value = ch.url;
  document.getElementById('sFilename').value = 'prog_' + formatFilenameForChannel(ch.name);
  showTab('schedule');
  showToast(`Canal "${ch.name}" listo para programar`, 'info');
}

function copyToClipboard(text, label = '') {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(`Enlace copiado${label ? ': ' + label : ''}`, 'success');
    }).catch(err => {
      showToast('Error al copiar: ' + err, 'error');
    });
  } else {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    try {
      document.execCommand('copy');
      showToast(`Enlace copiado${label ? ': ' + label : ''}`, 'success');
    } catch (err) {
      showToast('No se pudo copiar al portapapeles', 'error');
    }
    document.body.removeChild(textArea);
  }
}

function formatFilenameForChannel(name) {
  let safeName = name.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/gi, '_').toLowerCase();
  safeName = safeName.replace(/_+/g, '_').replace(/^_|_$/g, '');
  const now = new Date();
  const tag = now.getFullYear() + pad2(now.getMonth() + 1) + pad2(now.getDate()) + '_' + pad2(now.getHours()) + pad2(now.getMinutes());
  return (safeName || 'canal') + '_' + tag;
}

function setDirectDuration(val) {
  document.getElementById('duration').value = val;
}

function refreshDefaultFilename() {
  const now = new Date();
  const tag = now.getFullYear() + pad2(now.getMonth() + 1) + pad2(now.getDate()) + '_' + pad2(now.getHours()) + pad2(now.getMinutes()) + pad2(now.getSeconds());
  document.getElementById('filename').value = 'grabacion_' + tag;
}

// ── Grabación Inmediata y Estado en Vivo ───────────────────────────
let wasRunning = false;
let lastLoggedVpnIp = '';

async function submitForm() {
  const fd = new FormData();
  fd.append('url',         document.getElementById('url').value.trim());
  fd.append('duration',    document.getElementById('duration').value.trim());
  fd.append('filename',    document.getElementById('filename').value.trim());
  fd.append('format',      document.getElementById('format').value);
  fd.append('vpn_country', document.getElementById('rVpnCountry').value);
  const res  = await fetch('/record', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.error) {
    log('❌ ' + data.error);
    showToast(data.error, 'error');
    return;
  }
  log('▶️ ' + (data.message || 'Grabación iniciada'));
  showToast(data.message || 'Grabación iniciada', 'success');
  checkServerStatus();
}
document.getElementById('form').onsubmit = e => { e.preventDefault(); submitForm(); };

async function stopRecord() {
  const res  = await fetch('/stop', { method: 'POST' });
  const data = await res.json();
  log('⏹️ ' + (data.message || 'Detención solicitada'));
  showToast(data.message || 'Deteniendo grabación...', 'info');
  setTimeout(checkServerStatus, 800);
}

async function checkServerStatus() {
  try {
    const res  = await fetch('/status');
    const data = await res.json();
    const banner = document.getElementById('statusBanner');
    const dot    = document.getElementById('globalDot');
    const gText  = document.getElementById('globalStateText');
    const sIcon  = document.getElementById('statusIcon');
    const sText  = document.getElementById('statusText');
    const pCont  = document.getElementById('progressContainer');
    const pBar   = document.getElementById('progressBar');
    const timer  = document.getElementById('liveTimer');
    const stopBtn = document.getElementById('globalStopBtn');

    // Actualizar Píldora VPN compacta en el Topbar y registrar IP en la consola inferior
    if (data.vpn && data.vpn.enabled === false) {
      // App sin Gluetun: ocultar todos los controles de VPN
      document.getElementById('vpnPill').style.display = 'none';
      ['rVpnCountry', 'sVpnCountry'].forEach(id => {
        const group = document.getElementById(id)?.closest('.form-group');
        if (group) group.style.display = 'none';
      });
    } else if (data.vpn) {
      const vDot  = document.getElementById('vpnDot');
      const vTxt  = document.getElementById('vpnStatusText');
      const vPill = document.getElementById('vpnPill');
      if (data.vpn.switching) {
        vDot.className = 'dot switching';
        vTxt.textContent = `⏳ ${data.vpn.configured_country || 'VPN'}...`;
      } else if (data.vpn.public_ip) {
        vDot.className = 'dot';
        const cName = data.vpn.configured_country || data.vpn.ip_country || 'VPN';
        vTxt.textContent = `🛡️ ${cName}`;
        vPill.title = `VPN conectada en ${cName} · IP pública: ${data.vpn.public_ip}`;
        if (data.vpn.public_ip !== lastLoggedVpnIp) {
          lastLoggedVpnIp = data.vpn.public_ip;
          log(`🛡️ VPN activa en ${cName} · IP pública: ${data.vpn.public_ip}`);
        }
      } else if (data.vpn.available) {
        vDot.className = 'dot switching';
        vTxt.textContent = `🛡️ ${data.vpn.configured_country || 'VPN'}`;
      } else {
        vDot.className = 'dot';
        vTxt.textContent = '🛡️ VPN';
      }
    }

    if (data.schedules_pending != null) {
      document.getElementById('badgeSchedulesCount').textContent = data.schedules_pending;
    }
    if (data.history_count != null) {
      document.getElementById('badgeHistoryCount').textContent = data.history_count;
    }

    sText.textContent = data.progress || 'Listo';

    if (data.running) {
      wasRunning = true;
      banner.className = 'live-banner is-recording';
      dot.className = 'dot recording';
      gText.textContent = data.mode === 'scheduled' ? 'REC · Prog' : 'REC';
      sIcon.textContent = '🔴';
      pCont.style.display = 'block';
      stopBtn.style.display = 'inline-flex';

      if (data.start_time && data.duration_requested) {
        const elapsedSec = Math.max(0, Math.round((Date.now() - new Date(data.start_time).getTime()) / 1000));
        const totalSec   = Math.max(1, Number(data.duration_requested));
        const pct        = Math.min(100, (elapsedSec / totalSec) * 100);
        pBar.style.width = pct.toFixed(1) + '%';
        timer.style.display = 'inline';
        timer.textContent = `${secsToHHMMSS(elapsedSec)} / ${secsToHHMMSS(totalSec)} (${pct.toFixed(0)}%)`;
      } else {
        timer.style.display = 'none';
      }
    } else {
      dot.className = 'dot';
      gText.textContent = 'Listo';
      pCont.style.display = 'none';
      timer.style.display = 'none';
      stopBtn.style.display = 'none';

      if (data.last_result && data.last_result.status === 'ok') {
        banner.className = 'live-banner is-success';
        sIcon.textContent = '✅';
      } else if (data.last_result && data.last_result.status === 'failed') {
        banner.className = 'live-banner is-error';
        sIcon.textContent = '❌';
      } else {
        banner.className = 'live-banner';
        sIcon.textContent = '🟢';
      }

      if (wasRunning) {
        wasRunning = false;
        if (data.last_result) {
          if (data.last_result.status === 'ok') {
            log('✅ Finalizado: ' + (data.filename || ''));
            showToast('Grabación finalizada correctamente', 'success');
          } else if (data.last_result.status === 'stopped') {
            log('⏹️ Detenida: ' + (data.filename || ''));
          } else {
            log('❌ Error: ' + (data.last_result.reason || ''));
            showToast('La grabación terminó con errores', 'error');
          }
        }
        if (data.filename) {
          const dl = document.getElementById('download');
          dl.href = '/download/' + encodeURIComponent(data.filename);
          dl.innerHTML = `<span>⬇️ Descargar última grabación: <strong>${escapeHtml(data.filename)}</strong></span><span>Descargar archivo →</span>`;
          dl.style.display = 'flex';
        }
        loadSchedules();
        loadHistory();
      }
    }
  } catch (e) {}
}

setInterval(checkServerStatus, 2000);
checkServerStatus();

// ── Historial ──────────────────────────────────────────────────────
async function loadHistory() {
  const res  = await fetch('/history');
  const data = await res.json();
  const body = document.getElementById('historyBody');
  body.innerHTML = '';

  document.getElementById('badgeHistoryCount').textContent = data.length;
  document.getElementById('statTotal').textContent = data.length;
  document.getElementById('statOk').textContent = data.filter(x => x.status === 'ok' || x.status === 'stopped').length;
  document.getElementById('statFailed').textContent = data.filter(x => x.status === 'failed').length;
  const totalBytes = data.reduce((acc, x) => acc + (Number(x.filesize_bytes) || 0), 0);
  document.getElementById('statSize').textContent = formatBytes(totalBytes);

  if (data.length === 0) {
    body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:34px">Aún no hay grabaciones registradas</td></tr>';
    return;
  }

  data.slice().reverse().forEach(item => {
    const tr = document.createElement('tr');
    const typeBadge = item.type === 'scheduled'
      ? '<span class="badge badge-scheduled">📅 Programada</span>'
      : '<span class="badge badge-immediate">▶️ Directo</span>';

    let statusBadge = '';
    if (item.status === 'ok') {
      statusBadge = '<span class="badge badge-ok">✅ Completada</span>';
    } else if (item.status === 'stopped') {
      statusBadge = '<span class="badge badge-stopped">⏹️ Detenida</span>';
    } else {
      statusBadge = `<span class="badge badge-failed" title="${escapeHtml(item.status_reason)}">❌ Fallida</span>`;
      if (item.status_reason) {
        statusBadge += `<div style="font-size:11px;color:#fda4af;margin-top:3px;">${escapeHtml(item.status_reason)}</div>`;
      }
    }

    const realDur = item.media_duration_seconds != null
      ? secsToHHMMSS(item.media_duration_seconds)
      : (item.duration_seconds != null ? secsToHHMMSS(item.duration_seconds) : '--:--:--');

    const hasFile = (Number(item.filesize_bytes) || 0) > 0;
    const playBtn = hasFile
      ? `<button class="btn-primary btn-sm" title="Ver grabación aquí" data-filename="${escapeHtml(item.filename)}" onclick="openVideoPlayer(this.dataset.filename)">▶️ Ver</button> `
      : '';
    const dlBtn = hasFile
      ? `<a href="/download/${encodeURIComponent(item.filename)}" class="btn-success btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;" title="Descargar">⬇️</a> `
      : '';

    const vpnTag = item.vpn_country
      ? `<span class="badge-vpn">🌍 ${escapeHtml(item.vpn_country)}</span>`
      : '';

    tr.innerHTML = `
      <td class="cell-main">
        <div style="font-weight:600;color:#f8fafc;">${escapeHtml(item.filename)}</div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:3px;">
          <span style="font-size:11px;color:var(--text-muted);">${escapeHtml((item.format || '').toUpperCase())}</span>
          ${vpnTag}
        </div>
      </td>
      <td data-label="Modo">${typeBadge}</td>
      <td data-label="Inicio" style="font-size:12.5px;">${new Date(item.start).toLocaleString('es-ES')}</td>
      <td data-label="Dur. pedida" class="mono-input" style="font-size:12.5px;">${escapeHtml(item.duration_requested || '')}</td>
      <td data-label="Dur. grabada" class="mono-input" style="font-size:12.5px;">${realDur}</td>
      <td data-label="Tamaño" class="mono-input" style="font-size:12.5px;">${escapeHtml(item.filesize_human || '0 B')}</td>
      <td data-label="Estado">${statusBadge}</td>
      <td class="cell-actions" style="text-align:right;white-space:nowrap;">
        ${playBtn}${dlBtn}
        <button class="btn-danger btn-sm" title="Eliminar grabación" data-filename="${escapeHtml(item.filename)}" onclick="deleteRecording(this.dataset.filename)">🗑️</button>
      </td>
    `;
    body.appendChild(tr);
  });
}

async function deleteRecording(filename) {
  if (!confirm(`¿Eliminar el archivo "${filename}" y su entrada del historial?`)) return;
  const res  = await fetch('/history/delete/' + encodeURIComponent(filename), { method: 'POST' });
  const data = await res.json();
  showToast(data.message || 'Grabación eliminada', 'info');
  loadHistory();
}

async function clearHistory() {
  if (!confirm('¿Vaciar el registro del historial?')) return;
  const res  = await fetch('/history/clear', { method: 'POST' });
  const data = await res.json();
  showToast(data.message || 'Historial vaciado', 'info');
  loadHistory();
}

// ── Programador ────────────────────────────────────────────────────
function initScheduleDefaults() {
  const now   = new Date();
  const start = new Date(now.getTime() + 5 * 60 * 1000);
  const end   = new Date(start.getTime() + 60 * 60 * 1000);

  if (!document.getElementById('sStart').value) document.getElementById('sStart').value = fmtDatetimeLocal(start);
  if (!document.getElementById('sEnd').value)   document.getElementById('sEnd').value   = fmtDatetimeLocal(end);
  if (!document.getElementById('sFilename').value) {
    const tag = start.getFullYear() + pad2(start.getMonth() + 1) + pad2(start.getDate()) +
      '_' + pad2(start.getHours()) + pad2(start.getMinutes());
    document.getElementById('sFilename').value = 'prog_' + tag;
  }
  onScheduleDatesChanged();
}

document.getElementById('scheduleForm').onsubmit = async e => {
  e.preventDefault();
  const sStart = document.getElementById('sStart').value;
  const sEnd   = document.getElementById('sEnd').value;
  if (!sStart || !sEnd) { showToast('Completa las fechas de inicio y fin', 'error'); return; }

  const startRes = resolveInputDate(sStart, 'start');
  const endRes   = resolveInputDate(sEnd, 'end');

  if (endRes.date <= startRes.date) {
    showToast('La hora de fin debe ser posterior a la de inicio', 'error');
    return;
  }
  if (startRes.date < new Date(Date.now() - 30000)) {
    if (!confirm('⚠️ La hora de inicio ya ha pasado (comenzará inmediatamente). ¿Programar de todos modos?')) return;
  }

  const fd = new FormData();
  fd.append('url',         document.getElementById('sUrl').value.trim());
  fd.append('start',       toIsoWithOffset(startRes.date));
  fd.append('end',         toIsoWithOffset(endRes.date));
  fd.append('start_fold',  scheduleFolds.start);
  fd.append('end_fold',    scheduleFolds.end);
  fd.append('filename',    document.getElementById('sFilename').value.trim());
  fd.append('format',      document.getElementById('sFormat').value);
  fd.append('vpn_country', document.getElementById('sVpnCountry').value);

  const res  = await fetch('/schedule', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.error) {
    showToast(data.error, 'error');
    return;
  }
  showToast(data.message || 'Grabación programada con éxito', 'success');
  document.getElementById('sStart').value = '';
  document.getElementById('sEnd').value = '';
  document.getElementById('sFilename').value = '';
  document.getElementById('sVpnCountry').value = '';
  scheduleFolds = { start: 0, end: 0 };
  initScheduleDefaults();
  loadSchedules();
};

function formatDateTimeWithTz(isoStr) {
  const dt = new Date(isoStr);
  const str = dt.toLocaleString('es-ES', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  });
  const m = String(isoStr).match(/([+-]\d{2}:\d{2})$/);
  const tzTag = m ? ` <span class="badge-tz">UTC${m[1].replace(':00', '')}</span>` : '';
  return str + tzTag;
}

async function loadSchedules() {
  const res  = await fetch('/schedules');
  const data = await res.json();
  const body = document.getElementById('scheduleBody');
  body.innerHTML = '';

  const pendingCount = data.filter(j => j.status === 'pending' || j.status === 'running').length;
  document.getElementById('badgeSchedulesCount').textContent = pendingCount;

  if (data.length === 0) {
    body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:34px">No hay grabaciones programadas</td></tr>';
    return;
  }

  data.slice().reverse().forEach(job => {
    const tr = document.createElement('tr');
    const startDt = new Date(job.start_time);
    const endDt   = new Date(job.end_time);
    const durSecs = Math.max(0, Math.round((endDt.getTime() - startDt.getTime()) / 1000));

    const badge = {
      pending:   '<span class="badge badge-pending">🕐 Pendiente</span>',
      running:   '<span class="badge badge-running">🔴 Grabando</span>',
      ok:        '<span class="badge badge-ok">✅ Completada</span>',
      stopped:   '<span class="badge badge-stopped">⏹️ Detenida</span>',
      failed:    '<span class="badge badge-failed">❌ Fallida</span>',
      cancelled: '<span class="badge badge-cancelled">🚫 Cancelada</span>',
    }[job.status] || `<span class="badge">${escapeHtml(job.status)}</span>`;

    let actions = '';
    if (job.status === 'pending') {
      actions += `<button class="btn-warning btn-sm" title="Cancelar programación" onclick="cancelSchedule('${job.id}')">🚫 Cancelar</button> `;
    } else if (job.status === 'running') {
      actions += `<button class="btn-danger btn-sm" title="Detener grabación en curso" onclick="stopRecord()">⏹️ Detener</button> `;
    } else {
      if (job.status === 'ok' || job.status === 'stopped') {
        actions += `<button class="btn-primary btn-sm" title="Ver grabación" data-filename="${escapeHtml(job.filename)}" onclick="openVideoPlayer(this.dataset.filename)">▶️ Ver</button> `;
        actions += `<a href="/download/${encodeURIComponent(job.filename)}" class="btn-success btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;" title="Descargar">⬇️</a> `;
      }
      actions += `<button class="btn-danger btn-sm" title="Borrar registro" onclick="deleteSchedule('${job.id}')">🗑️</button>`;
    }

    let infoCell = '';
    if (job.status === 'pending') {
      infoCell = `<span class="countdown" data-start="${escapeHtml(job.start_time)}">...</span>`;
    } else if (job.status === 'running') {
      infoCell = `<span class="countdown" style="color:#6ee7b7;" data-end="${escapeHtml(job.end_time)}">En curso...</span>`;
    } else if (job.status === 'ok' || job.status === 'stopped') {
      infoCell = `<span style="color:#a7f3d0;font-size:12px;">${escapeHtml(job.filesize_human || 'Finalizada')}</span>`;
    } else if (job.status === 'failed' && job.status_reason) {
      infoCell = `<span style="color:#fda4af;font-size:12px;">${escapeHtml(job.status_reason)}</span>`;
    }

    const vpnTag = job.vpn_country
      ? `<div style="margin-top:3px;"><span class="badge-vpn">🌍 VPN: ${escapeHtml(job.vpn_country)}</span></div>`
      : `<div style="margin-top:3px;font-size:11px;color:var(--text-muted);">🛡️ VPN actual</div>`;

    tr.innerHTML = `
      <td class="cell-main">
        <strong>${escapeHtml(job.filename)}</strong>
        ${vpnTag}
      </td>
      <td data-label="Inicio" style="font-size:12.5px;">${formatDateTimeWithTz(job.start_time)}</td>
      <td data-label="Fin" style="font-size:12.5px;">${formatDateTimeWithTz(job.end_time)}</td>
      <td data-label="Duración" class="mono-input" style="font-size:12.5px;">${secsToHHMMSS(durSecs)}</td>
      <td data-label="Formato"><span class="badge-tz">${escapeHtml((job.format || '').toUpperCase())}</span></td>
      <td data-label="Estado">${badge}</td>
      <td data-label="Detalle">${infoCell}</td>
      <td class="cell-actions" style="white-space:nowrap;text-align:right;">${actions}</td>
    `;
    body.appendChild(tr);
  });
  updateCountdowns();
}

function updateCountdowns() {
  const now = Date.now();
  document.querySelectorAll('.countdown[data-start]').forEach(el => {
    const diff = Math.round((new Date(el.dataset.start).getTime() - now) / 1000);
    el.textContent = diff > 0 ? '⏳ En ' + secsToHHMMSS(diff) : '🔜 Iniciando...';
  });
  document.querySelectorAll('.countdown[data-end]').forEach(el => {
    const diff = Math.round((new Date(el.dataset.end).getTime() - now) / 1000);
    el.textContent = diff > 0 ? '⏺ Restan ' + secsToHHMMSS(diff) : '🏁 Finalizando...';
  });
}
setInterval(updateCountdowns, 1000);

setInterval(() => {
  if (document.getElementById('scheduleView').style.display !== 'none') loadSchedules();
}, 6000);

async function cancelSchedule(id) {
  if (!confirm('¿Cancelar esta grabación programada?')) return;
  await fetch('/schedules/cancel/' + id, { method: 'POST' });
  showToast('Programación cancelada', 'info');
  loadSchedules();
}

async function deleteSchedule(id) {
  await fetch('/schedules/delete/' + id, { method: 'POST' });
  showToast('Registro eliminado', 'info');
  loadSchedules();
}

async function clearSchedules() {
  if (!confirm('¿Limpiar de la lista todas las programaciones ya finalizadas/canceladas?')) return;
  await fetch('/schedules/clear', { method: 'POST' });
  showToast('Programaciones antiguas limpiadas', 'info');
  loadSchedules();
}

// ── Reproductor de Vídeo Modal "Modo Cine" + Multipista de Audio ───
let pState = {
  open: false,
  filename: '',
  duration: 0,
  audioTracks: [],
  activeTrackIdx: 0,
  isPipeMode: false,
  pipeStartOffset: 0,
  hideTimer: null
};

const proVideo = document.getElementById('proVideo');

async function openVideoPlayer(filename) {
  pState.open = true;
  pState.filename = filename;
  pState.duration = 0;
  pState.audioTracks = [];
  pState.activeTrackIdx = 0;
  pState.isPipeMode = filename.toLowerCase().endsWith('.ts');
  pState.pipeStartOffset = 0;

  document.getElementById('playerModalTitle').textContent = filename;
  document.getElementById('playerCodecBadge').style.display = 'none';
  document.getElementById('playerModalBackdrop').classList.add('open');

  pState.shareUrl = window.location.origin + '/stream/' + encodeURIComponent(filename);
  document.getElementById('playerVlcBtn').href = 'vlc://' + pState.shareUrl;
  document.getElementById('playerDownloadBtn').href = '/download/' + encodeURIComponent(filename);

  // Consultar metadatos y pistas de audio reales con ffprobe
  try {
    const res = await fetch('/api/media-info/' + encodeURIComponent(filename));
    const info = await res.json();
    pState.duration = Number(info.duration) || 0;
    pState.audioTracks = info.audio_tracks || [];
    // Con autenticación activa, VLC u otros reproductores necesitan un enlace firmado
    if (info.sig) {
      pState.shareUrl += '?sig=' + encodeURIComponent(info.sig);
      document.getElementById('playerVlcBtn').href = 'vlc://' + pState.shareUrl;
    }

    const badge = document.getElementById('playerCodecBadge');
    const parts = [];
    if (info.video_codec) parts.push(info.video_codec.toUpperCase());
    if (pState.audioTracks.length > 0) parts.push(`${pState.audioTracks.length} audio${pState.audioTracks.length > 1 ? 's' : ''}`);
    if (parts.length > 0) {
      badge.textContent = parts.join(' · ');
      badge.style.display = 'inline-block';
    }
  } catch (e) {
    pState.audioTracks = [{ index: 0, label: 'Pista 1 · Principal' }];
  }

  renderAudioTracksMenu();
  loadPlayerSource(0, 0);
}

function renderAudioTracksMenu() {
  const container = document.getElementById('vAudioMenuItems');
  const btnText   = document.getElementById('vAudioBtnText');
  container.innerHTML = '';

  const tracks = pState.audioTracks.length > 0
    ? pState.audioTracks
    : [{ index: 0, label: 'Pista 1 · Principal' }];

  btnText.textContent = tracks.length > 1
    ? `Pista ${pState.activeTrackIdx + 1} (${tracks.length})`
    : 'Pista 1';

  tracks.forEach((tr, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'v-menu-item' + (i === pState.activeTrackIdx ? ' active' : '');
    btn.innerHTML = `<span>${escapeHtml(tr.label)}</span><span>${i === pState.activeTrackIdx ? '✓' : ''}</span>`;
    btn.onclick = () => selectAudioTrack(i);
    container.appendChild(btn);
  });
}

function loadPlayerSource(trackIdx, seekSeconds = 0) {
  pState.activeTrackIdx = trackIdx;
  const isTs = pState.filename.toLowerCase().endsWith('.ts');
  const modeInfo = document.getElementById('playerModeInfo');

  // Si es la Pista 1 (índice 0) y no es .ts, usamos lectura directa de disco (HTTP Range 206)
  // Si el usuario selecciona Pista 2, 3... (o archivo .ts), usamos tubería en memoria (-c copy pipe:1, 0 bytes en disco)
  if (trackIdx === 0 && !isTs) {
    pState.isPipeMode = false;
    pState.pipeStartOffset = 0;
    proVideo.src = '/stream/' + encodeURIComponent(pState.filename);
    proVideo.currentTime = Math.max(0, seekSeconds);
    modeInfo.textContent = '⚡ Streaming directo desde disco (0% CPU · 0 bytes extra)';
  } else {
    pState.isPipeMode = true;
    pState.pipeStartOffset = Math.max(0, seekSeconds);
    const params = new URLSearchParams({
      track: String(trackIdx),
      start: String(Math.floor(pState.pipeStartOffset))
    });
    proVideo.src = '/stream-track/' + encodeURIComponent(pState.filename) + '?' + params.toString();
    modeInfo.textContent = `🎧 Reproduciendo Pista ${trackIdx + 1} en memoria (-c copy · 0 bytes en disco)`;
  }

  renderAudioTracksMenu();
  proVideo.play().catch(() => {});
}

function getEffectiveCurrentTime() {
  if (pState.isPipeMode) {
    return pState.pipeStartOffset + (Number(proVideo.currentTime) || 0);
  }
  return Number(proVideo.currentTime) || 0;
}

function getEffectiveDuration() {
  if (pState.duration > 0) return pState.duration;
  if (proVideo.duration && isFinite(proVideo.duration)) return proVideo.duration;
  return 0;
}

function selectAudioTrack(trackIdx) {
  document.getElementById('vAudioMenu').classList.remove('open');
  if (trackIdx === pState.activeTrackIdx) return;

  const currentSec = getEffectiveCurrentTime();
  // Si el navegador soporta audioTracks nativo en modo directo, cambiar también ahí si procede
  if (!pState.isPipeMode && proVideo.audioTracks && proVideo.audioTracks.length > trackIdx) {
    for (let i = 0; i < proVideo.audioTracks.length; i++) {
      proVideo.audioTracks[i].enabled = (i === trackIdx);
    }
    pState.activeTrackIdx = trackIdx;
    renderAudioTracksMenu();
    showToast(`Cambiado a Pista ${trackIdx + 1}`, 'info');
    return;
  }

  loadPlayerSource(trackIdx, currentSec);
  showToast(`Cambiado a Pista de audio ${trackIdx + 1}`, 'info');
}

function seekPlayerTo(targetSec) {
  const dur = getEffectiveDuration();
  if (dur > 0) targetSec = Math.max(0, Math.min(dur - 1, targetSec));
  else targetSec = Math.max(0, targetSec);

  if (!pState.isPipeMode) {
    proVideo.currentTime = targetSec;
  } else {
    loadPlayerSource(pState.activeTrackIdx, targetSec);
  }
}

function skipPlayerTime(deltaSec) {
  seekPlayerTo(getEffectiveCurrentTime() + deltaSec);
}

function togglePlayPause() {
  closeAllPlayerPopups();
  if (proVideo.paused) proVideo.play().catch(() => {});
  else proVideo.pause();
}

proVideo.addEventListener('play', () => {
  document.getElementById('vPlayBtn').textContent = '⏸️';
  onPlayerMouseMove();
});
proVideo.addEventListener('pause', () => {
  document.getElementById('vPlayBtn').textContent = '▶️';
  document.getElementById('videoStage').classList.remove('hide-controls');
});
proVideo.addEventListener('timeupdate', () => {
  const cur = getEffectiveCurrentTime();
  const dur = getEffectiveDuration();
  document.getElementById('vTimeLabel').textContent = `${secsToHHMMSS(cur)} / ${secsToHHMMSS(dur)}`;
  const pct = dur > 0 ? Math.min(100, (cur / dur) * 100) : 0;
  document.getElementById('vTimelineFill').style.width = pct.toFixed(2) + '%';
});

function onTimelineHover(e) {
  const rect = document.getElementById('vTimeline').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = getEffectiveDuration();
  const tt = document.getElementById('vTimelineTooltip');
  tt.style.left = (ratio * 100) + '%';
  tt.textContent = secsToHHMMSS(ratio * dur);
}

function onTimelineSeek(e) {
  const rect = document.getElementById('vTimeline').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = getEffectiveDuration();
  if (dur > 0) seekPlayerTo(ratio * dur);
}

function togglePlayerMute() {
  proVideo.muted = !proVideo.muted;
  document.getElementById('vMuteBtn').textContent = proVideo.muted ? '🔇' : '🔊';
}

function onVolumeChange(val) {
  proVideo.volume = Number(val);
  proVideo.muted = (Number(val) === 0);
  document.getElementById('vMuteBtn').textContent = proVideo.muted ? '🔇' : '🔊';
}

function togglePlayerPopup(menuId) {
  ['vAudioMenu', 'vSpeedMenu'].forEach(id => {
    const el = document.getElementById(id);
    if (id === menuId) el.classList.toggle('open');
    else el.classList.remove('open');
  });
}

function closeAllPlayerPopups() {
  ['vAudioMenu', 'vSpeedMenu'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('open');
  });
}

function setPlayerSpeed(rate) {
  proVideo.playbackRate = rate;
  document.getElementById('vSpeedBtnText').textContent = rate + 'x';
  const menu = document.getElementById('vSpeedMenu');
  menu.querySelectorAll('.v-menu-item').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.startsWith(rate + 'x'));
  });
  menu.classList.remove('open');
}

function togglePlayerFullscreen() {
  const stage = document.getElementById('videoStage');
  if (!document.fullscreenElement) {
    if (stage.requestFullscreen) stage.requestFullscreen();
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
  }
}

function onPlayerMouseMove() {
  const stage = document.getElementById('videoStage');
  stage.classList.remove('hide-controls');
  clearTimeout(pState.hideTimer);
  if (!proVideo.paused) {
    pState.hideTimer = setTimeout(() => {
      closeAllPlayerPopups();
      stage.classList.add('hide-controls');
    }, 2600);
  }
}

function copyCurrentStreamUrl() {
  const url = pState.shareUrl || (window.location.origin + '/stream/' + encodeURIComponent(pState.filename));
  copyToClipboard(url, pState.filename);
}

function closeVideoPlayer() {
  pState.open = false;
  clearTimeout(pState.hideTimer);
  closeAllPlayerPopups();
  proVideo.pause();
  proVideo.removeAttribute('src');
  proVideo.load();
  if (document.fullscreenElement && document.exitFullscreen) {
    document.exitFullscreen().catch(() => {});
  }
  document.getElementById('playerModalBackdrop').classList.remove('open');
}

document.addEventListener('keydown', e => {
  if (!pState.open) return;
  if (e.key === 'Escape') { closeVideoPlayer(); }
  else if (e.key === ' ' && document.activeElement.tagName !== 'BUTTON') { e.preventDefault(); togglePlayPause(); }
  else if (e.key === 'ArrowLeft')  { e.preventDefault(); skipPlayerTime(-10); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); skipPlayerTime(10); }
  else if (e.key.toLowerCase() === 'f') { e.preventDefault(); togglePlayerFullscreen(); }
  else if (e.key.toLowerCase() === 'm') { e.preventDefault(); togglePlayerMute(); }
});
