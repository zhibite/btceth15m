"use strict";

const $ = (id) => document.getElementById(id);

const fmt = (n, d = 1) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(d);
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

function topicLabel(topic) {
  if (topic === "crypto_prices_chainlink") return "Chainlink 原始价";
  return topic || "--";
}

function metrics(summary) {
  const out = [];
  const keys = (summary && summary.keys) || {};
  for (const [asset, topics] of Object.entries(keys)) {
    for (const [topic, m] of Object.entries(topics || {})) {
      out.push({ asset, topic, m });
    }
  }
  return out;
}

function pick(summary, field, stat, mode) {
  const vals = metrics(summary)
    .map((x) => (x.m && x.m[field] ? x.m[field][stat] : null))
    .filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)))
    .map(Number);
  if (!vals.length) return null;
  return mode === "min" ? Math.min(...vals) : Math.max(...vals);
}

function fmtOr(value) {
  return value === null || value === undefined ? "--" : fmt(value);
}

function card(label, value, sub) {
  return `<div class="stat-card">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${value}</div>
    <div class="stat-sub">${sub}</div>
  </div>`;
}

function renderCards(summary, decision) {
  const internal50 = pick(summary, "internal", "p50");
  const internal95 = pick(summary, "internal", "p95");
  const client50 = pick(summary, "client", "p50");
  const client95 = pick(summary, "client", "p95");
  const clientMin = pick(summary, "client", "min", "min");
  const e2e50 = pick(summary, "e2e", "p50");
  const e2e95 = pick(summary, "e2e", "p95");
  const dataAge = decision && decision.data_age_ms ? decision.data_age_ms.p50 : null;
  const bookAge = decision && decision.book_age_ms ? decision.book_age_ms.p50 : null;
  const fill = decision && decision.fill_ms ? decision.fill_ms.p50 : null;

  $("latency-cards").innerHTML = [
    card("RTDS 内部（数据→发布）", fmtOr(internal50) + " ms", "p95 " + fmtOr(internal95) + " ms"),
    card(
      "发布→收到（含时钟偏差）",
      fmtOr(client50) + " ms",
      "p95 " + fmtOr(client95) + " ms · min " + fmtOr(clientMin) + " ms"
    ),
    card("E2E（数据→本机）", fmtOr(e2e50) + " ms", "p95 " + fmtOr(e2e95) + " ms"),
    card(
      "决策→纸盘成交",
      fmtOr(fill) + " ms",
      "数据年龄 p50 " + fmtOr(dataAge) + " ms · 盘口年龄 p50 " + fmtOr(bookAge) + " ms"
    ),
  ].join("");
}

function renderConclusion(summary, decision) {
  const internal = pick(summary, "internal", "p50");
  const client = pick(summary, "client", "p50");
  const e2e = pick(summary, "e2e", "p50");
  const dataAge = decision && decision.data_age_ms ? decision.data_age_ms.p50 : null;
  const fill = decision && decision.fill_ms ? decision.fill_ms.p50 : null;
  if (internal === null || e2e === null) {
    $("latency-conclusion").textContent = "等待 RTDS 样本…";
    return;
  }
  const share = e2e > 0 ? ((internal / e2e) * 100).toFixed(0) : "--";
  $("latency-conclusion").textContent =
    `RTDS 内部 p50 ${fmt(internal)}ms，占 E2E 的 ${share}%；` +
    `发布→收到 p50 ${fmt(client)}ms；E2E p50 ${fmt(e2e)}ms；` +
    `入场数据年龄 p50 ${fmt(dataAge)}ms，纸盘决策→成交 p50 ${fmt(fill)}ms。`;
}

function renderSummary(summary) {
  const rows = metrics(summary);
  $("summary-count").textContent = String(rows.length);
  const html = rows.map(({ asset, topic, m }) => `<tr>
    <td>${esc(asset)}</td>
    <td>${esc(topicLabel(topic))}</td>
    <td class="mono">${m.n}</td>
    <td class="mono">${fmt(m.internal && m.internal.p50)}</td>
    <td class="mono">${fmt(m.internal && m.internal.p95)}</td>
    <td class="mono">${fmt(m.internal && m.internal.p99)}</td>
    <td class="mono">${fmt(m.client && m.client.p50)}</td>
    <td class="mono">${fmt(m.client && m.client.p95)}</td>
    <td class="mono">${fmt(m.client && m.client.p99)}</td>
    <td class="mono">${fmt(m.client && m.client.min)}</td>
    <td class="mono">${fmt(m.e2e && m.e2e.p50)}</td>
    <td class="mono">${fmt(m.e2e && m.e2e.p95)}</td>
    <td class="mono">${fmt(m.e2e && m.e2e.p99)}</td>
  </tr>`).join("");
  $("summary-tbody").innerHTML = html || '<tr><td colspan="13" class="no-data">等待样本...</td></tr>';
}

function renderBuckets(buckets) {
  const rows = (buckets || []).slice(0, 200);
  $("bucket-count").textContent = String((buckets || []).length);
  const html = rows.map((b) => `<tr>
    <td class="mono">${utcTime(b.bucket_ts)}</td>
    <td>${esc(b.asset)}</td>
    <td class="mono">${b.n}</td>
    <td class="mono">${fmt(b.internal_p50)}</td>
    <td class="mono">${fmt(b.internal_p95)}</td>
    <td class="mono">${fmt(b.client_p50)}</td>
    <td class="mono">${fmt(b.client_p95)}</td>
    <td class="mono">${fmt(b.e2e_p50)}</td>
    <td class="mono">${fmt(b.e2e_p95)}</td>
  </tr>`).join("");
  $("buckets-tbody").innerHTML = html || '<tr><td colspan="9" class="no-data">暂无历史</td></tr>';
}

function renderEvents(events) {
  const rows = (events || []).slice(0, 100);
  $("event-count").textContent = String((events || []).length);
  const html = rows.map((e) => `<tr>
    <td class="mono">${utcTime(e.ts)}</td>
    <td>${esc(e.asset)}</td>
    <td class="mono">${utcTime(e.window_start)}</td>
    <td class="mono">${fmt(e.data_age_ms)}</td>
    <td class="mono">${fmt(e.book_age_ms)}</td>
    <td class="mono">${fmt(e.fill_ms)}</td>
    <td class="muted">${esc(String(e.note || "").slice(0, 50))}</td>
  </tr>`).join("");
  $("events-tbody").innerHTML = html || '<tr><td colspan="7" class="no-data">暂无入场</td></tr>';
}

function renderNav(feeds) {
  const rtds = (feeds && feeds.rtds) || {};
  const ok =
    Object.keys(rtds).length > 0 &&
    Object.values(rtds).every((x) => x && x.connected) &&
    Boolean(feeds && feeds.clob && feeds.clob.connected);
  $("live-dot").className = "live-dot" + (ok ? "" : " offline");
  $("live-label").textContent = ok ? "在线" : "离线";
}

async function refresh() {
  try {
    const data = await getJSON("/api/latency?hours=24");
    renderCards(data.summary || {}, data.decision || {});
    renderConclusion(data.summary || {}, data.decision || {});
    renderSummary(data.summary || {});
    renderBuckets(data.buckets || []);
    renderEvents(data.events || []);
    renderNav(data.feeds || {});
  } catch (e) {
    if (String(e.message) !== "auth") $("live-label").textContent = "接口错误";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  refresh();
  setInterval(refresh, 5000);
});
