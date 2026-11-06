"use strict";

const $ = (id) => document.getElementById(id);
const ASSETS = ["BTC", "ETH"];

const CONFIG_FIELDS = [
  ["min_edge", "Min edge"],
  ["model_buffer", "Model buffer"],
  ["max_position_usd", "Max position $"],
  ["daily_loss_limit_usd", "Daily loss limit $"],
  ["entry_min_rel_s", "Entry min rel s"],
  ["entry_max_rel_s", "Entry max rel s"],
  ["max_spread", "Max spread"],
  ["max_stale_book_s", "Max book age s"],
  ["min_ask_shares", "Min best ask shares"],
  ["min_order_shares", "Min order shares"],
  ["taker_fee_rate", "Taker fee rate"],
  ["sigma_lookback_s", "Sigma lookback s"],
  ["min_sigma_samples", "Min sigma samples"],
  ["engine_tick_s", "Engine tick s"],
  ["signal_log_min_edge", "Signal log min edge"],
];

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function money(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : ""}$${n.toFixed(2)}`;
}

function pnlClass(value) {
  if (value === null || value === undefined) return "muted";
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "muted";
  return n > 0 ? "pos" : "neg";
}

function timeStr(sec) {
  if (!Number.isFinite(Number(sec))) return "—";
  let s = Math.max(0, Math.floor(Number(sec)));
  const m = Math.floor(s / 60);
  s = s % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function duration(sec) {
  if (!Number.isFinite(Number(sec))) return "—";
  let s = Math.max(0, Math.floor(Number(sec)));
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function clock(ts) {
  if (!ts) return "—";
  try {
    return new Date(Number(ts) * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  } catch {
    return "—";
  }
}

function statusClass(status) {
  const s = String(status || "");
  if (s.startsWith("entered") || s === "in_position") return "ok";
  if (s.includes("killswitch") || s.includes("daily_loss") || s.startsWith("execution_error")) return "bad";
  if (s.startsWith("warming") || s === "waiting_p0" || s === "stale_feed" || s === "stale_book") return "warn";
  return "mut";
}

function table(headers, rows, className = "") {
  if (!rows.length) return '<div class="empty">暂无数据</div>';
  return `<table class="${className}"><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr class="${r.className || ""}">${r.cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (response.status === 401) {
    document.body.classList.add("locked");
    throw new Error("authentication required");
  }
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return response.json();
}

function renderHeader(status) {
  $("mode").textContent = String(status.mode || "paper").toUpperCase();
  $("uptime").textContent = duration(status.uptime_s);

  const today = status.summary?.today?.pnl ?? 0;
  const total = status.summary?.total?.pnl ?? 0;
  const todayEl = $("today-pnl");
  const totalEl = $("total-pnl");
  todayEl.textContent = money(today);
  totalEl.textContent = money(total);
  todayEl.className = pnlClass(today);
  totalEl.className = pnlClass(total);

  const feeds = status.feeds || {};
  const rtds = feeds.rtds || {};
  const parts = Object.keys(rtds).map((asset) => `${asset}${rtds[asset]?.connected ? "✓" : "✗"}`);
  parts.push(`CLOB${feeds.clob?.connected ? "✓" : "✗"}`);
  $("feed").textContent = parts.join("  ");

  const active = Boolean(status.killswitch?.active);
  const badge = $("kill-badge");
  badge.textContent = active ? "STOPPED" : "RUNNING";
  badge.className = `badge ${active ? "bad" : "ok"}`;
  if (active && status.killswitch?.reason) {
    badge.title = status.killswitch.reason;
  }
}

function candidateTable(assetData) {
  const candidates = assetData.candidates || [];
  if (!candidates.length) return '<div class="empty">等待盘口…</div>';
  const bestSide = assetData.best?.side;
  const threshold = assetData.params
    ? Number(assetData.params.min_edge) + Number(assetData.params.model_buffer)
    : null;
  const rows = candidates.map((c) => ({
    className: c.side === bestSide ? "best" : "",
    cells: [
      `<strong>${esc(c.side)}</strong>`,
      fmt(c.fair, 3),
      fmt(c.best_bid, 3),
      fmt(c.best_ask, 3),
      fmt(c.spread, 3),
      fmt(c.avg_price, 3),
      `<strong class="${Number(c.edge) > 0 ? "pos" : "muted"}">${fmt(c.edge, 3)}</strong>`,
      fmt(c.shares, 1),
      fmt(c.age_s, 1),
    ],
  }));
  const head = ["Side", "Fair", "Bid", "Ask", "Spread", "Fill avg", "Edge", "Shares", "Age s"];
  const note = threshold === null ? "" : `<div class="muted" style="font-size:10px;margin-top:6px">entry threshold = min_edge + model_buffer = ${fmt(threshold, 3)}</div>`;
  return table(head, rows) + note;
}

function positionBlock(pos) {
  if (!pos) return '<div class="empty">本窗口未持仓</div>';
  const pnl = pos.unrealized;
  return `<div class="metric-grid" style="grid-template-columns:repeat(5,1fr)">
    <div><label>Side</label><strong>${esc(pos.side)}</strong></div>
    <div><label>Shares</label><strong>${fmt(pos.size, 2)}</strong></div>
    <div><label>Avg</label><strong>${fmt(pos.avg_price, 3)}</strong></div>
    <div><label>Cost</label><strong>$${fmt(pos.cost, 2)}</strong></div>
    <div><label>Unrealized</label><strong class="${pnlClass(pnl)}">${money(pnl)}</strong></div>
  </div>`;
}

function renderAssetCard(asset, data) {
  if (!data || !data.window) {
    return `<section class="asset-card">
      <div class="asset-head"><div><div class="asset-name">${esc(asset)}</div><div class="slug">waiting for window</div></div></div>
      <div class="status-line"><span class="badge warn">${esc(data?.status || "no_window")}</span></div>
      <div class="empty">等待 Gamma 发现 5 分钟窗口…</div>
    </section>`;
  }

  const w = data.window;
  const timeLeft = data.time_left ?? (w.end_ts - Date.now() / 1000);
  const p0 = data.price_to_beat ?? w.price_to_beat;
  const fairUp = data.fair_up;
  const fairDown = fairUp === null || fairUp === undefined ? null : 1 - fairUp;
  const bestEdge = data.best?.edge;
  const status = data.status || "";
  const position = data.position;

  return `<section class="asset-card">
    <div class="asset-head">
      <div>
        <div class="asset-name">${esc(asset)}</div>
        <div class="slug">${esc(w.slug || "")}</div>
      </div>
      <div class="asset-clock">
        <div class="big">${timeStr(timeLeft)}</div>
        <span class="muted">left · rel ${fmt(data.rel, 0)}s</span>
      </div>
    </div>
    <div class="status-line">
      <span class="badge ${statusClass(status)}">${esc(status || "waiting")}</span>
      <span class="muted">P0 source: ${esc(w.p0_source || data.p0_source || "—")}</span>
      <span class="muted">daily ${money(data.daily_pnl)}</span>
    </div>
    <div class="metric-grid">
      <div><label>Price to beat</label><strong>${fmt(p0, 2)}</strong></div>
      <div><label>Chainlink spot</label><strong>${fmt(data.spot, 2)}</strong></div>
      <div><label>60s TWAP</label><strong>${fmt(data.twap60, 2)}</strong></div>
      <div><label>σ / second</label><strong>${fmt(data.sigma_bps, 2)} bps</strong></div>
    </div>
    <div class="fair-row">
      <div class="fair up"><label>Fair UP</label><strong>${fmt(fairUp, 3)}</strong></div>
      <div class="fair down"><label>Fair DOWN</label><strong>${fmt(fairDown, 3)}</strong></div>
      <div class="fair edge"><label>Best edge</label><strong>${fmt(bestEdge, 3)}</strong></div>
    </div>
    ${candidateTable(data)}
    <div style="margin-top:12px">
      <div class="panel-head"><h2 style="margin:0">Position</h2></div>
      ${positionBlock(position)}
    </div>
  </section>`;
}

function renderAssets(assets) {
  $("assets").innerHTML = ASSETS.map((asset) => renderAssetCard(asset, assets?.[asset])).join("");
}

function renderPositions(positions) {
  const rows = (positions || []).map((p) => ({
    cells: [
      esc(p.asset),
      fmt(p.start_ts, 0),
      esc(p.side),
      fmt(p.size, 2),
      fmt(p.avg_price, 3),
      `$${fmt(p.cost, 2)}`,
      esc(p.status),
      `<span class="${pnlClass(p.pnl)}">${p.status === "settled" ? money(p.pnl) : "—"}</span>`,
    ],
  }));
  $("pos-meta").textContent = `${rows.length} open`;
  $("positions").innerHTML = table(
    ["Asset", "Window", "Side", "Shares", "Avg", "Cost", "Status", "PnL"],
    rows
  );
}

function renderFills(fills) {
  const rows = (fills || []).map((f) => ({
    cells: [
      clock(f.ts),
      esc(f.asset),
      esc(f.side),
      fmt(f.price, 3),
      fmt(f.size, 2),
      `$${fmt(f.fee, 4)}`,
      esc(f.role),
    ],
  }));
  $("fills").innerHTML = table(["Time", "Asset", "Side", "Price", "Shares", "Fee", "Role"], rows);
}

function renderSignals(signals) {
  const rows = (signals || []).map((s) => ({
    cells: [
      clock(s.ts),
      esc(s.asset),
      esc(s.side),
      fmt(s.fair, 3),
      fmt(s.ask, 3),
      `<span class="${Number(s.edge) > 0 ? "pos" : "neg"}">${fmt(s.edge, 3)}</span>`,
      s.acted ? "✓" : "—",
      `<span class="muted">${esc(String(s.reason || "").slice(0, 34))}</span>`,
    ],
  }));
  $("signals").innerHTML = table(
    ["Time", "Asset", "Side", "Fair", "Fill avg", "Edge", "Acted", "Reason"],
    rows
  );
}

function buildConfigForm(params) {
  const form = $("config-form");
  const fields = CONFIG_FIELDS.map(([key, label]) => {
    const value = params[key];
    return `<label>${esc(label)}
      <input type="number" step="any" data-key="${esc(key)}" value="${esc(value)}">
    </label>`;
  });
  fields.push(`<label class="check">
    <input type="checkbox" data-key="hold_to_settlement" ${params.hold_to_settlement ? "checked" : ""}>
    Hold to settlement
  </label>`);
  form.innerHTML = fields.join("");
}

async function loadConfig() {
  const data = await fetchJSON("/api/config");
  buildConfigForm(data.params || {});
}

async function saveConfig(event) {
  event.preventDefault();
  const patch = {};
  document.querySelectorAll("#config-form [data-key]").forEach((el) => {
    patch[el.dataset.key] = el.type === "checkbox" ? el.checked : Number(el.value);
  });
  try {
    await fetchJSON("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    flash("config-msg", "已保存");
  } catch (error) {
    flash("config-msg", `保存失败: ${error.message}`);
  }
}

async function resetConfig() {
  try {
    await fetchJSON("/api/config/reset", { method: "POST" });
    await loadConfig();
    flash("config-msg", "已恢复默认值");
  } catch (error) {
    flash("config-msg", `重置失败: ${error.message}`);
  }
}

let flashTimer = null;
function flash(id, text) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

async function refresh() {
  try {
    const status = await fetchJSON("/api/status");
    renderHeader(status);
    renderAssets(status.assets || {});
    renderPositions(status.positions || []);
    document.body.classList.remove("locked");

    const fills = await fetchJSON("/api/fills?limit=20");
    renderFills(fills.data || []);
    const signals = await fetchJSON("/api/signals?limit=20");
    renderSignals(signals.data || []);
  } catch (error) {
    if (String(error.message).includes("authentication")) return;
    $("feed").textContent = `error: ${error.message}`;
  }
}

async function stopBot() {
  try {
    await fetchJSON("/api/control/stop", { method: "POST" });
    await refresh();
  } catch (error) {
    flash("config-msg", `Stop 失败: ${error.message}`);
  }
}

async function startBot() {
  try {
    await fetchJSON("/api/control/start", { method: "POST" });
    await refresh();
  } catch (error) {
    flash("config-msg", `Resume 失败: ${error.message}`);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("btn-stop").addEventListener("click", stopBot);
  $("btn-start").addEventListener("click", startBot);
  $("btn-reset").addEventListener("click", resetConfig);
  $("config-form").addEventListener("submit", saveConfig);

  try {
    await loadConfig();
  } catch (error) {
    flash("config-msg", `读取配置失败: ${error.message}`);
  }
  await refresh();
  setInterval(refresh, 2000);
});
