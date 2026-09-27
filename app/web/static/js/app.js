'use strict';
/* ETH DGT Bot — utilidades compartidas por todas las páginas */

const REFRESH_MS = (Number(document.body.dataset.refresh) || 30) * 1000;

// ── Formato ─────────────────────────────────────────────────────────────
const nf = (v, d = 2) => Number(v).toLocaleString('es-AR', { minimumFractionDigits: d, maximumFractionDigits: d });
function usd(v, d = 2) { return (v === null || v === undefined) ? '$—' : '$' + nf(v, d); }
function signedUsd(v) {
  if (v === null || v === undefined) return '$—';
  const d = Math.abs(v) < 1 && v !== 0 ? 3 : 2;
  return (v > 0 ? '+' : v < 0 ? '−' : '') + '$' + nf(Math.abs(v), d);
}
function signedPct(v) { return (v > 0 ? '+' : v < 0 ? '−' : '') + nf(Math.abs(v || 0), 2) + '%'; }
function cls(v) { return v > 0 ? 't-pos' : (v < 0 ? 't-neg' : 't-neu'); }
function ago(iso) {
  if (!iso) return '';
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return 'recién';
  if (m < 60) return `hace ${m} min`;
  const h = Math.round(m / 60);
  if (h < 48) return `hace ${h} h`;
  return `hace ${Math.round(h / 24)} días`;
}
function hhmm(iso) { return iso ? new Date(iso).toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' }) : '—'; }
function dayLabel(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('es-AR', { weekday: 'short', day: '2-digit', month: '2-digit' });
}
function icon(name, extra = '') { return `<svg class="ic ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`; }

// ── DOM ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function setText(id, text) { const el = $(id); if (el) el.textContent = text; }
function setVal(id, text, v) {
  const el = $(id); if (!el) return;
  el.textContent = text;
  el.classList.remove('t-pos', 't-neg', 't-neu');
  if (v !== undefined) el.classList.add(cls(v));
}
function setDelta(id, text, v) {
  const el = $(id); if (!el) return;
  el.textContent = text;
  el.className = 'delta ' + cls(v);
}

// ── Red ─────────────────────────────────────────────────────────────────
async function getJson(url) { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); }
async function apiPost(url, body = null, label = 'OK') {
  try {
    const opts = { method: 'POST' };
    if (body) opts.body = body;
    const r = await fetch(url, opts);
    const d = await r.json();
    if (d.ok !== false) toast(label);
    else toast('Error: ' + (d.error || 'desconocido'), false);
    return d;
  } catch (e) { toast('Error de red', false); return null; }
}

// ── Feedback ────────────────────────────────────────────────────────────
function toast(msg, ok = true) {
  const t = $('toast');
  t.innerHTML = icon(ok ? 'check' : 'alert') + '<span></span>';
  t.querySelector('span').textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(t._tid);
  t._tid = setTimeout(() => { t.className = 'toast'; }, 3500);
}

function confirmDialog({ title, text, confirm = 'Confirmar', danger = false }) {
  const dlg = $('confirmDlg');
  dlg.querySelector('.modal-title').textContent = title;
  dlg.querySelector('.modal-text').textContent = text;
  dlg.querySelector('.modal-icon').className = 'modal-icon' + (danger ? ' danger' : '');
  dlg.querySelector('.modal-icon').innerHTML = icon(danger ? 'alert' : 'info', 'ic-lg');
  const ok = dlg.querySelector('button[value=ok]');
  ok.textContent = confirm;
  ok.className = 'btn ' + (danger ? 'btn-danger-solid' : 'btn-primary');
  dlg.returnValue = '';
  dlg.showModal();
  return new Promise(resolve => { dlg.onclose = () => resolve(dlg.returnValue === 'ok'); });
}

// ── Sidebar móvil ───────────────────────────────────────────────────────
function toggleSidebar(open) {
  const show = open ?? !$('sidebar').classList.contains('open');
  $('sidebar').classList.toggle('open', show);
  $('overlay').classList.toggle('show', show);
}

// ── Estado global (sidebar + barra superior) ────────────────────────────
const Shell = {
  status: null,
  livePrice: null,

  async load() {
    try { Shell.status = await getJson('/api/eth/status'); } catch (e) { return Shell.status; }
    Shell.render();
    return Shell.status;
  },

  render() {
    const s = Shell.status; if (!s) return;
    const on = s.bot_status === 'on';
    const pb = $('pillBot');
    pb.className = 'pill ' + (on ? 'pill-on' : 'pill-off');
    pb.textContent = on ? 'Encendido' : 'Apagado';
    const pm = $('pillMode');
    pm.className = 'pill ' + (s.dry_run ? 'pill-sim' : 'pill-live');
    pm.textContent = s.dry_run ? 'Simulador' : 'Live';
    Shell.setPrice(Shell.livePrice || s.price);
    Shell.renderNext();
  },

  setPrice(p, live = false) {
    if (live) Shell.livePrice = p;
    if (p) setText('tbPrice', usd(p));
  },

  renderNext() {
    const s = Shell.status; if (!s) return;
    let text = '—';
    if (s.bot_status !== 'on') text = 'Bot apagado';
    else if (s.next_tick) {
      const m = Math.round((new Date(s.next_tick).getTime() - Date.now()) / 60000);
      text = m < 1 ? 'Chequeando…' : `Próximo chequeo en ${m} min`;
    }
    setText('tbNext', text);
  },
};
setInterval(Shell.renderNext, 15000);

// Cada página llama every(refresh); las que no tienen datos propios solo refrescan el estado global.
function every(fn) { window.__pageRefresh = true; fn(); setInterval(fn, REFRESH_MS); }
document.addEventListener('DOMContentLoaded', () => { if (!window.__pageRefresh) every(Shell.load); });
