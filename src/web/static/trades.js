"use strict";

const $ = (id) => document.getElementById(id);

const fmt = (n, d = 3) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(d);
};
const money = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  const v = Number(n);
  return (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
};
const pnlClass = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v) || v === 0) return "muted";
  return v > 0 ? "green" : "red";
};
const pct = (n, d = 1) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return (Number(n) * 100).toFixed(d) + "%";
};
const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

function utcTime(ts) {
  if (!ts) return "--";
  return new Date(Number(ts) * 1000).toISOString().slice(0, 19).replace("T", " ");
}

function clockStr() {
  return new Date().toISOString().slice(11, 19) + " UTC";
}

function redirectLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (r.status === 401) { redirectLogin(); throw new Error("auth"); }
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

function outcomePill(outcome) {
  if (outcome !== "UP" && outcome !== "DOWN") return "--";
  return `<span class="pill ${outcome === "UP" ? "buy" : "sell"}">${outcome}</span>`;
}

function setPnlStat(id, value) {
  const el = $(id);
  el.textContent = money(value);
  el.className = "stat-value " + pnlClass(value);
}

function renderStats(summary) {
  const today = summary.today || {};
  const total = summary.total || {};
  setPnlStat("today-pnl", today.pnl);
  $("today-sub").textContent = (today.n || 0) + " 笔已结算";
  setPnlStat("total-pnl", total.pnl);
  $("total-sub").textContent = (total.n || 0) + " 笔已结算";

  const wins = Number(total.wins || 0);
  const losses = Number(total.losses || 0);
  const n = wins + losses;
  $("win-rate").textContent = n ? pct(wins / n) : "--";
  $("win-sub").textContent = wins + " 胜 / " + losses + " 负";
  $("trade-count").textContent = String(total.n || 0);
}

function renderWindows(rows) {
  $("window-count").textContent = String(rows.length);
  const html = rows.map((r) => `<tr>
    <td class="mono">${utcTime(r.start_ts)}</td>
    <td>${esc(r.asset)}</td>
    <td class="mono">${fmt(r.price_to_beat, 2)}</td>
    <td class="mono">${fmt(r.final_price, 2)}</td>
    <td>${outcomePill(r.outcome)}</td>
    <td>${r.status === "settled" ? "已结算" : "进行中"}</td>
  </tr>`).join("");
  $("windows-tbody").innerHTML = html || '<tr><td colspan="6" class="no-data">暂无窗口</td></tr>';
}

function renderPositions(rows) {
  $("position-count").textContent = String(rows.length);
  const html = rows.map((p) => {
    const pnl = p.status === "settled"
      ? `<span class="${pnlClass(p.pnl)}">${money(p.pnl)}</span>`
      : "--";
    return `<tr>
      <td class="mono">${utcTime(p.start_ts)}</td>
      <td>${esc(p.asset)}</td>
      <td>${outcomePill(p.side)}</td>
      <td class="mono">${fmt(p.size, 2)}</td>
      <td class="mono">${fmt(p.avg_price, 3)}</td>
      <td class="mono">$${fmt(p.cost, 2)}</td>
      <td class="mono">$${fmt(p.fees, 4)}</td>
      <td>${outcomePill(p.outcome)}</td>
      <td class="mono">${pnl}</td>
      <td>${p.status === "settled" ? "已结算" : "持仓中"}</td>
    </tr>`;
  }).join("");
  $("positions-tbody").innerHTML = html || '<tr><td colspan="10" class="no-data">暂无持仓</td></tr>';
}

function renderFills(rows) {
  $("fill-count").textContent = String(rows.length);
  const html = rows.map((f) => `<tr>
    <td class="mono">${utcTime(f.ts)}</td>
    <td>${esc(f.asset)}</td>
    <td><span class="pill ${f.side === "BUY" ? "buy" : "sell"}">${f.side === "BUY" ? "买入" : "卖出"}</span></td>
    <td class="mono">${fmt(f.price, 3)}</td>
    <td class="mono">${fmt(f.size, 2)}</td>
    <td class="mono">$${fmt(f.fee, 4)}</td>
    <td>${f.role === "TAKER" ? "Taker" : f.role === "MAKER" ? "Maker" : esc(f.role || "--")}</td>
  </tr>`).join("");
  $("fills-tbody").innerHTML = html || '<tr><td colspan="7" class="no-data">暂无成交</td></tr>';
}

function renderSignals(rows) {
  $("signal-count").textContent = String(rows.length);
  const html = rows.map((s) => `<tr>
    <td class="mono">${utcTime(s.ts)}</td>
    <td>${esc(s.asset)}</td>
    <td>${outcomePill(s.side)}</td>
    <td class="mono">${fmt(s.fair, 3)}</td>
    <td class="mono">${fmt(s.ask, 3)}</td>
    <td class="mono ${Number(s.edge) > 0 ? "green" : "red"}">${fmt(s.edge, 3)}</td>
    <td>${s.acted ? '<span class="green">✓</span>' : "--"}</td>
    <td class="muted">${esc(String(s.reason || "").slice(0, 48))}</td>
  </tr>`).join("");
  $("signals-tbody").innerHTML = html || '<tr><td colspan="8" class="no-data">暂无信号</td></tr>';
}

function renderNav(status) {
  const feeds = status.feeds || {};
  const rtds = feeds.rtds || {};
  const ok = Object.keys(rtds).length > 0 &&
    Object.values(rtds).every((x) => x && x.connected) &&
    Boolean(feeds.clob?.connected);
  $("live-dot").className = "live-dot" + (ok ? "" : " offline");
  $("live-label").textContent = ok ? "在线" : "离线";
}

async function refresh() {
  try {
    const [status, pnl, windows, positions, fills, signals] = await Promise.all([
      getJSON("/api/status"),
      getJSON("/api/pnl?days=7"),
      getJSON("/api/windows?limit=50"),
      getJSON("/api/positions?limit=100"),
      getJSON("/api/fills?limit=100"),
      getJSON("/api/signals?limit=100"),
    ]);
    renderStats(pnl.summary || {});
    renderWindows(windows.data || []);
    renderPositions(positions.data || []);
    renderFills(fills.data || []);
    renderSignals(signals.data || []);
    renderNav(status);
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("live-label").textContent = "接口错误";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  refresh();
  setInterval(refresh, 10000);
});
