"""
Dashboard para ver HUNTER mientras corre + paper trading (simulado).

Corre en el mismo proceso que main.py (una tarea asyncio más, ver
ACTIVE_LISTENERS/tasks ahí) -- así puede leer el precio de SOL/USD
cacheado en memoria (core/sol_price.py) sin tener que sincronizarlo
entre procesos. El resto de los datos (alertas, transacciones,
posiciones) los lee directo de data/hunter.db.

IMPORTANTE -- esto NUNCA ejecuta una operación real. Los botones de
"comprar"/"cerrar" abren y cierran posiciones simuladas (ver
core/paper_trading.py), sin firmar ninguna transacción ni tocar una
wallet real. Es la validación que pide el README antes de poder pensar
en ejecución real: MODE se queda en "alert_only" en config/settings.py
hasta tener resultados consistentes acá.
"""
import json
import logging
import secrets
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config.settings import MODE, DASHBOARD_USERNAME, DASHBOARD_PASSWORD
from core.db import (
    get_conn, get_paper_positions, get_last_transaction, get_setting, set_setting, now_iso,
)
from core.ev_calculator import FeeStructure, net_result_of_position
from core.paper_trading import open_position, close_position, DEFAULT_POSITION_USD, fees_for, free_cash_usd
from config.settings import SIM_BANKROLL_USD
from core.sol_price import get_cached_sol_usd

_FEES = FeeStructure()

logger = logging.getLogger("hunter.dashboard")

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    """Usuario/clave (config/settings.py) -- necesario porque cuando el
    dashboard queda expuesto a internet (ver deploy/), los botones de
    Comprar/Cerrar quedarían accesibles para cualquiera sin esto. Se
    compara con secrets.compare_digest para no filtrar la clave por
    diferencia de tiempo de respuesta (timing attack)."""
    user_ok = secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401, detail="Usuario o clave incorrectos",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# dependencies=[...] a nivel app -- protege TODAS las rutas de una sola
# vez (index, /api/data, /api/paper/*), así no hay riesgo de agregar
# una ruta nueva más adelante y olvidarse de protegerla.
app = FastAPI(dependencies=[Depends(require_auth)])

HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>HUNTER</title>
<style>
  :root { color-scheme: dark; }
  body {
    background: #0d1117; color: #c9d1d9;
    font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
    margin: 0; padding: 24px;
  }
  h1 { font-size: 18px; margin: 0 0 4px 0; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .stats { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 16px; min-width: 140px;
  }
  .card .label { color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .card .value { font-size: 20px; margin-top: 4px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .badge.alert_only { background: #1f6feb33; color: #58a6ff; }
  .badge.buy { color: #3fb950; }
  .badge.sell { color: #f85149; }
  h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; margin: 28px 0 8px 0; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #21262d; white-space: nowrap; }
  th { color: #8b949e; font-weight: 500; }
  .mono { color: #8b949e; }
  .empty { color: #8b949e; font-style: italic; padding: 12px 0; }
  button {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; cursor: pointer;
    font-family: inherit;
  }
  button:hover { background: #30363d; }
  button:disabled { opacity: 0.5; cursor: default; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .paper-banner {
    background: #1f6feb1a; border: 1px solid #1f6feb55; border-radius: 8px;
    padding: 10px 14px; font-size: 12px; color: #79c0ff; margin-bottom: 16px;
  }
  .toast {
    position: fixed; top: 16px; right: 16px; padding: 10px 16px;
    border-radius: 8px; font-size: 12px; max-width: 320px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 100;
    opacity: 0; transform: translateY(-8px); transition: opacity 0.2s, transform 0.2s;
    pointer-events: none;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.ok { background: #1f6feb; color: white; }
  .toast.err { background: #da3633; color: white; }
  .already-bought { color: #3fb950; font-size: 11px; display: block; margin-top: 4px; }
  .toolbar {
    display: flex; align-items: center; gap: 12px; margin-bottom: 6px;
    font-size: 11px; color: #8b949e;
  }
  select {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; padding: 4px 8px; font-size: 11px; font-family: inherit;
  }
  .pager { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .pager button { padding: 2px 8px; }
  .token-cell { display: flex; align-items: center; gap: 6px; }
  .copy-btn, .explorer-link {
    padding: 1px 6px; font-size: 10px; border-radius: 4px; text-decoration: none;
  }
  .explorer-link { background: #1f6feb33; color: #79c0ff; border: 1px solid #1f6feb55; }
  .explorer-link:hover { background: #1f6feb55; }
  .view-bar {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 11px; color: #8b949e; margin-bottom: 16px;
  }
  .view-bar .link-btn {
    background: none; border: none; color: #79c0ff; text-decoration: underline;
    cursor: pointer; font-size: 11px; padding: 0; font-family: inherit;
  }
</style>
</head>
<body>
  <h1>HUNTER</h1>
  <div class="sub">Detector de manadas -- se refresca solo cada 5s -- horarios en Buenos Aires (UTC-3)</div>
  <div class="paper-banner">
    Paper trading: los botones "Comprar"/"Cerrar" simulan operaciones a precio
    de mercado real, SIN plata real. Nada acá firma una transacción de verdad.
  </div>
  <div class="view-bar">
    <span id="view-status">Mostrando: todo el historial</span>
    <button onclick="resetView()">Limpiar vista (desde ahora)</button>
    <button id="show-all-btn" class="link-btn" onclick="showAllView()" hidden>ver todo el historial</button>
  </div>
  <div id="toast" class="toast"></div>

  <div class="stats">
    <div class="card"><div class="label">Modo</div><div class="value" id="mode">--</div></div>
    <div class="card"><div class="label">SOL/USD</div><div class="value" id="sol-price">--</div></div>
    <div class="card"><div class="label">Alertas</div><div class="value" id="alert-count">--</div></div>
    <div class="card"><div class="label">Posiciones abiertas (paper)</div><div class="value" id="open-count">--</div></div>
    <div class="card"><div class="label">PnL realizado (paper)</div><div class="value" id="closed-pnl">--</div></div>
  </div>

  <h2>Alertas de manada</h2>
  <div class="toolbar">
    <label>Ordenar: <select id="alerts-sort" onchange="onAlertsSortChange()">
      <option value="time_desc">Más reciente</option>
      <option value="best_1h">Mejor resultado a 1h</option>
      <option value="worst_1h">Peor resultado a 1h</option>
    </select></label>
    <div class="pager">
      <button onclick="changePage('alerts', -1)">&larr;</button>
      <span id="alerts-page-label">-- </span>
      <button onclick="changePage('alerts', 1)">&rarr;</button>
    </div>
  </div>
  <table id="alerts-table">
    <thead><tr>
      <th>Hora</th><th>Token</th><th>Edad del token</th><th>Wallets</th>
      <th>brain.py</th>
      <th>win-rate wallets</th>
      <th>compra ($)</th>
      <th>historial dev</th>
      <th>bundled</th>
      <th>filtro</th>
      <th>5m (%)</th><th>30m (%)</th><th>1h (%)</th><th>Estado</th><th></th>
    </tr></thead>
    <tbody id="alerts-body"></tbody>
  </table>

  <h2>Posiciones de paper trading</h2>
  <div class="toolbar">
    <label>Ordenar: <select id="positions-sort" onchange="onPositionsSortChange()">
      <option value="time_desc">Más reciente</option>
      <option value="best_pnl">Mayor ganancia</option>
      <option value="worst_pnl">Mayor pérdida</option>
    </select></label>
    <div class="pager">
      <button onclick="changePage('positions', -1)">&larr;</button>
      <span id="positions-page-label">--</span>
      <button onclick="changePage('positions', 1)">&rarr;</button>
    </div>
  </div>
  <table id="positions-table">
    <thead><tr>
      <th>Abierta</th><th>Token</th><th>USD</th><th>Restante</th>
      <th>Precio (%)</th><th>PnL total ($)</th><th>PnL total (%)</th>
      <th>Estado</th><th></th>
    </tr></thead>
    <tbody id="positions-body"></tbody>
  </table>

  <h2>Transacciones</h2>
  <div class="toolbar">
    <div class="pager">
      <button onclick="changePage('tx', -1)">&larr;</button>
      <span id="tx-page-label">--</span>
      <button onclick="changePage('tx', 1)">&rarr;</button>
    </div>
  </div>
  <table id="tx-table">
    <thead><tr>
      <th>Hora</th><th>Wallet</th><th>Token</th><th>Lado</th><th>USD</th><th>Precio</th>
    </tr></thead>
    <tbody id="tx-body"></tbody>
  </table>

<script>
const PAGE_SIZE = 25;
let alertsSort = "time_desc";
let alertsPage = 1;
let positionsSort = "time_desc";
let positionsPage = 1;
let txPage = 1;
let lastData = null;

function short(s, n) {
  if (!s) return "--";
  return s.length > n ? s.slice(0, n) + "…" : s;
}
// Todo se guarda en UTC en la base (ver core/db.py::now_iso) -- acá
// solo se muestra convertido a Buenos Aires (UTC-3 fijo, sin horario
// de verano desde 2009) para tener una referencia horaria real.
function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "--";
  const ars = new Date(d.getTime() - 3 * 60 * 60 * 1000);
  const pad = n => String(n).padStart(2, "0");
  return `${ars.getUTCFullYear()}-${pad(ars.getUTCMonth() + 1)}-${pad(ars.getUTCDate())} `
       + `${pad(ars.getUTCHours())}:${pad(ars.getUTCMinutes())}:${pad(ars.getUTCSeconds())}`;
}
function fmtNum(x, digits) {
  if (x === null || x === undefined) return "--";
  return Number(x).toPrecision(digits);
}
function fmtPct(x) {
  if (x === null || x === undefined) return "--";
  return (x >= 0 ? "+" : "") + x.toFixed(1) + "%";
}
// % de cambio entre un precio actual/posterior y un precio base (el de
// la alerta o el de entrada) -- lo que la gente realmente quiere ver
// en vez de la notación científica cruda del precio.
function fmtChangePct(current, base) {
  if (current === null || current === undefined || !base) return null;
  return ((current / base) - 1) * 100;
}
function fmtAge(seconds) {
  // Edad del token al momento de la alerta -- None si todavía no vimos
  // la creación de ese token (ver core/token_lifecycle_solana.py y
  // chains/robinhood.py). No se inventa un valor cuando no lo sabemos.
  if (seconds === null || seconds === undefined) return "sin datos";
  if (seconds < 60) return seconds.toFixed(0) + "s";
  if (seconds < 3600) return (seconds / 60).toFixed(1) + "m";
  return (seconds / 3600).toFixed(1) + "h";
}
// Mientras no pasó suficiente tiempo real desde la alerta, el job de
// outcome-tracking (core/outcome_tracker.py) todavía no completó esta
// columna -- mostramos cuánto falta en vez de dejarlo vacío sin
// explicación.
function fmtIntervalPctCell(value, basePrice, triggeredAtIso, intervalSeconds) {
  if (value !== null && value !== undefined) return fmtPct(fmtChangePct(value, basePrice));
  const elapsedMs = Date.now() - new Date(triggeredAtIso).getTime();
  const remainingSec = intervalSeconds - elapsedMs / 1000;
  if (remainingSec > 0) {
    return `<span class="mono">faltan ${Math.ceil(remainingSec / 60)}m</span>`;
  }
  return '<span class="mono">calculando…</span>';
}

// Dónde verificar un token en otra plataforma -- distinto por cadena
// (DexScreener no tiene indexada Robinhood Chain todavía, ver README,
// así que ahí apunta al explorador nativo de esa cadena).
function explorerUrl(chain, tokenAddress) {
  if (chain === "solana") return `https://dexscreener.com/solana/${tokenAddress}`;
  if (chain === "robinhood") return `https://robinhoodchain.blockscout.com/token/${tokenAddress}`;
  return null;
}

function copyToClipboard(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const original = btn.textContent;
    btn.textContent = "✓";
    setTimeout(() => { btn.textContent = original; }, 1200);
  }).catch(() => showToast("No se pudo copiar -- copiá manualmente", true));
}

// Dirección COMPLETA del token (no recortada) + botón de copiar + link
// directo al explorador para verificarlo en otra plataforma.
function tokenCell(chain, tokenAddress) {
  const url = explorerUrl(chain, tokenAddress);
  const link = url ? `<a class="explorer-link" href="${url}" target="_blank" rel="noopener">ver ↗</a>` : "";
  const safeAddr = tokenAddress.replace(/'/g, "\\'");
  return `
    <div class="token-cell">
      <span class="mono">${tokenAddress}</span>
      <button class="copy-btn" onclick="copyToClipboard('${safeAddr}', this)">copiar</button>
      ${link}
    </div>
  `;
}

// PnL total (%) de una posición -- realizado (ya vendido) + no
// realizado (lo que queda abierto), sobre el tamaño ORIGINAL. Centralizado
// acá porque se usa tanto para pintar la fila como para ordenar la tabla.
function totalPnlPctOf(p) {
  const isOpen = p.status === "open";
  const realized = p.pnl_usd || 0;
  const unrealized = isOpen ? (p.unrealized_pnl_usd || 0) : 0;
  const hasAnyPnl = p.pnl_usd !== null || (isOpen && p.unrealized_pnl_usd !== null && p.unrealized_pnl_usd !== undefined);
  if (!hasAnyPnl) return null;
  return ((realized + unrealized) / Number(p.amount_usd)) * 100;
}

function alertResultPct(a) {
  if (!a.price_after_1h || !a.price_at_alert) return null;
  return fmtChangePct(a.price_after_1h, a.price_at_alert);
}

function sortAlerts(alerts, sortKey) {
  if (sortKey === "time_desc") return alerts;
  const arr = [...alerts];
  arr.sort((a, b) => {
    const pa = alertResultPct(a), pb = alertResultPct(b);
    if (pa === null && pb === null) return 0;
    if (pa === null) return 1;
    if (pb === null) return -1;
    return sortKey === "best_1h" ? (pb - pa) : (pa - pb);
  });
  return arr;
}

function sortPositions(positions, sortKey) {
  if (sortKey === "time_desc") return positions;
  const arr = [...positions];
  arr.sort((a, b) => {
    const pa = totalPnlPctOf(a), pb = totalPnlPctOf(b);
    if (pa === null && pb === null) return 0;
    if (pa === null) return 1;
    if (pb === null) return -1;
    return sortKey === "best_pnl" ? (pb - pa) : (pa - pb);
  });
  return arr;
}

function paginate(arr, page) {
  const totalPages = Math.max(1, Math.ceil(arr.length / PAGE_SIZE));
  const clampedPage = Math.min(Math.max(1, page), totalPages);
  const start = (clampedPage - 1) * PAGE_SIZE;
  return { slice: arr.slice(start, start + PAGE_SIZE), page: clampedPage, totalPages, total: arr.length };
}

function changePage(which, delta) {
  if (which === "alerts") { alertsPage += delta; renderAlerts(); }
  else if (which === "positions") { positionsPage += delta; renderPositions(); }
  else if (which === "tx") { txPage += delta; renderTransactions(); }
}

function onAlertsSortChange() {
  alertsSort = document.getElementById("alerts-sort").value;
  alertsPage = 1;
  renderAlerts();
}

function onPositionsSortChange() {
  positionsSort = document.getElementById("positions-sort").value;
  positionsPage = 1;
  renderPositions();
}

function showToast(message, isError) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = "toast show " + (isError ? "err" : "ok");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { toast.className = "toast"; }, 4000);
}

// "Limpiar vista": NUNCA borra nada de la base -- solo guarda desde
// qué momento mostrar acá. brain.py sigue aprendiendo de todo el
// historial completo igual (ver dashboard.py::/api/view/reset).
async function resetView() {
  if (!confirm("Esto oculta todo lo anterior de la vista (no se borra nada de la base -- brain.py lo sigue usando). ¿Confirmás?")) return;
  await fetch("/api/view/reset", { method: "POST" });
  showToast("Vista limpiada -- desde ahora solo se muestra lo nuevo.", false);
  await refresh();
}

async function showAllView() {
  await fetch("/api/view/show_all", { method: "POST" });
  showToast("Mostrando todo el historial de nuevo.", false);
  await refresh();
}

async function buyPaper(chain, tokenAddress, btn) {
  btn.disabled = true;
  btn.textContent = "comprando...";
  try {
    const resp = await fetch("/api/paper/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chain, token_address: tokenAddress }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      showToast("No se pudo comprar: " + (body.error || "sin precio disponible"), true);
    } else {
      showToast(`✅ Posición abierta: $${body.amount_usd} en ${tokenAddress.slice(0, 10)}...`, false);
    }
  } catch (e) {
    showToast("Error de red al comprar: " + e, true);
  } finally {
    await refresh();
  }
}

async function closePaper(positionId, btn) {
  btn.disabled = true;
  btn.textContent = "cerrando...";
  try {
    const resp = await fetch(`/api/paper/close/${positionId}`, { method: "POST" });
    const body = await resp.json();
    if (!resp.ok) {
      showToast("No se pudo cerrar: " + (body.error || "sin precio disponible"), true);
    } else {
      const pnlText = body.pnl_usd >= 0 ? `+$${body.pnl_usd.toFixed(2)}` : `-$${Math.abs(body.pnl_usd).toFixed(2)}`;
      showToast(`Posición #${positionId} cerrada -- PnL neto: ${pnlText}`, body.pnl_usd < 0);
    }
  } catch (e) {
    showToast("Error de red al cerrar: " + e, true);
  } finally {
    await refresh();
  }
}

function renderAlerts() {
  if (!lastData) return;
  const sorted = sortAlerts(lastData.alerts, alertsSort);
  const { slice, page, totalPages, total } = paginate(sorted, alertsPage);
  alertsPage = page;
  document.getElementById("alerts-page-label").textContent = `${page}/${totalPages} (${total})`;

  const alertsBody = document.getElementById("alerts-body");
  alertsBody.innerHTML = "";
  if (slice.length === 0) {
    alertsBody.innerHTML = '<tr><td colspan="15" class="empty">Todavía no se detectó ninguna manada.</td></tr>';
  }
  for (const a of slice) {
    const existing = lastData.positions.filter(
      p => p.chain === a.chain && p.token_address === a.token_address
    ).length;
    const buyLabel = existing > 0 ? `Comprar otra vez ($${lastData.default_position_usd})` : `Comprar ($${lastData.default_position_usd})`;
    const alreadyTag = existing > 0
      ? `<span class="already-bought">✓ ${existing} posición(es) abierta(s) de este token</span>` : "";
    // Wallets: si siguieron comprando después del disparo (ver
    // core/stampede.py), mostramos el pico real además del umbral fijo.
    const walletsText = (a.peak_wallet_count && a.peak_wallet_count > a.wallet_count)
      ? `${a.wallet_count} <span class="mono">(pico: ${a.peak_wallet_count})</span>` : `${a.wallet_count}`;
    // brain.py: puramente informativo, no filtra la compra -- ver README.
    const brainText = (a.brain_predicted_prob === null || a.brain_predicted_prob === undefined)
      ? '<span class="mono">sin modelo</span>' : (a.brain_predicted_prob * 100).toFixed(1) + "%";
    // Win-rate histórico de las wallets de la manada -- NUEVO 2026-09-16,
    // igual de informativo que brain.py todavía (ver core/db.py::
    // MIN_ALERTS_FOR_WALLET_WIN_RATE: con pocas apariciones por wallet
    // esto sigue siendo ruido estadístico, no una señal para filtrar).
    const winRateText = (a.avg_wallet_win_rate === null || a.avg_wallet_win_rate === undefined)
      ? '<span class="mono">sin muestra</span>'
      : `${(a.avg_wallet_win_rate * 100).toFixed(0)}% <span class="mono">(mín: ${(a.min_wallet_win_rate * 100).toFixed(0)}%)</span>`;
    // Tamaño de compra de las wallets de la manada -- NUEVO 2026-09-16.
    // Dato real (no informativo-especulativo como brain.py): con datos
    // reales, compras más CHICAS correlacionaron con MEJOR resultado
    // (ver análisis del mismo día) -- por eso no hay que leer "más
    // grande = mejor" acá, es al revés.
    const buyAmountText = (a.avg_wallet_buy_usd === null || a.avg_wallet_buy_usd === undefined)
      ? '<span class="mono">sin dato</span>'
      : `$${a.avg_wallet_buy_usd.toFixed(0)} <span class="mono">(mín: $${a.min_wallet_buy_usd.toFixed(0)})</span>`;
    // Historial del creador del token -- NUEVO 2026-09-17, inspirado en
    // ver a un trader real chequear esto antes de comprar (cuántos
    // tokens previos de un dev graduaron). Objetivo y verificable
    // on-chain, a diferencia de las demás columnas informativas.
    const devText = (a.creator_tokens_created === null || a.creator_tokens_created === undefined)
      ? '<span class="mono">sin muestra</span>'
      : `${(a.creator_migration_rate * 100).toFixed(0)}% <span class="mono">(${a.creator_tokens_created} tokens)</span>`;
    // "Bundled": % del volumen de compra de los primeros 60s que se
    // llevó 1 sola wallet -- NUEVO 2026-09-17. >70% es la banda donde
    // vimos peor win-rate con datos reales (ver core/db.py::
    // get_early_buy_concentration), se resalta en rojo como aviso
    // visual, no como filtro automático.
    const bundledText = (a.early_buy_concentration === null || a.early_buy_concentration === undefined)
      ? '<span class="mono">sin dato</span>'
      : `<span class="mono"${a.early_buy_concentration > 0.70 ? ' style="color:#e05252"' : ''}>${(a.early_buy_concentration * 100).toFixed(0)}%</span>`;
    // Filtro de entrada aprendido (core/entry_filter.py, 2026-09-18):
    // pass = operada, explore = descartada pero operada igual para
    // medir el filtro, skip = registrada sin operar.
    const decLabels = {pass: "operada", explore: "explora", skip: "descartada", nofilter: "sin filtro"};
    const decColor = {pass: "#2ecc71", explore: "#f1c40f", skip: "#e05252"};
    const filterText = (!a.entry_decision || a.entry_decision === "nofilter")
      ? '<span class="mono">--</span>'
      : `<span class="mono" style="color:${decColor[a.entry_decision] || "inherit"}">${decLabels[a.entry_decision] || a.entry_decision}${(a.entry_score !== null && a.entry_score !== undefined) ? " " + a.entry_score.toFixed(2) : ""}</span>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(a.triggered_at)}</td>
      <td>${tokenCell(a.chain, a.token_address)}</td>
      <td class="mono">${fmtAge(a.token_age_seconds)}</td>
      <td>${walletsText}</td>
      <td class="mono">${brainText}</td>
      <td class="mono">${winRateText}</td>
      <td class="mono">${buyAmountText}</td>
      <td class="mono">${devText}</td>
      <td>${bundledText}</td>
      <td>${filterText}</td>
      <td class="mono">${fmtIntervalPctCell(a.price_after_5m, a.price_at_alert, a.triggered_at, 300)}</td>
      <td class="mono">${fmtIntervalPctCell(a.price_after_30m, a.price_at_alert, a.triggered_at, 1800)}</td>
      <td class="mono">${fmtIntervalPctCell(a.price_after_1h, a.price_at_alert, a.triggered_at, 3600)}</td>
      <td>${a.outcome_checked ? "verificada" : "pendiente"}</td>
      <td><button onclick="buyPaper('${a.chain}', '${a.token_address}', this)">${buyLabel}</button>${alreadyTag}</td>
    `;
    alertsBody.appendChild(tr);
  }
}

function renderPositions() {
  if (!lastData) return;
  const sorted = sortPositions(lastData.positions, positionsSort);
  const { slice, page, totalPages, total } = paginate(sorted, positionsPage);
  positionsPage = page;
  document.getElementById("positions-page-label").textContent = `${page}/${totalPages} (${total})`;

  const positionsBody = document.getElementById("positions-body");
  positionsBody.innerHTML = "";
  if (slice.length === 0) {
    positionsBody.innerHTML = '<tr><td colspan="9" class="empty">Sin posiciones todavía -- usá "Comprar" en una alerta.</td></tr>';
  }
  for (const p of slice) {
    const isOpen = p.status === "open";
    const realized = p.pnl_usd || 0;
    const unrealized = isOpen ? (p.unrealized_pnl_usd || 0) : 0;
    const hasAnyPnl = p.pnl_usd !== null || (isOpen && p.unrealized_pnl_usd !== null && p.unrealized_pnl_usd !== undefined);
    const totalPnlUsd = hasAnyPnl ? realized + unrealized : null;
    const totalPnlPct = totalPnlPctOf(p);
    const priceCol = isOpen ? (p.current_price ?? p.entry_price) : p.exit_price;
    const priceChangePct = fmtChangePct(priceCol, p.entry_price);
    const remainingPct = Math.round((p.remaining_fraction ?? (isOpen ? 1 : 0)) * 100);

    const pnlClass = totalPnlUsd === null ? "" : (totalPnlUsd >= 0 ? "pnl-pos" : "pnl-neg");
    const pnlUsdText = totalPnlUsd === null
      ? (isOpen ? "esperando trade nuevo" : "--") : (totalPnlUsd >= 0 ? "+$" : "-$") + Math.abs(totalPnlUsd).toFixed(2);
    const pnlPctText = totalPnlPct === null ? "" : fmtPct(totalPnlPct);
    let statusLabel;
    if (!isOpen) {
      statusLabel = "cerrada";
    } else if (remainingPct < 100) {
      statusLabel = p.has_fresh_price ? `parcial, ${remainingPct}% corriendo` : `parcial, sin trades nuevos`;
    } else {
      statusLabel = p.has_fresh_price ? "abierta (no realizado)" : "abierta (sin trades nuevos)";
    }
    const action = isOpen
      ? `<button onclick="closePaper(${p.id}, this)">Cerrar ${remainingPct < 100 ? "el resto" : ""}</button>`
      : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(p.opened_at)}</td>
      <td>${tokenCell(p.chain, p.token_address)}</td>
      <td>$${Number(p.amount_usd).toFixed(2)}</td>
      <td>${isOpen ? remainingPct + "%" : "--"}</td>
      <td class="${priceChangePct === null ? '' : (priceChangePct >= 0 ? 'pnl-pos' : 'pnl-neg')}">${priceChangePct === null ? '--' : fmtPct(priceChangePct)}</td>
      <td class="${pnlClass}">${pnlUsdText}</td>
      <td class="${pnlClass}">${pnlPctText}</td>
      <td>${statusLabel}</td>
      <td>${action}</td>
    `;
    positionsBody.appendChild(tr);
  }
}

function renderTransactions() {
  if (!lastData) return;
  const { slice, page, totalPages, total } = paginate(lastData.transactions, txPage);
  txPage = page;
  document.getElementById("tx-page-label").textContent = `${page}/${totalPages} (${total})`;

  const txBody = document.getElementById("tx-body");
  txBody.innerHTML = "";
  if (slice.length === 0) {
    txBody.innerHTML = '<tr><td colspan="6" class="empty">Todavía no se registró ninguna transacción.</td></tr>';
  }
  for (const t of slice) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(t.detected_at)}</td>
      <td class="mono">${short(t.wallet, 10)}</td>
      <td>${tokenCell(t.chain, t.token_address)}</td>
      <td class="badge ${t.side}">${t.side}</td>
      <td>${t.amount_usd !== null ? "$" + Number(t.amount_usd).toFixed(2) : "--"}</td>
      <td class="mono">${fmtNum(t.price, 4)}</td>
    `;
    txBody.appendChild(tr);
  }
}

async function refresh() {
  const resp = await fetch("/api/data");
  const data = await resp.json();
  lastData = data;

  document.getElementById("mode").innerHTML =
    `<span class="badge ${data.mode}">${data.mode}</span>`;
  document.getElementById("sol-price").textContent =
    data.sol_usd !== null ? "$" + data.sol_usd.toFixed(2) : "--";
  document.getElementById("alert-count").textContent = data.stats.alert_total;

  const viewStatus = document.getElementById("view-status");
  const showAllBtn = document.getElementById("show-all-btn");
  if (data.view_cutoff_at) {
    viewStatus.textContent = `Mostrando: solo desde ${fmtTime(data.view_cutoff_at)}`;
    showAllBtn.hidden = false;
  } else {
    viewStatus.textContent = "Mostrando: todo el historial";
    showAllBtn.hidden = true;
  }

  // Totales calculados por el servidor sobre TODA la base y SIN las
  // posiciones con anomalías de precio (ver _build_api_data).
  const st = data.stats;
  const totalPnl = st.pnl_total;
  document.getElementById("open-count").textContent = st.open_count;
  const pnlEl = document.getElementById("closed-pnl");
  const wr = st.win_rate !== null ? ` · win-rate ${(st.win_rate * 100).toFixed(1)}%` : "";
  pnlEl.textContent = (totalPnl >= 0 ? "+$" : "-$") + Math.abs(totalPnl).toFixed(2);
  pnlEl.title = `${st.closed_count} cerradas${wr} · ${st.anomalies_excluded} excluidas por anomalía de precio · caja simulada $${st.cash_free_usd.toFixed(0)} (referencia $${st.bankroll_usd.toFixed(0)}, sin tope de capital)`;
  pnlEl.className = "value " + (totalPnl >= 0 ? "pnl-pos" : "pnl-neg");

  renderAlerts();
  renderPositions();
  renderTransactions();
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


def _with_pnl_pct(position: dict) -> dict:
    """Agrega pnl_pct a una posición CERRADA -- % sobre lo invertido
    originalmente (pnl_usd ya viene acumulado de todos los tramos
    vendidos, parciales + el cierre final -- ver record_partial_exit)."""
    if position["pnl_usd"] is not None and position["amount_usd"]:
        position["pnl_pct"] = (position["pnl_usd"] / position["amount_usd"]) * 100
    else:
        position["pnl_pct"] = None
    return position


def _with_unrealized_pnl(conn, position: dict) -> dict:
    """
    PnL de una posición ABIERTA, separado en dos partes:
    - realized_pnl_usd: lo que YA se vendió (tomas de ganancia parciales,
      ver core/auto_trader.py) -- position["pnl_usd"] ya viene acumulado
      de la DB, puede ser no-nulo aunque siga 'open'.
    - unrealized_pnl_usd: "como si vendieras AHORA" lo que queda
      (remaining_fraction), usando el último precio que el propio
      sistema vio on-chain (gratis, sin llamar a una API externa en
      cada refresh de 3s).

    OJO -- distinción importante: si la última transacción que tenemos
    es de ANTES de haber abierto la posición, significa que no vimos
    NINGÚN trade nuevo de ese token todavía -- no es que el precio esté
    "sin cambios", es que no tenemos información nueva. Se distingue
    con `has_fresh_price`.
    """
    position = _with_pnl_pct(position)  # PnL realizado hasta ahora (puede ser no-nulo y seguir open)
    remaining_fraction = position["remaining_fraction"]

    last_tx = get_last_transaction(conn, position["chain"], position["token_address"])
    has_fresh_price = last_tx is not None and last_tx["detected_at"] > position["opened_at"]

    if not has_fresh_price or not position["entry_price"] or remaining_fraction <= 1e-9:
        position["current_price"] = last_tx["price"] if last_tx else None
        position["has_fresh_price"] = False
        position["unrealized_pnl_usd"] = None
        position["unrealized_pnl_pct"] = None
        return position

    multiplier = last_tx["price"] / position["entry_price"]
    remaining_usd = position["amount_usd"] * remaining_fraction
    unrealized = net_result_of_position(remaining_usd, multiplier, fees_for(position["chain"]))
    position["current_price"] = last_tx["price"]
    position["has_fresh_price"] = True
    position["unrealized_pnl_usd"] = unrealized
    # % sobre el tamaño ORIGINAL (no solo sobre lo que queda), para que
    # sea comparable con pnl_pct de las posiciones cerradas.
    position["unrealized_pnl_pct"] = (unrealized / position["amount_usd"]) * 100
    return position


# Cache de la respuesta de /api/data. BUG REAL (2026-09-18): con la base
# en 448 MB, esta consulta tardaba 53-64 SEGUNDOS (ORDER BY detected_at
# sin índice = recorrer toda `transactions`, más 3.2 MB de JSON), el
# navegador la pedía cada 1s desde 6 pestañas, y como corría en el hilo
# principal congelaba los listeners: Robinhood se atrasaba miles de
# bloques y se perdían transacciones. Ahora: ordena por la clave primaria
# (instantáneo), límites chicos, corre en un thread aparte (endpoint
# `def`, no `async def`), y se calcula como mucho una vez cada
# _CACHE_TTL segundos sin importar cuántas pestañas haya abiertas.
_CACHE_TTL = 3.0
_cache = {"t": 0.0, "data": None}
_cache_lock = threading.Lock()

ALERTS_LIMIT = 600
TRANSACTIONS_LIMIT = 200
POSITIONS_LIMIT = 2000


def _build_api_data() -> dict:
    with get_conn() as conn:
        # "Limpiar vista" (ver /api/view/reset) NUNCA borra nada de la
        # DB -- solo guarda desde qué momento mostrar en el navegador.
        # brain.py sigue entrenando con la tabla completa, sin filtro.
        cutoff = get_setting(conn, "view_cutoff_at")

        # ORDER BY id DESC (clave primaria, cronológica porque las
        # columnas de fecha son "cuándo se insertó") en vez de ordenar por
        # fecha: no necesita índice ni ordenar millones de filas.
        if cutoff:
            alerts = conn.execute(
                "SELECT * FROM stampede_alerts WHERE triggered_at > ? "
                "ORDER BY id DESC LIMIT ?", (cutoff, ALERTS_LIMIT),
            ).fetchall()
            txs = conn.execute(
                "SELECT * FROM transactions WHERE detected_at > ? "
                "ORDER BY id DESC LIMIT ?", (cutoff, TRANSACTIONS_LIMIT),
            ).fetchall()
            position_rows = conn.execute(
                "SELECT * FROM paper_positions WHERE opened_at > ? "
                "ORDER BY id DESC LIMIT ?", (cutoff, POSITIONS_LIMIT),
            ).fetchall()
        else:
            alerts = conn.execute(
                "SELECT * FROM stampede_alerts ORDER BY id DESC LIMIT ?", (ALERTS_LIMIT,)
            ).fetchall()
            txs = conn.execute(
                "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (TRANSACTIONS_LIMIT,)
            ).fetchall()
            position_rows = conn.execute(
                "SELECT * FROM paper_positions ORDER BY id DESC LIMIT ?", (POSITIONS_LIMIT,)
            ).fetchall()

        # Posiciones con una salida marcada como anomalía de precio (ver
        # core/token_price.py: DexScreener en otra unidad daba
        # multiplicadores de 100x-6937x). Su pnl_usd es FALSO -- inflaba
        # el PnL mostrado a +$82k cuando el real es ~-$8k -- así que se
        # excluyen de los totales y no se muestran como ganancia.
        anomaly_ids = {
            r["position_id"] for r in conn.execute(
                "SELECT DISTINCT position_id FROM paper_position_exits "
                "WHERE reason LIKE '%_price_anomaly'")
        }

        positions = []
        for row in position_rows:
            p = dict(row)
            if p["status"] == "open":
                p = _with_unrealized_pnl(conn, p)
            else:
                p = _with_pnl_pct(p)
            if p["id"] in anomaly_ids:
                p["price_anomaly"] = True
                p["pnl_usd"] = None
                p["pnl_pct"] = None
            positions.append(p)

        # Totales sobre TODA la base (no solo lo que se carga en la
        # tabla): las tarjetas de arriba antes sumaban solo las filas
        # cargadas, y "Alertas" pasó a mostrar el tope de carga (600).
        where_a, where_p, args = "", "", ()
        if cutoff:
            where_a, where_p, args = "WHERE triggered_at > ?", "WHERE opened_at > ?", (cutoff,)
        alert_total = conn.execute(f"SELECT COUNT(*) c FROM stampede_alerts {where_a}", args).fetchone()["c"]
        pnl_total, closed_n, wins, open_n = 0.0, 0, 0, 0
        for r in conn.execute(f"SELECT id, status, pnl_usd FROM paper_positions {where_p}", args):
            if r["id"] in anomaly_ids:
                continue
            pnl_total += r["pnl_usd"] or 0
            if r["status"] == "open":
                open_n += 1
            else:
                closed_n += 1
                wins += 1 if (r["pnl_usd"] or 0) > 0 else 0
        stats = {
            "alert_total": alert_total, "pnl_total": pnl_total,
            "closed_count": closed_n, "win_rate": (wins / closed_n) if closed_n else None,
            "open_count": open_n, "anomalies_excluded": len(anomaly_ids),
            "bankroll_usd": SIM_BANKROLL_USD, "cash_free_usd": free_cash_usd(conn),
        }

    return {
        "stats": stats,
        "mode": MODE,
        "sol_usd": get_cached_sol_usd(),
        "default_position_usd": DEFAULT_POSITION_USD,
        "view_cutoff_at": cutoff,
        "alerts": [dict(row) for row in alerts],
        "transactions": [dict(row) for row in txs],
        "positions": positions,
    }


@app.get("/api/data")
def api_data():
    # Se serializa a JSON UNA vez, acá adentro (thread), y se devuelven
    # los bytes ya listos: si se devuelve un dict, FastAPI lo convierte
    # (jsonable_encoder) en el hilo principal, y con ~2 MB en esta
    # máquina eso solo tardaba varios segundos congelando los listeners.
    with _cache_lock:
        now = time.monotonic()
        if _cache["data"] is None or now - _cache["t"] >= _CACHE_TTL:
            _cache["data"] = json.dumps(_build_api_data(), default=str).encode("utf-8")
            _cache["t"] = time.monotonic()
        body = _cache["data"]
    return Response(content=body, media_type="application/json")


@app.post("/api/view/reset")
async def api_view_reset():
    """'Limpiar vista' -- guarda un punto de corte, no borra nada. Todo
    lo anterior sigue en la DB intacto (brain.py lo sigue usando)."""
    cutoff = now_iso()
    with get_conn() as conn:
        set_setting(conn, "view_cutoff_at", cutoff)
    logger.info(f"Vista del dashboard reiniciada -- ocultando todo antes de {cutoff} (datos intactos en la DB).")
    return {"view_cutoff_at": cutoff}


@app.post("/api/view/show_all")
async def api_view_show_all():
    """Saca el filtro de 'vista limpia' -- vuelve a mostrar todo el historial."""
    with get_conn() as conn:
        conn.execute("DELETE FROM dashboard_settings WHERE key = 'view_cutoff_at'")
    return {"view_cutoff_at": None}


@app.post("/api/paper/open")
async def api_paper_open(body: dict):
    chain = body.get("chain", "solana")
    token_address = body.get("token_address")
    amount_usd = body.get("amount_usd", DEFAULT_POSITION_USD)
    if not token_address:
        return JSONResponse({"error": "falta token_address"}, status_code=400)

    result = await open_position(chain, token_address, amount_usd)
    if result is None:
        return JSONResponse({"error": "no se pudo obtener precio del token"}, status_code=502)
    return result


@app.post("/api/paper/close/{position_id}")
async def api_paper_close(position_id: int):
    result = await close_position(position_id)
    if result is None:
        return JSONResponse({"error": "posición no encontrada, ya cerrada, o sin precio disponible"}, status_code=502)
    return result


async def run_dashboard(host: str = "127.0.0.1", port: int = 8000):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info(f"Dashboard disponible en http://{host}:{port}")
    await server.serve()
