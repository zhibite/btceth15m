"use strict";

const $ = (id) => document.getElementById(id);
const PAGERS = {
  windows: { limit: 20, offset: 0, total: 0 },
  fills: { limit: 20, offset: 0, total: 0 },
};
const initialAsset = new URLSearchParams(location.search).get("asset") || "";
let ASSET = initialAsset.trim().toUpperCase(); // "" = 全部币种

function assetQuery() {
  return ASSET ? "&asset=" + encodeURIComponent(ASSET) : "";
}

function syncAssetUrl() {
  const url = new URL(location.href);
  if (ASSET) url.searchParams.set("asset", ASSET);
  else url.searchParams.delete("asset");
  history.replaceState(null, "", url);
}

function ensureAssetOptions(status) {
  const select = $("asset-filter");
  if (!select) return;
  // Confirmed configuration first, so the selector still works before the
  // engine has published any tick for an asset.
  const names = new Set(status && status.configured_assets ? status.configured_assets : []);
  Object.keys((status && status.assets) || {}).forEach((a) => names.add(a));
  const assets = Array.from(names).sort();
  const existing = new Set(Array.from(select.options).map((o) => o.value));
  for (const asset of assets) {
    if (!existing.has(asset)) {
      const option = document.createElement("option");
      option.value = asset;
      option.textContent = asset;
      select.appendChild(option);
    }
  }
  select.value = ASSET;
  $("asset-meta").textContent = ASSET || "全部币种";
}

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

function modePill(mode) {
  return String(mode || "paper") === "live"
    ? '<span class="pill sell">实盘</span>'
    : '<span class="pill idle">纸盘</span>';
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

function renderFills(rows) {
  const html = rows.map((f) => {
    const outcome = f.window_outcome || f.position_outcome;
    const settled = f.position_status === "settled" && Boolean(outcome);
    const win = settled && f.position_side === outcome;
    const result = settled
      ? `${outcomePill(outcome)} <span class="${win ? "green" : "red"}">${win ? "赢" : "输"}</span>`
      : '<span class="muted">进行中</span>';
    const pnlValue = settled ? f.position_pnl : null;
    const roi = settled && Number(f.position_cost) > 0
      ? Number(f.position_pnl) / Number(f.position_cost)
      : null;
    const pnl = pnlValue === null || pnlValue === undefined
      ? "--"
      : `<span class="${pnlClass(pnlValue)}">${money(pnlValue)}</span>` +
        (roi === null ? "" : ` <span class="muted">(${pct(roi)})</span>`);
    return `<tr>
      <td class="mono">${utcTime(f.ts)}</td>
      <td>${esc(f.asset)}</td>
      <td>${modePill(f.mode)}</td>
      <td><span class="pill ${f.side === "BUY" ? "buy" : "sell"}">${f.side === "BUY" ? "买入" : "卖出"}</span></td>
      <td>${outcomePill(f.position_side)}</td>
      <td class="mono">${fmt(f.price, 3)}</td>
      <td class="mono">${fmt(f.size, 2)}</td>
      <td class="mono">$${fmt(f.fee, 4)}</td>
      <td>${f.role === "TAKER" ? "Taker" : f.role === "MAKER" ? "Maker" : esc(f.role || "--")}</td>
      <td>${result}</td>
      <td class="mono">${pnl}</td>
    </tr>`;
  }).join("");
  $("fills-tbody").innerHTML = html || '<tr><td colspan="11" class="no-data">暂无成交</td></tr>';
}

function renderNav(status) {
  const feeds = status.feeds || {};
  const rtds = feeds.rtds || {};
  const enabled = Array.isArray(status.enabled_assets) && status.enabled_assets.length
    ? new Set(status.enabled_assets)
    : null;
  const entries = Object.entries(rtds).filter(([asset]) => !enabled || enabled.has(asset));
  const ok = entries.length > 0 &&
    entries.every(([, x]) => x && x.connected) &&
    Boolean(feeds.clob?.connected);
  $("live-dot").className = "live-dot" + (ok ? "" : " offline");
  $("live-label").textContent = ok ? "在线" : "离线";
}

function updatePager(prefix, state) {
  const page = Math.floor(state.offset / state.limit) + 1;
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  const info = $(prefix + "-page-info");
  if (info) info.textContent = `第 ${page} / ${pages} 页 · 共 ${state.total} 条`;
  const prev = $(prefix + "-prev");
  const next = $(prefix + "-next");
  if (prev) prev.disabled = state.offset <= 0;
  if (next) next.disabled = state.offset + state.limit >= state.total;
}

async function loadWindows() {
  const state = PAGERS.windows;
  const res = await getJSON(
    `/api/windows?limit=${state.limit}&offset=${state.offset}${assetQuery()}`
  );
  renderWindows(res.data || []);
  state.total = Number(res.pagination?.total || 0);
  $("window-count").textContent = String(state.total);
  updatePager("windows", state);
}

async function loadFills() {
  const state = PAGERS.fills;
  const res = await getJSON(
    `/api/fills?limit=${state.limit}&offset=${state.offset}${assetQuery()}`
  );
  renderFills(res.data || []);
  state.total = Number(res.pagination?.total || 0);
  $("fill-count").textContent = String(state.total);
  updatePager("fills", state);
}

async function refresh() {
  if (document.hidden) return; // don't poll from background tabs
  try {
    const [status, pnl] = await Promise.all([
      getJSON("/api/status"),
      getJSON(`/api/pnl?days=7${assetQuery()}`),
    ]);
    ensureAssetOptions(status);
    renderStats(pnl.summary || {});
    await Promise.all([loadWindows(), loadFills()]);
    renderNav(status);
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("live-label").textContent = "接口错误";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const loaders = {
    windows: loadWindows,
    fills: loadFills,
  };
  Object.keys(loaders).forEach((prefix) => {
    $(prefix + "-prev").addEventListener("click", () => {
      PAGERS[prefix].offset = Math.max(0, PAGERS[prefix].offset - PAGERS[prefix].limit);
      loaders[prefix]().catch(() => {});
    });
    $(prefix + "-next").addEventListener("click", () => {
      PAGERS[prefix].offset += PAGERS[prefix].limit;
      loaders[prefix]().catch(() => {});
    });
  });
  $("asset-filter").addEventListener("change", async (e) => {
    ASSET = e.target.value;
    $("asset-meta").textContent = ASSET || "全部币种";
    syncAssetUrl();
    Object.keys(PAGERS).forEach((key) => {
      PAGERS[key].offset = 0;
    });
    await refresh();
  });
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  refresh();
  setInterval(refresh, 10000);
});
