"use strict";

const $ = (id) => document.getElementById(id);

const GROUPS = [
  {
    title: "入场与 Edge",
    fields: [
      { key: "min_edge", label: "最小 Edge", help: "公允概率 − 成交均价 − 手续费 的最低值；低于该值不入场。", step: 0.01 },
      { key: "model_buffer", label: "模型缓冲", help: "额外安全边际，抵消模型在 0.9 以上区间的过度自信。", step: 0.01 },
      { key: "max_position_usd", label: "单笔名义金额", help: "每个窗口最多投入的 USDC。", step: 0.5, unit: "USDC" },
      { key: "entry_min_rel_s", label: "最早入场（秒）", help: "窗口开始后多少秒才允许入场。", step: 1, unit: "秒" },
      { key: "entry_max_rel_s", label: "最晚入场（秒）", help: "窗口结束前多少秒停止入场。", step: 1, unit: "秒" },
    ],
  },
  {
    title: "风控",
    fields: [
      { key: "daily_loss_limit_usd", label: "日损失上限", help: "当日已实现亏损达到该值后停止新入场。", step: 1, unit: "USDC" },
    ],
  },
  {
    title: "盘口过滤",
    fields: [
      { key: "max_spread", label: "最大价差", help: "买一/卖一价差超过该值不入场。", step: 0.005 },
      { key: "max_stale_book_s", label: "盘口最大年龄", help: "盘口超过该秒数未更新不入场。", step: 0.5, unit: "秒" },
      { key: "min_ask_shares", label: "最小卖一深度", help: "卖一档至少这么多份额。", step: 1, unit: "份" },
      { key: "min_order_shares", label: "最小订单份额", help: "Polymarket 5m 市场最小订单通常为 5 份。", step: 1, unit: "份" },
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
];

let DATA = null;
let INITIAL = {};

const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

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

function fieldHtml(field) {
  const value = DATA.params[field.key];
  const unit = field.unit ? `<span class="config-unit">${esc(field.unit)}</span>` : "";
  return `<div class="field" data-field="${esc(field.key)}">
    <label>${esc(field.label)} <span class="scope immediate">立即生效</span></label>
    <div style="display:flex;align-items:center;gap:6px">
      <input type="number" step="${esc(field.step || "any")}" data-key="${esc(field.key)}" value="${esc(value)}">
      ${unit}
    </div>
    <div class="config-help">${esc(field.help || "")}</div>
    <div class="config-key">${esc(field.key)}</div>
  </div>`;
}

function render() {
  $("mode").textContent = DATA.mode === "paper" ? "纸盘" : "实盘";
  $("db-path").textContent = DATA.db_path || "--";
  $("db-path-note").textContent = DATA.db_path || "/app/data/poly5m.db";
  INITIAL = Object.assign({}, DATA.params);
  $("groups").innerHTML = GROUPS.map((group) => `<div class="config-group">
    <h2><span class="dot"></span>${esc(group.title)}<span class="count">${group.fields.length} 项</span></h2>
    <div class="config-grid">${group.fields.map(fieldHtml).join("")}</div>
  </div>`).join("");
  document.querySelectorAll("#groups [data-key]").forEach((el) => {
    el.addEventListener("input", () => {
      const changed = Number(el.value) !== Number(INITIAL[el.dataset.key]);
      const box = el.closest(".field");
      if (box) box.classList.toggle("changed", changed);
      updateSaveState();
    });
  });
  updateSaveState();
}

function changedFields() {
  return Array.from(document.querySelectorAll("#groups [data-key]")).filter(
    (el) => Number(el.value) !== Number(INITIAL[el.dataset.key])
  );
}

function updateSaveState() {
  const changed = changedFields();
  $("save-btn").disabled = changed.length === 0;
  $("save-msg").textContent = changed.length
    ? changed.length + " 项待保存"
    : "没有待保存的修改";
}

async function load() {
  try {
    DATA = await getJSON("/api/config");
    render();
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("groups").innerHTML = '<div class="alert warn">读取配置失败：' + esc(e.message) + "</div>";
  }
}

async function saveConfig() {
  const patch = {};
  document.querySelectorAll("#groups [data-key]").forEach((el) => {
    patch[el.dataset.key] = Number(el.value);
  });
  $("save-btn").disabled = true;
  $("save-msg").textContent = "保存中...";
  try {
    const res = await sendJSON("/api/config", "PUT", patch);
    DATA.params = res.params;
    render();
    $("save-msg").textContent = "已保存，下一 tick 生效";
    setTimeout(updateSaveState, 4000);
  } catch (e) {
    if (String(e.message) !== "auth") {
      $("save-msg").textContent = "保存失败：" + e.message;
      $("save-btn").disabled = false;
    }
  }
}

async function resetConfig() {
  if (!confirm("恢复 config.toml 中的默认参数？当前保存的覆盖会被清除。")) return;
  try {
    const res = await sendJSON("/api/config/reset", "POST");
    DATA.params = res.params;
    render();
    $("save-msg").textContent = "已恢复默认值";
    setTimeout(updateSaveState, 4000);
  } catch (e) {
    if (String(e.message) !== "auth") {
      $("save-msg").textContent = "重置失败：" + e.message;
    }
  }
}

function clockStr() {
  return new Date().toISOString().slice(11, 19) + " UTC";
}

async function refreshNav() {
  try {
    const s = await getJSON("/api/status");
    const feeds = s.feeds || {};
    const rtds = feeds.rtds || {};
    const ok = Object.keys(rtds).length > 0 &&
      Object.values(rtds).every((x) => x && x.connected) &&
      Boolean(feeds.clob?.connected);
    $("live-dot").className = "live-dot" + (ok ? "" : " offline");
    $("live-label").textContent = ok ? "在线" : "离线";
  } catch (e) {
    /* 配置页不因状态接口失败而中断 */
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("save-btn").addEventListener("click", saveConfig);
  $("reset-btn").addEventListener("click", resetConfig);
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  load();
  refreshNav();
  setInterval(refreshNav, 5000);
});
