"use strict";

const $ = (id) => document.getElementById(id);

const GROUPS = [
  {
    title: "入场与 Edge",
    fields: [
      { key: "min_edge", label: "最小 Edge", help: "公允概率 − 成交均价 − 手续费 的最低值；低于该值不入场。", step: 0.01 },
      { key: "model_buffer", label: "模型缓冲", help: "额外安全边际，抵消模型的过度自信；入场阈值 = 最小 Edge + 模型缓冲。", step: 0.01 },
      { key: "min_fair", label: "最小 Fair（方向）", help: "只考虑模型概率不低于该值的方向；过滤 fair≈0.5 的彩票票。0 = 关闭。", step: 0.01 },
      { key: "min_entry_price", label: "最小有效入场价", help: "含手续费的平均成交价低于该值时不入场；便宜票意味着市场不认同模型，模型偏自信时尤其危险。0 = 关闭。", step: 0.01 },
      { key: "max_position_usd", label: "单笔名义金额", help: "每个窗口最多投入的 USDC。", step: 0.5, unit: "USDC" },
      { key: "entry_min_rel_s", label: "最早入场（秒）", help: "窗口开始后多少秒才允许入场。", step: 1, unit: "秒" },
      { key: "entry_max_rel_s", label: "最晚入场（秒）", help: "窗口结束前多少秒停止入场。", step: 1, unit: "秒" },
    ],
  },
  {
    title: "风控",
    fields: [
      { key: "daily_loss_limit_usd", label: "日损失上限", help: "该币种当日已实现亏损达到该值后停止新入场；全局默认值同时作为所有币种合计的止损上限。", step: 1, unit: "USDC" },
    ],
  },
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
    title: "Maker 影子",
    fields: [
      { key: "maker_shadow_enabled", label: "启用影子对比", type: "bool", help: "每次 taker 入场时并行模拟 maker-first 路径；只写统计，不改变实际纸盘成交。" },
      { key: "maker_buffer", label: "Maker 安全边际", help: "挂在 bid 时额外要求的 edge，用来抵消被动成交的逆向选择。", step: 0.01 },
      { key: "maker_fallback_rel_s", label: "转 Taker 时间", help: "窗口开始后多少秒仍未成交，就按当前 ask 模拟 taker fallback。", step: 5, unit: "秒" },
      { key: "maker_chase_bid", label: "跟随买一", type: "bool", help: "买一上移时撤单改挂到新的 best bid；关闭则保持原价排队。" },
    ],
  },
];

let DATA = null;
let INITIAL = {};
let ASSET = ""; // "" = 全局默认

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
  if (field.type === "bool") {
    return `<div class="field" data-field="${esc(field.key)}">
      <label>${esc(field.label)}</label>
      <div style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" data-key="${esc(field.key)}"${value ? " checked" : ""}>
      </div>
      <div class="config-help">${esc(field.help || "")}</div>
      <div class="config-key">${esc(field.key)}</div>
    </div>`;
  }
  return `<div class="field" data-field="${esc(field.key)}">
    <label>${esc(field.label)}</label>
    <div style="display:flex;align-items:center;gap:6px">
      <input type="number" step="${esc(field.step || "any")}" data-key="${esc(field.key)}" value="${esc(value)}">
      ${unit}
    </div>
    <div class="config-help">${esc(field.help || "")}</div>
    <div class="config-key">${esc(field.key)}</div>
  </div>`;
}

function renderAssetSelect(assets) {
  const select = $("asset-select");
  if (!select) return;
  const options = ['<option value="">全局默认</option>'].concat(
    (assets || []).map((asset) => `<option value="${esc(asset)}">${esc(asset)}</option>`)
  );
  select.innerHTML = options.join("");
  select.value = ASSET;
}

function configSource() {
  if (!ASSET) return "global";
  if (DATA.source) return DATA.source;
  return DATA.overridden ? "override" : "global";
}

function render() {
  $("mode").textContent = DATA.mode === "paper" ? "纸盘" : "实盘";
  const source = configSource();
  const state = $("override-state");
  if (state) {
    state.textContent = !ASSET
      ? "全局默认"
      : source === "override"
        ? "已单独设置"
        : source === "asset_default"
          ? "币种默认"
          : "继承全局";
  }
  const resetBtn = $("reset-btn");
  if (resetBtn) {
    resetBtn.textContent = !ASSET
      ? "恢复默认"
      : source === "override"
        ? "取消币种覆盖"
        : "恢复币种默认";
    resetBtn.disabled = Boolean(ASSET) && source !== "override";
  }
  INITIAL = Object.assign({}, DATA.params);
  $("groups").innerHTML = GROUPS.map((group) => `<div class="config-group">
    <h2><span class="dot"></span>${esc(group.title)}<span class="count">${group.fields.length} 项</span></h2>
    <div class="config-grid">${group.fields.map(fieldHtml).join("")}</div>
  </div>`).join("");
  document.querySelectorAll("#groups [data-key]").forEach((el) => {
    el.addEventListener("input", () => {
      const box = el.closest(".field");
      if (box) box.classList.toggle("changed", fieldChanged(el));
      updateSaveState();
    });
  });
  updateSaveState();
}

function fieldChanged(el) {
  const initial = INITIAL[el.dataset.key];
  if (el.type === "checkbox") return el.checked !== Boolean(initial);
  return Number(el.value) !== Number(initial);
}

function fieldValue(el) {
  return el.type === "checkbox" ? el.checked : Number(el.value);
}

function changedFields() {
  return Array.from(document.querySelectorAll("#groups [data-key]")).filter(
    fieldChanged
  );
}

function updateSaveState() {
  const changed = changedFields();
  $("save-btn").disabled = changed.length === 0;
  $("save-msg").textContent = changed.length
    ? changed.length + " 项待保存"
    : "没有待保存的修改";
}

async function load(asset = "") {
  try {
    const query = asset ? "?asset=" + encodeURIComponent(asset) : "";
    DATA = await getJSON("/api/config" + query);
    ASSET = DATA.asset || "";
    renderAssetSelect(DATA.assets || []);
    render();
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("groups").innerHTML = '<div class="alert warn">读取配置失败：' + esc(e.message) + "</div>";
  }
}

async function saveConfig() {
  const patch = {};
  document.querySelectorAll("#groups [data-key]").forEach((el) => {
    patch[el.dataset.key] = fieldValue(el);
  });
  $("save-btn").disabled = true;
  $("save-msg").textContent = "保存中...";
  try {
    const res = await sendJSON("/api/config", "PUT", {
      asset: ASSET,
      params: patch,
    });
    DATA = Object.assign({}, DATA, res);
    render();
    $("save-msg").textContent = ASSET ? `已保存（${ASSET}）` : "已保存（全局）";
    setTimeout(updateSaveState, 4000);
  } catch (e) {
    if (String(e.message) !== "auth") {
      $("save-msg").textContent = "保存失败：" + e.message;
      $("save-btn").disabled = false;
    }
  }
}

async function resetConfig() {
  const label = ASSET ? `${ASSET} 的币种覆盖` : "全局默认";
  if (!confirm(`恢复${label}？`)) return;
  try {
    const res = await sendJSON("/api/config/reset", "POST", { asset: ASSET });
    DATA = Object.assign({}, DATA, res);
    render();
    $("save-msg").textContent = ASSET
      ? `已恢复 ${ASSET} 的全局继承`
      : "已恢复全局默认";
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
  $("asset-select").addEventListener("change", async (e) => {
    const next = e.target.value;
    if (changedFields().length && !confirm("当前有未保存修改，切换后会丢失，继续？")) {
      e.target.value = ASSET;
      return;
    }
    await load(next);
  });
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  load();
  refreshNav();
  setInterval(refreshNav, 5000);
});
