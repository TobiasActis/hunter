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
  form.jf { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:10px; background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; margin:10px 0; }
  form.jf label { display:flex; flex-direction:column; font-size:11px; color:#8b949e; gap:4px; }
  form.jf input, form.jf select { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:6px 8px; font-family:inherit; font-size:12px; }
  form.jf button, td button { background:#238636; color:#fff; border:1px solid #2ea043; border-radius:6px; padding:6px 12px; font-family:inherit; font-size:12px; cursor:pointer; }
  td button.sec { background:#21262d; border-color:#30363d; color:#c9d1d9; padding:2px 8px; font-size:11px; }
  .msg { font-size:12px; margin:4px 0; min-height:16px; }
  #j-chart { width:100%; max-width:640px; height:120px; background:#161b22; border:1px solid #30363d; border-radius:8px; }
  .tabs { display:flex; gap:6px; margin:14px 0 18px 0; flex-wrap:wrap; border-bottom:1px solid #30363d; }
  .tabbtn { background:none; border:none; border-bottom:2px solid transparent; color:#8b949e; padding:8px 14px; font-family:inherit; font-size:13px; cursor:pointer; }
  .tabbtn.active { color:#c9d1d9; border-bottom-color:#58a6ff; }
  .tabbtn:hover { color:#c9d1d9; }
  details.info { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:8px 14px; margin:8px 0 14px 0; font-size:12px; color:#8b949e; }
  details.info summary { cursor:pointer; color:#79c0ff; }
  details.info p { margin:8px 0 0 0; line-height:1.5; }
  td.what { white-space:normal; max-width:280px; line-height:1.35; }
  .tag { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; background:#21262d; color:#c9d1d9; }
  .tag.paper { background:#1f6feb33; color:#79c0ff; }
</style>
</head>
<body>
  <h1>HUNTER &middot; Criptos establecidas <span class="tag paper">PAPEL &middot; nada se opera de verdad</span></h1>
  <div class="sub"><a href="/">&larr; Memecoins</a> &middot; BTC, ETH y SOL &middot; se refresca solo &middot; horarios en Buenos Aires (UTC-3)</div>
  <div class="tabs">
    <button class="tabbtn active" data-tab="resumen" onclick="showTab('resumen')">Resumen</button>
    <button class="tabbtn" data-tab="cerebro" onclick="showTab('cerebro')">Cerebro (aprende solo)</button>
    <button class="tabbtn" data-tab="momentum" onclick="showTab('momentum')">Momentum semanal</button>
    <button class="tabbtn" data-tab="motor" onclick="showTab('motor')">Motor CRT</button>
    <button class="tabbtn" data-tab="journal" onclick="showTab('journal')">Diario manual</button>
  </div>

  <div class="tab" id="tab-resumen">
    <div class="stats" id="sum-cards"></div>
    <h2>Estrategias (todas en papel)</h2>
    <table><thead><tr><th>Estrategia</th><th>Qu&eacute; hace</th><th>Cerradas</th><th>Abiertas</th><th>Win-rate</th><th>Resultado</th><th>Por operaci&oacute;n</th><th>Lectura</th></tr></thead><tbody id="sum-body"></tbody></table>
    <h2>Posiciones abiertas ahora</h2>
    <table><thead><tr><th>Estrategia</th><th>Activo</th><th>Lado</th><th>Entrada</th><th>Precio</th><th>PnL no realizado</th><th>Cierra / detalle</th></tr></thead><tbody id="sum-open"></tbody></table>
    <h2>&Uacute;ltimos cierres</h2>
    <table><thead><tr><th>Cerrada</th><th>Estrategia</th><th>Activo</th><th>Lado</th><th>Resultado</th></tr></thead><tbody id="sum-closed"></tbody></table>
    <details class="info"><summary>C&oacute;mo leer esta pantalla</summary>
      <p>Todo lo de aqu&iacute; es simulaci&oacute;n con precios reales de Binance: no hay claves ni &oacute;rdenes. Cada estrategia empieza con su propio capital de papel. Con menos de 30 operaciones cerradas el resultado es puro ruido; se empieza a poder juzgar con 100 o m&aacute;s.</p>
      <p>Los backtests hist&oacute;ricos de todo lo que hay ac&aacute; dieron cero o negativo despu&eacute;s de comisiones. Esta pantalla existe para comprobarlo en vivo, no porque se sepa que funciona.</p>
    </details>
  </div>

  <div class="tab" id="tab-cerebro" hidden>
    <h2>Cerebro: operaciones en papel</h2>
    <details class="info"><summary>C&oacute;mo funciona y qu&eacute; esperar</summary>
      <p>Un modelo de aprendizaje autom&aacute;tico aprende de las velas de 1 hora de BTC, ETH y SOL (precio, volumen, volatilidad, hora y lo que hacen los otros activos), se reentrena solo cada semana
        y cada hora predice la probabilidad de que el precio suba en las pr&oacute;ximas 4 y 24 horas. Si la probabilidad se aleja m&aacute;s de 5 puntos de 50%, abre una posici&oacute;n <b>en papel</b> de $100 (sin palanca) y la cierra al cumplirse el horizonte. Una posici&oacute;n por activo y horizonte a la vez.</p>
      <p><b>Qu&eacute; esperar:</b> en la investigaci&oacute;n (2020-2026, probando siempre sobre datos posteriores al entrenamiento) acert&oacute; la direcci&oacute;n algo mejor que el azar (AUC 0,52 a 0,55), pero la ganancia bruta (0 a +10 puntos base por operaci&oacute;n) no cubri&oacute; el costo de 14. Lo esperable es que pierda peque&ntilde;o; la prueba en vivo dir&aacute; si eso cambia.</p>
    </details>
    <div class="stats" id="bt-cards"></div>
    <h2>Por activo y horizonte</h2>
    <table><thead><tr><th>Grupo</th><th>Cerradas</th><th>Win-rate</th><th>PnL</th><th>Bruto medio (bps)</th><th>Neto medio (bps)</th></tr></thead><tbody id="bt-groups"></tbody></table>
    <h2>Posiciones abiertas</h2>
    <table><thead><tr><th>Abierta</th><th>Activo</th><th>Horizonte</th><th>Lado</th><th>P(sube)</th><th>Entrada</th><th>Precio</th><th>PnL no realizado</th><th>Cierra</th></tr></thead><tbody id="bt-open"></tbody></table>
    <h2>Cerradas (&uacute;ltimas 100)</h2>
    <table><thead><tr><th>Cerrada</th><th>Activo</th><th>Horizonte</th><th>Lado</th><th>Entrada</th><th>Salida</th><th>Retorno bruto</th><th>PnL</th></tr></thead><tbody id="bt-closed"></tbody></table>
    <h2>Qu&eacute; predice y qu&eacute; tan bien acierta</h2>
  <div class="sub" id="brain-meta">Cargando...</div>
  <table><thead><tr><th>Activo</th><th>Horizonte</th><th>P(sube) &uacute;ltima</th><th>Vela</th><th>Evaluadas</th><th>AUC en vivo</th><th>AUC investigaci&oacute;n</th><th>Acierta direcci&oacute;n</th><th>Se&ntilde;ales</th><th>Bruto (bps)</th><th>Neto taker (bps)</th><th>Neto maker (bps)</th></tr></thead><tbody id="brain-body"></tbody></table>

  </div>

  <div class="tab" id="tab-momentum" hidden>
    <h2>Momentum semanal entre monedas (papel)</h2>
    <details class="info"><summary>C&oacute;mo funciona y qu&eacute; esperar</summary>
      <p>Cada mi&eacute;rcoles 00:10 UTC toma las ~45 monedas m&aacute;s l&iacute;quidas de Binance, las ordena por cu&aacute;nto subieron en los &uacute;ltimos 28 d&iacute;as (medido al cierre del lunes) y <b>compra el 20% que m&aacute;s subi&oacute; y vende el 20% que m&aacute;s cay&oacute;</b>, con pesos iguales ($500 por pata). Mantiene 7 d&iacute;as; lo que se repite no paga costo. Todo en papel, con comisi&oacute;n de 0,07% por lado.</p>
      <p><b>Qu&eacute; esperar:</b> es el &uacute;nico efecto con respaldo en estudios y en nuestro backtest 2019-2026 (+50% a +60% anual neto de costos, Sharpe 0,6 a 0,9), pero es <b>d&eacute;bil e inestable</b>: de 16 variantes 12 dieron positivo con resultados de +12% a +66%, las ca&iacute;das llegaron a -64% / -90%, y 2024-2026 fue m&aacute;s flojo. Ese backtest usa las monedas l&iacute;quidas de hoy (sobrevivientes) y no incluye el funding de los cortos, as&iacute; que en vivo se espera algo peor. Con menos de 12 semanas no se puede concluir nada: mirar la tendencia, no cada semana.</p>
    </details>
    <div class="stats" id="xs-cards"></div>
    <h2>Cartera actual</h2>
    <table><thead><tr><th>Lado</th><th>Moneda</th><th>Momentum 28 d</th><th>Nocional</th><th>Entrada</th><th>Precio</th><th>PnL no realizado</th><th>Origen</th></tr></thead><tbody id="xs-open"></tbody></table>
    <h2>Semanas cerradas</h2>
    <table><thead><tr><th>Semana</th><th>Resultado</th><th>% del capital</th></tr></thead><tbody id="xs-weeks"></tbody></table>
  </div>

  <div class="tab" id="tab-motor" hidden>
    <details class="info"><summary>C&oacute;mo funciona y qu&eacute; esperar</summary>
      <p>M&eacute;todo CRT (Candle Range Theory) con palanca simulada: la vela 2 barre el m&aacute;ximo o m&iacute;nimo de la vela 1 y cierra de vuelta dentro de su rango; se entra en contra del barrido con stop en el extremo barrido y objetivo en el extremo opuesto. Riesgo 1% del capital por operaci&oacute;n.</p>
      <p><b>Qu&eacute; esperar:</b> en el backtest 2020-2026 dio entre -0,2R y -1,3R por operaci&oacute;n, y la operaci&oacute;n inversa rindi&oacute; casi igual (sin ventaja). La palanca multiplica ganancias y p&eacute;rdidas.</p>
    </details>
  <h2 id="motor">Motor CRT autom&aacute;tico (papel)</h2>
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

  </div>

  <div class="tab" id="tab-journal" hidden>
  <h2 id="journal">Diario de operaciones (manual, en demo)</h2>
  <div class="sub">Anot&aacute; cada operaci&oacute;n que hagas en demo siguiendo un m&eacute;todo (por ejemplo el de un canal). Calcula el R neto de comisiones, tu win-rate con margen de error,
    la expectativa y cu&aacute;nto win-rate necesit&aacute;s para no perder. Con 30 a 50 operaciones cerradas ya se puede empezar a distinguir un m&eacute;todo con ventaja de uno al azar. Nada de esto opera de verdad.</div>
  <div class="stats" id="j-stats"></div>
  <div class="banner" id="j-verdict">Todav&iacute;a no hay operaciones anotadas.</div>
  <svg id="j-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
  <form class="jf" id="j-form" onsubmit="addTrade(event)">
    <label>M&eacute;todo<input name="method" value="Show del Trading" maxlength="40"></label>
    <label>Activo<input name="symbol" value="BTC" maxlength="20" required></label>
    <label>Marco<input name="tf" value="3m" maxlength="10"></label>
    <label>Lado<select name="side"><option value="long">LARGO</option><option value="short">CORTO</option></select></label>
    <label>Entrada<input name="entry" type="number" step="any" required></label>
    <label>Stop<input name="stop" type="number" step="any" required></label>
    <label>Objetivo (opcional)<input name="target" type="number" step="any"></label>
    <label>Salida (si ya cerr&oacute;)<input name="exit_price" type="number" step="any"></label>
    <label>Comisi&oacute;n por lado %<input name="fee_side_pct" type="number" step="any" value="0.07"></label>
    <label>Palanca (opcional)<input name="leverage" type="number" step="any"></label>
    <label>Nota<input name="note" maxlength="200"></label>
    <label>&nbsp;<button type="submit">Anotar operaci&oacute;n</button></label>
  </form>
  <div class="msg" id="j-msg"></div>
  <table><thead><tr><th>#</th><th>Fecha</th><th>M&eacute;todo</th><th>Activo</th><th>TF</th><th>Lado</th><th>Entrada</th><th>Stop</th><th>Stop %</th><th>Objetivo</th><th>Ratio plan.</th><th>Salida</th><th>R neto</th><th></th></tr></thead><tbody id="j-body"></tbody></table>

  </div>

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

const esc = s => String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
const pctf = x => (x === null || x === undefined) ? "--" : (x * 100).toFixed(1) + "%";
const rf = x => (x === null || x === undefined) ? "--" : (x >= 0 ? "+" : "") + x.toFixed(2) + "R";
function setMsg(t, err) { const m = document.getElementById("j-msg"); m.textContent = t; m.style.color = err ? "#f85149" : "#3fb950"; }
function verdict(s) {
  if (!s.n_closed) return "Todavía no hay operaciones cerradas. Anotá al menos 30 a 50 para poder sacar conclusiones.";
  let t = `Con ${s.n_closed} operaciones cerradas, tu win-rate es ${pctf(s.wr)} (intervalo de confianza 95%: ${pctf(s.wr_lo)} a ${pctf(s.wr_hi)}). `;
  if (s.n_closed < 30) return t + "Es muy pronto: con menos de 30 operaciones el margen de error es enorme.";
  const need = s.breakeven_wr;
  if (need !== null && need !== undefined) {
    if (s.wr_lo > need) t += `Supera con confianza el ${pctf(need)} que necesitás para no perder con tus costos y tu ganancia y pérdida medias reales.`;
    else if (s.wr_hi < need) t += `Está por debajo del ${pctf(need)} que necesitás para no perder (con tu ganancia y pérdida medias reales).`;
    else t += `Todavía no se puede distinguir del ${pctf(need)} que necesitás para no perder: seguí anotando.`;
  }
  return t + ` Expectativa neta: ${rf(s.expectancy_r)} por operación.`;
}
function drawCum(cum) {
  const svg = document.getElementById("j-chart");
  if (!cum || cum.length < 1) { svg.innerHTML = ""; return; }
  const pts = [0].concat(cum), W = 600, H = 120, mn = Math.min(0, ...pts), mx = Math.max(0, ...pts), span = (mx - mn) || 1;
  const X = i => (i / Math.max(pts.length - 1, 1)) * (W - 20) + 10, Y = v => H - 10 - ((v - mn) / span) * (H - 20);
  const line = pts.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  svg.innerHTML = `<line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="#30363d" stroke-dasharray="4"/>
    <polyline points="${line}" fill="none" stroke="${pts[pts.length - 1] >= 0 ? "#3fb950" : "#f85149"}" stroke-width="2"/>
    <text x="12" y="14" fill="#8b949e" font-size="11">R acumulado (neto de comisiones): ${rf(pts[pts.length - 1])}</text>`;
}
function renderJournal(d) {
  const s = d.stats, card = (label, val, sub) => `<div class="card"><div class="label">${label}</div><div class="value">${val}</div><div class="mono" style="font-size:11px;margin-top:2px">${sub || ""}</div></div>`;
  let c = card("Cerradas", s.n_closed, `${s.n_open} abiertas &middot; meta 50`);
  if (s.n_closed) {
    c += card("Win-rate", pctf(s.wr), `IC 95%: ${pctf(s.wr_lo)} a ${pctf(s.wr_hi)}`);
    c += card("Gana / pierde (media)", `${rf(s.avg_win_r)} / -${s.avg_loss_r.toFixed(2)}R`, `costo medio ${s.avg_cost_r.toFixed(2)}R por operación`);
    c += card("WR necesario", pctf(s.breakeven_wr), "para no perder con tus números");
    c += card("Expectativa", rf(s.expectancy_r), `total ${rf(s.total_r)} &middot; caída máx ${s.max_drawdown_r.toFixed(2)}R`);
    c += card("Ratio planificado", s.avg_planned_rr === null || s.avg_planned_rr === undefined ? "--" : "1:" + s.avg_planned_rr.toFixed(2), s.share_rr_ge2 === null || s.share_rr_ge2 === undefined ? "" : `${pctf(s.share_rr_ge2)} con ratio &ge; 1:2`);
  }
  document.getElementById("j-stats").innerHTML = c;
  document.getElementById("j-verdict").textContent = verdict(s);
  drawCum(s.cum_r);
  const b = document.getElementById("j-body");
  b.innerHTML = d.trades.length ? "" : '<tr><td colspan="14" class="empty">Sin operaciones todavía.</td></tr>';
  for (const t of d.trades) {
    const closed = t.exit_price !== null && t.exit_price !== undefined;
    b.insertAdjacentHTML("beforeend", `<tr><td>${t.id}</td><td>${fmtTime(t.created_at)}</td><td>${esc(t.method)}</td><td>${esc(t.symbol)}</td><td>${esc(t.tf)}</td>
      <td class="${t.side}">${t.side === "long" ? "LARGO" : "CORTO"}</td><td>${px(t.entry)}</td><td>${px(t.stop)}</td><td class="mono">${t.stop_pct.toFixed(2)}%</td>
      <td>${px(t.target)}</td><td class="${t.planned_rr !== null && t.planned_rr < 2 ? "neg" : ""}">${t.planned_rr === null || t.planned_rr === undefined ? "--" : "1:" + t.planned_rr.toFixed(2)}</td>
      <td>${closed ? px(t.exit_price) : "--"}</td><td class="${cls(t.net_r)}">${rf(t.net_r)}</td>
      <td>${closed ? "" : `<button class="sec" onclick="closeTrade(${t.id})">Cerrar</button> `}<button class="sec" onclick="deleteTrade(${t.id})">Borrar</button></td></tr>`);
  }
}
async function loadJournal() { renderJournal(await (await fetch("/api/journal")).json()); }
async function postJson(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "error");
  return j;
}
async function addTrade(ev) {
  ev.preventDefault();
  const body = Object.fromEntries(new FormData(ev.target).entries());
  try {
    await postJson("/api/journal/add", body);
    setMsg("Operación anotada.", false);
    for (const n of ["entry", "stop", "target", "exit_price", "note"]) ev.target.elements[n].value = "";
    await loadJournal();
  } catch (e) { setMsg(e.message, true); }
}
async function closeTrade(id) {
  const v = prompt("Precio de salida de la operación #" + id + ":");
  if (v === null || v === "") return;
  try { await postJson("/api/journal/close/" + id, {exit_price: v}); setMsg("Operación cerrada.", false); await loadJournal(); } catch (e) { setMsg(e.message, true); }
}
async function deleteTrade(id) {
  if (!confirm("¿Borrar la operación #" + id + "?")) return;
  await postJson("/api/journal/delete/" + id, {}); await loadJournal();
}
refresh(); setInterval(() => refresh().catch(() => {}), 5000);
loadJournal().catch(() => {});
const fmtP = p => (p === null || p === undefined) ? "--" : (p * 100).toFixed(1) + "%";
const bps = x => (x === null || x === undefined) ? "--" : (x >= 0 ? "+" : "") + x.toFixed(1);
async function loadBrain() {
  const d = await (await fetch("/api/brain")).json();
  document.getElementById("brain-meta").textContent = d.trained_at
    ? `Último entrenamiento: ${fmtTime(d.trained_at)} con ${d.n_train_rows} velas de 1 h; se reentrena solo cada ${d.retrain_days} días. Costo asumido: ${d.cost_taker_bps.toFixed(0)} bps (taker) / ${d.cost_maker_bps.toFixed(0)} bps (maker) ida y vuelta. Señal = probabilidad a más de ${(d.threshold * 100).toFixed(0)} puntos de 50%.`
    : "El cerebro todavía está descargando el historial y entrenando la primera vez (unos minutos).";
  const b = document.getElementById("brain-body");
  b.innerHTML = "";
  for (const r of d.rows) {
    const pcol = r.last_p === null || r.last_p === undefined ? "" : (r.last_p >= 0.55 ? "pos" : (r.last_p <= 0.45 ? "neg" : ""));
    b.insertAdjacentHTML("beforeend", `<tr><td>${r.symbol}</td><td>${r.horizon} h</td><td class="${pcol}">${fmtP(r.last_p)}</td><td class="mono">${r.last_candle_ms ? fmtTime(new Date(r.last_candle_ms).toISOString()) : "--"}</td>
      <td>${r.n_scored}</td><td>${r.auc === undefined || r.auc === null ? "--" : r.auc.toFixed(3)}</td><td class="mono">${r.research_auc === null || r.research_auc === undefined ? "--" : r.research_auc.toFixed(3)}</td>
      <td>${r.dir_acc === undefined ? "--" : fmtP(r.dir_acc)}</td><td>${r.n_signals === undefined ? "--" : r.n_signals}</td>
      <td>${bps(r.gross_bps)}</td><td class="${cls(r.net_taker_bps)}">${bps(r.net_taker_bps)}</td><td class="${cls(r.net_maker_bps)}">${bps(r.net_maker_bps)}</td></tr>`);
  }
}
loadBrain().catch(() => {}); setInterval(() => loadBrain().catch(() => {}), 60000);


// ---------- pestanas
function showTab(name) {
  if (!document.getElementById("tab-" + name)) name = "resumen";
  document.querySelectorAll(".tab").forEach(e => { e.hidden = e.id !== "tab-" + name; });
  document.querySelectorAll(".tabbtn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  try { localStorage.setItem("cryptoTab", name); } catch (e) {}
}
// ---------- resumen
function reading(n, avg) {
  if (!n) return "Sin operaciones todavía";
  if (n < 30) return `Muy pronto (${n} de 30 operaciones)`;
  return avg < 0 ? "Pierde hasta ahora" : "Gana hasta ahora (falta confirmar con 100+)";
}
async function getJson(url) { try { return await (await fetch(url)).json(); } catch (e) { return null; } }
function btKey(t) { return `${t.symbol} ${t.horizon}h`; }
async function loadSummary() {
  const [m, b, j, x] = await Promise.all([getJson("/api/scalp"), getJson("/api/brain_trades"), getJson("/api/journal"), getJson("/api/xs")]);
  const rows = [], cards = [], open = [], closed = [];
  const card = (label, val, sub, c) => `<div class="card"><div class="label">${label}</div><div class="value ${c || ""}">${val}</div><div class="mono" style="font-size:11px;margin-top:2px">${sub || ""}</div></div>`;
  if (b) {
    const t = b.total;
    cards.push(card("Cerebro (papel)", "$" + b.equity.toFixed(2), `PnL ${money(t.pnl)} &middot; inicial $${b.config.bankroll.toFixed(0)}`, cls(t.pnl)));
    rows.push(`<tr><td><b>Cerebro</b></td><td class="mono what">Modelo que aprende de velas de 1 h; opera BTC/ETH/SOL a 4 h y 24 h, $${b.config.notional} sin palanca</td><td>${t.n}</td><td>${b.open.length}</td>
      <td>${t.n ? (t.wins / t.n * 100).toFixed(1) + "%" : "--"}</td><td class="${cls(t.n ? t.pnl : null)}">${t.n ? money(t.pnl) : "--"}</td>
      <td class="${cls(t.n ? t.avg_net_bps : null)}">${t.n ? bps(t.avg_net_bps) + " bps" : "--"}</td><td>${reading(t.n, t.avg_net_bps)}</td></tr>`);
    for (const o of b.open) open.push(`<tr><td>Cerebro ${o.horizon} h</td><td>${o.symbol}</td><td class="${o.side}">${o.side === "long" ? "LARGO" : "CORTO"}</td><td>${px(o.entry)}</td><td>${px(o.mark)}</td>
      <td class="${cls(o.unrealized_usd)}">${money(o.unrealized_usd)}</td><td class="mono">cierra ${fmtTime(new Date(o.due_candle_ms + 3600000).toISOString())}</td></tr>`);
    for (const c of b.closed.slice(0, 30)) closed.push({ts: c.closed_at, html: `<tr><td>${fmtTime(c.closed_at)}</td><td>Cerebro ${c.horizon} h</td><td>${c.symbol}</td><td class="${c.side}">${c.side === "long" ? "LARGO" : "CORTO"}</td><td class="${cls(c.pnl_usd)}">${money(c.pnl_usd)}</td></tr>`});
  }
  if (m) {
    const t = m.total;
    cards.push(card("Motor CRT (papel)", "$" + m.equity.toFixed(2), `PnL ${money(t.pnl)} &middot; inicial $${m.config.bankroll.toFixed(0)}`, cls(t.pnl)));
    rows.push(`<tr><td><b>Motor CRT</b></td><td class="mono what">Patrón de velas CRT en 15 m y 1 h, palanca simulada, riesgo 1% por operación</td><td>${t.n}</td><td>${m.open.length}</td>
      <td>${t.n ? (t.wins / t.n * 100).toFixed(1) + "%" : "--"}</td><td class="${cls(t.n ? t.pnl : null)}">${t.n ? money(t.pnl) : "--"}</td>
      <td class="${cls(t.n ? t.avg_r : null)}">${t.n ? t.avg_r.toFixed(2) + "R" : "--"}</td><td>${reading(t.n, t.avg_r)}</td></tr>`);
    for (const o of m.open) open.push(`<tr><td>CRT ${o.tf}</td><td>${sym(o.symbol)}</td><td class="${o.side}">${o.side === "long" ? "LARGO" : "CORTO"}</td><td>${px(o.entry)}</td><td>${px(o.mark)}</td>
      <td class="${cls(o.unrealized_usd)}">${money(o.unrealized_usd)}</td><td class="mono">stop ${px(o.stop)} / objetivo ${px(o.target)}</td></tr>`);
    for (const c of m.closed.slice(0, 30)) closed.push({ts: c.closed_at, html: `<tr><td>${fmtTime(c.closed_at)}</td><td>CRT ${c.tf}</td><td>${sym(c.symbol)}</td><td class="${c.side}">${c.side === "long" ? "LARGO" : "CORTO"}</td><td class="${cls(c.pnl_usd)}">${money(c.pnl_usd)} (${c.r_multiple.toFixed(2)}R)</td></tr>`});
    const marks = Object.entries(m.marks).map(([s, p]) => `${sym(s)} $${Number(p).toLocaleString("en-US", {maximumFractionDigits: 2})}`).join(" &middot; ");
    cards.push(card("Precios", marks.split(" &middot; ").join("<br>"), ""));
  }
  if (j) {
    const s = j.stats;
    cards.push(card("Diario manual", s.n_closed ? rf(s.total_r) : "--", `${s.n_closed} cerradas &middot; ${s.n_open} abiertas`, s.n_closed ? cls(s.total_r) : ""));
    rows.push(`<tr><td><b>Diario manual</b></td><td class="mono what">Operaciones que anotás vos en demo (por ejemplo del canal), con comisiones</td><td>${s.n_closed}</td><td>${s.n_open}</td>
      <td>${s.n_closed ? pctf(s.wr) : "--"}</td><td class="${s.n_closed ? cls(s.total_r) : ""}">${s.n_closed ? rf(s.total_r) : "--"}</td>
      <td class="${s.n_closed ? cls(s.expectancy_r) : ""}">${s.n_closed ? rf(s.expectancy_r) : "--"}</td><td>${reading(s.n_closed, s.n_closed ? s.expectancy_r : 0)}</td></tr>`);
  }
  if (x) {
    const total = x.realized + x.unrealized;
    cards.push(card("Momentum semanal (papel)", "$" + x.equity.toFixed(2), `PnL ${money(total)} &middot; inicial $${x.config.bankroll.toFixed(0)}`, cls(total)));
    const rd = x.n_weeks < 12 ? `Muy pronto (${x.n_weeks} de 12 semanas)` : (x.avg_week_pct < 0 ? "Pierde hasta ahora" : "Gana hasta ahora (falta confirmar)");
    rows.push(`<tr><td><b>Momentum semanal</b></td><td class="mono what">Compra las monedas que más subieron en 28 días y vende las que más cayeron (45 líquidas), reequilibra cada miércoles</td><td>${x.n_weeks}</td><td>${x.open.length}</td>
      <td>${x.n_weeks ? (x.win_weeks / x.n_weeks * 100).toFixed(0) + "%" : "--"}</td><td class="${cls(total)}">${money(total)}</td>
      <td class="${x.avg_week_pct === null ? "" : cls(x.avg_week_pct)}">${x.avg_week_pct === null ? "--" : (x.avg_week_pct >= 0 ? "+" : "") + x.avg_week_pct.toFixed(2) + "% / semana"}</td><td>${rd}</td></tr>`);
  }
  document.getElementById("sum-cards").innerHTML = cards.join("");
  document.getElementById("sum-body").innerHTML = rows.join("") || '<tr><td colspan="8" class="empty">Cargando...</td></tr>';
  document.getElementById("sum-open").innerHTML = open.join("") || '<tr><td colspan="7" class="empty">Sin posiciones abiertas ahora.</td></tr>';
  closed.sort((a, c) => (c.ts || "").localeCompare(a.ts || ""));
  document.getElementById("sum-closed").innerHTML = closed.slice(0, 15).map(x => x.html).join("") || '<tr><td colspan="5" class="empty">Todavía no se cerró ninguna operación.</td></tr>';
}
// ---------- detalle del cerebro
async function loadBrainTrades() {
  const b = await getJson("/api/brain_trades");
  if (!b) return;
  const t = b.total, card = (label, val, sub, c) => `<div class="card"><div class="label">${label}</div><div class="value ${c || ""}">${val}</div><div class="mono" style="font-size:11px;margin-top:2px">${sub || ""}</div></div>`;
  document.getElementById("bt-cards").innerHTML =
    card("Capital (papel)", "$" + b.equity.toFixed(2), `inicial $${b.config.bankroll.toFixed(0)} &middot; $${b.config.notional} por operación`, cls(t.pnl)) +
    card("PnL realizado", money(t.pnl), "", cls(t.pnl)) + card("Cerradas", t.n, `${b.open.length} abiertas`) +
    card("Win-rate", t.n ? (t.wins / t.n * 100).toFixed(1) + "%" : "--", "") +
    card("Neto medio", t.n ? bps(t.avg_net_bps) + " bps" : "--", t.n ? `bruto ${bps(t.avg_gross_bps)} bps` : "", t.n ? cls(t.avg_net_bps) : "");
  let g = "";
  for (const [name, x] of Object.entries(b.by_key)) g += `<tr><td>${name}</td><td>${x.n}</td><td>${x.n ? (x.wins / x.n * 100).toFixed(1) + "%" : "--"}</td><td class="${cls(x.n ? x.pnl : null)}">${x.n ? money(x.pnl) : "--"}</td><td>${x.n ? bps(x.avg_gross_bps) : "--"}</td><td class="${cls(x.n ? x.avg_net_bps : null)}">${x.n ? bps(x.avg_net_bps) : "--"}</td></tr>`;
  document.getElementById("bt-groups").innerHTML = g;
  const ob = document.getElementById("bt-open");
  ob.innerHTML = b.open.length ? "" : '<tr><td colspan="9" class="empty">Sin posiciones abiertas: espera una señal fuerte en la próxima hora en punto.</td></tr>';
  for (const o of b.open) ob.insertAdjacentHTML("beforeend", `<tr><td>${fmtTime(o.opened_at)}</td><td>${o.symbol}</td><td>${o.horizon} h</td><td class="${o.side}">${o.side === "long" ? "LARGO" : "CORTO"}</td><td>${fmtP(o.p_up)}</td><td>${px(o.entry)}</td><td>${px(o.mark)}</td><td class="${cls(o.unrealized_usd)}">${money(o.unrealized_usd)}</td><td class="mono">${fmtTime(new Date(o.due_candle_ms + 3600000).toISOString())}</td></tr>`);
  const cb = document.getElementById("bt-closed");
  cb.innerHTML = b.closed.length ? "" : '<tr><td colspan="8" class="empty">Todavía no se cerró ninguna operación (la primera cierra 4 horas después de abrirse).</td></tr>';
  for (const c of b.closed) cb.insertAdjacentHTML("beforeend", `<tr><td>${fmtTime(c.closed_at)}</td><td>${c.symbol}</td><td>${c.horizon} h</td><td class="${c.side}">${c.side === "long" ? "LARGO" : "CORTO"}</td><td>${px(c.entry)}</td><td>${px(c.exit_price)}</td><td class="${cls(c.gross_ret)}">${(c.gross_ret * 100).toFixed(2)}%</td><td class="${cls(c.pnl_usd)}">${money(c.pnl_usd)}</td></tr>`);
}
let initial = "resumen";
try { initial = localStorage.getItem("cryptoTab") || "resumen"; } catch (e) {}
if (location.hash && document.getElementById("tab-" + location.hash.slice(1))) initial = location.hash.slice(1);
showTab(initial);
loadSummary(); setInterval(loadSummary, 10000);
loadBrainTrades(); setInterval(loadBrainTrades, 10000);

// ---------- momentum semanal
function nextWednesdayUTC() {
  const n = new Date(), d = new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate(), 0, 10, 0));
  let add = (3 - d.getUTCDay() + 7) % 7;
  if (add === 0 && n.getTime() > d.getTime()) add = 7;
  d.setUTCDate(d.getUTCDate() + add);
  return d;
}
async function loadXs() {
  const x = await getJson("/api/xs");
  if (!x) return;
  const card = (label, val, sub, c) => `<div class="card"><div class="label">${label}</div><div class="value ${c || ""}">${val}</div><div class="mono" style="font-size:11px;margin-top:2px">${sub || ""}</div></div>`;
  const total = x.realized + x.unrealized;
  document.getElementById("xs-cards").innerHTML =
    card("Capital (papel)", "$" + x.equity.toFixed(2), `inicial $${x.config.bankroll.toFixed(0)} &middot; $${x.config.leg} por pata`, cls(total)) +
    card("PnL realizado", money(x.realized), "", cls(x.realized)) + card("No realizado", money(x.unrealized), "", cls(x.unrealized)) +
    card("Semanas cerradas", x.n_weeks, `${x.win_weeks} ganadoras`) +
    card("Media semanal", x.avg_week_pct === null ? "--" : (x.avg_week_pct >= 0 ? "+" : "") + x.avg_week_pct.toFixed(2) + "%", "del capital", x.avg_week_pct === null ? "" : cls(x.avg_week_pct)) +
    card("Próximo reequilibrio", fmtTime(nextWednesdayUTC().toISOString()), "miércoles 00:10 UTC");
  const ob = document.getElementById("xs-open");
  ob.innerHTML = x.open.length ? "" : '<tr><td colspan="8" class="empty">Todavía no hay cartera: se arma en el primer ciclo.</td></tr>';
  for (const o of x.open) ob.insertAdjacentHTML("beforeend", `<tr><td class="${o.side}">${o.side === "long" ? "LARGO" : "CORTO"}</td><td>${o.symbol}</td><td class="${cls(o.mom)}">${o.mom === null || o.mom === undefined ? "--" : (o.mom * 100).toFixed(1) + "%"}</td>
    <td>$${o.notional.toFixed(0)}</td><td>${px(o.entry)}</td><td>${px(o.mark)}</td><td class="${cls(o.unrealized_usd)}">${money(o.unrealized_usd)}</td><td class="mono">${o.carried ? "continúa de la semana anterior" : "nueva"} &middot; ${o.week_key}</td></tr>`);
  const wb = document.getElementById("xs-weeks");
  wb.innerHTML = x.weeks.length ? "" : '<tr><td colspan="3" class="empty">La primera semana cierra en el próximo reequilibrio.</td></tr>';
  for (const w of x.weeks.slice().reverse()) wb.insertAdjacentHTML("beforeend", `<tr><td>${w.week}</td><td class="${cls(w.pnl)}">${money(w.pnl)}</td><td class="${cls(w.ret_pct)}">${w.ret_pct >= 0 ? "+" : ""}${w.ret_pct.toFixed(2)}%</td></tr>`);
}
loadXs(); setInterval(loadXs, 60000);
</script>
</body>
</html>
"""
