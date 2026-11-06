"use strict";

const $ = (id) => document.getElementById(id);
const LIMIT = 50;
let offset = 0;
let total = 0;

const fmt = (n, d = 1) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(d);
};
const money = (n, d = 2) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  const v = Number(n);
  return (v > 0 ? "+" : "") + "$" + v.toFixed(d);
};
const pct = (n, d = 0) => {
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

function pnlClass(value) {
  if (value === null || value === undefined) return "muted";
  const v = Number(value);
  if (v > 0) return "green";
  if (v < 0) return "red";
  return "muted";
}

function outcomePill(side) {
  const s = String(side || "").toUpperCase();
  if (s === "UP") return '<span class="pill buy">UP</span>';
  if (s === "DOWN") return '<span class="pill sell">DOWN</span>';
  return "--";
}

function statusLabel(row) {
  const maker = Number(row.maker_filled || 0);
  const fallback = Number(row.fallback_filled || 0);
  const target = Number(row.target_shares || 0);
  if (maker + fallback <= 0) {
    if (row.settled_ts) return "未成交";
    return row.status === "canceled" ? "已撤单" : "挂单中";
  }
  if (fallback > 0 && maker > 0) return "Maker+转Taker";
  if (fallback > 0) return "转Taker";
  if (target > 0 && maker >= target - 1e-6) return "全Maker";
  return "部分Maker";
}

function card(label, value, sub) {
  return `<div class="stat-card">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${value}</div>
    <div class="stat-sub">${sub}</div>
  </div>`;
}

function renderCards(s) {
  const fillRate = s.maker_fill_rate;
  const wait = s.avg_wait_s;
  const filledPs = s.filled_pnl_ps;
  const filledTakerPs = s.filled_taker_pnl_ps;
  $("maker-cards").innerHTML = [
    card(
      "Maker 成交率",
      pct(fillRate),
      `全成交 ${s.maker_full || 0} · 转 Taker ${s.fallback || 0} · 未成交 ${s.unfilled || 0}`
    ),
    card(
      "Maker 轨 PnL",
      `<span class="${pnlClass(s.pnl_maker)}">${money(s.pnl_maker)}</span>`,
      `Taker 轨 ${money(s.pnl_taker)} · 已结算 ${s.settled || 0}`
    ),
    card(
      "每信号 PnL 差",
      `<span class="${pnlClass(s.avg_pnl_diff)}">${money(s.avg_pnl_diff)}</span>`,
      `总差 ${money(s.pnl_diff)}（Maker − Taker）`
    ),
    card(
      "手续费节省",
      money(s.fee_saved_total),
      `每信号 ${money(s.fee_saved_per_signal)}`
    ),
    card(
      "平均等待",
      wait === null || wait === undefined ? "--" : fmt(wait, 1) + " s",
      `Maker 成交单胜率 ${pct(s.win_rate_maker)}`
    ),
    card(
      "成交单每份 PnL",
      `<span class="${pnlClass(filledPs)}">${filledPs === null || filledPs === undefined ? "--" : money(filledPs, 4)}</span>`,
      `同信号 Taker ${filledTakerPs === null || filledTakerPs === undefined ? "--" : money(filledTakerPs, 4)}`
    ),
  ].join("");
}

function renderConclusion(s) {
  if (!s || !s.n) {
    $("maker-conclusion").textContent = "等待第一个入场信号…";
    return;
  }
  const verdict = Number(s.settled || 0) < 20
    ? "样本还小，先看方向，别下结论。"
    : (Number(s.pnl_diff || 0) >= 0
      ? "当前 Maker 轨领先；继续观察成交单的逆向选择。"
      : "当前 Taker 轨领先；注意 Maker 没成交的信号是 0 PnL，不是亏损。");
  $("maker-conclusion").textContent =
    `共 ${s.n} 个信号（已结算 ${s.settled}）：Maker 成交率 ${pct(s.maker_fill_rate)}，` +
    `全成交 ${s.maker_full || 0}；Maker 轨 PnL ${money(s.pnl_maker)} vs Taker ${money(s.pnl_taker)}，` +
    `差 ${money(s.pnl_diff)}。${verdict}`;
}

function renderAssets(rows) {
  $("asset-count").textContent = String((rows || []).length);
  const html = (rows || []).map((a) => `<tr>
    <td>${esc(a.asset)}</td>
    <td class="mono">${a.n}</td>
    <td class="mono">${pct(a.maker_fill_rate)}</td>
    <td class="mono">${a.maker_full}</td>
    <td class="mono">${a.fallback}</td>
    <td class="mono">${a.unfilled}</td>
    <td class="mono">${a.avg_wait_s === null || a.avg_wait_s === undefined ? "--" : fmt(a.avg_wait_s, 1) + " s"}</td>
    <td class="mono">${fmt(a.avg_maker_price, 3)}</td>
    <td class="mono">${fmt(a.avg_taker_price, 3)}</td>
    <td class="mono">${money(a.fee_saved_total)}</td>
    <td class="mono ${pnlClass(a.pnl_maker)}">${money(a.pnl_maker)}</td>
    <td class="mono ${pnlClass(a.pnl_taker)}">${money(a.pnl_taker)}</td>
    <td class="mono ${pnlClass(a.pnl_diff)}">${money(a.pnl_diff)}</td>
  </tr>`).join("");
  $("asset-tbody").innerHTML = html || '<tr><td colspan="13" class="no-data">暂无数据</td></tr>';
}

function renderRows(rows) {
  $("row-count").textContent = String(total);
  const html = (rows || []).map((r) => {
    const settled = Boolean(r.settled_ts);
    const makerPnl = settled
      ? `<span class="${pnlClass(r.pnl)}">${money(r.pnl)}</span>`
      : "--";
    const takerPnl = settled
      ? `<span class="${pnlClass(r.taker_pnl)}">${money(r.taker_pnl)}</span>`
      : "--";
    const feeSaved = Number(r.taker_fee || 0) - Number(r.fallback_fee || 0);
    return `<tr>
      <td class="mono">${utcTime(r.ts)}</td>
      <td>${esc(r.asset)}</td>
      <td>${outcomePill(r.side)}</td>
      <td>${esc(statusLabel(r))}</td>
      <td class="mono">${fmt(r.quote_price, 3)}</td>
      <td class="mono">${fmt(r.maker_filled, 2)}/${fmt(r.target_shares, 2)}</td>
      <td class="mono">${fmt(r.maker_avg_price, 3)}</td>
      <td class="mono">${r.wait_s === null || r.wait_s === undefined ? "--" : fmt(r.wait_s, 1) + " s"}</td>
      <td class="mono">${fmt(r.taker_shares, 2)}</td>
      <td class="mono">${fmt(r.taker_price, 3)}</td>
      <td class="mono">${money(feeSaved, 4)}</td>
      <td class="mono">${makerPnl}</td>
      <td class="mono">${takerPnl}</td>
    </tr>`;
  }).join("");
  $("rows-tbody").innerHTML = html || '<tr><td colspan="13" class="no-data">暂无信号</td></tr>';
}

function renderPager() {
  const page = Math.floor(offset / LIMIT) + 1;
  const pages = Math.max(1, Math.ceil(total / LIMIT));
  $("rows-page-info").textContent = `${page} / ${pages}`;
  $("rows-prev").disabled = offset <= 0;
  $("rows-next").disabled = offset + LIMIT >= total;
}

async function refreshNav() {
  try {
    const s = await getJSON("/api/status");
    const feeds = s.feeds || {};
    const rtds = feeds.rtds || {};
    const ok =
      Object.keys(rtds).length > 0 &&
      Object.values(rtds).every((x) => x && x.connected) &&
      Boolean(feeds.clob && feeds.clob.connected);
    $("live-dot").className = "live-dot" + (ok ? "" : " offline");
    $("live-label").textContent = ok ? "在线" : "离线";
  } catch (e) {
    /* 状态接口失败不影响对照表 */
  }
}

async function refresh() {
  try {
    const data = await getJSON(`/api/maker-shadow?days=14&limit=${LIMIT}&offset=${offset}`);
    total = Number((data.pagination && data.pagination.total) || 0);
    renderCards(data.summary || {});
    renderConclusion(data.summary || {});
    renderAssets(data.by_asset || []);
    renderRows(data.data || []);
    renderPager();
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("maker-conclusion").textContent = "读取数据失败：" + e.message;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  $("rows-prev").addEventListener("click", () => {
    offset = Math.max(0, offset - LIMIT);
    refresh();
  });
  $("rows-next").addEventListener("click", () => {
    if (offset + LIMIT < total) {
      offset += LIMIT;
      refresh();
    }
  });
  refresh();
  refreshNav();
  setInterval(refresh, 10000);
  setInterval(refreshNav, 5000);
});
