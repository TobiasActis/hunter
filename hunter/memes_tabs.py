"""Pestanas de la vista de Memes (/): estilos, HTML y JS de 'Graduaciones lentas' (Solana) y 'Bot de Telegram'. Autonomo: no depende de las funciones de la pagina principal. Datos: /api/sg (core/slowgrad.py)."""

MEMES_CSS = """
  .tabs { display:flex; gap:6px; margin:14px 0 18px 0; flex-wrap:wrap; border-bottom:1px solid #30363d; }
  .tabbtn { background:none; border:none; border-bottom:2px solid transparent; border-radius:0; color:#8b949e; padding:8px 14px; font-family:inherit; font-size:13px; cursor:pointer; }
  .tabbtn.active { color:#c9d1d9; border-bottom-color:#58a6ff; }
  .tabbtn:hover { color:#c9d1d9; background:none; }
  .tabbtn .count { display:inline-block; margin-left:6px; padding:0 6px; border-radius:9px; background:#21262d; color:#8b949e; font-size:10px; }
  details.info { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:8px 14px; margin:8px 0 14px 0; font-size:12px; color:#8b949e; }
  details.info summary { cursor:pointer; color:#79c0ff; }
  details.info p { margin:8px 0 0 0; line-height:1.5; }
  .pnl-pos { color:#3fb950; } .pnl-neg { color:#f85149; }
  .sgform textarea { width:100%; box-sizing:border-box; background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:8px; font-family:inherit; font-size:12px; }
  .sgform button { background:#238636; color:#fff; border:1px solid #2ea043; margin-top:6px; padding:6px 12px; font-size:12px; }
  .sgform button:hover { background:#2ea043; }
"""

SG_TAB_HTML = """  <div class="mtab" id="mtab-sg" hidden>
    <h2 style="margin-top:6px">Graduaciones lentas de pump.fun (Solana, papel)</h2>
    <details class="info"><summary>C&oacute;mo funciona y qu&eacute; esperar</summary>
      <p>Un token de pump.fun suele llenar su curva y "graduar" a PumpSwap en ~20 minutos. El bot de Telegram <b>@kotte_memescan_bot</b> avisa cuando tarda <b>6 horas o m&aacute;s</b> y dice (en su repo, qlo) que esos tokens duplican 1,5 veces m&aacute;s seguido y hacen 5x 2,4 veces m&aacute;s seguido. <b>Eso es una probabilidad, no un retorno neto</b> de entrar tarde y salir por un pool chico.</p>
      <p>Ac&aacute; se <b>detectan las mismas graduaciones</b> con nuestro rastreador y se mide con precios reales de DexScreener: compra simulada de $100 apenas aparece el pool (se mide el retraso) y el precio se registra a 1, 3, 5, 10, 15, 30 min y 1, 2, 4, 6, 12, 24 h. <b>Costos:</b> comisi&oacute;n de PumpSwap 0,30% por lado, deslizamiento seg&uacute;n la liquidez real del pool (los graduados lentos suelen tener solo $3k a $15k) y $0,50 de transacci&oacute;n. Si el pool desaparece queda <b>sin dato</b>, no en cero.</p>
      <p><b>Filas:</b> <b>lenta</b> (&ge; 6 h, la se&ntilde;al del bot), <b>muy lenta</b> (no vimos su creaci&oacute;n: m&aacute;s vieja que nuestros datos) y <b>control</b> (graduaci&oacute;n r&aacute;pida, 1 de cada 8). Regla para creerle: 100 o m&aacute;s se&ntilde;ales con neto medio positivo (IC95% sin cruzar 0) y mejor que el control, mirando el horizonte de 1 hora fijado de antemano.</p>
    </details>
    <div class="stats" id="sg-cards"></div>
    <h2>Resultados por horizonte (neto de costos)</h2>
    <table><thead><tr><th>Grupo</th><th>Horizonte</th><th>Medidas</th><th>Sin dato</th><th>Mediana bruta</th><th>Neto medio (&plusmn;IC95)</th><th>% que gana neto</th><th>% &ge; 2x</th><th>% &ge; 5x</th></tr></thead><tbody id="sg-body"></tbody></table>
    <h2>&Uacute;ltimas se&ntilde;ales</h2>
    <table><thead><tr><th>Grupo</th><th>Token</th><th>Horas hasta graduar</th><th>Retraso de entrada</th><th>Reserva SOL</th><th>Estado</th><th>&Uacute;ltima medici&oacute;n</th></tr></thead><tbody id="sg-recent"></tbody></table>
  </div>
"""

BOT_TAB_HTML = """  <div class="mtab" id="mtab-bot" hidden>
    <h2 style="margin-top:6px">Alertas del bot de Telegram (papel)</h2>
    <details class="info"><summary>C&oacute;mo funciona</summary>
      <p>Las alertas reales de <b>@kotte_memescan_bot</b> llegan por mensaje directo a tu cuenta. Un oyente en el servidor (<b>hunter-tg</b>, solo lectura, lee &uacute;nicamente a ese bot) toma cada alerta <b>SLOW GRADUATION</b> y la simula al primer precio real disponible, con los mismos costos y ventanas que la pesta&ntilde;a de graduaciones. Las alertas de hace m&aacute;s de 5 minutos se descartan (si el oyente estuvo ca&iacute;do, simular ahora no reflejar&iacute;a la alerta). Las alertas "NEW LAUNCH" de su lista de devs se ignoran: todav&iacute;a no tienen pool para medir.</p>
      <p>Tambi&eacute;n pod&eacute;s <b>pegar a mano</b> el texto o el CA de una alerta: se simula al precio del momento en que la peg&aacute;s. El retraso de entrada de las alertas autom&aacute;ticas se mide contra la hora del mensaje de Telegram (incluye la demora del propio bot).</p>
    </details>
    <div class="stats" id="bot-cards"></div>
    <h2>Pegar alerta del bot</h2>
    <div class="sgform"><textarea id="sg-text" rows="3" placeholder="Peg&aacute; el texto de la alerta (o solo el CA); uno o varios"></textarea>
      <button onclick="sgAdd()">Simular entrada ahora</button> <span class="mono" id="sg-msg" style="margin-left:10px"></span></div>
    <h2>Resultados por horizonte (neto de costos)</h2>
    <table><thead><tr><th>Grupo</th><th>Horizonte</th><th>Medidas</th><th>Sin dato</th><th>Mediana bruta</th><th>Neto medio (&plusmn;IC95)</th><th>% que gana neto</th><th>% &ge; 2x</th><th>% &ge; 5x</th></tr></thead><tbody id="bot-body"></tbody></table>
    <h2>&Uacute;ltimas alertas</h2>
    <table><thead><tr><th>Grupo</th><th>Token</th><th>Horas hasta graduar</th><th>Retraso de entrada</th><th>Reserva SOL</th><th>Estado</th><th>&Uacute;ltima medici&oacute;n</th></tr></thead><tbody id="bot-recent"></tbody></table>
  </div>
"""

AT_TAB_HTML = """  <div class="mtab" id="mtab-at" hidden>
    <h2 style="margin-top:6px">Atenci&oacute;n: &iquest;qu&eacute; se&ntilde;ales anticipan a las buenas memes? (papel)</h2>
    <details class="info"><summary>C&oacute;mo funciona y qu&eacute; esperar</summary>
      <p>Cada fuente es una lista p&uacute;blica de tokens (DexScreener, GeckoTerminal, GMGN y los escaneos de otros traders). La <b>primera vez</b> que un token aparece en una fuente se simula una compra de <b>$100</b> al primer precio de DexScreener y se mide su precio a 5 min, 15 min, 1 h, 4 h y 24 h. <b>Nunca opera de verdad.</b></p>
      <p><b>Costos:</b> comisi&oacute;n por lado (0,3% en Solana y Base, 1% en Robinhood), deslizamiento seg&uacute;n la liquidez real del pool y costo de transacci&oacute;n. Si el token desaparece queda <b>sin dato</b>; el <b>peor caso</b> cuenta esos como p&eacute;rdida total (un token que desaparece suele ser un rug).</p>
      <p><b>Regla para creerle</b> (fijada el 2026-09-21, un solo horizonte: 1 h): al menos 100 medidos, neto medio en peor caso mayor a +0% con IC95 que no cruza 0, y mejor que la base de comparaci&oacute;n de esa cadena (control de pools nuevos, o los perfiles nuevos de DexScreener) con IC95 de la diferencia mayor a 0. Hasta llegar a 100 dice <b>falta muestra</b>: no es un resultado.</p>
    </details>
    <div class="stats" id="at-cards"></div>
    <h2>Veredicto a 1 hora (neto de costos)</h2>
    <table><thead><tr><th>Fuente</th><th>Cadena</th><th>Eventos</th><th>Con entrada</th><th>Medidas a 1 h</th><th>Neto medio (&plusmn;IC95)</th><th>Peor caso</th><th>% que gana neto</th><th>Edad mediana</th><th>Veredicto</th></tr></thead><tbody id="at-body"></tbody></table>
    <h2>Neto medio por horizonte</h2>
    <table><thead><tr><th>Fuente</th><th>Cadena</th><th>5 min</th><th>15 min</th><th>1 h</th><th>4 h</th><th>24 h</th></tr></thead><tbody id="at-hz"></tbody></table>
    <h2>&Uacute;ltimos eventos</h2>
    <table><thead><tr><th>Hace</th><th>Fuente</th><th>Cadena</th><th>Token</th><th>Estado</th><th>Retraso de entrada</th><th>Edad del token</th><th>Liquidez</th><th>&Uacute;ltima medici&oacute;n</th></tr></thead><tbody id="at-recent"></tbody></table>
  </div>
"""

MEMES_JS = """
// ---------- pestanas de la vista de memes
function showMTab(name) {
  if (!document.getElementById("mtab-" + name)) name = "resumen";
  document.querySelectorAll(".mtab").forEach(e => { e.hidden = e.id !== "mtab-" + name; });
  document.querySelectorAll(".mtabbtn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  try { localStorage.setItem("memesTab", name); } catch (e) {}
  if (history.replaceState) history.replaceState(null, "", "#" + name);
}
(function () {
  let t = "resumen";
  try { t = localStorage.getItem("memesTab") || "resumen"; } catch (e) {}
  if (location.hash && document.getElementById("mtab-" + location.hash.slice(1))) t = location.hash.slice(1);
  showMTab(t);
})();

// ---------- graduaciones lentas y bot de Telegram
const SG_NAMES = {SG6: "lenta", SGOLD: "muy lenta", SGCTL: "control", BOT: "alerta del bot", MANUAL: "pegada a mano"};
const SG_STATUS = {pending: "buscando precio", active: "midiendo", done: "completa", rejected: "rechazada (liquidez)", no_price: "sin precio"};
function sgHl(s) { return s < 3600 ? (s / 60) + " min" : (s / 3600) + " h"; }
function sgCls(x) { return x === null || x === undefined ? "" : (x >= 0 ? "pnl-pos" : "pnl-neg"); }
function sgPc(x) { return x === null || x === undefined ? "--" : (((x - 1) * 100) >= 0 ? "+" : "") + ((x - 1) * 100).toFixed(1) + "%"; }
function sgCard(label, val, sub, c) { return `<div class="card"><div class="label">${label}</div><div class="value ${c || ""}">${val}</div><div class="mono" style="font-size:11px;margin-top:2px">${sub || ""}</div></div>`; }
function sgAt(v, h) { return v.horizons.find(x => x.horizon_s === h); }
function sgAgo(ms) {
  if (!ms) return "ninguna todavía";
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return m < 60 ? `hace ${m} min` : (m < 2880 ? `hace ${(m / 60).toFixed(1)} h` : `hace ${(m / 1440).toFixed(1)} días`);
}
function sgTable(s, keys, bodyId) {
  const body = document.getElementById(bodyId);
  body.innerHTML = "";
  for (const v of s.variants.filter(x => keys.includes(x.variant))) for (const h of v.horizons) {
    const dec = h.horizon_s === s.config.decision_horizon_s ? " <b>(decisión)</b>" : "";
    body.insertAdjacentHTML("beforeend", `<tr><td>${v.name}</td><td>${sgHl(h.horizon_s)}${dec}</td><td>${h.n}</td><td>${h.missed}</td><td>${h.n ? h.median_gross.toFixed(2) + "x" : "--"}</td>
      <td class="${h.n ? sgCls(h.mean_net - 1) : ""}">${h.n ? sgPc(h.mean_net) + (h.ci95 === null || h.ci95 === undefined ? "" : " &plusmn;" + (h.ci95 * 100).toFixed(0) + "%") : "--"}</td>
      <td>${h.n ? (h.pct_net_pos * 100).toFixed(0) + "%" : "--"}</td><td>${h.n ? (h.pct_2x * 100).toFixed(0) + "%" : "--"}</td><td>${h.n ? (h.pct_5x * 100).toFixed(0) + "%" : "--"}</td></tr>`);
  }
}
function sgRecent(s, keys, bodyId, emptyMsg) {
  const rb = document.getElementById(bodyId);
  const rows = s.recent.filter(r => keys.includes(r.variant)).slice(0, 40);
  rb.innerHTML = rows.length ? "" : `<tr><td colspan="7" class="empty">${emptyMsg}</td></tr>`;
  for (const r of rows) rb.insertAdjacentHTML("beforeend", `<tr><td>${SG_NAMES[r.variant]}</td><td class="mono">${r.symbol || "--"} ${r.mint.slice(0, 6)}…</td><td>${r.hours_to_grad === null || r.hours_to_grad === undefined ? "--" : r.hours_to_grad.toFixed(1)}</td>
    <td>${r.entry_lag_s === null || r.entry_lag_s === undefined ? "--" : r.entry_lag_s.toFixed(0) + " s"}</td><td>${r.entry_quote_sol === null || r.entry_quote_sol === undefined ? "--" : r.entry_quote_sol.toFixed(1)}</td><td>${SG_STATUS[r.status]}</td>
    <td class="${r.last_net === undefined || r.last_net === null ? "" : sgCls(r.last_net - 1)}">${r.last_h === undefined ? "--" : sgHl(r.last_h) + ": " + r.last_gross.toFixed(2) + "x bruto / " + (r.last_net === null ? "--" : r.last_net.toFixed(2) + "x neto")}</td></tr>`);
}
async function sgAdd() {
  const t = document.getElementById("sg-text").value, m = document.getElementById("sg-msg");
  try {
    const r = await fetch("/api/sg/manual", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: t})});
    const j = await r.json();
    m.textContent = r.ok ? `Agregados ${j.agregados} de ${j.encontrados} (los repetidos no se duplican)` : (j.error || "error");
    if (r.ok) { document.getElementById("sg-text").value = ""; loadSg(); }
  } catch (e) { m.textContent = "error de red"; }
}
async function loadSg() {
  let s = null;
  try { s = await (await fetch("/api/sg")).json(); } catch (e) { return; }
  const g = (k) => s.variants.find(v => v.variant === k);
  const v6 = g("SG6"), ctl = g("SGCTL"), bot = g("BOT"), man = g("MANUAL"), H = s.config.decision_horizon_s;
  const d6 = sgAt(v6, H), dc = sgAt(ctl, H), db = sgAt(bot, H), dm = sgAt(man, H);
  document.getElementById("sg-cards").innerHTML =
    sgCard("Señales lentas", v6.signals, `${v6.entered} con entrada &middot; ${v6.rejected} rechazadas por liquidez`) +
    sgCard("Control (rápidas)", ctl.signals, `${ctl.entered} con entrada`) +
    sgCard("Neto a 1 h (lentas)", d6.n ? sgPc(d6.mean_net) : "--", `${d6.n} medidas &middot; mediana bruta ${d6.n ? d6.median_gross.toFixed(2) + "x" : "--"}`, d6.n ? sgCls(d6.mean_net - 1) : "") +
    sgCard("Neto a 1 h (control)", dc.n ? sgPc(dc.mean_net) : "--", `${dc.n} medidas`, dc.n ? sgCls(dc.mean_net - 1) : "") +
    sgCard("Retraso de entrada", v6.median_lag_s === null ? "--" : v6.median_lag_s.toFixed(0) + " s", "mediana desde la graduación (el bot promete &lt; 180 s)");
  sgTable(s, ["SG6", "SGOLD", "SGCTL"], "sg-body");
  sgRecent(s, ["SG6", "SGOLD", "SGCTL"], "sg-recent", "Todavía no hay señales: se detectan apenas gradúa un token nuevo.");
  document.getElementById("bot-cards").innerHTML =
    sgCard("Alertas recibidas", bot.signals, `${bot.entered} con entrada &middot; ${bot.rejected} rechazadas por liquidez`) +
    sgCard("Última alerta", sgAgo(s.bot_last_ms), s.bot_last_ms ? "" : "revisá que la cuenta le dio /start al bot") +
    sgCard("Neto a 1 h (bot)", db.n ? sgPc(db.mean_net) : "--", `${db.n} medidas`, db.n ? sgCls(db.mean_net - 1) : "") +
    sgCard("Neto a 1 h (pegadas)", dm.n ? sgPc(dm.mean_net) : "--", `${dm.n} medidas &middot; ${man.signals} pegadas`, dm.n ? sgCls(dm.mean_net - 1) : "") +
    sgCard("Retraso de entrada (bot)", bot.median_lag_s === null ? "--" : bot.median_lag_s.toFixed(0) + " s", "desde la hora del mensaje");
  sgTable(s, ["BOT", "MANUAL"], "bot-body");
  sgRecent(s, ["BOT", "MANUAL"], "bot-recent", "Todavía no llegó ninguna alerta del bot.");
  const c1 = document.getElementById("mcount-sg"), c2 = document.getElementById("mcount-bot");
  if (c1) c1.textContent = v6.signals; if (c2) c2.textContent = bot.signals + man.signals;
}
loadSg(); setInterval(loadSg, 30000);

// ---------- laboratorio de atencion (fuentes publicas de tokens)
const AT_STATUS = {pending: "buscando precio", active: "midiendo", done: "completo", rejected: "rechazado (liquidez)", no_price: "sin precio"};
const AT_VERDICT = {"CUMPLE": ["CUMPLE", "pnl-pos"], "NO CUMPLE": ["NO CUMPLE", "pnl-neg"], "FALTA MUESTRA": ["falta muestra", ""], "BASE": ["base", ""]};
function atEsc(s) { return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])); }
function atHz(h) { return h.n ? `<span class="${sgCls(h.mean - 1)}">${sgPc(h.mean)}</span> <span class="mono" style="color:#8b949e">n=${h.n}</span>` : `<span class="mono" style="color:#8b949e">--</span>`; }
async function loadAt() {
  let s = null;
  try { s = await (await fetch("/api/at")).json(); } catch (e) { return; }
  const cards = document.getElementById("at-cards");
  if (!s.available) { cards.innerHTML = sgCard("Laboratorio", "sin datos", "todavía no arrancó el servicio hunter-attention"); return; }
  const H = s.config.decision_horizon_s, key = String(H);
  const measured = s.rows.filter(r => r.horizons[key].n > 0);
  const cumple = s.rows.filter(r => r.verdict === "CUMPLE").length;
  const best = s.rows.filter(r => r.horizons[key].n >= 30 && r.verdict !== "BASE").sort((a, b) => b.horizons[key].mean_worst - a.horizons[key].mean_worst)[0];
  cards.innerHTML =
    sgCard("Eventos registrados", s.total_events, `${s.rows.length} combinaciones fuente/cadena`) +
    sgCard("Fuentes que cumplen la regla", cumple, `de ${s.rows.filter(r => r.verdict !== "BASE").length} &middot; necesitan ${s.config.min_n}+ medidas a 1 h`, cumple ? "pnl-pos" : "") +
    sgCard("Con medición a 1 h", measured.length, `${measured.reduce((a, r) => a + r.horizons[key].n, 0)} medidas en total`) +
    sgCard("Mejor neto (peor caso, 30+ medidas)", best ? sgPc(best.horizons[key].mean_worst) : "--", best ? `${atEsc(best.label)} &middot; ${atEsc(best.chain)} &middot; n=${best.horizons[key].n}` : "todavía no hay 30 medidas", best ? sgCls(best.horizons[key].mean_worst - 1) : "");
  const body = document.getElementById("at-body"), hzb = document.getElementById("at-hz");
  body.innerHTML = ""; hzb.innerHTML = "";
  for (const r of s.rows) {
    const h = r.horizons[key], v = AT_VERDICT[r.verdict] || [r.verdict, ""];
    const ci = h.ci === null || h.ci === undefined ? "" : " &plusmn;" + (h.ci * 100).toFixed(0) + "%";
    body.insertAdjacentHTML("beforeend", `<tr><td>${atEsc(r.label)}</td><td>${atEsc(r.chain)}</td><td>${r.events}</td><td>${r.entered}</td><td>${h.n}</td>
      <td class="${h.mean === null ? "" : sgCls(h.mean - 1)}">${h.mean === null ? "--" : sgPc(h.mean) + ci}</td><td class="${h.mean_worst === null ? "" : sgCls(h.mean_worst - 1)}">${h.mean_worst === null ? "--" : sgPc(h.mean_worst)}</td>
      <td>${h.win === null ? "--" : (h.win * 100).toFixed(0) + "%"}</td><td>${r.median_age_h === null ? "--" : r.median_age_h < 48 ? r.median_age_h.toFixed(1) + " h" : (r.median_age_h / 24).toFixed(1) + " d"}</td>
      <td class="${v[1]}" title="${atEsc(r.detail)}">${v[0]}<div class="mono" style="font-size:10px;color:#8b949e">${atEsc(r.detail)}</div></td></tr>`);
    hzb.insertAdjacentHTML("beforeend", `<tr><td>${atEsc(r.label)}</td><td>${atEsc(r.chain)}</td>` + s.config.horizons.map(x => `<td>${atHz(r.horizons[String(x)])}</td>`).join("") + `</tr>`);
  }
  if (!s.rows.length) body.innerHTML = `<tr><td colspan="10" class="empty">Todavía no hay eventos.</td></tr>`;
  const rb = document.getElementById("at-recent");
  rb.innerHTML = s.recent.length ? "" : `<tr><td colspan="9" class="empty">Todavía no hay eventos.</td></tr>`;
  for (const e of s.recent) {
    const last = e.last_h === null ? "--" : sgHl(e.last_h) + ": " + (e.last_net === null ? "--" : `<span class="${sgCls(e.last_net - 1)}">${sgPc(e.last_net)} neto</span>`);
    rb.insertAdjacentHTML("beforeend", `<tr><td>${sgAgo(e.ms)}</td><td>${atEsc(e.label)}${e.who ? ' <span class="mono" style="color:#8b949e">@' + atEsc(e.who) + '</span>' : ""}</td><td>${atEsc(e.chain)}</td>
      <td>${atEsc(e.symbol || (e.token || "").slice(0, 8))}</td><td>${AT_STATUS[e.status] || atEsc(e.status)}</td><td>${e.lag_s === null ? "--" : e.lag_s.toFixed(0) + " s"}</td>
      <td>${e.age_h === null ? "--" : e.age_h < 48 ? e.age_h.toFixed(1) + " h" : (e.age_h / 24).toFixed(1) + " d"}</td><td>${e.liq === null ? "--" : "$" + Math.round(e.liq).toLocaleString("en-US")}</td><td>${last}</td></tr>`);
  }
  const c = document.getElementById("mcount-at"); if (c) c.textContent = s.total_events;
}
loadAt(); setInterval(loadAt, 60000);
"""
