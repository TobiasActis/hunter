"""Pagina /scalping del dashboard: scalping en PAPEL de criptos establecidas (metodo CRT). Ver core/scalper.py."""

SCALP_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>HUNTER - Scalping (papel)</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0d1117; color:#c9d1d9; font-family: ui-monospace, "Cascadia Code", Consolas, monospace; margin:0; padding:24px; }
  h1 { font-size:18px; margin:0 0 4px 0; }
  h2 { font-size:14px; color:#8b949e; text-transform:uppercase; margin:26px 0 8px 0; }
  a { color:#79c0ff; }
  .sub { color:#8b949e; font-size:13px; margin-bottom:16px; }
  .banner { background:#1f6feb1a; border:1px solid #1f6feb55; border-radius:8px; padding:10px 14px; font-size:12px; color:#79c0ff; margin-bottom:10px; }
  .warn { background:#da36331a; border:1px solid #da363355; color:#ff7b72; }
  .stats { display:flex; gap:16px; margin-bottom:12px; flex-wrap:wrap; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 16px; min-width:130px; }
  .card .label { color:#8b949e; font-size:11px; text-transform:uppercase; }
  .card .value { font-size:20px; margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #21262d; white-space:nowrap; }
  th { color:#8b949e; font-weight:500; }
  .mono { color:#8b949e; }
  .empty { color:#8b949e; font-style:italic; padding:12px 0; }
  .pos { color:#3fb950; } .neg { color:#f85149; }
  .long { color:#3fb950; } .short { color:#f85149; }
</style>
</head>
<body>
  <h1>HUNTER &middot; Scalping en papel (criptos establecidas)</h1>
  <div class="sub"><a href="/">&larr; volver a memecoins</a> &middot; se refresca solo cada 5 s &middot; horarios en Buenos Aires (UTC-3)</div>
  <div class="banner">Paper trading con palanca SIMULADA: no hay claves ni ordenes reales, solo precios p&uacute;blicos de Binance. M&eacute;todo CRT (Candle Range Theory):
    la vela 2 barre el m&aacute;ximo o m&iacute;nimo de la vela 1 y cierra de vuelta dentro de su rango; se entra en contra del barrido con stop en el extremo barrido y objetivo en el extremo opuesto.</div>
  <div class="banner warn"><b>Ojo:</b> en el backtest hist&oacute;rico (2020-2026, BTC/ETH/SOL, con comisiones) este m&eacute;todo dio <b>entre -0,2R y -1,3R por operaci&oacute;n</b> y la operaci&oacute;n inversa rindi&oacute; casi igual:
    no mostr&oacute; ventaja. Esta pantalla sirve para medirlo en vivo con datos nuevos, no porque se sepa que funciona. La palanca multiplica ganancias y p&eacute;rdidas.</div>

  <div class="stats">
    <div class="card"><div class="label">Capital (papel)</div><div class="value" id="equity">--</div><div class="mono" id="equity-sub" style="font-size:11px"></div></div>
    <div class="card"><div class="label">PnL realizado</div><div class="value" id="pnl">--</div></div>
    <div class="card"><div class="label">Cerradas</div><div class="value" id="n">--</div></div>
    <div class="card"><div class="label">Win-rate</div><div class="value" id="wr">--</div></div>
    <div class="card"><div class="label">R medio / operaci&oacute;n</div><div class="value" id="avgr">--</div></div>
    <div class="card"><div class="label">Abiertas</div><div class="value" id="nopen">--</div></div>
    <div class="card"><div class="label">Precios</div><div class="mono" id="marks" style="font-size:12px;margin-top:6px"></div></div>
  </div>
  <div class="sub" id="cfg"></div>

  <h2>Resultados por activo y marco temporal</h2>
  <table><thead><tr><th>Grupo</th><th>Cerradas</th><th>Win-rate</th><th>PnL</th><th>R medio</th><th>R medio del backtest</th></tr></thead><tbody id="groups"></tbody></table>

  <h2>Posiciones abiertas</h2>
  <table><thead><tr><th>Abierta</th><th>Activo</th><th>TF</th><th>Lado</th><th>Entrada</th><th>Stop</th><th>Objetivo</th><th>Palanca</th><th>Liquidaci&oacute;n</th><th>Precio</th><th>PnL no realizado</th></tr></thead><tbody id="open"></tbody></table>

  <h2>Cerradas (&uacute;ltimas 100)</h2>
  <table><thead><tr><th>Cerrada</th><th>Activo</th><th>TF</th><th>Lado</th><th>Entrada</th><th>Salida</th><th>Motivo</th><th>Palanca</th><th>PnL</th><th>R</th></tr></thead><tbody id="closed"></tbody></table>

<script>
function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso); if (isNaN(d.getTime())) return "--";
  const a = new Date(d.getTime() - 3 * 3600 * 1000), p = n => String(n).padStart(2, "0");
  return `${p(a.getUTCMonth() + 1)}-${p(a.getUTCDate())} ${p(a.getUTCHours())}:${p(a.getUTCMinutes())}:${p(a.getUTCSeconds())}`;
}
const money = x => (x === null || x === undefined) ? "--" : (x >= 0 ? "+$" : "-$") + Math.abs(x).toFixed(2);
const px = x => (x === null || x === undefined) ? "--" : Number(x).toPrecision(6);
const cls = x => x === null || x === undefined ? "" : (x >= 0 ? "pos" : "neg");
const sym = s => s.replace("USDT", "");
const BASE = {"BTC 1h": -0.62, "ETH 1h": -0.50, "SOL 1h": -0.34, "BTC 15m": -1.09, "ETH 15m": -0.93, "SOL 15m": -0.69};
function groupRow(name, g, base) {
  const wr = g.n ? (g.wins / g.n * 100).toFixed(1) + "%" : "--";
  return `<tr><td>${name}</td><td>${g.n}</td><td>${wr}</td><td class="${cls(g.n ? g.pnl : null)}">${g.n ? money(g.pnl) : "--"}</td>
    <td class="${cls(g.n ? g.avg_r : null)}">${g.n ? g.avg_r.toFixed(2) + "R" : "--"}</td><td class="mono">${base === undefined ? "" : base.toFixed(2) + "R"}</td></tr>`;
}
async function refresh() {
  const d = await (await fetch("/api/scalp")).json();
  const t = d.total, c = d.config;
  document.getElementById("equity").textContent = "$" + d.equity.toFixed(2);
  document.getElementById("equity-sub").textContent = `inicial $${c.bankroll.toFixed(0)}`;
  const pe = document.getElementById("pnl"); pe.textContent = money(t.pnl); pe.className = "value " + cls(t.pnl);
  document.getElementById("n").textContent = t.n;
  document.getElementById("wr").textContent = t.n ? (t.wins / t.n * 100).toFixed(1) + "%" : "--";
  const ar = document.getElementById("avgr"); ar.textContent = t.n ? t.avg_r.toFixed(2) + "R" : "--"; ar.className = "value " + cls(t.n ? t.avg_r : null);
  document.getElementById("nopen").textContent = d.open.length;
  document.getElementById("marks").innerHTML = Object.entries(d.marks).map(([s, p]) => `${sym(s)} $${Number(p).toLocaleString("en-US", {maximumFractionDigits: 2})}`).join("<br>");
  document.getElementById("cfg").textContent = `Riesgo ${(c.risk_pct * 100).toFixed(1)}% del capital por operación, palanca máxima ${c.max_leverage}x, costo ${c.fee_side_pct.toFixed(2)}% por lado (comisión + slippage), salida por tiempo a las ${c.n_max} velas.`;

  let rows = groupRow("TOTAL", t);
  for (const [name, g] of Object.entries(d.by_combo)) rows += groupRow(name, g, BASE[name]);
  document.getElementById("groups").innerHTML = rows;

  const ob = document.getElementById("open");
  ob.innerHTML = d.open.length ? "" : '<tr><td colspan="11" class="empty">Sin posiciones abiertas: esperando una señal CRT (no es frecuente en 1 h; en 15 m aparece varias veces por día).</td></tr>';
  for (const p of d.open) {
    ob.insertAdjacentHTML("beforeend", `<tr><td>${fmtTime(p.opened_at)}</td><td>${sym(p.symbol)}</td><td>${p.tf}</td><td class="${p.side}">${p.side === "long" ? "LARGO" : "CORTO"}</td>
      <td>${px(p.entry)}</td><td>${px(p.stop)}</td><td>${px(p.target)}</td><td>${p.leverage.toFixed(1)}x</td><td class="mono">${px(p.liq_price)}</td><td>${px(p.mark)}</td>
      <td class="${cls(p.unrealized_usd)}">${money(p.unrealized_usd)} (${p.unrealized_r === undefined ? "--" : p.unrealized_r.toFixed(2) + "R"})</td></tr>`);
  }
  const cb = document.getElementById("closed");
  cb.innerHTML = d.closed.length ? "" : '<tr><td colspan="10" class="empty">Todavía no se cerró ninguna operación.</td></tr>';
  for (const p of d.closed) {
    cb.insertAdjacentHTML("beforeend", `<tr><td>${fmtTime(p.closed_at)}</td><td>${sym(p.symbol)}</td><td>${p.tf}</td><td class="${p.side}">${p.side === "long" ? "LARGO" : "CORTO"}</td>
      <td>${px(p.entry)}</td><td>${px(p.exit_price)}</td><td>${p.exit_reason}</td><td>${p.leverage.toFixed(1)}x</td><td class="${cls(p.pnl_usd)}">${money(p.pnl_usd)}</td><td class="${cls(p.r_multiple)}">${p.r_multiple.toFixed(2)}R</td></tr>`);
  }
}
refresh(); setInterval(() => refresh().catch(() => {}), 5000);
</script>
</body>
</html>
"""
