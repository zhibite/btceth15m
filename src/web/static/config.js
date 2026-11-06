"use strict";

const $ = (id) => document.getElementById(id);

// Per-asset key parameters shown in the asset table. Everything else is a
// common parameter below. Keep this list in sync with ASSET_KEY_FIELDS in
// src/config.py (the API rejects unknown key fields).
const KEY_FIELDS = [
  { key: "min_edge", step: 0.01, title: "fair − 成交均价 − fee 的最低值；低于该值不入场" },
  { key: "model_buffer", step: 0.01, title: "额外安全边际；入场阈值 = 最小 Edge + 模型缓冲" },
  { key: "max_position_usd", step: 0.5, title: "每个窗口最多投入的 USDC" },
  { key: "entry_min_rel_s", step: 1, title: "窗口开始后多少秒才允许入场" },
  { key: "entry_max_rel_s", step: 1, title: "窗口开始后多少秒停止入场" },
  { key: "min_fair", step: 0.01, title: "方向 fair 下限；0 = 关闭" },
  { key: "min_entry_price", step: 0.01, title: "含费有效入场价下限；0 = 关闭" },
  { key: "daily_loss_limit_usd", step: 1, title: "该币种当日已实现亏损达到该值后停止入场" },
];

const COMMON_GROUPS = [
  {
    title: "盘口过滤",
    fields: [
      { key: "max_spread", label: "最大价差", help: "买一/卖一价差超过该值不入场。", step: 0.005 },
      { key: "max_stale_book_s", label: "盘口最大年龄", help: "盘口超过该秒数未更新不入场。", step: 0.5, unit: "秒" },
      { key: "min_ask_shares", label: "最小卖一深度", help: "卖一档至少这么多份额。", step: 1, unit: "份" },
      { key: "min_order_shares", label: "最小订单份额", help: "Polymarket 短周期加密市场最小订单通常为 5 份。", step: 1, unit: "份" },
    ],
  },
  {
    title: "模型与执行",
    fields: [
      { key: "taker_fee_rate", label: "Taker 费率", help: "官方 crypto 费率 0.07；fee = rate × p × (1−p)。", step: 0.01 },
      { key: "sigma_lookback_s", label: "σ 回看窗口", help: "用最近多少秒的 1s 收益估计波动率。", step: 30, unit: "秒" },
      { key: "min_sigma_samples", label: "σ 最少样本", help: "不足时显示预热中，不入场。", step: 10, unit: "个" },
      { key: "engine_tick_s", label: "引擎评估间隔", help: "每多少秒评估一次。", step: 0.1, unit: "秒" },
      { key: "signal_log_min_edge", label: "信号记录阈值", help: "Edge 超过该值时记录未入场信号，便于复盘。", step: 0.01 },
    ],
  },
  {
    title: "组合风控",
    fields: [
      { key: "daily_loss_limit_usd", label: "组合日损失上限", help: "所有币种合计的当日已实现亏损上限；同时作为未单独设置日损失上限的币种默认值。", step: 1, unit: "USDC" },
    ],
  },
];

let DATA = null;
const INITIAL = { assets: {}, global: {} };

const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const modeLabel = (mode) => (mode === "live" ? "实盘" : "纸盘");

function redirectLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (r.status === 401) { redirectLogin(); throw new Error("auth"); }
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function sendJSON(url, method, body) {
  const opts = { method, cache: "no-store" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) { redirectLogin(); throw new Error("auth"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(formatDetail(data.detail));
  return data;
}

function formatDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (d.loc ? d.loc + ": " : "") + (d.msg || "")).join("；");
  }
  return detail || "请求失败";
}

/* ── Asset table ──────────────────────────────────────────────────────── */
function sourceLabel(row) {
  return row.source === "override"
    ? "已覆盖"
    : row.source === "asset_default"
      ? "币种默认"
      : "继承全局";
}

function sourceClass(row) {
  return row.source === "override"
    ? "override"
    : row.source === "asset_default"
      ? "asset_default"
      : "global";
}

function modeSelect(row) {
  const live = DATA.live || {};
  const labels = { inherit: "跟随全局", paper: "纸盘", live: "实盘" };
  const titles = {
    inherit: `使用全局默认模式（当前${modeLabel(DATA.global_mode)}）`,
    paper: "该币种只做纸盘",
    live: live.available ? "该币种真实下单" : `实盘不可用：${live.reason || ""}`,
  };
  const options = ["inherit", "paper", "live"];
  return `<select data-role="mode" class="config-select">
    ${options.map((value) => {
      const disabled = value === "live" && !live.available ? " disabled" : "";
      return `<option value="${value}" title="${esc(titles[value] || "")}"${row.mode === value ? " selected" : ""}${disabled}>${esc(labels[value])}</option>`;
    }).join("")}
  </select>`;
}

function keyCell(row, field) {
  const value = row.params[field.key];
  const overridden = (row.override_fields || []).includes(field.key);
  return `<td class="config-num-cell${overridden ? " overridden" : ""}">
    <input type="number" class="config-num" data-key="${esc(field.key)}"
      step="${esc(field.step)}" value="${esc(value)}" title="${esc(field.title || "")}"
      autocomplete="off" spellcheck="false" inputmode="decimal">
  </td>`;
}

function renderAssetTable() {
  const rows = DATA.asset_rows || [];
  $("asset-count").textContent = rows.length + " 个币种";
  INITIAL.assets = {};
  if (!rows.length) {
    $("asset-tbody").innerHTML = '<tr><td colspan="12" class="no-data">未配置币种</td></tr>';
    return;
  }
  $("asset-tbody").innerHTML = rows.map((row) => {
    INITIAL.assets[row.asset] = {
      enabled: Boolean(row.enabled),
      mode: row.mode || "inherit",
      params: Object.assign({}, row.params),
      inherited: Object.assign({}, row.inherited),
      override_fields: (row.override_fields || []).slice(),
    };
    const liveWarn = row.mode === "live" && row.effective_mode !== "live";
    const dotClass = liveWarn ? "warn" : sourceClass(row);
    const title = `${row.asset} · ${sourceLabel(row)}` +
      (liveWarn ? " · 实盘不可用，当前按纸盘" : "");
    return `<tr data-asset="${esc(row.asset)}" class="${row.enabled ? "" : "asset-off"}">
      <td class="asset-name" title="${esc(title)}">
        <span class="asset-code">${esc(row.asset)}</span>
        <span class="asset-dot ${dotClass}"></span>
      </td>
      <td class="config-center">
        <input type="checkbox" data-role="enabled" class="config-check"${row.enabled ? " checked" : ""}>
      </td>
      <td>${modeSelect(row)}</td>
      ${KEY_FIELDS.map((field) => keyCell(row, field)).join("")}
      <td class="config-center">
        <button class="btn btn-ghost btn-sm" type="button" data-role="reset">重置</button>
      </td>
    </tr>`;
  }).join("");
}

/* ── Common params ────────────────────────────────────────────────────── */
function fieldHtml(field) {
  const value = DATA.global_params[field.key];
  const unit = field.unit ? `<span class="config-unit">${esc(field.unit)}</span>` : "";
  return `<div class="field" data-field="${esc(field.key)}">
    <label>${esc(field.label)}</label>
    <div style="display:flex;align-items:center;gap:6px">
      <input type="number" step="${esc(field.step || "any")}" data-common="${esc(field.key)}" value="${esc(value)}">
      ${unit}
    </div>
    <div class="config-help">${esc(field.help || "")}</div>
    <div class="config-key">${esc(field.key)}</div>
  </div>`;
}

function renderCommon() {
  INITIAL.global = Object.assign({}, DATA.global_params);
  $("groups").innerHTML = COMMON_GROUPS.map((group) => `<div class="config-group">
    <h2><span class="dot"></span>${esc(group.title)}<span class="count">${group.fields.length} 项</span></h2>
    <div class="config-grid">${group.fields.map(fieldHtml).join("")}</div>
  </div>`).join("");
}

function renderLiveInfo() {
  const live = DATA.live || {};
  $("global-mode").textContent = modeLabel(DATA.global_mode);
  $("live-state").textContent = live.available
    ? "可用"
    : (live.enabled ? "不可用" : "未启用");
  const alert = $("live-alert");
  if (live.available) {
    alert.innerHTML = "";
  } else {
    const reason = live.reason || (live.enabled ? "实盘客户端不可用" : "LIVE_ENABLED=false");
    alert.innerHTML = `<div class="alert warn">实盘不可用：${esc(reason)}。所有已开启币种按纸盘运行。</div>`;
  }
}

function render() {
  renderLiveInfo();
  renderAssetTable();
  renderCommon();
  bindInputs();
  updateSaveState();
}

/* ── Change tracking ──────────────────────────────────────────────────── */
function commonChanged(el) {
  const initial = INITIAL.global[el.dataset.common];
  return Number(el.value) !== Number(initial);
}

function assetRowChanged(tr) {
  const asset = tr.dataset.asset;
  const init = INITIAL.assets[asset];
  if (!init) return false;
  if (tr.querySelector('[data-role="enabled"]').checked !== init.enabled) return true;
  if (tr.querySelector('[data-role="mode"]').value !== init.mode) return true;
  return Array.from(tr.querySelectorAll("[data-key]")).some((el) => {
    const key = el.dataset.key;
    const raw = el.value.trim();
    if (raw === "") return init.override_fields.includes(key);
    const value = Number(raw);
    return Number.isFinite(value) && Math.abs(value - Number(init.params[key])) > 1e-9;
  });
}

function collectCommonPatch() {
  const patch = {};
  document.querySelectorAll("#groups [data-common]").forEach((el) => {
    if (!commonChanged(el)) return;
    const value = Number(el.value);
    if (Number.isFinite(value)) patch[el.dataset.common] = value;
  });
  return patch;
}

function collectAssetPatch(tr) {
  const asset = tr.dataset.asset;
  const init = INITIAL.assets[asset];
  const params = {};
  tr.querySelectorAll("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    const raw = el.value.trim();
    if (raw === "") return; // empty = inherit; omit from the override patch
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    const inherited = Number(init.inherited[key]);
    if (Math.abs(value - inherited) > 1e-9) params[key] = value;
  });
  // Only send a switch that actually changed. This matters when a coin was
  // saved as live and live is now unavailable: editing its params/on-off must
  // not resend mode=live and be rejected by the server.
  const patch = { asset, params };
  const enabled = tr.querySelector('[data-role="enabled"]').checked;
  const mode = tr.querySelector('[data-role="mode"]').value;
  if (enabled !== init.enabled) patch.enabled = enabled;
  if (mode !== init.mode) patch.mode = mode;
  return patch;
}

function changedAssetRows() {
  return Array.from(document.querySelectorAll("#asset-tbody tr[data-asset]"))
    .filter(assetRowChanged)
    .map(collectAssetPatch);
}

function changedFieldCount() {
  return Array.from(document.querySelectorAll("#groups [data-common]"))
    .filter(commonChanged).length;
}

function updateSaveState() {
  const assets = changedAssetRows();
  const common = changedFieldCount();
  const count = assets.length + common;
  $("save-btn").disabled = count === 0;
  $("save-msg").textContent = count
    ? `${assets.length} 个币种、${common} 个通用参数待保存`
    : "没有待保存的修改";
}

function inputDirty(el) {
  const tr = el.closest("tr[data-asset]");
  const init = tr && INITIAL.assets[tr.dataset.asset];
  if (!init) return false;
  const raw = el.value.trim();
  if (raw === "") return init.override_fields.includes(el.dataset.key);
  const value = Number(raw);
  return Number.isFinite(value) &&
    Math.abs(value - Number(init.params[el.dataset.key])) > 1e-9;
}

function bindInputs() {
  document.querySelectorAll("#asset-tbody [data-role], #asset-tbody [data-key]").forEach((el) => {
    el.addEventListener("input", () => {
      if (el.matches("input[data-key]")) el.classList.toggle("dirty", inputDirty(el));
      updateSaveState();
    });
    el.addEventListener("change", updateSaveState);
  });
  document.querySelectorAll("#groups [data-common]").forEach((el) => {
    el.addEventListener("input", () => {
      const box = el.closest(".field");
      if (box) box.classList.toggle("changed", commonChanged(el));
      updateSaveState();
    });
  });
  document.querySelectorAll('#asset-tbody [data-role="reset"]').forEach((btn) => {
    btn.addEventListener("click", () => resetAsset(btn.closest("tr").dataset.asset));
  });
}

/* ── Load / save / reset ──────────────────────────────────────────────── */
async function load() {
  try {
    DATA = await getJSON("/api/config");
    render();
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("groups").innerHTML = '<div class="alert warn">读取配置失败：' + esc(e.message) + "</div>";
    $("asset-tbody").innerHTML = '<tr><td colspan="12" class="no-data">读取配置失败</td></tr>';
  }
}

async function saveConfig() {
  const assets = changedAssetRows();
  const globalPatch = collectCommonPatch();
  if (!assets.length && !Object.keys(globalPatch).length) return;
  const body = { assets };
  if (Object.keys(globalPatch).length) body.global_params = globalPatch;
  $("save-btn").disabled = true;
  $("save-msg").textContent = "保存中...";
  try {
    const res = await sendJSON("/api/config/bulk", "PUT", body);
    DATA = res;
    render();
    $("save-msg").textContent = "已保存";
    setTimeout(updateSaveState, 4000);
  } catch (e) {
    if (String(e.message) !== "auth") {
      $("save-msg").textContent = "保存失败：" + e.message;
      $("save-btn").disabled = false;
    }
  }
}

async function resetAsset(asset) {
  if (!confirm(`重置 ${asset} 的全部参数覆盖？`)) return;
  try {
    await sendJSON("/api/config/reset", "POST", { asset });
    await load();
    $("save-msg").textContent = `已重置 ${asset}`;
  } catch (e) {
    if (String(e.message) !== "auth") $("save-msg").textContent = "重置失败：" + e.message;
  }
}

async function resetGlobal() {
  if (!confirm("恢复全局默认参数？已保存的币种覆盖不会删除。")) return;
  try {
    await sendJSON("/api/config/reset", "POST", {});
    await load();
    $("save-msg").textContent = "已恢复全局默认";
  } catch (e) {
    if (String(e.message) !== "auth") $("save-msg").textContent = "重置失败：" + e.message;
  }
}

/* ── Nav ──────────────────────────────────────────────────────────────── */
function clockStr() {
  return new Date().toISOString().slice(11, 19) + " UTC";
}

async function refreshNav() {
  if (document.hidden) return; // don't poll from background tabs
  try {
    const s = await getJSON("/api/status");
    const feeds = s.feeds || {};
    const rtds = feeds.rtds || {};
    const enabled = Array.isArray(s.enabled_assets) && s.enabled_assets.length
      ? new Set(s.enabled_assets)
      : null;
    const entries = Object.entries(rtds).filter(([asset]) => !enabled || enabled.has(asset));
    const ok = entries.length > 0 &&
      entries.every(([, x]) => x && x.connected) &&
      Boolean(feeds.clob?.connected);
    $("live-dot").className = "live-dot" + (ok ? "" : " offline");
    $("live-label").textContent = ok ? "在线" : "离线";
  } catch (e) {
    /* 配置页不因状态接口失败而中断 */
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("save-btn").addEventListener("click", saveConfig);
  $("reset-btn").addEventListener("click", resetGlobal);
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  load();
  refreshNav();
  setInterval(refreshNav, 5000);
});
