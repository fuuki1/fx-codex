const state = {
  chart: {
    symbol: "USDJPY",
    timeframe: "1h",
    range: "24h",
    lastLoadedAt: 0,
    zoom: 1,
    pan: 0,
    hover: null,
    layers: {
      decisions: true,
      forecast: true,
      targets: true,
      spread: true,
    },
  },
};

const JOURNAL_FILE = "briefing_journal.jsonl";
const LEARNING_FILE = "briefing_learning.json";
const TF_JOURNAL_FILE = "briefing_tf_journal.jsonl";
const TF_LEARNING_FILE = "briefing_tf_learning.json";
const HORIZON_JOURNAL_FILE = "briefing_horizon_forecasts.jsonl";
const HORIZON_LEARNING_FILE = "briefing_horizon_learning.json";
const ML_FILE = "ml_model.json";
const DECISION_MONITOR_FILE = "decision_expectancy_monitor.json";

const $ = (id) => document.getElementById(id);

function pct(value, fallback = "--") {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 100)}%`
    : fallback;
}

function precisePct(value, fallback = "--") {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toLocaleString("ja-JP", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}%`
    : fallback;
}

function num(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function shortDate(value) {
  if (!value) return "未記録";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function preciseDate(value) {
  if (!value) return "未記録";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function setText(id, text) {
  $(id).textContent = text;
}

function setBar(id, value) {
  const safe = Math.max(0, Math.min(1, Number(value) || 0));
  $(id).style.width = `${Math.round(safe * 100)}%`;
}

function empty(text) {
  const div = document.createElement("div");
  div.className = "empty";
  div.textContent = text;
  return div;
}

function markPanelEmpty(target, isEmpty) {
  const panel = target.closest(".panel");
  if (panel) panel.classList.toggle("is-empty-panel", Boolean(isEmpty));
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, children = []) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    el.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child) el.appendChild(child);
  }
  return el;
}

// linear interpolation between two hex colors (dataviz diverging poles)
function lerpHex(a, b, t) {
  const pa = [parseInt(a.slice(1, 3), 16), parseInt(a.slice(3, 5), 16), parseInt(a.slice(5, 7), 16)];
  const pb = [parseInt(b.slice(1, 3), 16), parseInt(b.slice(3, 5), 16), parseInt(b.slice(5, 7), 16)];
  const mix = pa.map((v, i) => Math.round(v + (pb[i] - v) * Math.max(0, Math.min(1, t))));
  return `#${mix.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

const HIT_LO = "#e34948";
const HIT_MID = "#454842";
const HIT_HI = "#1baf7a";

// diverging hit-rate color: <50% toward red pole, >50% toward green pole.
function hitColor(rate) {
  if (typeof rate !== "number" || !Number.isFinite(rate)) return "#2d2f29";
  if (rate <= 0.5) return lerpHex(HIT_LO, HIT_MID, rate / 0.5);
  return lerpHex(HIT_MID, HIT_HI, (rate - 0.5) / 0.5);
}

// readable ink on a colored fill, chosen by luminance
function inkOn(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luma > 0.6 ? "#181915" : "#f8f7f2";
}

function legendItem(label, color) {
  const item = document.createElement("span");
  item.className = "legend-item";
  const sw = document.createElement("span");
  sw.className = "legend-swatch";
  sw.style.background = color;
  const text = document.createElement("span");
  text.textContent = label;
  item.append(sw, text);
  return item;
}

// ===== 共有ツールチップ(全チャート共通のマウス追従オーバーレイ) =====
// dataviz方針: 値を主、ラベルを従。系列キーは短い線。ラベルは textContent のみ。
let _tooltipEl = null;

function tooltipEl() {
  if (_tooltipEl) return _tooltipEl;
  const el = document.createElement("div");
  el.className = "chart-tooltip";
  el.setAttribute("role", "tooltip");
  el.hidden = true;
  document.body.appendChild(el);
  _tooltipEl = el;
  return el;
}

// rows: [{ label, value, color?, muted? }] / title は見出し
function showTooltip(evt, title, rows) {
  const el = tooltipEl();
  el.replaceChildren();
  if (title) {
    const head = document.createElement("div");
    head.className = "tt-title";
    head.textContent = title;
    el.appendChild(head);
  }
  for (const row of rows) {
    const line = document.createElement("div");
    line.className = `tt-row${row.muted ? " tt-muted" : ""}`;
    if (row.color) {
      const key = document.createElement("span");
      key.className = "tt-key";
      key.style.background = row.color;
      line.appendChild(key);
    }
    if (row.label !== undefined && row.label !== "") {
      const label = document.createElement("span");
      label.className = "tt-label";
      label.textContent = row.label;
      line.appendChild(label);
    }
    const value = document.createElement("span");
    value.className = "tt-value";
    value.textContent = row.value;
    line.appendChild(value);
    el.appendChild(line);
  }
  el.hidden = false;
  moveTooltip(evt);
}

function moveTooltip(evt) {
  const el = _tooltipEl;
  if (!el || el.hidden) return;
  const pad = 14;
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + w + 8 > window.innerWidth) x = evt.clientX - w - pad;
  if (y + h + 8 > window.innerHeight) y = evt.clientY - h - pad;
  el.style.left = `${Math.max(4, x)}px`;
  el.style.top = `${Math.max(4, y)}px`;
}

function hideTooltip() {
  if (_tooltipEl) _tooltipEl.hidden = true;
}

// マーク(バー/セル/点)にホバー+フォーカスでツールチップを付ける。
// getContent() は { title, rows } を返す。tabindex を付けてキーボードでも出す。
function attachTooltip(node, getContent) {
  const enter = (evt) => {
    const { title, rows } = getContent();
    showTooltip(evt, title, rows);
  };
  node.addEventListener("pointerenter", enter);
  node.addEventListener("pointermove", moveTooltip);
  node.addEventListener("pointerleave", hideTooltip);
  node.addEventListener("focus", (evt) => {
    const rect = node.getBoundingClientRect();
    const fake = { clientX: rect.left + rect.width / 2, clientY: rect.top };
    const { title, rows } = getContent();
    showTooltip(fake, title, rows);
  });
  node.addEventListener("blur", hideTooltip);
}

function setReality({ badge, title, body, tone, reasons }) {
  const panel = $("realityPanel");
  panel.className = `reality-panel ${tone}`;
  setText("realityBadge", badge);
  setText("realityTitle", title);
  setText("realityBody", body);

  const list = $("realityReasons");
  list.replaceChildren();
  reasons.forEach((reason) => {
    const li = document.createElement("li");
    li.textContent = reason;
    list.appendChild(li);
  });
}

function renderReality(data) {
  const files = data.files || {};
  const fusionJournalExists = Boolean(files[JOURNAL_FILE]?.exists);
  const timeframeJournalExists = Boolean(files[TF_JOURNAL_FILE]?.exists);
  const journalExists = fusionJournalExists || timeframeJournalExists;
  const fusionLearningExists = Boolean(files[LEARNING_FILE]?.exists);
  const timeframeLearningExists = Boolean(files[TF_LEARNING_FILE]?.exists);
  const learningExists = fusionLearningExists || timeframeLearningExists;
  const hasMlModel = Boolean(data.ml?.has_model);
  const journalTotal = Number(data.journal?.total || 0);
  const evaluated = Number(data.learning?.evaluated || 0);
  const pending = Number(data.evaluation?.pending || 0);
  const source = data.learning_source || {};
  const reasons = [];

  if (!journalExists && !learningExists) {
    reasons.push("判断ログが0件なので、まだ当たり外れを学習できません。");
    reasons.push("dry-runやno-journalでは学習用ログが残らない可能性があります。");
  } else if (!journalExists && learningExists) {
    reasons.push("判断ログ本体は見つかりませんが、保存済みの学習ファイルを読んでいます。");
  } else if (evaluated === 0) {
    reasons.push(`全系統の判断ログは${journalTotal}件ありますが、融合24h判断の比較がまだです。`);
    if (pending > 0) reasons.push(`${pending}件は採点待ちです。`);
  } else {
    reasons.push(`融合24h判断を${evaluated}件採点済みです。時間足別成績とは分けて表示しています。`);
    const counterfactual = Number(data.learning?.counterfactual_evaluated || 0);
    if (counterfactual > 0) {
      reasons.push(
        `うち${counterfactual}件は期待値ガード見送り中のシャドー分析(反実仮想)の採点です。` +
          "運用推奨の答え合わせ(○×)とは分けて数えています。"
      );
    }
  }

  if (!fusionJournalExists && timeframeJournalExists) {
    reasons.push("融合1判断ログは未作成ですが、時間足別判断ログがあります。");
  }
  if (source.mode === "timeframe") {
    reasons.push("この表示は時間足別AI学習(15m/1h/4h/1d)を元にしています。");
  }
  if (!learningExists) {
    reasons.push("重み調整ファイルはまだ作られていません。");
  }
  if (!hasMlModel) {
    const training = data.ml?.training || {};
    const modelReasons = Array.isArray(data.ml?.reasons) ? data.ml.reasons : [];
    const eligible = Number(training.eligible_after_thinning || 0);
    const minimumRequired = Number(training.minimum_required || 0);
    const pendingMl = Number(training.pending || 0);
    const pitIneligible = Number(training.pit_ineligible || 0);
    if (modelReasons.length > 0) {
      reasons.push(`GBDT学習未完了: ${modelReasons.join(" / ")}`);
    } else if (minimumRequired > 0) {
      reasons.push(
        `GBDTの初期件数ゲートは、融合24時間判断を${training.thin_gap_hours || 4}時間` +
          `間引きした採点済みデータが${eligible}/${minimumRequired}件です。`,
      );
      reasons.push(
        "件数・クラス数・時系列分割を通過するとモデルを保存し、検証スコア不合格なら判断参加を無効のまま保持します。",
      );
      if (pitIneligible > 0) {
        reasons.push(
          `旧形式${pitIneligible}件は特徴量取得時刻を証明できないため、GBDT学習から除外しています。`,
        );
      }
      if (pendingMl > 0) reasons.push(`融合判断の採点待ちは${pendingMl}件です。`);
    } else {
      reasons.push("GBDTのMLモデルはまだ保存されていません。");
    }
  } else if (!data.ml.usable) {
    reasons.push("MLモデルはありますが、検証スコア不足などで判断参加は無効です。");
  }

  if (!journalExists && !learningExists) {
    setReality({
      badge: "not trained",
      title: "今は学習していません",
      body: "この画面はAI本体ではなく監視画面です。読み取る学習ログが無いため、現在の状態は未学習です。",
      tone: "is-bad",
      reasons,
    });
    return;
  }

  if (source.mode === "timeframe" && evaluated > 0) {
    setReality({
      badge: "timeframe trained",
      title: "時間足別AIは学習中",
      body: "Discordの時間足別学習メモと同じ系統の学習ファイルを読み取り、時間足ごとの重みと的中率を表示しています。",
      tone: "is-good",
      reasons,
    });
    return;
  }

  if (evaluated === 0) {
    setReality({
      badge: "waiting",
      title: "判断は記録済み、学習は採点待ち",
      body: "判断から約24時間後の価格が揃ってから、当たり外れを採点して学習材料にします。",
      tone: "is-waiting",
      reasons,
    });
    return;
  }

  if (!data.ml.has_model) {
    setReality({
      badge: "profile only",
      title: "自己学習は開始、MLモデルは未学習",
      body: "採点済みログから重みや苦手条件は調整できます。GBDTモデルは別途サンプル数と学習実行が必要です。",
      tone: "is-waiting",
      reasons,
    });
    return;
  }

  setReality({
    badge: data.ml.usable ? "ml usable" : "ml gated",
    title: data.ml.usable ? "MLモデルが判断参加できます" : "MLモデルは学習済みですが無効です",
    body: data.ml.usable
      ? "採点済みログからMLモデルが作成され、検証ゲートを通過しています。"
      : "モデルは保存されていますが、検証結果が基準を満たすまで判断には参加しません。",
    tone: data.ml.usable ? "is-good" : "is-waiting",
    reasons,
  });
}

function barRow(label, value, detail, className = "green") {
  const row = document.createElement("div");
  row.className = "bar-row";
  const labelEl = document.createElement("label");
  labelEl.textContent = label;
  const track = document.createElement("div");
  track.className = "bar-track";
  const fill = document.createElement("div");
  fill.className = `bar-fill ${className}`;
  fill.style.width = `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`;
  track.appendChild(fill);
  const output = document.createElement("output");
  output.textContent = detail;
  row.append(labelEl, track, output);
  return row;
}

function renderFlow(data) {
  $("flowJournal").classList.toggle("active", data.journal.total > 0);
  $("flowMature").classList.toggle("active", data.evaluation.evaluated > 0);
  $("flowMature").classList.toggle("warn", data.evaluation.pending > 0 && data.evaluation.evaluated === 0);
  $("flowProfile").classList.toggle(
    "active",
    data.files[LEARNING_FILE].exists || data.files[TF_LEARNING_FILE].exists,
  );
  $("flowMl").classList.toggle("active", data.ml.has_model);
  $("flowMl").classList.toggle("warn", !data.ml.usable && data.ml.has_model);
  $("flowPromotion").classList.toggle(
    "active",
    data.promotion.stages.macro !== "shadow" || data.promotion.stages.ml !== "shadow",
  );
}

// 直近描画したcurveを保持し、ウィンドウリサイズ時に再描画する(canvasは物理px依存)。
let lastCurve = [];

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

const CHART_PRIMARY_HORIZON = { "15m": "15m", "1h": "1h", "4h": null, "1d": "24h" };
const FORECAST_CHART_CACHE_TTL_MS = 55_000;
const FORECAST_CHART_CACHE_LIMIT = 8;
const FORECAST_CHART_REFRESH_MS = 60_000;
const FORECAST_CHART_REFRESH_OFFSET_MS = 45_000;
const FORECAST_CHART_WHEEL_LINE_PIXELS = 34;
const FORECAST_CHART_WHEEL_MAX_DELTA = 120;
const FORECAST_CHART_WHEEL_SENSITIVITY = 0.002;
let lastForecastChart = null;
let lastDashboardState = null;
let forecastChartHitTargets = [];
let forecastChartGeometry = null;
let forecastChartDrag = null;
let forecastChartAnimationFrame = 0;
let forecastChartWheelAnimationFrame = 0;
let forecastChartWheelDelta = 0;
let forecastChartWheelAnchorRatio = 0.5;
let forecastChartRequest = null;
let forecastChartRequestSequence = 0;
const forecastChartCache = new Map();

function forecastChartSelection() {
  return {
    symbol: state.chart.symbol,
    timeframe: state.chart.timeframe,
    range: state.chart.range,
  };
}

function forecastChartKey(selection = forecastChartSelection()) {
  return `${selection.symbol}:${selection.timeframe}:${selection.range}`;
}

function cachedForecastChart(key) {
  const cached = forecastChartCache.get(key);
  if (!cached) return null;
  forecastChartCache.delete(key);
  forecastChartCache.set(key, cached);
  return cached;
}

function cacheForecastChart(key, payload, loadedAt = Date.now()) {
  forecastChartCache.delete(key);
  forecastChartCache.set(key, { payload, loadedAt });
  while (forecastChartCache.size > FORECAST_CHART_CACHE_LIMIT) {
    const oldestKey = forecastChartCache.keys().next().value;
    forecastChartCache.delete(oldestKey);
  }
}

function setForecastChartBusy(busy) {
  const panel = $("forecastChartPanel");
  if (panel) panel.setAttribute("aria-busy", String(Boolean(busy)));
}

function chartDigits(symbol) {
  return String(symbol || "").endsWith("JPY") ? 3 : 5;
}

function chartNumber(value, symbol) {
  const parsed = num(value);
  return parsed === null ? "--" : parsed.toFixed(chartDigits(symbol));
}

function chartClamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function chartPanLimit(zoom = state.chart.zoom) {
  return Math.max(0, (1 - 1 / zoom) / 2);
}

function resetForecastViewport({ redraw = true } = {}) {
  if (forecastChartWheelAnimationFrame) {
    window.cancelAnimationFrame(forecastChartWheelAnimationFrame);
    forecastChartWheelAnimationFrame = 0;
    forecastChartWheelDelta = 0;
  }
  state.chart.zoom = 1;
  state.chart.pan = 0;
  state.chart.hover = null;
  forecastChartGeometry = null;
  const hover = $("forecastChartHover");
  if (hover) hover.hidden = true;
  if (redraw) scheduleForecastChartRedraw();
}

function scheduleForecastChartRedraw() {
  if (!lastForecastChart || forecastChartAnimationFrame) return;
  forecastChartAnimationFrame = window.requestAnimationFrame(() => {
    forecastChartAnimationFrame = 0;
    const canvas = $("forecastChartCanvas");
    if (canvas) drawForecastChart(canvas, lastForecastChart);
  });
}

function setForecastChartZoom(nextZoom, anchorRatio = 0.5) {
  const geometry = forecastChartGeometry;
  const oldZoom = state.chart.zoom;
  const zoom = chartClamp(nextZoom, 1, 24);
  if (!geometry || zoom === oldZoom) {
    state.chart.zoom = zoom;
    state.chart.pan = chartClamp(state.chart.pan, -chartPanLimit(), chartPanLimit());
    scheduleForecastChartRedraw();
    return;
  }
  const ratio = chartClamp(anchorRatio, 0, 1);
  const oldSpan = geometry.maxTime - geometry.minTime;
  const anchorTime = geometry.minTime + oldSpan * ratio;
  const newSpan = (geometry.fullMaxTime - geometry.fullMinTime) / zoom;
  const newCenter = anchorTime + (0.5 - ratio) * newSpan;
  const fullCenter = (geometry.fullMinTime + geometry.fullMaxTime) / 2;
  state.chart.zoom = zoom;
  state.chart.pan = (newCenter - fullCenter) / (geometry.fullMaxTime - geometry.fullMinTime);
  state.chart.pan = chartClamp(state.chart.pan, -chartPanLimit(), chartPanLimit());
  scheduleForecastChartRedraw();
}

function normalizedForecastWheelDelta(event) {
  const multiplier =
    event.deltaMode === 1
      ? FORECAST_CHART_WHEEL_LINE_PIXELS
      : event.deltaMode === 2
        ? window.innerHeight
        : 1;
  return event.deltaY * multiplier;
}

function queueForecastWheelZoom(event, anchorRatio) {
  forecastChartWheelDelta += normalizedForecastWheelDelta(event);
  forecastChartWheelAnchorRatio = chartClamp(anchorRatio, 0, 1);
  if (forecastChartWheelAnimationFrame) return;
  forecastChartWheelAnimationFrame = window.requestAnimationFrame(() => {
    forecastChartWheelAnimationFrame = 0;
    const delta = chartClamp(
      forecastChartWheelDelta,
      -FORECAST_CHART_WHEEL_MAX_DELTA,
      FORECAST_CHART_WHEEL_MAX_DELTA,
    );
    forecastChartWheelDelta = 0;
    const factor = Math.exp(-delta * FORECAST_CHART_WHEEL_SENSITIVITY);
    setForecastChartZoom(
      state.chart.zoom * factor,
      forecastChartWheelAnchorRatio,
    );
  });
}

function nearestForecastPrice(prices, timeMs) {
  if (!prices.length) return null;
  let low = 0;
  let high = prices.length - 1;
  while (low < high) {
    const midpoint = Math.floor((low + high) / 2);
    if (prices[midpoint].timeMs < timeMs) low = midpoint + 1;
    else high = midpoint;
  }
  if (low === 0) return prices[0];
  const before = prices[low - 1];
  const after = prices[low];
  return Math.abs(before.timeMs - timeMs) <= Math.abs(after.timeMs - timeMs) ? before : after;
}

function snapDecisionToPrice(decision, prices) {
  const predictionTimeMs = new Date(decision.prediction_time).getTime();
  const snappedPrice = Number.isFinite(predictionTimeMs)
    ? nearestForecastPrice(prices, predictionTimeMs)
    : null;
  return {
    ...decision,
    predictionTimeMs,
    markerTimeMs: snappedPrice?.timeMs ?? predictionTimeMs,
    marker_price_time: snappedPrice?.event_time ?? null,
    marker_time_offset_ms: snappedPrice
      ? snappedPrice.timeMs - predictionTimeMs
      : null,
  };
}

function chartWindowDecisions(chart) {
  const startMs = new Date(chart?.window?.start).getTime();
  const endMs = new Date(chart?.window?.end).getTime();
  const rows = Array.isArray(chart?.decisions) ? chart.decisions : [];
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return rows;
  return rows.filter((row) => {
    const predictionTimeMs = new Date(row.prediction_time).getTime();
    return (
      Number.isFinite(predictionTimeMs) &&
      predictionTimeMs >= startMs &&
      predictionTimeMs <= endMs
    );
  });
}

function markerTimeOffsetLabel(offsetMs) {
  if (!Number.isFinite(offsetMs)) return "--";
  const seconds = Math.round(offsetMs / 1000);
  if (seconds === 0) return "同時刻";
  return `${seconds > 0 ? "+" : ""}${seconds}秒`;
}

function renderForecastQuote(prices, selected = null) {
  if (!prices.length) return;
  const latest = prices.at(-1);
  const row = selected || latest;
  const close = num(row.close);
  const previousIndex = Math.max(0, prices.indexOf(row) - 1);
  const previous = num(prices[previousIndex]?.close);
  const change = close !== null && previous !== null ? close - previous : null;
  const changePct = change !== null && previous ? change / previous : null;
  setText("forecastChartLastPrice", chartNumber(close, state.chart.symbol));
  const changeEl = $("forecastChartChange");
  if (changeEl) {
    changeEl.classList.toggle("up", change !== null && change > 0);
    changeEl.classList.toggle("down", change !== null && change < 0);
    changeEl.textContent =
      change === null
        ? "--"
        : `${change >= 0 ? "+" : ""}${change.toFixed(chartDigits(state.chart.symbol))} (${
            changePct === null ? "--" : `${changePct >= 0 ? "+" : ""}${(changePct * 100).toFixed(3)}%`
          })`;
  }
  const open = num(row.open);
  const high = num(row.high);
  const low = num(row.low);
  const bid = num(row.bid);
  const ask = num(row.ask);
  setText(
    "forecastChartHoverOhlc",
    `${shortDate(row.event_time)}  O ${chartNumber(open, state.chart.symbol)}  H ${chartNumber(
      high,
      state.chart.symbol,
    )}  L ${chartNumber(low, state.chart.symbol)}  C ${chartNumber(
      close,
      state.chart.symbol,
    )}  Bid ${chartNumber(bid, state.chart.symbol)}  Ask ${chartNumber(
      ask,
      state.chart.symbol,
    )}`,
  );
}

function renderForecastHoverCard(row) {
  const target = $("forecastChartHover");
  if (!target) return;
  if (!row) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  target.textContent = `${shortDate(row.event_time)}  O ${chartNumber(
    row.open,
    state.chart.symbol,
  )}  H ${chartNumber(row.high, state.chart.symbol)}  L ${chartNumber(
    row.low,
    state.chart.symbol,
  )}  C ${chartNumber(row.close, state.chart.symbol)}  Bid ${chartNumber(
    row.bid,
    state.chart.symbol,
  )}  Ask ${chartNumber(row.ask, state.chart.symbol)}`;
}

function restoreChartSelection() {
  try {
    const saved = JSON.parse(window.localStorage.getItem("fxForecastChart") || "{}");
    if (["USDJPY", "EURUSD", "GBPUSD"].includes(saved.symbol)) state.chart.symbol = saved.symbol;
    if (TIMEFRAME_ORDER.includes(saved.timeframe)) state.chart.timeframe = saved.timeframe;
    if (["6h", "24h", "3d", "7d"].includes(saved.range)) state.chart.range = saved.range;
    if (saved.layers && typeof saved.layers === "object") {
      Object.keys(state.chart.layers).forEach((key) => {
        if (typeof saved.layers[key] === "boolean") state.chart.layers[key] = saved.layers[key];
      });
    }
  } catch (error) {
    // 保存値が壊れていても既定値で継続する。
  }
}

function saveChartSelection() {
  try {
    window.localStorage.setItem(
      "fxForecastChart",
      JSON.stringify({
        symbol: state.chart.symbol,
        timeframe: state.chart.timeframe,
        range: state.chart.range,
        layers: state.chart.layers,
      }),
    );
  } catch (error) {
    // localStorageが使えない場合もチャート表示は継続する。
  }
}

function syncChartControls() {
  const symbol = $("forecastChartSymbol");
  if (symbol) symbol.value = state.chart.symbol;
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => {
    const active = button.dataset.chartTimeframe === state.chart.timeframe;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-chart-range]").forEach((button) => {
    const active = button.dataset.chartRange === state.chart.range;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-chart-layer]").forEach((button) => {
    const active = state.chart.layers[button.dataset.chartLayer] !== false;
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function qualityChip(text, tone = "") {
  const chip = document.createElement("span");
  chip.className = `chart-quality-chip${tone ? ` ${tone}` : ""}`;
  chip.textContent = text;
  return chip;
}

function renderForecastChartQuality(chart) {
  const target = $("forecastChartQuality");
  if (!target) return;
  target.replaceChildren();
  const quality = chart?.quality || {};
  const latest = quality.latest_available_time;
  const latestMs = new Date(latest).getTime();
  const ageSeconds = Number.isFinite(latestMs)
    ? Math.max(0, Math.round((Date.now() - latestMs) / 1000))
    : null;
  target.appendChild(
    qualityChip(
      quality.status === "ok"
        ? `価格 ${shortDate(latest)}${ageSeconds === null ? "" : ` / ${ageSeconds}秒前`}`
        : "価格データ stale",
      quality.status !== "ok" ? "bad" : ageSeconds !== null && ageSeconds > 360 ? "warn" : "good",
    ),
  );
  target.appendChild(
    qualityChip(
      chart?.price_mode === "completed_candles"
        ? "完成bid/ask足"
        : "約5分間隔の形成中スナップショット線",
      chart?.price_mode === "completed_candles" ? "good" : "warn",
    ),
  );
  const coverage = num(quality.bid_ask_coverage);
  target.appendChild(
    qualityChip(
      `bid/ask ${coverage === null ? "--" : Math.round(coverage * 100)}%`,
      coverage && coverage > 0 ? "good" : "warn",
    ),
  );
  const excluded = Number(quality.pit_excluded || 0);
  target.appendChild(
    qualityChip(`PIT除外 ${excluded}`, excluded ? "warn" : "good"),
  );
  const decisionCount = chartWindowDecisions(chart).length;
  const decisionTotal = Number(quality.decisions_total);
  const sampled = quality.decisions_sampled === true;
  target.appendChild(
    qualityChip(
      sampled && Number.isFinite(decisionTotal)
        ? `判断 ${decisionTotal}件(表示${decisionCount}件に間引き)`
        : `判断 ${decisionCount}件`,
      decisionCount ? "good" : "warn",
    ),
  );
  if (!CHART_PRIMARY_HORIZON[state.chart.timeframe]) {
    target.appendChild(qualityChip("4hと一致する予想帯なし", "warn"));
  }
}

function matchingForecasts() {
  const latest = lastDashboardState?.horizon?.latest;
  if (!Array.isArray(latest)) return [];
  return latest.filter((row) => row.symbol === state.chart.symbol);
}

function primaryForecast() {
  const wanted = CHART_PRIMARY_HORIZON[state.chart.timeframe];
  if (!wanted) return null;
  const matches = matchingForecasts().filter((row) => row.horizon === wanted);
  return matches.sort((a, b) => new Date(a.ts) - new Date(b.ts)).at(-1) || null;
}

function forecastBandPrices(forecast) {
  const close = num(forecast?.close);
  const p10 = num(forecast?.band_p10);
  const p50 = num(forecast?.band_p50);
  const p90 = num(forecast?.band_p90);
  if ([close, p10, p50, p90].some((value) => value === null)) return null;
  return {
    p10: close + p10,
    p50: close + p50,
    p90: close + p90,
  };
}

function setForecastChartUnavailable(message) {
  const canvas = $("forecastChartCanvas");
  const emptyEl = $("forecastChartEmpty");
  if (canvas) canvas.hidden = true;
  if (emptyEl) {
    emptyEl.hidden = false;
    emptyEl.textContent = message;
  }
  setText("forecastChartStatus", message);
  forecastChartHitTargets = [];
  forecastChartGeometry = null;
  state.chart.hover = null;
  renderForecastHoverCard(null);
  setText("forecastChartLastPrice", "--");
  setText("forecastChartChange", "--");
  setText("forecastChartHoverOhlc", message);
}

function renderForecastDecisionDetail(decision) {
  const target = $("forecastChartDetail");
  if (!target) return;
  if (!decision) {
    target.textContent =
      "表示範囲内にPIT適格な判断がありません。価格と最新ホライズン帯だけを表示しています。";
    return;
  }
  const action = ["long", "short"].includes(decision.direction)
    ? `運用判断 ${DIRECTION_LABEL[decision.direction] || decision.direction}`
    : `運用判断 ${DIRECTION_LABEL[decision.direction] || decision.direction || "中立"}`;
  const analysis = ["long", "short"].includes(decision.analysis_direction)
    ? `分析 ${DIRECTION_LABEL[decision.analysis_direction]}`
    : "分析方向なし";
  const moderateShadow = ["long", "short"].includes(decision.moderate_shadow_direction)
    ? `中強気shadow ${DIRECTION_LABEL[decision.moderate_shadow_direction]}`
    : decision.moderate_shadow_direction === "neutral"
      ? "中強気shadow 見送り"
      : "中強気shadow 記録なし";
  const score = num(decision.analysis_score);
  const levels = [
    `価格 ${chartNumber(decision.prediction_close, state.chart.symbol)}`,
    `stop ${chartNumber(decision.stop, state.chart.symbol)}`,
    `target1 ${chartNumber(decision.target1, state.chart.symbol)}`,
    `target2 ${chartNumber(decision.target2, state.chart.symbol)}`,
  ].join(" / ");
  const snapped = decision.marker_price_time
    ? ` / 表示位置 ${preciseDate(decision.marker_price_time)} (${markerTimeOffsetLabel(
        decision.marker_time_offset_ms,
      )})`
    : "";
  target.textContent = `判断時刻 ${preciseDate(decision.prediction_time)}${snapped} / ${
    state.chart.symbol
  } ${
    TIMEFRAME_LABEL[state.chart.timeframe]
  } — ${analysis}${score === null ? "" : ` score ${score >= 0 ? "+" : ""}${score.toFixed(3)}`} / ${
    moderateShadow
  }（本番不使用） / ${action}${
    decision.primary_gate ? ` / 見送りゲート ${decision.primary_gate}` : ""
  } / ${levels} / ${decision.outcome_status || "pending"}`;
}

function drawForecastChart(canvas, chart) {
  const prices = (chart?.prices || [])
    .map((row) => ({ ...row, timeMs: new Date(row.event_time).getTime() }))
    .filter((row) => Number.isFinite(row.timeMs) && num(row.close) !== null)
    .sort((a, b) => a.timeMs - b.timeMs);
  const quality = chart?.quality || {};
  if (!prices.length) {
    setForecastChartUnavailable("選択範囲の実測価格がありません");
    return;
  }
  if (quality.status !== "ok") {
    setForecastChartUnavailable("価格データがstaleのためチャートを停止しました");
    return;
  }

  canvas.hidden = false;
  const emptyEl = $("forecastChartEmpty");
  if (emptyEl) emptyEl.hidden = true;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const ratio = window.devicePixelRatio || 1;
  const cssWidth = Math.max(
    320,
    Math.floor(canvas.getBoundingClientRect().width || canvas.parentElement?.clientWidth || 900),
  );
  const expanded = $("forecastChartPanel")?.classList.contains("is-expanded");
  const cssHeight = expanded
    ? Math.max(460, window.innerHeight - 250)
    : window.innerWidth <= 720
      ? 340
      : 430;
  canvas.width = Math.round(cssWidth * ratio);
  canvas.height = Math.round(cssHeight * ratio);
  canvas.style.height = `${cssHeight}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const pad = { left: 14, right: 88, top: 22, bottom: 36 };
  const plotW = Math.max(10, cssWidth - pad.left - pad.right);
  const plotH = Math.max(10, cssHeight - pad.top - pad.bottom);
  const axisLeft = pad.left + plotW;
  const axisWidth = Math.max(1, cssWidth - axisLeft);
  const decisions = chartWindowDecisions(chart).map((row) =>
    snapDecisionToPrice(row, prices),
  );
  const forecast = primaryForecast();
  const band = forecastBandPrices(forecast);
  const forecastStart = forecast ? new Date(forecast.ts).getTime() : NaN;
  const forecastDue = forecast ? new Date(forecast.due_time).getTime() : NaN;
  const decisionTimes = decisions.flatMap((row) => [
    row.markerTimeMs,
    new Date(row.due_time).getTime(),
  ]);
  const times = [
    ...prices.map((row) => row.timeMs),
    ...decisionTimes.filter(Number.isFinite),
    ...(Number.isFinite(forecastStart) ? [forecastStart] : []),
    ...(Number.isFinite(forecastDue) ? [forecastDue] : []),
  ];
  let fullMinTime = Math.min(...times);
  let fullMaxTime = Math.max(...times);
  if (fullMinTime === fullMaxTime) fullMaxTime += 1;
  const fullSpan = fullMaxTime - fullMinTime;
  state.chart.zoom = chartClamp(state.chart.zoom, 1, 24);
  state.chart.pan = chartClamp(state.chart.pan, -chartPanLimit(), chartPanLimit());
  const viewSpan = fullSpan / state.chart.zoom;
  const fullCenter = (fullMinTime + fullMaxTime) / 2;
  const viewCenter = fullCenter + state.chart.pan * fullSpan;
  const minTime = viewCenter - viewSpan / 2;
  const maxTime = viewCenter + viewSpan / 2;
  const visiblePrices = prices.filter(
    (row) => row.timeMs >= minTime && row.timeMs <= maxTime,
  );
  const plottedPrices = visiblePrices.length ? visiblePrices : prices;
  const visibleDecisions = decisions.filter((row) => {
    const timeMs = row.markerTimeMs;
    return Number.isFinite(timeMs) && timeMs >= minTime && timeMs <= maxTime;
  });
  const latestVisibleDecision = visibleDecisions.at(-1) || null;
  const latestDecision = latestVisibleDecision || decisions.at(-1) || null;

  const levels = plottedPrices.flatMap((row) => [
    num(row.low) ?? num(row.close),
    num(row.high) ?? num(row.close),
    num(row.bid),
    num(row.ask),
  ]).filter((value) => value !== null);
  if (state.chart.layers.decisions) {
    visibleDecisions.forEach((row) => {
      const value = num(row.prediction_close);
      if (value !== null) levels.push(value);
    });
  }
  if (latestVisibleDecision && state.chart.layers.targets) {
    ["stop", "target1", "target2"].forEach((key) => {
      const value = num(latestVisibleDecision[key]);
      if (value !== null) levels.push(value);
    });
  }
  const bandInView =
    band &&
    Number.isFinite(forecastStart) &&
    Number.isFinite(forecastDue) &&
    forecastDue >= minTime &&
    forecastStart <= maxTime;
  if (bandInView && state.chart.layers.forecast) levels.push(band.p10, band.p50, band.p90);
  let minPrice = Math.min(...levels);
  let maxPrice = Math.max(...levels);
  const priceMargin = Math.max((maxPrice - minPrice) * 0.08, Math.abs(maxPrice) * 0.00005);
  minPrice -= priceMargin;
  maxPrice += priceMargin;
  const x = (timeMs) => pad.left + ((timeMs - minTime) / (maxTime - minTime)) * plotW;
  const y = (price) => pad.top + ((maxPrice - price) / (maxPrice - minPrice)) * plotH;
  forecastChartGeometry = {
    pad,
    plotW,
    plotH,
    axisLeft,
    axisWidth,
    minTime,
    maxTime,
    fullMinTime,
    fullMaxTime,
    minPrice,
    maxPrice,
    prices,
  };

  const line = cssVar("--line", "#3c3f37");
  const muted = cssVar("--muted", "#aaa79c");
  const text = cssVar("--text", "#f3f1e9");
  const cyan = cssVar("--cyan", "#66b7c9");
  const amber = cssVar("--amber", "#d6a64b");
  const green = cssVar("--green", "#5dc98c");
  const red = cssVar("--red", "#de6e64");
  const longColor = cssVar("--viz-1", "#3987e5");
  const shortColor = cssVar("--viz-2", "#d95926");

  ctx.fillStyle = "#191a17";
  ctx.fillRect(axisLeft, 0, axisWidth, cssHeight);
  ctx.strokeStyle = line;
  ctx.beginPath();
  ctx.moveTo(axisLeft + 0.5, 0);
  ctx.lineTo(axisLeft + 0.5, cssHeight);
  ctx.stroke();

  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;
  for (let index = 0; index <= 4; index += 1) {
    const fraction = index / 4;
    const price = maxPrice - (maxPrice - minPrice) * fraction;
    const py = pad.top + plotH * fraction;
    ctx.strokeStyle = line;
    ctx.globalAlpha = 0.45;
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(pad.left + plotW, py);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = muted;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(chartNumber(price, state.chart.symbol), axisLeft + 8, py);
  }
  for (let index = 0; index <= 5; index += 1) {
    const timeMs = minTime + ((maxTime - minTime) * index) / 5;
    const px = x(timeMs);
    ctx.strokeStyle = line;
    ctx.globalAlpha = 0.25;
    ctx.beginPath();
    ctx.moveTo(px, pad.top);
    ctx.lineTo(px, pad.top + plotH);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = muted;
    ctx.textAlign = index === 0 ? "left" : index === 5 ? "right" : "center";
    ctx.textBaseline = "top";
    ctx.fillText(shortDate(new Date(timeMs).toISOString()), px, pad.top + plotH + 8);
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(pad.left, pad.top, plotW, plotH);
  ctx.clip();

  if (bandInView && state.chart.layers.forecast) {
    const left = x(forecastStart);
    const right = x(forecastDue);
    ctx.fillStyle = "rgba(57, 135, 229, 0.15)";
    ctx.strokeStyle = "rgba(57, 135, 229, 0.65)";
    ctx.fillRect(left, y(band.p90), Math.max(1, right - left), y(band.p10) - y(band.p90));
    ctx.strokeRect(left, y(band.p90), Math.max(1, right - left), y(band.p10) - y(band.p90));
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(left, y(band.p50));
    ctx.lineTo(right, y(band.p50));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = text;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(
      `${forecast.horizon} p10–p90${forecast.calibrated ? "" : "（未較正）"}`,
      left + 5,
      y(band.p90) - 5,
    );
  }

  const allQuotes = plottedPrices.every(
    (row) => num(row.bid) !== null && num(row.ask) !== null,
  );
  if (state.chart.layers.spread && allQuotes) {
    ctx.fillStyle = "rgba(102, 183, 201, 0.10)";
    ctx.beginPath();
    plottedPrices.forEach((row, index) => {
      const px = x(row.timeMs);
      const py = y(num(row.ask));
      if (index === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    [...plottedPrices]
      .reverse()
      .forEach((row) => ctx.lineTo(x(row.timeMs), y(num(row.bid))));
    ctx.closePath();
    ctx.fill();
  }

  if (chart.price_mode === "completed_candles") {
    const candleWidth = Math.max(2, Math.min(11, (plotW / plottedPrices.length) * 0.65));
    plottedPrices.forEach((row) => {
      const open = num(row.open);
      const high = num(row.high);
      const low = num(row.low);
      const close = num(row.close);
      if ([open, high, low, close].some((value) => value === null)) return;
      const color = close >= open ? green : red;
      const px = x(row.timeMs);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(px, y(high));
      ctx.lineTo(px, y(low));
      ctx.stroke();
      ctx.fillRect(
        px - candleWidth / 2,
        Math.min(y(open), y(close)),
        candleWidth,
        Math.max(1, Math.abs(y(close) - y(open))),
      );
    });
  } else {
    ctx.strokeStyle = cyan;
    ctx.lineWidth = 2;
    ctx.beginPath();
    plottedPrices.forEach((row, index) => {
      const px = x(row.timeMs);
      const py = y(num(row.close));
      if (index === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  forecastChartHitTargets = [];
  if (latestVisibleDecision && state.chart.layers.targets) {
    const startX = x(latestVisibleDecision.markerTimeMs);
    const endX = x(new Date(latestVisibleDecision.due_time).getTime());
    ["stop", "target1", "target2"].forEach((key) => {
      const value = num(latestVisibleDecision[key]);
      if (value === null) return;
      ctx.strokeStyle = key === "stop" ? red : amber;
      ctx.globalAlpha = 0.75;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(startX, y(value));
      ctx.lineTo(endX, y(value));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      ctx.fillStyle = key === "stop" ? red : amber;
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(
        `${key} ${chartNumber(value, state.chart.symbol)}`,
        pad.left + plotW - 8,
        y(value) - 2,
      );
    });
  }

  const decisionMarkerBuckets = new Map();
  const markerSpacing = state.chart.zoom < 2 ? 20 : state.chart.zoom < 5 ? 12 : 7;
  visibleDecisions.forEach((decision) => {
    const timeMs = decision.markerTimeMs;
    if (!Number.isFinite(timeMs)) return;
    const bucket = Math.floor((x(timeMs) - pad.left) / markerSpacing);
    decisionMarkerBuckets.set(bucket, decision);
  });
  const plottedDecisions = [...decisionMarkerBuckets.values()];
  if (state.chart.layers.decisions) plottedDecisions.forEach((decision) => {
    const timeMs = decision.markerTimeMs;
    const price = num(decision.prediction_close);
    if (!Number.isFinite(timeMs) || price === null) return;
    const px = x(timeMs);
    const py = y(price);
    const actionDirection = ["long", "short"].includes(decision.direction)
      ? decision.direction
      : null;
    const analysisDirection = ["long", "short"].includes(decision.analysis_direction)
      ? decision.analysis_direction
      : null;
    const moderateDirection = ["long", "short"].includes(
      decision.moderate_shadow_direction,
    )
      ? decision.moderate_shadow_direction
      : null;
    const direction = actionDirection || moderateDirection || analysisDirection;
    const color = direction === "short" ? shortColor : direction === "long" ? longColor : muted;
    const up = direction !== "short";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    if (up) {
      ctx.moveTo(px, py - 8);
      ctx.lineTo(px - 6, py + 5);
      ctx.lineTo(px + 6, py + 5);
    } else {
      ctx.moveTo(px, py + 8);
      ctx.lineTo(px - 6, py - 5);
      ctx.lineTo(px + 6, py - 5);
    }
    ctx.closePath();
    if (actionDirection) ctx.fill();
    else if (moderateDirection) {
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(px, py, 2.5, 0, Math.PI * 2);
      ctx.fill();
    } else if (analysisDirection) ctx.stroke();
    else {
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.lineWidth = 1;
    forecastChartHitTargets.push({ x: px, y: py, decision });
  });

  const current = plottedPrices.at(-1);
  const currentY = y(num(current.close));
  ctx.strokeStyle = cyan;
  ctx.globalAlpha = 0.5;
  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(pad.left, currentY);
  ctx.lineTo(pad.left + plotW, currentY);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.fillStyle = cyan;
  ctx.beginPath();
  ctx.arc(x(current.timeMs), y(num(current.close)), 3.5, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  const currentLabel = chartNumber(current.close, state.chart.symbol);
  ctx.font = "600 11px system-ui, sans-serif";
  ctx.fillStyle = cyan;
  ctx.fillRect(axisLeft, currentY - 9, axisWidth, 18);
  ctx.fillStyle = "#071015";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(currentLabel, axisLeft + axisWidth / 2, currentY);

  const hover = state.chart.hover;
  if (
    hover &&
    hover.x >= pad.left &&
    hover.x <= pad.left + plotW &&
    hover.y >= pad.top &&
    hover.y <= pad.top + plotH
  ) {
    const hoverTime = minTime + ((hover.x - pad.left) / plotW) * (maxTime - minTime);
    const hoverPrice = maxPrice - ((hover.y - pad.top) / plotH) * (maxPrice - minPrice);
    ctx.strokeStyle = muted;
    ctx.globalAlpha = 0.8;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(hover.x, pad.top);
    ctx.lineTo(hover.x, pad.top + plotH);
    ctx.moveTo(pad.left, hover.y);
    ctx.lineTo(pad.left + plotW, hover.y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    ctx.font = "11px system-ui, sans-serif";
    const timeLabel = shortDate(new Date(hoverTime).toISOString());
    const timeWidth = Math.max(92, ctx.measureText(timeLabel).width + 14);
    const timeX = chartClamp(hover.x - timeWidth / 2, pad.left, pad.left + plotW - timeWidth);
    ctx.fillStyle = cssVar("--surface-strong", "#252821");
    ctx.fillRect(timeX, pad.top + plotH, timeWidth, 22);
    ctx.fillStyle = text;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(timeLabel, timeX + timeWidth / 2, pad.top + plotH + 11);

    ctx.fillStyle = cssVar("--surface-strong", "#252821");
    ctx.fillRect(axisLeft, hover.y - 9, axisWidth, 18);
    ctx.fillStyle = text;
    ctx.fillText(
      chartNumber(hoverPrice, state.chart.symbol),
      axisLeft + axisWidth / 2,
      hover.y,
    );
  }

  renderForecastQuote(prices, hover?.row || current);
  setText(
    "forecastChartStatus",
    `${state.chart.symbol} ${TIMEFRAME_LABEL[state.chart.timeframe]} / ${
      plottedPrices.length
    }/${prices.length}点 / 表示 ${Math.round(state.chart.zoom * 100)}% / ${shortDate(
      current.event_time,
    )}`,
  );
  renderForecastDecisionDetail(latestDecision);
}

function renderForecastChart(chart) {
  lastForecastChart = chart;
  renderForecastChartQuality(chart);
  const canvas = $("forecastChartCanvas");
  if (canvas) drawForecastChart(canvas, chart);
}

async function loadForecastChart({ force = false } = {}) {
  const selection = forecastChartSelection();
  const key = forecastChartKey(selection);
  if (forecastChartRequest && forecastChartRequest.key !== key) {
    forecastChartRequest.controller.abort();
  }
  const cached = cachedForecastChart(key);
  const now = Date.now();
  if (cached) {
    state.chart.lastLoadedAt = cached.loadedAt;
    renderForecastChart(cached.payload);
    if (!force && now - cached.loadedAt < FORECAST_CHART_CACHE_TTL_MS) {
      if (!forecastChartRequest || forecastChartRequest.key !== key) {
        setForecastChartBusy(false);
      }
      return cached.payload;
    }
  }

  if (forecastChartRequest?.key === key) {
    return forecastChartRequest.promise;
  }
  if (forecastChartRequest) {
    forecastChartRequest.controller.abort();
  }

  const requestId = ++forecastChartRequestSequence;
  const controller = new AbortController();
  if (!cached) {
    lastForecastChart = null;
    setForecastChartUnavailable("価格データを読み込み中");
  }
  setForecastChartBusy(true);
  const parameters = new URLSearchParams({
    symbol: selection.symbol,
    timeframe: selection.timeframe,
    range: selection.range,
  });

  const promise = (async () => {
    try {
      const response = await fetch(`/api/chart?${parameters}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (
        requestId !== forecastChartRequestSequence ||
        key !== forecastChartKey()
      ) {
        return null;
      }
      const loadedAt = Date.now();
      cacheForecastChart(key, payload, loadedAt);
      state.chart.lastLoadedAt = loadedAt;
      renderForecastChart(payload);
      return payload;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return null;
      if (
        requestId !== forecastChartRequestSequence ||
        key !== forecastChartKey()
      ) {
        return null;
      }
      const quality = $("forecastChartQuality");
      if (cached) {
        console.warn(error);
        if (quality) {
          quality.appendChild(qualityChip("更新失敗・キャッシュ表示", "warn"));
        }
      } else {
        console.error(error);
        setForecastChartUnavailable(`価格チャートを表示できません: ${error.message}`);
      }
      if (!cached && quality) {
        quality.replaceChildren(qualityChip("read-only価格API未接続", "bad"));
      }
      return null;
    } finally {
      if (forecastChartRequest?.requestId === requestId) {
        forecastChartRequest = null;
      }
      if (key === forecastChartKey()) setForecastChartBusy(false);
    }
  })();
  forecastChartRequest = { key, requestId, controller, promise };
  return promise;
}

function bindForecastChartControls() {
  const toggleExpanded = (force) => {
    const panel = $("forecastChartPanel");
    const button = $("forecastChartFullscreen");
    if (!panel) return;
    const expanded =
      typeof force === "boolean" ? force : !panel.classList.contains("is-expanded");
    panel.classList.toggle("is-expanded", expanded);
    document.body.classList.toggle("chart-expanded", expanded);
    if (button) {
      button.textContent = expanded ? "通常表示" : "拡大表示";
      button.setAttribute("aria-pressed", String(expanded));
    }
    scheduleForecastChartRedraw();
    if (expanded) {
      $("forecastChartCanvas")?.focus({ preventScroll: true });
      panel.scrollTop = 0;
    }
  };
  const prepareReload = () => {
    state.chart.lastLoadedAt = 0;
    resetForecastViewport({ redraw: false });
    saveChartSelection();
    syncChartControls();
    loadForecastChart();
  };
  const symbol = $("forecastChartSymbol");
  if (symbol) {
    symbol.addEventListener("change", () => {
      state.chart.symbol = symbol.value;
      prepareReload();
    });
  }
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chart.timeframe = button.dataset.chartTimeframe;
      prepareReload();
    });
  });
  document.querySelectorAll("[data-chart-range]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chart.range = button.dataset.chartRange;
      prepareReload();
    });
  });
  document.querySelectorAll("[data-chart-layer]").forEach((button) => {
    button.addEventListener("click", () => {
      const layer = button.dataset.chartLayer;
      if (!(layer in state.chart.layers)) return;
      state.chart.layers[layer] = !state.chart.layers[layer];
      saveChartSelection();
      syncChartControls();
      scheduleForecastChartRedraw();
    });
  });
  $("forecastChartZoomOut")?.addEventListener("click", () => {
    setForecastChartZoom(state.chart.zoom / 1.35);
  });
  $("forecastChartZoomIn")?.addEventListener("click", () => {
    setForecastChartZoom(state.chart.zoom * 1.35);
  });
  $("forecastChartReset")?.addEventListener("click", () => resetForecastViewport());
  $("forecastChartFullscreen")?.addEventListener("click", () => toggleExpanded());

  const showDecisionTooltip = (event, px, py) => {
    const hit = forecastChartHitTargets.find(
      (target) => Math.hypot(target.x - px, target.y - py) <= 10,
    );
    if (!hit) {
      hideTooltip();
      return null;
    }
    const row = hit.decision;
    showTooltip(event, `${state.chart.symbol} 判断マーカー`, [
      {
        label: "元の判断時刻",
        value: preciseDate(row.prediction_time),
      },
      {
        label: "表示価格時刻",
        value: row.marker_price_time
          ? `${preciseDate(row.marker_price_time)} (${markerTimeOffsetLabel(
              row.marker_time_offset_ms,
            )})`
          : "--",
        muted: true,
      },
      {
        label: "分析",
        value: DIRECTION_LABEL[row.analysis_direction] || row.analysis_direction || "--",
        color: cssVar("--viz-1", "#3987e5"),
      },
      {
        label: "中強気shadow（本番不使用）",
        value:
          DIRECTION_LABEL[row.moderate_shadow_direction] ||
          (row.moderate_shadow_direction === "neutral" ? "見送り" : null) ||
          "記録なし",
        color: cssVar("--viz-1", "#3987e5"),
      },
      {
        label: "運用判断",
        value: DIRECTION_LABEL[row.direction] || row.direction || "--",
      },
      {
        label: "価格",
        value: chartNumber(row.prediction_close, state.chart.symbol),
      },
      {
        label: "ゲート",
        value: row.primary_gate || "なし",
        muted: !row.primary_gate,
      },
    ]);
    return hit;
  };

  const canvas = $("forecastChartCanvas");
  if (canvas) {
    const pointerPosition = (event) => {
      const rect = canvas.getBoundingClientRect();
      return {
        px: event.clientX - rect.left,
        py: event.clientY - rect.top,
      };
    };
    canvas.addEventListener(
      "wheel",
      (event) => {
        if (!forecastChartGeometry) return;
        event.preventDefault();
        const { px } = pointerPosition(event);
        const ratio =
          (px - forecastChartGeometry.pad.left) / forecastChartGeometry.plotW;
        state.chart.hover = null;
        renderForecastHoverCard(null);
        queueForecastWheelZoom(event, ratio);
      },
      { passive: false },
    );
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !forecastChartGeometry) return;
      canvas.focus();
      const { px } = pointerPosition(event);
      forecastChartDrag = {
        pointerId: event.pointerId,
        startX: px,
        startPan: state.chart.pan,
        moved: false,
      };
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("is-dragging");
    });
    canvas.addEventListener("pointermove", (event) => {
      const geometry = forecastChartGeometry;
      if (!geometry) return;
      const { px, py } = pointerPosition(event);
      if (forecastChartDrag?.pointerId === event.pointerId) {
        const dx = px - forecastChartDrag.startX;
        if (Math.abs(dx) > 3) forecastChartDrag.moved = true;
        state.chart.pan = chartClamp(
          forecastChartDrag.startPan - dx / (geometry.plotW * state.chart.zoom),
          -chartPanLimit(),
          chartPanLimit(),
        );
        state.chart.hover = null;
        renderForecastHoverCard(null);
        hideTooltip();
        scheduleForecastChartRedraw();
        return;
      }
      const inside =
        px >= geometry.pad.left &&
        px <= geometry.pad.left + geometry.plotW &&
        py >= geometry.pad.top &&
        py <= geometry.pad.top + geometry.plotH;
      if (!inside) {
        state.chart.hover = null;
        renderForecastHoverCard(null);
        hideTooltip();
        scheduleForecastChartRedraw();
        return;
      }
      const timeMs =
        geometry.minTime +
        ((px - geometry.pad.left) / geometry.plotW) *
          (geometry.maxTime - geometry.minTime);
      const row = nearestForecastPrice(geometry.prices, timeMs);
      state.chart.hover = { x: px, y: py, row };
      renderForecastHoverCard(row);
      renderForecastQuote(geometry.prices, row);
      showDecisionTooltip(event, px, py);
      scheduleForecastChartRedraw();
    });
    const endDrag = (event) => {
      if (forecastChartDrag?.pointerId !== event.pointerId) return;
      const drag = forecastChartDrag;
      const { px, py } = pointerPosition(event);
      if (canvas.hasPointerCapture(event.pointerId)) {
        canvas.releasePointerCapture(event.pointerId);
      }
      forecastChartDrag = null;
      canvas.classList.remove("is-dragging");
      if (!drag.moved) {
        const hit = forecastChartHitTargets.find(
          (target) => Math.hypot(target.x - px, target.y - py) <= 12,
        );
        if (hit) renderForecastDecisionDetail(hit.decision);
      }
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("pointerleave", () => {
      if (forecastChartDrag) return;
      state.chart.hover = null;
      renderForecastHoverCard(null);
      if (forecastChartGeometry) {
        renderForecastQuote(forecastChartGeometry.prices);
      }
      hideTooltip();
      scheduleForecastChartRedraw();
    });
    canvas.addEventListener("dblclick", () => resetForecastViewport());
    canvas.addEventListener("keydown", (event) => {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setForecastChartZoom(state.chart.zoom * 1.35);
      } else if (event.key === "-") {
        event.preventDefault();
        setForecastChartZoom(state.chart.zoom / 1.35);
      } else if (event.key === "0") {
        event.preventDefault();
        resetForecastViewport();
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const direction = event.key === "ArrowLeft" ? -1 : 1;
        state.chart.pan = chartClamp(
          state.chart.pan + direction * (0.08 / state.chart.zoom),
          -chartPanLimit(),
          chartPanLimit(),
        );
        scheduleForecastChartRedraw();
      } else if (event.key.toLowerCase() === "f") {
        event.preventDefault();
        toggleExpanded();
      } else if (event.key === "Escape") {
        toggleExpanded(false);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      $("forecastChartPanel")?.classList.contains("is-expanded")
    ) {
      toggleExpanded(false);
    }
  });
}

function renderCurve(data) {
  const curve = Array.isArray(data.evaluation?.curve) ? data.evaluation.curve : [];
  lastCurve = curve;
  const canvas = $("curveCanvas");
  const emptyEl = $("curveEmpty");
  const summaryEl = $("curveSummary");
  if (!canvas) return;

  if (!curve.length) {
    canvas.hidden = true;
    if (emptyEl) emptyEl.hidden = false;
    if (summaryEl) summaryEl.textContent = "採点待ち";
    return;
  }
  canvas.hidden = false;
  if (emptyEl) emptyEl.hidden = true;

  const last = curve[curve.length - 1];
  if (summaryEl) {
    let summary = `採点 ${last.scored}件 / 累積的中率 ${pct(last.hit_rate)}`;
    if ((last.move_atr_points || 0) > 0) {
      const moveAtr = Number(last.cum_move_atr);
      summary += ` / ATR換算値幅 ${(moveAtr >= 0 ? "+" : "") + moveAtr.toFixed(2)}(${last.move_atr_points}件)`;
    }
    summaryEl.textContent = summary;
  }
  drawCurve(canvas, curve);
}

function drawCurve(canvas, curve) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  // 高解像度対応: CSSサイズ×devicePixelRatio の物理pxで描く
  const ratio = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 640;
  const cssHeight = 240;
  canvas.width = Math.round(cssWidth * ratio);
  canvas.height = Math.round(cssHeight * ratio);
  canvas.style.height = `${cssHeight}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const padL = 44;
  const padR = 44;
  const padT = 16;
  const padB = 28;
  const plotW = cssWidth - padL - padR;
  const plotH = cssHeight - padT - padB;

  const line = cssVar("--line", "#3c3f37");
  const muted = cssVar("--muted", "#aaa79c");
  const cyan = cssVar("--cyan", "#66b7c9");
  const green = cssVar("--green", "#5dc98c");
  const amber = cssVar("--amber", "#d6a64b");
  const text = cssVar("--text", "#f3f1e9");

  const n = curve.length;
  const maxScored = Math.max(1, ...curve.map((p) => p.scored));
  const xFor = (i) => padL + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const yRate = (rate) => padT + plotH * (1 - rate); // 0..1 を上下反転
  const yScored = (s) => padT + plotH * (1 - s / maxScored);

  // グリッド(0/25/50/75/100%)と左軸ラベル
  ctx.strokeStyle = line;
  ctx.fillStyle = muted;
  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.textBaseline = "middle";
  [0, 0.25, 0.5, 0.75, 1].forEach((r) => {
    const y = yRate(r);
    ctx.globalAlpha = r === 0.5 ? 0.55 : 0.25;
    ctx.beginPath();
    if (r === 0.5) ctx.setLineDash([4, 4]);
    else ctx.setLineDash([]);
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
    ctx.textAlign = "right";
    ctx.fillText(`${Math.round(r * 100)}%`, padL - 8, y);
  });

  // 累積採点数の棒(薄いシアン)。右軸スケール
  const barW = Math.max(2, Math.min(18, (plotW / n) * 0.5));
  ctx.fillStyle = cyan;
  ctx.globalAlpha = 0.22;
  curve.forEach((p, i) => {
    const x = xFor(i);
    const y = yScored(p.scored);
    ctx.fillRect(x - barW / 2, y, barW, padT + plotH - y);
  });
  ctx.globalAlpha = 1;
  // 右軸(採点数)の上端ラベル
  ctx.fillStyle = cyan;
  ctx.textAlign = "left";
  ctx.fillText(`${maxScored}件`, padL + plotW + 8, yScored(maxScored));
  ctx.fillStyle = muted;
  ctx.fillText("0", padL + plotW + 8, padT + plotH);

  // 累積的中率の折れ線(緑)
  ctx.strokeStyle = green;
  ctx.lineWidth = 2;
  ctx.beginPath();
  curve.forEach((p, i) => {
    const x = xFor(i);
    const y = yRate(p.hit_rate);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  // 各採点点のマーカー
  ctx.fillStyle = green;
  curve.forEach((p, i) => {
    ctx.beginPath();
    ctx.arc(xFor(i), yRate(p.hit_rate), n > 40 ? 1.5 : 3, 0, Math.PI * 2);
    ctx.fill();
  });

  // legacy終値照合の累積ATR換算値幅(琥珀)。正準gross/net Rではない。
  const hasMoveAtr = curve.some((p) => (p.move_atr_points || 0) > 0);
  if (hasMoveAtr) {
    const moveVals = curve.map((p) => num(p.cum_move_atr) ?? 0);
    const maxAbs = Math.max(0.5, ...moveVals.map((v) => Math.abs(v)));
    const yMove = (v) => padT + plotH * (1 - (v / maxAbs + 1) / 2);
    // 0の基準線(琥珀の点線)
    ctx.strokeStyle = amber;
    ctx.globalAlpha = 0.35;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, yMove(0));
    ctx.lineTo(padL + plotW, yMove(0));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    // 累積ATR換算値幅の線
    ctx.strokeStyle = amber;
    ctx.lineWidth = 2;
    ctx.beginPath();
    curve.forEach((p, i) => {
      const y = yMove(num(p.cum_move_atr) ?? 0);
      if (i === 0) ctx.moveTo(xFor(i), y);
      else ctx.lineTo(xFor(i), y);
    });
    ctx.stroke();
    // ATR換算値幅のレンジラベル
    ctx.fillStyle = amber;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(`ATR換算 +${maxAbs.toFixed(1)}`, padL + 4, padT + 2);
    ctx.textBaseline = "bottom";
    ctx.fillText(`-${maxAbs.toFixed(1)}`, padL + 4, padT + plotH - 2);
  }

  // 最新値のラベル
  const lp = curve[curve.length - 1];
  ctx.fillStyle = text;
  ctx.textAlign = "right";
  ctx.textBaseline = "bottom";
  ctx.font = "600 12px system-ui, sans-serif";
  ctx.fillText(pct(lp.hit_rate), padL + plotW, yRate(lp.hit_rate) - 6);
  if (hasMoveAtr) {
    const moveVals = curve.map((p) => num(p.cum_move_atr) ?? 0);
    const maxAbs = Math.max(0.5, ...moveVals.map((v) => Math.abs(v)));
    const yMove = (v) => padT + plotH * (1 - (v / maxAbs + 1) / 2);
    ctx.fillStyle = amber;
    ctx.fillText(
      `${(lp.cum_move_atr >= 0 ? "+" : "") + Number(lp.cum_move_atr).toFixed(2)} ATR`,
      padL + plotW,
      yMove(num(lp.cum_move_atr) ?? 0) - 6,
    );
  }
}

function renderMetrics(data) {
  setText("journalTotal", String(data.journal.total));
  setText(
    "journalLatest",
    `融合 ${data.journal.fusion_total || 0} / 時間足 ${data.journal.timeframe_total || 0}`,
  );
  setText("evaluatedTotal", String(data.learning.evaluated || 0));
  setText(
    "pendingTotal",
    `採点待ち ${data.evaluation.pending || 0} / 小動き ${data.learning.flat || 0}`,
  );
  setText("hitRate", pct(data.learning.hit_rate));
  setText("hitCount", `${data.learning.hits || 0} / ${data.learning.evaluated || 0}`);
  setText("mlStatus", data.ml.has_model ? (data.ml.usable ? "有効" : "無効") : "未学習");
  const training = data.ml.training || {};
  setText(
    "mlRows",
    data.ml.has_model
      ? `学習${data.ml.n_train || 0} / 検証${data.ml.n_valid || 0}`
      : `GBDT PIT適格 ${training.eligible_after_thinning || 0} / ${training.minimum_required || 150}`,
  );
  const tfEvaluated = Number(data.tf_learning?.evaluated || 0);
  const tfHits = Number(data.tf_learning?.hits || 0);
  const decisionOverall = data.decision_monitor?.overall || {};
  setText(
    "scopeFusion",
    `融合24h方向: ${data.learning.hits || 0}/${data.learning.evaluated || 0} (${pct(data.learning.hit_rate)})`,
  );
  setText(
    "scopeTimeframe",
    `時間足別方向: ${tfHits}/${tfEvaluated} (${pct(tfEvaluated ? tfHits / tfEvaluated : null)})`,
  );
  setText(
    "scopeExpectancy",
    `売買期待R: n=${decisionOverall.evaluated || 0} / ${signedR(num(decisionOverall.expectancy_r))} / PF ${
      num(decisionOverall.profit_factor_r) === null
        ? "--"
        : Number(decisionOverall.profit_factor_r).toFixed(2)
    }`,
  );
}

function renderWeights(data) {
  const lw = data.learning;
  const sourceLabel = lw.source_label_ja ? `${lw.source_label_ja} / ` : "";
  setText("learningGenerated", `${sourceLabel}${shortDate(lw.generated_at)}`);
  setText("techWeight", pct(lw.tech_weight));
  setText("newsWeight", pct(lw.news_weight));
  setBar("techWeightBar", lw.tech_weight);
  setBar("newsWeightBar", lw.news_weight);
  setText("techHit", pct(lw.tech_hit_rate));
  setText("newsHit", pct(lw.news_hit_rate));
  const brier = num(lw.conviction_brier);
  const base = num(lw.conviction_brier_base);
  setText("brierScore", brier === null ? "--" : `${brier.toFixed(3)}${base !== null ? ` / ${base.toFixed(3)}` : ""}`);
}

function renderStages(data) {
  const target = $("stageList");
  target.replaceChildren();
  for (const member of ["macro", "ml"]) {
    const div = document.createElement("div");
    div.className = "stage";
    const label = document.createElement("strong");
    label.textContent = member === "macro" ? "マクロ委員" : "ML委員";
    const stage = document.createElement("span");
    stage.textContent = data.promotion.stages[member] || "shadow";
    div.append(label, stage);
    target.appendChild(div);
  }
  setText("promotionUpdated", shortDate(data.promotion.updated_at));

  const history = $("promotionHistory");
  history.replaceChildren();
  const rows = data.promotion.history || [];
  if (!rows.length) {
    history.appendChild(empty("昇格・降格の履歴はまだありません"));
    return;
  }
  rows.slice(-4).reverse().forEach((row) => {
    const div = document.createElement("div");
    div.className = "timeline-item";
    div.textContent = `${shortDate(row.ts)}  ${row.member}: ${row.from} → ${row.to}`;
    history.appendChild(div);
  });
}

// 融合1判断にペア別採点があればそれを使い、無ければ時間足別の symbol_stats を
// ペア横断で合算する(採点実体は時間足別なので空表示にならないようにする)。
function symbolRowsFor(data) {
  const fusion = (data.learning?.symbols || []).filter((s) => Number(s.evaluated || 0) > 0);
  if (fusion.length) return { rows: fusion, hasFactor: true };
  const agg = new Map();
  (data.tf_learning?.timeframes || []).forEach((tf) => {
    (tf.symbols || []).forEach((s) => {
      const cur = agg.get(s.symbol) || { symbol: s.symbol, evaluated: 0, hits: 0 };
      cur.evaluated += Number(s.evaluated || 0);
      cur.hits += Number(s.hits || 0);
      agg.set(s.symbol, cur);
    });
  });
  const rows = [...agg.values()]
    .filter((s) => s.evaluated > 0)
    .map((s) => ({ ...s, hit_rate: s.evaluated ? s.hits / s.evaluated : null }))
    .sort((a, b) => b.evaluated - a.evaluated);
  return { rows, hasFactor: false };
}

function renderSymbolBars(data) {
  const target = $("symbolBars");
  target.replaceChildren();
  const { rows, hasFactor } = symbolRowsFor(data);
  if (!rows.length) {
    target.appendChild(empty("ペア別に採点できる判断がまだありません"));
    return;
  }
  rows.forEach((row) => {
    const factor = hasFactor ? ` 係数×${Number(row.factor || 1).toFixed(2)}` : "";
    target.appendChild(
      barRow(
        row.symbol,
        row.hit_rate || 0,
        `${pct(row.hit_rate)} (${row.hits}/${row.evaluated})${factor}`,
        row.hit_rate >= 0.5 ? "green" : "red",
      ),
    );
  });
}

const TIMEFRAME_ORDER = ["15m", "1h", "4h", "1d"];
const TIMEFRAME_LABEL = { "15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足" };
const TIMEFRAME_HORIZON = { "15m": "15分後", "1h": "1時間後", "4h": "4時間後", "1d": "24時間後" };

function renderTimeframeBars(data) {
  const target = $("timeframeBars");
  if (!target) return;
  target.replaceChildren();
  const learnedRows = data.tf_learning?.timeframes || [];
  if (learnedRows.length) {
    learnedRows.forEach((row) => {
      const evaluated = Number(row.evaluated || 0);
      const hits = Number(row.hits || 0);
      const rate = evaluated ? hits / evaluated : null;
      const tf = row.timeframe || "";
      const label = TIMEFRAME_LABEL[tf] || tf;
      const horizon = TIMEFRAME_HORIZON[tf] || "";
      target.appendChild(
        barRow(
          `${label}${horizon ? ` (${horizon})` : ""}`,
          rate || 0,
          `${pct(rate)} (${hits}/${evaluated}) / 技術${pct(row.tech_weight)} ニュース${pct(row.news_weight)}`,
          rate >= 0.5 ? "green" : "red",
        ),
      );
    });
    return;
  }
  const byTf = data.evaluation?.by_timeframe || {};
  const timeframes = Object.keys(byTf).sort(
    (a, b) => TIMEFRAME_ORDER.indexOf(a) - TIMEFRAME_ORDER.indexOf(b),
  );
  if (!timeframes.length) {
    target.appendChild(
      empty("時間足別に採点できる判断がまだありません(--per-timeframe で記録)"),
    );
    return;
  }
  timeframes.forEach((tf) => {
    const stat = byTf[tf] || {};
    const evaluated = Number(stat.evaluated || 0);
    const hits = Number(stat.hits || 0);
    const rate = evaluated ? hits / evaluated : null;
    const label = TIMEFRAME_LABEL[tf] || tf;
    const horizon = TIMEFRAME_HORIZON[tf] || "";
    target.appendChild(
      barRow(
        `${label}${horizon ? ` (${horizon})` : ""}`,
        rate || 0,
        `${pct(rate)} (${hits}/${evaluated})`,
        rate >= 0.5 ? "green" : "red",
      ),
    );
  });
}

const HORIZON_ORDER = ["5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h", "3d"];

function renderHorizons(data) {
  const horizon = data.horizon || {};
  setText(
    "horizonUpdated",
    `${shortDate(horizon.generated_at)} / ${horizon.contract || "horizon-pit-v1"}`,
  );
  const matrix = $("horizonMatrix");
  matrix.replaceChildren();
  const latest = horizon.latest || [];
  const profiles = horizon.profiles || [];
  markPanelEmpty(matrix, !latest.length && !profiles.length);
  const symbols = [...new Set(latest.map((row) => row.symbol))].sort();
  if (!symbols.length) {
    matrix.appendChild(empty("ホライズン予測はまだ記録されていません"));
  } else {
    const corner = document.createElement("strong");
    corner.textContent = "pair";
    matrix.appendChild(corner);
    HORIZON_ORDER.forEach((label) => {
      const head = document.createElement("strong");
      head.textContent = label === "5m" ? "5m shadow" : label;
      matrix.appendChild(head);
    });
    symbols.forEach((symbol) => {
      const label = document.createElement("strong");
      label.textContent = symbol;
      matrix.appendChild(label);
      HORIZON_ORDER.forEach((name) => {
        const row = latest.find((item) => item.symbol === symbol && item.horizon === name);
        const cell = document.createElement("div");
        cell.className = `horizon-cell ${row?.direction || "missing"}`;
        cell.textContent = row
          ? `${row.direction} ${row.conviction ?? 0}${row.calibrated ? " ✓" : " *"}`
          : "--";
        cell.title = row
          ? `up ${pct(row.p_up)} / down ${pct(row.p_down)} / flat ${pct(row.p_flat)}`
          : "未記録";
        matrix.appendChild(cell);
      });
    });
  }

  const metrics = $("horizonMetrics");
  metrics.replaceChildren();
  if (!profiles.length) {
    metrics.appendChild(empty(`満期採点待ち ${horizon.immature || 0} / 未解決 ${horizon.unresolved || 0}`));
    return;
  }
  profiles.forEach((row) => {
    const remaining = row.permanent_shadow
      ? "恒久shadow"
      : `昇格まで ${row.remaining_n ?? "--"}件`;
    metrics.appendChild(
      tradeItem(
        `${row.symbol} ${row.horizon} / ${row.stage}`,
        `方向 ${pct(row.hit_rate)} / Brier ${num(row.mean_brier)?.toFixed(3) || "--"} / ` +
          `logloss ${num(row.mean_log_loss)?.toFixed(3) || "--"} / 帯 ${pct(row.band_coverage)} / ` +
          `純R ${signedR(num(row.mean_net_r))} / n=${row.n_scored} / ${remaining}`,
        row.stage === "adopted" ? "ok" : "",
      ),
    );
  });
}

const OUTCOME_STYLE = {
  hit: { badge: "✅ 的中", className: "hit" },
  miss: { badge: "❌ 外れ", className: "miss" },
  flat: { badge: "○ 小動き", className: "flat" },
  pending: { badge: "⏳ 未採点", className: "pending" },
  neutral: { badge: "○ 中立", className: "neutral" },
  standby: { badge: "📊 分析稼働", className: "standby" },
  closed: { badge: "💤 休場", className: "closed" },
};
const DIRECTION_LABEL = {
  long: "上昇予想",
  short: "下落予想",
  neutral: "中立判断",
  standby: "売買見送り",
  closed: "市場休場",
};
const ANALYSIS_OUTCOME_LABEL = {
  hit: "的中",
  miss: "外れ",
  flat: "小動き",
  pending: "未採点",
};

// 選択中の時間足タブ("all" or "15m"/"1h"/"4h"/"1d")。5分ポーリングの
// 再描画でも選択を維持する。
let outcomeTabSelection = "all";
// 選択中の銘柄タブ("all" or "USDJPY" 等)。時間足タブと同じく再描画で維持する。
let outcomeSymbolSelection = "all";
// 選択中の日付タブ("all" or "2026-07-23" 等の YYYY-MM-DD)。
let outcomeDateSelection = "all";
// 結果リストのページ番号(0始まり)。絞り込みを変えたら先頭に戻す。
let outcomePage = 0;
const OUTCOME_PAGE_SIZE = 20;

// 行のtsから現地時間の日付キー(YYYY-MM-DD)を作る。日付タブの絞り込みと
// 見出しラベル(今日/昨日/…)の両方で同じ基準を使う。
function outcomeDateKey(row) {
  const d = new Date(row.ts || 0);
  if (Number.isNaN(d.getTime())) return "";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// 日付キーを「今日 / 昨日 / 一昨日 / M/D」の見やすいラベルにする。
function outcomeDateLabel(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const target = new Date(y, m - 1, d);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diffDays = Math.round((today - target) / 86400000);
  if (diffDays === 0) return "今日";
  if (diffDays === 1) return "昨日";
  if (diffDays === 2) return "一昨日";
  return `${m}/${d}`;
}

function outcomeRowElement(row) {
  const style = OUTCOME_STYLE[row.outcome] || { badge: row.outcome || "?", className: "flat" };
  const item = document.createElement("div");
  item.className = `outcome-row ${style.className}`;
  const badge = document.createElement("span");
  badge.className = "outcome-badge";
  badge.textContent = style.badge;
  const main = document.createElement("span");
  main.className = "outcome-main";
  const tfLabel = TIMEFRAME_LABEL[row.timeframe] || row.timeframe || "";
  const horizon = TIMEFRAME_HORIZON[row.timeframe] || "";
  const isDirectional = ["long", "short"].includes(row.direction);
  const analysisStatus = ANALYSIS_OUTCOME_LABEL[row.analysis_outcome] || "";
  const blockedGate = row.blocked_gate && typeof row.blocked_gate === "object"
    ? row.blocked_gate
    : null;
  let gateReason = "";
  if (blockedGate?.gate === "event_window") {
    const eventName = [blockedGate.event_currency, blockedGate.event_title].filter(Boolean).join(" ");
    const until = blockedGate.blocked_until ? shortDate(blockedGate.blocked_until) : "";
    gateReason = `イベント警戒${eventName ? `: ${eventName}` : ""}${until ? ` / ${until}まで` : ""}`;
  } else if (blockedGate?.gate) {
    gateReason = blockedGate.gate;
  }
  const analysisDirection =
    !isDirectional && ["long", "short", "neutral"].includes(row.analysis_direction)
    ? ` / 分析は${DIRECTION_LABEL[row.analysis_direction]}${analysisStatus ? `（${analysisStatus}）` : ""}`
    : "";
  const scoring = isDirectional && horizon
    ? `(${horizon}${row.outcome === "pending" ? "待ち" : "を採点"})`
    : "";
  if (row.direction === "standby") {
    const analysisScore = num(row.analysis_score);
    const analysisConviction = num(row.analysis_conviction);
    const analysisDetail = [
      analysisScore === null ? "" : `score ${analysisScore >= 0 ? "+" : ""}${analysisScore.toFixed(2)}`,
      analysisConviction === null ? "" : `確信度 ${Math.round(analysisConviction)}`,
    ].filter(Boolean).join(" / ");
    const runningAnalysis = ["long", "short", "neutral"].includes(row.analysis_direction)
      ? `${DIRECTION_LABEL[row.analysis_direction]}${analysisStatus ? `（${analysisStatus}）` : ""}${analysisDetail ? ` [${analysisDetail}]` : ""}`
      : "方向データを収集中";
    main.textContent = `${row.symbol || "?"} ${tfLabel} 分析稼働: ${runningAnalysis} / 売買見送り${gateReason ? `（${gateReason}）` : ""}`;
  } else {
    main.textContent = `${row.symbol || "?"} ${tfLabel} ${DIRECTION_LABEL[row.direction] || row.direction || ""}${analysisDirection}${scoring}`;
  }
  const move = document.createElement("span");
  move.className = "outcome-move";
  const moveValue = num(row.move ?? row.analysis_move);
  move.textContent = moveValue === null ? "--" : `${moveValue > 0 ? "+" : ""}${moveValue}`;
  // 値動きの符号で色を付ける(上昇=緑 / 下落=赤)。判断方向の当否ではなく
  // 「その後どちらへ動いたか」を示すので、行の左枠(結果色)とは独立。
  if (moveValue !== null && moveValue !== 0) {
    move.classList.add(moveValue > 0 ? "up" : "down");
  }
  const ts = document.createElement("span");
  ts.className = "outcome-ts subtle";
  ts.textContent = shortDate(row.ts);
  item.append(badge, main, move, ts);
  return item;
}

function outcomeGroups(data) {
  const allDecisions = data.evaluation?.recent_decisions_by_timeframe;
  if (allDecisions && typeof allDecisions === "object" && Object.keys(allDecisions).length) {
    return allDecisions;
  }
  // 旧サーバでは満期済み方向判断だけを表示する。
  const grouped = data.evaluation?.recent_outcomes_by_timeframe;
  if (grouped && typeof grouped === "object" && Object.keys(grouped).length) {
    return grouped;
  }
  const fallback = {};
  (data.evaluation?.recent_outcomes || []).forEach((row) => {
    const tf = row.timeframe || "";
    if (!tf) return;
    (fallback[tf] = fallback[tf] || []).push(row);
  });
  return fallback;
}

function outcomeCounts(rows) {
  const counts = { hit: 0, miss: 0, flat: 0, pending: 0, neutral: 0, standby: 0, closed: 0 };
  rows.forEach((row) => {
    if (counts[row.outcome] !== undefined) counts[row.outcome] += 1;
  });
  return counts;
}

function nonScoredCount(counts) {
  return counts.pending + counts.neutral + counts.standby + counts.closed;
}

function outcomeSummaryChip(tf, rows, byTimeframe, analysisByTimeframe) {
  const chip = document.createElement("div");
  chip.className = "outcome-chip";
  const counts = outcomeCounts(rows);

  // 見出し行(時間足名 + 主ホライズン)
  const head = document.createElement("div");
  head.className = "outcome-chip-head";
  const label = document.createElement("strong");
  label.textContent = TIMEFRAME_LABEL[tf] || tf;
  const horizon = document.createElement("span");
  horizon.className = "subtle";
  horizon.textContent = TIMEFRAME_HORIZON[tf] || "";
  head.append(label, horizon);

  // 直近の内訳を「絵文字 + 件数」の小さなタグの並びにする。0件のものは省いて
  // 情報量を絞る(以前は 1行に全カテゴリを詰め込んでいて読みにくかった)。
  const recent = document.createElement("div");
  recent.className = "outcome-chip-recent";
  const parts = [
    ["✅", counts.hit],
    ["❌", counts.miss],
    ["○", counts.flat],
    ["⏳", counts.pending],
    ["中立", counts.neutral],
    ["見送り", counts.standby],
    ["休場", counts.closed],
  ];
  parts
    .filter(([, n]) => n > 0)
    .forEach(([sym, n]) => {
      const tag = document.createElement("span");
      tag.className = "outcome-chip-tag";
      tag.textContent = `${sym}${n}`;
      recent.appendChild(tag);
    });
  if (!recent.childElementCount) {
    const tag = document.createElement("span");
    tag.className = "outcome-chip-tag subtle";
    tag.textContent = "該当なし";
    recent.appendChild(tag);
  }

  chip.append(head, recent);

  // 通算的中率・分析仮説は別々の行にして、割合を強調する
  const total = byTimeframe?.[tf];
  if (total && Number(total.evaluated)) {
    const evaluated = Number(total.evaluated);
    const hits = Number(total.hits || 0);
    chip.appendChild(outcomeChipRate("通算的中", pct(hits / evaluated), `${hits}/${evaluated}`));
  }
  const analysis = analysisByTimeframe?.[tf];
  if (analysis && (Number(analysis.evaluated) || Number(analysis.pending))) {
    const evaluated = Number(analysis.evaluated || 0);
    const hits = Number(analysis.hits || 0);
    const pending = Number(analysis.pending || 0);
    chip.appendChild(
      outcomeChipRate(
        "分析仮説",
        evaluated ? pct(hits / evaluated) : "--",
        `${hits}/${evaluated}${pending ? ` ・未採点${pending}` : ""}`,
      ),
    );
  }
  return chip;
}

// 集計チップの中の1行「ラベル: 割合 (内訳)」を作る。
function outcomeChipRate(label, rate, detail) {
  const row = document.createElement("div");
  row.className = "outcome-chip-rate";
  const name = document.createElement("span");
  name.className = "subtle";
  name.textContent = label;
  const value = document.createElement("strong");
  value.textContent = rate;
  const sub = document.createElement("span");
  sub.className = "subtle";
  sub.textContent = detail;
  row.append(name, value, sub);
  return row;
}

function renderRecentOutcomes(data) {
  const target = $("recentOutcomes");
  if (!target) return;
  const tabs = $("outcomeTabs");
  const symbolTabs = $("outcomeSymbolTabs");
  const dateTabs = $("outcomeDateTabs");
  const summary = $("outcomeSummary");
  const pager = $("outcomePager");
  target.replaceChildren();
  if (tabs) tabs.replaceChildren();
  if (symbolTabs) symbolTabs.replaceChildren();
  if (dateTabs) dateTabs.replaceChildren();
  if (summary) summary.replaceChildren();
  if (pager) pager.replaceChildren();

  const groups = outcomeGroups(data);
  const availableTfs = TIMEFRAME_ORDER.filter((tf) => (groups[tf] || []).length);
  if (!availableTfs.length) {
    target.appendChild(empty("時間足別の判断ログはまだありません"));
    return;
  }
  if (outcomeTabSelection !== "all" && !availableTfs.includes(outcomeTabSelection)) {
    outcomeTabSelection = "all";
  }

  // 時間足タブで絞り込んだ後の行(銘柄タブの選択肢集計に使う)
  const tfFilteredRows =
    outcomeTabSelection === "all"
      ? Object.values(groups).flat()
      : groups[outcomeTabSelection] || [];
  const bySymbolFilter = (rows) =>
    outcomeSymbolSelection === "all"
      ? rows
      : rows.filter((row) => (row.symbol || "") === outcomeSymbolSelection);

  const availableSymbols = Array.from(
    new Set(tfFilteredRows.map((row) => row.symbol).filter(Boolean)),
  ).sort();
  if (outcomeSymbolSelection !== "all" && !availableSymbols.includes(outcomeSymbolSelection)) {
    outcomeSymbolSelection = "all";
  }

  // 日付フィルタ。時間足・銘柄で絞った後の行から、存在する日付を新しい順に集める。
  const byDateFilter = (rows) =>
    outcomeDateSelection === "all"
      ? rows
      : rows.filter((row) => outcomeDateKey(row) === outcomeDateSelection);
  const tfSymFilteredRows = bySymbolFilter(tfFilteredRows);
  const availableDates = Array.from(
    new Set(tfSymFilteredRows.map((row) => outcomeDateKey(row)).filter(Boolean)),
  ).sort((a, b) => (a < b ? 1 : -1));
  if (outcomeDateSelection !== "all" && !availableDates.includes(outcomeDateSelection)) {
    outcomeDateSelection = "all";
  }

  // タブは「ラベル + 件数バッジ」で作る。件数バッジは総数だけを小さく添える
  // (以前は各タブに ✅/❌/他 を全部並べていて、時間足タブと銘柄タブの両方に
  // 出ると情報過多で読みにくかった)。
  const makeTab = (key, label, rows, selected) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `outcome-tab${selected ? " active" : ""}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", selected ? "true" : "false");
    const name = document.createElement("span");
    name.className = "outcome-tab-label";
    name.textContent = label;
    const badge = document.createElement("span");
    badge.className = "outcome-tab-count";
    badge.textContent = String(rows.length);
    button.append(name, badge);
    return button;
  };

  // 時間足タブ(すべて / 15分足 / 1時間足 / 4時間足 / 日足)
  if (tabs) {
    const options = [["all", "すべて"], ...availableTfs.map((tf) => [tf, TIMEFRAME_LABEL[tf] || tf])];
    options.forEach(([key, label]) => {
      const rows = key === "all" ? Object.values(groups).flat() : groups[key] || [];
      const button = makeTab(key, label, rows, outcomeTabSelection === key);
      button.addEventListener("click", () => {
        outcomeTabSelection = key;
        outcomeSymbolSelection = "all";
        outcomeDateSelection = "all";
        outcomePage = 0;
        renderRecentOutcomes(data);
      });
      tabs.appendChild(button);
    });
  }

  // 銘柄タブ(すべて / USDJPY / EURUSD 等、選択中の時間足に存在するものだけ)
  if (symbolTabs) {
    const options = [["all", "すべて"], ...availableSymbols.map((sym) => [sym, sym])];
    options.forEach(([key, label]) => {
      const rows =
        key === "all" ? tfFilteredRows : tfFilteredRows.filter((row) => row.symbol === key);
      const button = makeTab(key, label, rows, outcomeSymbolSelection === key);
      button.addEventListener("click", () => {
        outcomeSymbolSelection = key;
        outcomeDateSelection = "all";
        outcomePage = 0;
        renderRecentOutcomes(data);
      });
      symbolTabs.appendChild(button);
    });
  }

  // 日付タブ(すべて / 今日 / 昨日 / 一昨日 / M/D …、新しい順)。
  // 時間足・銘柄で絞った後に存在する日付だけを候補に出す。
  if (dateTabs) {
    const options = [
      ["all", "すべて"],
      ...availableDates.map((dateKey) => [dateKey, outcomeDateLabel(dateKey)]),
    ];
    options.forEach(([key, label]) => {
      const rows =
        key === "all"
          ? tfSymFilteredRows
          : tfSymFilteredRows.filter((row) => outcomeDateKey(row) === key);
      const button = makeTab(key, label, rows, outcomeDateSelection === key);
      button.addEventListener("click", () => {
        outcomeDateSelection = key;
        outcomePage = 0;
        renderRecentOutcomes(data);
      });
      dateTabs.appendChild(button);
    });
  }

  // 選択中タブの集計チップ(直近の✅❌⚪と通算的中率)。銘柄タブで絞り込んでいる間は
  // 通算的中率の分母(by_timeframe)が銘柄横断のままになるため、そのチップは省く。
  const byTimeframe = data.evaluation?.by_timeframe || {};
  const analysisByTimeframe = data.evaluation?.analysis?.by_timeframe || {};
  if (summary) {
    const tfsToShow = outcomeTabSelection === "all" ? availableTfs : [outcomeTabSelection];
    // 通算的中率(by_timeframe)は全期間・全銘柄の集計なので、銘柄または日付で
    // 絞り込んでいる間は分母がかみ合わない。そのチップは省いて誤読を防ぐ。
    const showCumulative = outcomeSymbolSelection === "all" && outcomeDateSelection === "all";
    tfsToShow.forEach((tf) => {
      summary.appendChild(
        outcomeSummaryChip(
          tf,
          byDateFilter(bySymbolFilter(groups[tf] || [])),
          showCumulative ? byTimeframe : null,
          showCumulative ? analysisByTimeframe : null,
        ),
      );
    });
  }

  // 結果リスト(新しい順)。「すべて」は全時間足を時刻でマージする。
  // 表示は20件区切りのページ送りにして、選択中の絞り込みに該当する全件を
  // 一気に描画しない(件数が多いと重いのと、まず新しい判断から見たいため)。
  const parseTs = (row) => {
    const t = new Date(row.ts || 0).getTime();
    return Number.isNaN(t) ? 0 : t;
  };
  const allRows =
    outcomeTabSelection === "all"
      ? byDateFilter(bySymbolFilter(Object.values(groups).flat())).sort(
          (a, b) => parseTs(b) - parseTs(a),
        )
      : [...byDateFilter(bySymbolFilter(groups[outcomeTabSelection] || []))].reverse();
  if (!allRows.length) {
    target.appendChild(empty("この絞り込みに該当する判断はまだありません"));
    return;
  }
  const pageCount = Math.max(1, Math.ceil(allRows.length / OUTCOME_PAGE_SIZE));
  if (outcomePage >= pageCount) outcomePage = pageCount - 1;
  if (outcomePage < 0) outcomePage = 0;
  const pageStart = outcomePage * OUTCOME_PAGE_SIZE;
  allRows
    .slice(pageStart, pageStart + OUTCOME_PAGE_SIZE)
    .forEach((row) => target.appendChild(outcomeRowElement(row)));

  if (pager && pageCount > 1) {
    const goToPage = (page) => {
      outcomePage = page;
      renderRecentOutcomes(data);
    };
    const prevButton = document.createElement("button");
    prevButton.type = "button";
    prevButton.className = "outcome-pager-btn";
    prevButton.textContent = "← 前へ";
    prevButton.disabled = outcomePage === 0;
    prevButton.addEventListener("click", () => goToPage(outcomePage - 1));
    const status = document.createElement("span");
    status.className = "outcome-pager-status subtle";
    status.textContent = `${outcomePage + 1} / ${pageCount} ページ（全${allRows.length}件）`;
    const nextButton = document.createElement("button");
    nextButton.type = "button";
    nextButton.className = "outcome-pager-btn";
    nextButton.textContent = "次へ →";
    nextButton.disabled = outcomePage >= pageCount - 1;
    nextButton.addEventListener("click", () => goToPage(outcomePage + 1));
    pager.append(prevButton, status, nextButton);
  }
}

function renderOps(data) {
  const ops = data.ops || {};
  setText("opsHealth", ops.status || "unknown");

  const processList = $("opsProcessList");
  processList.replaceChildren();
  const processes = ops.processes || [];
  if (!processes.length) {
    processList.appendChild(empty("プロセス情報を取得できません"));
  }
  processes.forEach((process) => {
    const running = Boolean(process.running);
    const exitCode = process.last_exit_code;
    let status = "未登録";
    if (running && process.state === "running") {
      status = `実行中${(process.pids || []).length ? ` pid=${process.pids.join(", ")}` : ""}`;
    } else if (running && process.launchd_label) {
      status = `登録済み・次周期待ち${exitCode === null || exitCode === undefined ? "" : ` / 前回終了 ${exitCode}`}`;
    } else if (running) {
      status = `稼働中${(process.pids || []).length ? ` pid=${process.pids.join(", ")}` : ""}`;
    }
    let tone = running ? "ok" : "fail";
    if (running && exitCode !== null && exitCode !== undefined && exitCode !== 0) {
      tone = process.key === "briefing_service" && exitCode === 5 ? "warn" : "fail";
    }
    processList.appendChild(
      tradeItem(
        process.label_ja || process.key || "--",
        status,
        tone,
      ),
    );
  });

  const inputList = $("opsInputList");
  inputList.replaceChildren();
  const signals = ops.signals || {};
  [
    ["判断ログ", signals.has_any_journal],
    ["5分価格系列", signals.has_timeframe_prices],
    ["9ホライズンshadow", signals.has_horizon_track],
    ["学習プロファイル", signals.has_any_learning],
  ].forEach(([label, ok]) => {
    inputList.appendChild(
      tradeItem(label, ok ? "あり" : "未作成", ok ? "ok" : "warn"),
    );
  });
  const runtimeLogs = ops.runtime_logs || [];
  runtimeLogs.forEach((row) => {
    const age = num(row.age_minutes);
    inputList.appendChild(
      tradeItem(
        row.label_ja || row.name || "--",
        row.exists
          ? `更新 ${shortDate(row.mtime)}${age === null ? "" : ` / ${Math.round(age)}分前`}`
          : "未作成",
        row.exists ? "ok" : "warn",
      ),
    );
  });

  const alertList = $("opsAlertList");
  alertList.replaceChildren();
  const alerts = ops.alerts || [];
  if (!alerts.length) {
    alertList.appendChild(empty("追加対応が必要な項目はありません"));
    return;
  }
  alerts.slice(0, 6).forEach((alert) => {
    alertList.appendChild(
      tradeItem(
        alert.message_ja || "--",
        alert.action_ja || "",
        alert.severity || "info",
      ),
    );
  });
}

function signedR(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value > 0 ? "+" : ""}${value.toFixed(2)}R`
    : "--";
}

function tradeItem(title, body, tone = "") {
  const div = document.createElement("div");
  div.className = `condition-item ${tone}`.trim();
  const strong = document.createElement("strong");
  strong.textContent = title;
  const span = document.createElement("span");
  span.textContent = body;
  div.append(strong, span);
  return div;
}

function renderTradeMonitor(data) {
  const trade = data.trade_monitor || {};
  const counts = trade.counts || {};
  setText("tradeMonitorUpdated", shortDate(trade.generated_at));
  setText("tradeHealth", trade.status || "unknown");
  setText(
    "tradeCounts",
    `active ${counts.active || 0} / approved ${counts.approved || 0} / paused ${counts.auto_paused || 0}`,
  );

  const actions = $("tradeActionList");
  actions.replaceChildren();
  const readyForReview = trade.ready_for_review || [];
  const paused = trade.auto_paused || [];
  if (!readyForReview.length && !paused.length) {
    actions.appendChild(empty("承認待ち・自動停止中の改善候補はありません"));
  }
  readyForReview.slice(0, 5).forEach((row) => {
    const metrics = row.prospective_metrics || {};
    actions.appendChild(
      tradeItem(
        `承認待ち ${row.priority || ""}`,
        `${row.title_ja || row.candidate_id || "--"} / prospective effective ${
          metrics.effective_samples || 0
        } / expiry ${shortDate(row.prospective_end)}`,
        "amber-text",
      ),
    );
  });
  paused.slice(0, 5).forEach((row) => {
    actions.appendChild(
      tradeItem(
        "自動停止",
        `${row.title_ja || row.candidate_id || "--"} / ${row.auto_pause_reason_ja || ""}`,
        "red-text",
      ),
    );
  });

  const policyStats = $("tradePolicyStats");
  policyStats.replaceChildren();
  const stats = trade.approved_policy_stats || [];
  if (!stats.length) {
    policyStats.appendChild(empty("承認済みTP/SL候補の採点はまだありません"));
  }
  stats.slice(0, 8).forEach((row) => {
    const netExpectancy = num(row.net_expectancy_r);
    const grossExpectancy = num(row.gross_expectancy_r);
    policyStats.appendChild(
      tradeItem(
        `${row.stage || "--"} ${row.candidate_id || "--"}`,
        `canonical net期待R ${signedR(netExpectancy)} / net PF ${
          num(row.net_profit_factor_r)?.toFixed(2) || "--"
        } / gross診断 ${signedR(grossExpectancy)} / net n=${row.net_label_samples || 0}`,
        netExpectancy !== null && netExpectancy < 0 ? "red-text" : "green-text",
      ),
    );
  });

  const events = $("tradeEvents");
  events.replaceChildren();
  const rows = trade.recent_events || [];
  if (!rows.length) {
    events.appendChild(empty("改善候補の監査イベントはまだありません"));
    return;
  }
  rows.slice(-6).reverse().forEach((row) => {
    const div = document.createElement("div");
    div.className = "timeline-item";
    div.textContent = `${shortDate(row.ts)}  ${row.event_type || "--"}: ${row.candidate_id || "--"} ${row.from_stage || ""}→${row.to_stage || ""}`;
    events.appendChild(div);
  });
}

function renderDecisionMonitor(data) {
  const decision = data.decision_monitor || {};
  const overall = decision.overall || {};
  const performance = decision.performance || {};
  const modelDelta = decision.model_expectancy_delta || {};
  const counts = decision.counts || {};
  const grossExpectancy = num(overall.gross_expectancy_r);
  const netExpectancy = num(overall.net_expectancy_r);
  const grossProfitFactor = num(overall.gross_profit_factor_r);
  const netProfitFactor = num(overall.net_profit_factor_r);
  const netR = num(performance.net_R);
  const deltaExpected = num(modelDelta.delta_expected_R);
  const cellCount = Object.values(counts).reduce((total, value) => total + Number(value || 0), 0);
  const canonicalNet = data.net_r || {};

  setText("decisionMonitorUpdated", shortDate(decision.generated_at));
  setText("decisionHealth", decision.status || "unknown");
  setText(
    "decisionExpectancy",
    `canonical net期待R ${signedR(netExpectancy)} / 累積net ${signedR(netR)} / gross診断 ${signedR(
      grossExpectancy,
    )} / Δ ${signedR(deltaExpected)} / net PF ${
      netProfitFactor === null ? "--" : netProfitFactor.toFixed(2)
    } / gross PF ${grossProfitFactor === null ? "--" : grossProfitFactor.toFixed(2)}`,
  );
  setText(
    "decisionCounts",
    `events ${decision.decision_events || 0} / scored ${decision.scored_outcomes || 0} / cells ${cellCount}`,
  );
  setText(
    "decisionNetR",
    `正準net期待 ${signedR(num(canonicalNet.net_expectancy_r))} / 累積 ${signedR(num(canonicalNet.cumulative_net_r))} / gross期待 ${signedR(num(canonicalNet.gross_expectancy_r))}`,
  );
  setText(
    "decisionNetCoverage",
    `net label ${canonicalNet.net_label_samples || 0} / gross ${canonicalNet.gross_samples || 0} (${pct(num(canonicalNet.net_label_coverage))}) / raw ${canonicalNet.raw_samples || 0} → effective ${canonicalNet.effective_samples || 0} / overlap ${pct(num(canonicalNet.overlap_ratio))} / ${canonicalNet.sample_ok ? "sample OK" : "sample不足"} / version ${(canonicalNet.label_versions || []).join(", ") || "--"} / cost ${(canonicalNet.cost_model_ids || []).join(", ") || "--"}`,
  );

  const actions = $("decisionActionList");
  actions.replaceChildren();
  const actionable = decision.actionable_cells || [];
  if (!actionable.length) {
    actions.appendChild(empty("見送り・減衰セルはまだありません"));
  }
  actionable.slice(0, 8).forEach((row) => {
    const action = row.action || "--";
    const tone = action === "avoid" ? "red-text" : "amber-text";
    const label = `${row.symbol || "--"} ${row.timeframe || "--"} ${row.direction || "--"} / ${action}`;
    const sl = pct(num(row.sl_rate));
    const factor = num(row.factor);
    actions.appendChild(
      tradeItem(
        label,
        `期待R ${signedR(num(row.expectancy_r))} / SL ${sl} / n=${row.tradable || 0}${
          factor === null ? "" : ` / ×${factor.toFixed(2)}`
        }`,
        tone,
      ),
    );
  });

  const failures = $("decisionFailureList");
  failures.replaceChildren();
  const zeroReasons = decision.tradable_zero_reasons?.reasons || [];
  const reasons = [...(decision.failure_reason_summary || []), ...zeroReasons];
  if (!reasons.length) {
    failures.appendChild(empty("失敗理由はまだ分類されていません"));
  }
  reasons.slice(0, 8).forEach((row) => {
    failures.appendChild(
      tradeItem(
        row.label_ja || row.key || "--",
        `count ${row.count || 0}${
          row.primary_count === undefined ? "" : ` / primary ${row.primary_count || 0}`
        }`,
        row.pending ? "ok" : "amber-text",
      ),
    );
  });

  const worst = $("decisionWorstCells");
  worst.replaceChildren();
  const worstCells = decision.worst_cells || [];
  if (!worstCells.length) {
    worst.appendChild(empty("期待Rが悪化した成熟セルはまだありません"));
  }
  worstCells.slice(0, 8).forEach((row) => {
    const tone = num(row.expectancy_r) !== null && num(row.expectancy_r) <= 0 ? "red-text" : "";
    worst.appendChild(
      tradeItem(
        `${row.symbol || "--"} ${row.timeframe || "--"} ${row.direction || "--"}`,
        `期待R ${signedR(num(row.expectancy_r))} / MFE ${signedR(num(row.avg_mfe_r))} / MAE ${signedR(num(row.avg_mae_r))} / n=${row.tradable || 0}`,
        tone,
      ),
    );
  });
}

// ===== 学習の中身: 全体的中率ドーナツ =====
function renderHitDonut(data) {
  const host = $("hitRateDonut");
  if (!host) return;
  host.replaceChildren();
  const evaluated = Number(data.learning.evaluated || 0);
  const hits = Number(data.learning.hits || 0);
  const rate = evaluated ? hits / evaluated : null;

  const size = 132;
  const cx = size / 2;
  const cy = size / 2;
  const r = 52;
  const circ = 2 * Math.PI * r;
  const frac = rate === null ? 0 : rate;

  const el = svg("svg", { viewBox: `0 0 ${size} ${size}`, role: "img" });
  el.appendChild(
    svg("circle", { cx, cy, r, fill: "none", stroke: "#1b1c18", "stroke-width": 14 }),
  );
  if (rate !== null) {
    el.appendChild(
      svg("circle", {
        cx,
        cy,
        r,
        fill: "none",
        stroke: hitColor(rate),
        "stroke-width": 14,
        "stroke-linecap": "round",
        "stroke-dasharray": `${(circ * frac).toFixed(2)} ${circ.toFixed(2)}`,
        transform: `rotate(-90 ${cx} ${cy})`,
      }),
    );
  }
  const center = svg("text", {
    x: cx,
    y: cy + 6,
    "text-anchor": "middle",
    fill: "var(--viz-ink)",
    "font-size": 26,
    "font-weight": 800,
  });
  center.textContent = rate === null ? "--" : `${Math.round(rate * 100)}%`;
  el.appendChild(center);
  host.appendChild(el);

  setText("donutHitRate", pct(rate));
  // 点推定だけを大きく出すと過信を招くため、区間があれば併記する。
  const ciLow = num(data.learning.ci_low);
  const ciHigh = num(data.learning.ci_high);
  const hasCI = ciLow !== null && ciHigh !== null;
  setText(
    "donutHitCount",
    hasCI
      ? `${hits} / ${evaluated} 採点 (95%区間 ${Math.round(ciLow * 100)}-${Math.round(ciHigh * 100)}%)`
      : `${hits} / ${evaluated} 採点`,
  );
  const note = $("donutSampleNote");
  if (note) {
    // 「十分」と言えるのは重み学習の発火件数を満たしたときだけで、的中率の
    // 推定精度が十分という意味ではない。区間が広い間はそれを明示する。
    note.textContent =
      evaluated === 0
        ? "採点済みの判断がまだありません"
        : evaluated < 20
          ? `重み学習の目安20件まであと${20 - evaluated}件`
          : data.learning.ci_wide === true
            ? "重み学習には足りていますが、的中率はまだ誤差が大きく優劣は読めません"
            : "重み学習に十分なサンプル数です";
  }
}

const TF_LABEL = { "15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足", fusion: "融合1判断" };
const TF_HORIZON = { "15m": "15分後", "1h": "1時間後", "4h": "4時間後", "1d": "24時間後" };

// ===== 学習の中身: 時間足 × 通貨ペア の的中率マトリクス =====
function renderLearnedMatrix(data) {
  const host = $("learnedMatrix");
  if (!host) return;
  host.replaceChildren();

  const tfRows = (data.tf_learning?.timeframes || []).filter((row) => Number(row.evaluated || 0) > 0);
  // 融合1判断にも採点があれば擬似的な行として足す
  const fusion = data.learning;
  const fusionSymbols = (fusion?.symbols || []).filter((s) => Number(s.evaluated || 0) > 0);
  const rows = tfRows.map((row) => ({
    tf: row.timeframe,
    symbols: row.symbols || [],
    evaluated: Number(row.evaluated || 0),
    hits: Number(row.hits || 0),
  }));
  if (fusion?.source === "fusion" && fusionSymbols.length) {
    rows.push({ tf: "fusion", symbols: fusionSymbols, evaluated: Number(fusion.evaluated || 0), hits: Number(fusion.hits || 0) });
  }

  if (!rows.length) {
    host.appendChild(empty("採点済みの学習がまだありません(判断→主ホライズン後に採点されます)"));
    return;
  }

  // 全ペアを列に(出現順を安定させる)
  const symbolOrder = [];
  rows.forEach((row) => {
    row.symbols.forEach((s) => {
      if (!symbolOrder.includes(s.symbol)) symbolOrder.push(s.symbol);
    });
  });
  symbolOrder.sort();

  const cols = `minmax(96px, 0.8fr) repeat(${symbolOrder.length}, minmax(76px, 1fr)) minmax(84px, 0.9fr)`;

  const header = document.createElement("div");
  header.className = "matrix-row";
  header.style.gridTemplateColumns = cols;
  const corner = document.createElement("div");
  corner.className = "matrix-corner matrix-colhead";
  corner.textContent = "時間足 ＼ ペア";
  header.appendChild(corner);
  symbolOrder.forEach((sym) => {
    const c = document.createElement("div");
    c.className = "matrix-colhead";
    c.textContent = sym;
    header.appendChild(c);
  });
  const totHead = document.createElement("div");
  totHead.className = "matrix-colhead";
  totHead.textContent = "時間足計";
  header.appendChild(totHead);
  host.appendChild(header);

  rows.forEach((row) => {
    const tr = document.createElement("div");
    tr.className = "matrix-row";
    tr.style.gridTemplateColumns = cols;

    const rh = document.createElement("div");
    rh.className = "matrix-rowhead";
    const rhLabel = document.createElement("span");
    rhLabel.textContent = TF_LABEL[row.tf] || row.tf;
    rh.appendChild(rhLabel);
    if (TF_HORIZON[row.tf]) {
      const small = document.createElement("small");
      small.textContent = TF_HORIZON[row.tf];
      rh.appendChild(small);
    }
    tr.appendChild(rh);

    const tfLabel = TF_LABEL[row.tf] || row.tf;
    const bySym = new Map(row.symbols.map((s) => [s.symbol, s]));
    symbolOrder.forEach((sym) => {
      const s = bySym.get(sym);
      tr.appendChild(matrixCell(s, { tf: tfLabel, symbol: sym }));
    });

    // 時間足合計セル
    const rate = row.evaluated ? row.hits / row.evaluated : null;
    tr.appendChild(
      matrixCell(
        {
          hit_rate: rate,
          hits: row.hits,
          evaluated: row.evaluated,
          // 時間足計セルも区間を出す(サーバが行に付けた値をそのまま渡す)
          ci_low: row.ci_low,
          ci_high: row.ci_high,
          ci_wide: row.ci_wide,
        },
        { tf: tfLabel, symbol: "全ペア", isTotal: true },
      ),
    );

    host.appendChild(tr);
  });
}

function matrixCell(stat, ctx = {}) {
  const cell = document.createElement("div");
  const evaluated = stat ? Number(stat.evaluated || 0) : 0;
  if (!stat || evaluated === 0) {
    cell.className = "matrix-cell empty";
    cell.tabIndex = 0;
    const rate = document.createElement("span");
    rate.className = "cell-rate";
    rate.textContent = "—";
    cell.appendChild(rate);
    attachTooltip(cell, () => ({
      title: `${ctx.tf || ""} × ${ctx.symbol || ""}`,
      rows: [{ label: "採点", value: "まだなし", muted: true }],
    }));
    return cell;
  }
  const rate = evaluated ? Number(stat.hits || 0) / evaluated : null;
  // Wilson 95%区間はサーバ側(fx_backtester.calibration)が算出する。区間が広い
  // セルは的中率の点推定を根拠にできないため、色を出さずグレーに倒す。
  // 「5/6=83%を緑で見せる」ことが誤読の主因なので、色の抑制が本質。
  const ciLow = num(stat.ci_low);
  const ciHigh = num(stat.ci_high);
  const hasCI = ciLow !== null && ciHigh !== null;
  const unreliable = stat.ci_wide === true;
  const color = unreliable ? "rgba(120,120,128,0.28)" : hitColor(rate);
  cell.className = `matrix-cell filled${unreliable ? " dim" : ""}`;
  cell.tabIndex = 0;
  cell.style.background = color;
  cell.style.color = inkOn(color);
  cell.style.borderLeftColor = unreliable
    ? "rgba(120,120,128,0.55)"
    : rate >= 0.5
      ? "rgba(9,42,28,0.55)"
      : "rgba(60,12,12,0.55)";
  const rateEl = document.createElement("span");
  rateEl.className = "cell-rate";
  rateEl.textContent = pct(rate);
  const nEl = document.createElement("span");
  nEl.className = "cell-n";
  // 区間が出せるセルは分子分母に加えて幅を併記する(例: 5/6 [44-97%])。
  nEl.textContent = hasCI
    ? `${stat.hits}/${evaluated} [${Math.round(ciLow * 100)}-${Math.round(ciHigh * 100)}%]`
    : `${stat.hits}/${evaluated}`;
  cell.append(rateEl, nEl);
  attachTooltip(cell, () => ({
    title: `${ctx.tf || ""} × ${ctx.symbol || ""}`,
    rows: [
      { label: "的中率(点推定)", value: pct(rate), color },
      { label: "的中 / 採点", value: `${stat.hits} / ${evaluated}`, muted: true },
      ...(hasCI
        ? [
            {
              label: "95%信頼区間",
              value: `${Math.round(ciLow * 100)}% 〜 ${Math.round(ciHigh * 100)}%`,
              muted: true,
            },
          ]
        : []),
      ...(unreliable
        ? [
            {
              label: "判定",
              value: "サンプル不足 — この数値から優劣は読めません",
              muted: true,
            },
          ]
        : []),
    ],
  }));
  return cell;
}

function renderMatrixLegend() {
  const host = $("matrixLegend");
  if (!host || host.childElementCount) return;
  const lo = document.createElement("span");
  lo.className = "legend-item";
  lo.textContent = "0%";
  const grad = document.createElement("span");
  grad.className = "legend-gradient";
  const hi = document.createElement("span");
  hi.className = "legend-item";
  hi.textContent = "100%";
  const wrap = document.createElement("span");
  wrap.className = "legend-item";
  wrap.append(lo, grad, hi);
  host.appendChild(wrap);
}

// ===== 学習の中身: AIが書いた学習メモ =====
const NOTE_TF_COLOR = { "15m": "#3987e5", "1h": "#1baf7a", "4h": "#c98500", "1d": "#9085e9", fusion: "#d55181" };

function renderLearnedNotes(data) {
  const host = $("learnedNotes");
  if (!host) return;
  host.replaceChildren();

  const cards = [];
  (data.tf_learning?.timeframes || []).forEach((row) => {
    const notes = (row.notes_ja || []).filter(Boolean);
    if (notes.length) cards.push({ tf: row.timeframe, label: TF_LABEL[row.timeframe] || row.timeframe, notes });
  });
  const fusionNotes = (data.learning?.notes_ja || []).filter(Boolean);
  if (data.learning?.source === "fusion" && fusionNotes.length) {
    cards.push({ tf: "fusion", label: "融合1判断", notes: fusionNotes });
  }

  if (!cards.length) {
    host.appendChild(empty("学習メモはまだありません(採点が進むと生成されます)"));
    return;
  }

  cards.forEach((card) => {
    const el = document.createElement("div");
    el.className = "note-card";
    el.style.borderLeftColor = NOTE_TF_COLOR[card.tf] || "var(--viz-1)";
    const h4 = document.createElement("h4");
    const pill = document.createElement("span");
    pill.className = "tf-pill";
    pill.style.background = NOTE_TF_COLOR[card.tf] || "var(--viz-1)";
    pill.textContent = card.label;
    h4.appendChild(pill);
    el.appendChild(h4);
    const ul = document.createElement("ul");
    card.notes.forEach((note) => {
      const li = document.createElement("li");
      li.textContent = note;
      ul.appendChild(li);
    });
    el.appendChild(ul);
    host.appendChild(el);
  });
}

function renderLearnedSummary(data) {
  const source = data.learning_source || {};
  setText("learnedSource", `${source.label_ja || "未生成"} / ${shortDate(data.tf_learning?.generated_at || data.learning?.generated_at)}`);
  renderHitDonut(data);
  renderMatrixLegend();
  renderLearnedMatrix(data);
  renderLearnedNotes(data);
}

// ===== 記録アクティビティ(直近48時間・方向別の積み上げ縦棒) =====
const DIRECTIONS = [
  { key: "long", label: "ロング", color: "#3987e5" },
  { key: "short", label: "ショート", color: "#d95926" },
  { key: "neutral", label: "中立", color: "#c98500" },
  { key: "standby", label: "見送り", color: "#6f7268" },
];

function renderActivity(data) {
  const host = $("activityChart");
  if (!host) return;
  host.replaceChildren();

  const legend = $("activityLegend");
  if (legend && !legend.childElementCount) {
    DIRECTIONS.forEach((d) => legend.appendChild(legendItem(d.label, d.color)));
  }

  const buckets = data.journal?.activity?.buckets || [];
  if (!buckets.length) {
    host.appendChild(empty("記録アクティビティはまだありません"));
    return;
  }

  const W = 960;
  const H = 220;
  const pad = { top: 12, right: 12, bottom: 28, left: 34 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const n = buckets.length;
  const maxTotal = Math.max(1, ...buckets.map((b) => Number(b.total || 0)));
  const slot = plotW / n;
  const barW = Math.min(18, slot - 2);

  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "記録アクティビティ" });

  // y gridlines + ticks (clean steps)
  const yMax = niceCeil(maxTotal);
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const val = (yMax / steps) * i;
    const y = pad.top + plotH - (val / yMax) * plotH;
    el.appendChild(svg("line", { x1: pad.left, y1: y, x2: pad.left + plotW, y2: y, class: "svg-grid" }));
    const t = svg("text", { x: pad.left - 6, y: y + 4, "text-anchor": "end", class: "svg-tick" });
    t.textContent = String(Math.round(val));
    el.appendChild(t);
  }

  buckets.forEach((b, i) => {
    const x = pad.left + slot * i + (slot - barW) / 2;
    let yTop = pad.top + plotH;
    const total = Number(b.total || 0);
    DIRECTIONS.forEach((d) => {
      const v = Number(b[d.key] || 0);
      if (v <= 0) return;
      const h = (v / yMax) * plotH;
      yTop -= h;
      el.appendChild(
        svg("rect", {
          x,
          y: yTop,
          width: barW,
          height: Math.max(0, h - 1.5), // 1.5px surface gap between stacked segments
          fill: d.color,
          rx: 1.5,
        }),
      );
    });
    // 列全体を覆う透明ヒット領域(バーの隙間もカバー)。ホバーで内訳ツールチップ。
    if (total > 0) {
      const hit = svg("rect", {
        x: pad.left + slot * i,
        y: pad.top,
        width: slot,
        height: plotH,
        fill: "transparent",
        tabindex: 0,
        role: "img",
        class: "activity-hit",
      });
      attachTooltip(hit, () => ({
        title: shortDate(b.ts),
        rows: [
          ...DIRECTIONS.filter((d) => Number(b[d.key] || 0) > 0).map((d) => ({
            label: d.label,
            value: `${b[d.key]}件`,
            color: d.color,
          })),
          { label: "合計", value: `${total}件`, muted: true },
        ],
      }));
      el.appendChild(hit);
    }
    // hour tick every 6h
    const hour = new Date(b.ts).getHours();
    if (Number.isFinite(hour) && hour % 6 === 0 && total >= 0 && (i === 0 || i === n - 1 || i % 6 === 0)) {
      const t = svg("text", { x: x + barW / 2, y: H - 10, "text-anchor": "middle", class: "svg-tick" });
      t.textContent = `${String(hour).padStart(2, "0")}時`;
      el.appendChild(t);
    }
  });

  // baseline
  el.appendChild(svg("line", { x1: pad.left, y1: pad.top + plotH, x2: pad.left + plotW, y2: pad.top + plotH, class: "svg-axis" }));
  host.appendChild(el);
}

function niceCeil(value) {
  if (value <= 5) return 5;
  if (value <= 10) return 10;
  const mag = Math.pow(10, Math.floor(Math.log10(value)));
  return Math.ceil(value / mag) * mag;
}

// ===== 確信度キャリブレーション(帯の予測中点 vs 実的中率の散布 + 対角線) =====
// 確信度キャリブレーションに使う bins を選ぶ。融合1判断に採点があればそれ、
// 無ければ最も採点数の多い時間足プロファイルの bins を使う。
function pickCalibrationBins(data) {
  const norm = (raw) =>
    (raw || [])
      .map((b) => {
        const evaluated = Number(b.evaluated || 0);
        const hits = Number(b.hits || 0);
        return { low: Number(b.low || 0), high: Number(b.high || 0), evaluated, hits, rate: evaluated ? hits / evaluated : null };
      })
      .filter((b) => b.evaluated > 0);

  const fusion = norm(data.learning?.bins);
  if (fusion.length) return { bins: fusion, label: "融合1判断" };

  let best = null;
  (data.tf_learning?.timeframes || []).forEach((row) => {
    const scored = norm(row.bins);
    const total = scored.reduce((s, b) => s + b.evaluated, 0);
    if (scored.length && (!best || total > best.total)) {
      best = { bins: scored, label: TF_LABEL[row.timeframe] || row.timeframe, total };
    }
  });
  return best || { bins: [], label: "" };
}

function renderCalibration(data) {
  const host = $("calibrationChart");
  if (!host) return;
  host.replaceChildren();

  const picked = pickCalibrationBins(data);
  const scored = picked.bins;
  setText("calibrationSource", picked.label ? `${picked.label}の学習` : "確信度帯 → 実際の的中率");
  if (!scored.length) {
    host.appendChild(empty("確信度帯別の採点はまだありません"));
    return;
  }

  const W = 420;
  const H = 300;
  const pad = { top: 14, right: 16, bottom: 40, left: 44 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const xOf = (conv) => pad.left + (conv / 100) * plotW;
  const yOf = (rate) => pad.top + plotH - rate * plotH;

  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "確信度キャリブレーション" });

  // grid + axis ticks at 0/25/50/75/100
  [0, 25, 50, 75, 100].forEach((v) => {
    const x = xOf(v);
    const y = yOf(v / 100);
    el.appendChild(svg("line", { x1: x, y1: pad.top, x2: x, y2: pad.top + plotH, class: "svg-grid" }));
    el.appendChild(svg("line", { x1: pad.left, y1: y, x2: pad.left + plotW, y2: y, class: "svg-grid" }));
    const xt = svg("text", { x, y: H - 22, "text-anchor": "middle", class: "svg-tick" });
    xt.textContent = String(v);
    el.appendChild(xt);
    const yt = svg("text", { x: pad.left - 8, y: y + 4, "text-anchor": "end", class: "svg-tick" });
    yt.textContent = `${v}%`;
    el.appendChild(yt);
  });

  // perfect-calibration diagonal (reference)
  el.appendChild(
    svg("line", {
      x1: xOf(0),
      y1: yOf(0),
      x2: xOf(100),
      y2: yOf(1),
      stroke: "var(--viz-axis)",
      "stroke-width": 1.5,
      "stroke-dasharray": "4 4",
    }),
  );
  const diagLabel = svg("text", { x: xOf(82), y: yOf(0.9) - 6, "text-anchor": "middle", class: "svg-label" });
  diagLabel.textContent = "理想";
  el.appendChild(diagLabel);

  // connecting path between bin points
  const pts = scored.map((b) => ({ x: xOf((b.low + b.high) / 2), y: yOf(b.rate), b }));
  if (pts.length > 1) {
    let d = "";
    pts.forEach((p, i) => {
      d += `${i === 0 ? "M" : "L"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`;
    });
    el.appendChild(svg("path", { d, fill: "none", stroke: "#3987e5", "stroke-width": 2, "stroke-linejoin": "round" }));
  }

  // bin markers, radius scaled by sample count, colored by hit rate
  const maxN = Math.max(...scored.map((b) => b.evaluated));
  pts.forEach((p) => {
    const r = 5 + 5 * Math.sqrt(p.b.evaluated / maxN);
    el.appendChild(svg("circle", { cx: p.x, cy: p.y, r: r + 2, fill: "var(--viz-surface)" })); // surface ring
    const dot = svg("circle", { cx: p.x, cy: p.y, r, fill: hitColor(p.b.rate), class: "calib-dot" });
    el.appendChild(dot);
    // ≥24px の透明ヒット領域(点は小さいので当てやすくする)+リッチツールチップ
    const mid = (p.b.low + p.b.high) / 2;
    const gap = p.b.rate - mid / 100; // 予測との差(較正誤差)
    const hit = svg("circle", {
      cx: p.x,
      cy: p.y,
      r: Math.max(14, r + 4),
      fill: "transparent",
      tabindex: 0,
      role: "img",
      class: "calib-hit",
    });
    hit.addEventListener("pointerenter", () => dot.classList.add("is-active"));
    hit.addEventListener("pointerleave", () => dot.classList.remove("is-active"));
    hit.addEventListener("focus", () => dot.classList.add("is-active"));
    hit.addEventListener("blur", () => dot.classList.remove("is-active"));
    attachTooltip(hit, () => ({
      title: `確信度 ${p.b.low}–${p.b.high}`,
      rows: [
        { label: "実際の的中率", value: pct(p.b.rate), color: hitColor(p.b.rate) },
        { label: "的中 / 採点", value: `${p.b.hits} / ${p.b.evaluated}`, muted: true },
        {
          label: gap >= 0 ? "予測より上振れ" : "予測より下振れ",
          value: `${gap >= 0 ? "+" : ""}${Math.round(gap * 100)}pt`,
          muted: true,
        },
      ],
    }));
    el.appendChild(hit);
  });

  // axis titles
  const xTitle = svg("text", { x: pad.left + plotW / 2, y: H - 4, "text-anchor": "middle", class: "svg-label" });
  xTitle.textContent = "確信度 (予測)";
  el.appendChild(xTitle);
  const yTitle = svg("text", {
    x: 12,
    y: pad.top + plotH / 2,
    "text-anchor": "middle",
    class: "svg-label",
    transform: `rotate(-90 12 ${pad.top + plotH / 2})`,
  });
  yTitle.textContent = "実際の的中率";
  el.appendChild(yTitle);

  host.appendChild(el);
}

const COND_LABEL = {
  rsi_1h: "RSI(1h)",
  ma_gap_atr: "MA乖離(ATR)",
  atr_pct: "ボラティリティ",
  tf_agreement: "時間足一致",
  news_count: "ニュース件数",
  adx_1h: "ADX(1h)",
  rating_4h: "4hレーティング",
  rating_1d: "1dレーティング",
};

// ===== 市場条件別の学習結果(条件バケット → 的中率、50%基準の中央振り分け) =====
function renderConditionChart(data) {
  const host = $("conditionChart");
  if (!host) return;
  host.replaceChildren();

  // 採点数の多い時間足を代表として使う
  const tfRows = (data.tf_learning?.timeframes || []).filter((r) => (r.conditions || []).length);
  let chosen = null;
  tfRows.forEach((r) => {
    if (!chosen || Number(r.evaluated || 0) > Number(chosen.evaluated || 0)) chosen = r;
  });
  const conditions = chosen ? chosen.conditions || [] : data.learning?.conditions || [];
  setText("conditionChartTf", chosen ? `${TF_LABEL[chosen.timeframe] || chosen.timeframe}の学習` : "融合1判断");

  const usable = conditions.filter((c) => Number(c.evaluated || 0) > 0);
  if (!usable.length) {
    host.appendChild(empty("市場条件別に採点できる学習がまだありません"));
    return;
  }

  // 特徴量ごとにグルーピングし、サンプル数の多い順で上位を表示
  const groups = new Map();
  usable.forEach((c) => {
    const key = c.feature;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  });
  const orderedGroups = [...groups.entries()]
    .map(([feature, rows]) => ({
      feature,
      rows: rows.sort((a, b) => Number(b.evaluated || 0) - Number(a.evaluated || 0)),
      total: rows.reduce((sum, r) => sum + Number(r.evaluated || 0), 0),
    }))
    .sort((a, b) => b.total - a.total)
    .slice(0, 4);

  orderedGroups.forEach((group) => {
    const wrap = document.createElement("div");
    const title = document.createElement("div");
    title.className = "cond-group-title";
    title.textContent = COND_LABEL[group.feature] || group.feature;
    wrap.appendChild(title);
    group.rows.slice(0, 5).forEach((c) => {
      wrap.appendChild(conditionRow(c, COND_LABEL[group.feature] || group.feature));
    });
    host.appendChild(wrap);
  });
}

function conditionRow(c, featureLabel) {
  const rate = Number(c.evaluated || 0) ? Number(c.hits || 0) / Number(c.evaluated || 0) : null;
  const row = document.createElement("div");
  row.className = "cond-row";
  row.tabIndex = 0;

  const label = document.createElement("div");
  label.className = "cond-label";
  const dirLabel = c.direction === "long" ? "ロング" : c.direction === "short" ? "ショート" : c.direction;
  label.textContent = `${c.bucket} `;
  const small = document.createElement("small");
  small.textContent = `· ${dirLabel}`;
  label.appendChild(small);

  const track = document.createElement("div");
  track.className = "cond-track";
  const baseline = document.createElement("div");
  baseline.className = "cond-baseline";
  track.appendChild(baseline);
  if (rate !== null) {
    const fill = document.createElement("div");
    fill.className = "cond-fill";
    // 50%基準で中央から左右に伸ばす(diverging)
    const color = hitColor(rate);
    if (rate >= 0.5) {
      fill.style.left = "50%";
      fill.style.width = `${(rate - 0.5) * 100}%`;
    } else {
      fill.style.right = "50%";
      fill.style.width = `${(0.5 - rate) * 100}%`;
    }
    fill.style.background = color;
    track.appendChild(fill);
  }

  const out = document.createElement("output");
  out.textContent = `${pct(rate)} (${c.hits}/${c.evaluated})`;

  row.append(label, track, out);

  // ホバー/フォーカスで詳細ツールチップ(50%基準からの優劣も明示)
  const diff = rate === null ? null : rate - 0.5;
  attachTooltip(row, () => ({
    title: `${featureLabel || ""}: ${c.bucket}`,
    rows: [
      { label: "方向", value: dirLabel, muted: true },
      { label: "的中率", value: pct(rate), color: rate === null ? undefined : hitColor(rate) },
      { label: "的中 / 採点", value: `${c.hits} / ${c.evaluated}`, muted: true },
      ...(diff === null
        ? []
        : [
            {
              label: diff >= 0 ? "五分以上に強い" : "五分より弱い",
              value: `${diff >= 0 ? "+" : ""}${Math.round(diff * 100)}pt`,
              muted: true,
            },
          ]),
    ],
  }));
  return row;
}

function renderMl(data) {
  setText("mlTrainedAt", shortDate(data.ml.trained_at));
  setText("mlUsable", String(Boolean(data.ml.usable)));
  const metrics = data.ml.metrics || {};
  const brier = num(metrics.val_brier);
  const base = num(metrics.baseline_brier);
  setText("mlBrier", brier === null ? "--" : brier.toFixed(3));
  setText("mlBaseBrier", base === null ? "--" : base.toFixed(3));
  const returnHead = data.ml.return_head || {};
  const sourceHash = String(returnHead.source_dataset_hash || "");
  const returnReasons = Array.isArray(returnHead.reasons) ? returnHead.reasons : [];
  const returnSummary = returnHead.model
    ? `純R shadow=${Boolean(returnHead.usable)} / RMSE ${
        num(returnHead.val_rmse)?.toFixed(3) || "--"
      } / DSR ${num(returnHead.dsr)?.toFixed(3) || "--"} / teacher ${
        returnHead.label_version || "--"
      } / cost ${returnHead.cost_model_id || "--"} / effective ${
        returnHead.effective_samples || 0
      } / dataset ${sourceHash ? sourceHash.slice(0, 12) : "--"}`
    : `純Rモデル未学習${
        returnReasons.length ? ` (${returnReasons.slice(0, 3).join("; ")})` : ""
      }`;
  const reasons = data.ml.reasons || [];
  const training = data.ml.training || {};
  const progress = data.ml.has_model
    ? ""
    : `融合24時間判断のPIT適格な初期件数ゲート: 4時間間引き後 ` +
      `${training.eligible_after_thinning || 0} / ${training.minimum_required || 150}件` +
      `（採点待ち ${training.pending || 0}件、旧形式除外 ${training.pit_ineligible || 0}件、` +
      `通過後も追加検証あり）`;
  const primaryReasons = reasons.length ? reasons : progress ? [progress] : [];
  setText("mlReasons", [...primaryReasons, returnSummary].join(" / "));

  const target = $("importanceBars");
  target.replaceChildren();
  const rows = data.ml.importance || [];
  if (!rows.length) {
    target.appendChild(empty("特徴量重要度はまだありません"));
    return;
  }
  const max = Math.max(...rows.map((row) => row.value), 1);
  rows.forEach((row) => {
    target.appendChild(barRow(row.name, row.value / max, row.value.toFixed(3), "cyan"));
  });
}

function renderConditions(data) {
  const target = $("conditionList");
  target.replaceChildren();
  const rows = data.learning.conditions || [];
  if (!rows.length) {
    target.appendChild(empty("苦手条件はまだ検出されていません"));
    return;
  }
  rows.slice(0, 8).forEach((row) => {
    const div = document.createElement("div");
    div.className = "condition-item";
    const title = document.createElement("strong");
    title.textContent = `${row.feature}: ${row.bucket} / ${row.direction}`;
    const body = document.createElement("span");
    body.textContent = `的中 ${pct(row.hit_rate)} (${row.hits}/${row.evaluated})${
      row.factor ? ` / 減衰×${Number(row.factor).toFixed(2)}` : ""
    }`;
    div.append(title, body);
    target.appendChild(div);
  });
}

function renderDimensions(data) {
  const target = $("dimensionList");
  target.replaceChildren();
  const rows = data.learning.dimensions || [];
  const outcomeRows = data.dimension_outcomes || [];
  markPanelEmpty(target, !rows.length && !outcomeRows.length);
  if (!rows.length && !outcomeRows.length) {
    target.appendChild(empty("市場セッション・レジームの成熟データはまだありません"));
    return;
  }
  rows.slice(0, 12).forEach((row) => {
    const div = document.createElement("div");
    div.className = "condition-item";
    const title = document.createElement("strong");
    title.textContent = `${row.dimension}: ${row.bucket} / ${row.direction}`;
    const body = document.createElement("span");
    const move = num(row.avg_move_atr);
    body.textContent = `的中 ${pct(row.hit_rate)} (${row.hits}/${row.evaluated})${
      move === null ? "" : ` / 平均 ${move >= 0 ? "+" : ""}${move.toFixed(2)} ATR`
    }`;
    div.append(title, body);
    target.appendChild(div);
  });
  outcomeRows.slice(0, 12).forEach((row) => {
    const div = document.createElement("div");
    div.className = "condition-item";
    const title = document.createElement("strong");
    title.textContent = `${row.dimension}: ${row.bucket} / ${row.direction} (純R)`;
    const body = document.createElement("span");
    const net = num(row.net_expectancy_r);
    body.textContent = `平均 ${net === null ? "--" : `${net >= 0 ? "+" : ""}${net.toFixed(2)}R`} ` +
      `/ label ${row.net_labels || 0} / coverage ${pct(row.net_label_coverage)}`;
    div.append(title, body);
    target.appendChild(div);
  });
}

function renderShadow(data) {
  const target = $("shadowList");
  target.replaceChildren();
  const shadow = data.shadow || {};
  const producers = shadow.by_producer || {};
  const rows = Object.entries(producers);
  markPanelEmpty(target, !rows.length);
  if (!rows.length) {
    target.appendChild(empty("shadow仮説はまだ記録・採点されていません"));
    return;
  }
  rows.forEach(([producer, stat]) => {
    const div = document.createElement("div");
    div.className = "condition-item";
    const title = document.createElement("strong");
    title.textContent = producer;
    const body = document.createElement("span");
    const net = num(stat.net_expectancy_r);
    const timeframeText = Object.entries(stat.by_timeframe || {})
      .map(([timeframe, cell]) => `${timeframe}:${pct(cell.hit_rate)}(n=${cell.effective || 0})`)
      .join(" / ");
    body.textContent = `${stat.active_version || "unknown"} / 方向 ${pct(stat.hit_rate)} (${stat.hits || 0}/${stat.effective || 0}) / ` +
      `純R ${net === null ? "--" : `${net >= 0 ? "+" : ""}${net.toFixed(2)}R`} ` +
      `(label ${stat.net_labels || 0}) / abstain ${stat.abstained || 0}` +
      `${timeframeText ? ` / TF ${timeframeText}` : ""}`;
    div.append(title, body);
    target.appendChild(div);
  });
}

function renderInputContext(data) {
  const target = $("inputContextList");
  target.replaceChildren();
  const input = data.input_context || {};
  markPanelEmpty(target, !input.context_rows);
  if (!input.context_rows) {
    target.appendChild(empty("未接続 — 最新判断に版付きinput contextがありません"));
    return;
  }
  const add = (titleText, bodyText) => {
    const div = document.createElement("div");
    div.className = "condition-item";
    const title = document.createElement("strong");
    title.textContent = titleText;
    const body = document.createElement("span");
    body.textContent = bodyText;
    div.append(title, body);
    target.appendChild(div);
  };
  add(
    "Context coverage",
    `${pct(input.coverage)} (${input.context_rows || 0}/${input.rows || 0}) / unique ${input.unique_contexts || 0}`,
  );
  Object.entries(input.macro_status || {}).forEach(([status, count]) =>
    add(`Macro: ${status}`, `${count} context`),
  );
  Object.entries(input.liquidity_status || {}).forEach(([status, count]) =>
    add(`Liquidity: ${status}`, `${count} context`),
  );
  Object.entries(input.quote_sources || {}).forEach(([source, count]) =>
    add(`Quote: ${source}`, `${count} context`),
  );
  const spread = input.spread_pips || {};
  if (spread.n) {
    add(
      "Spread distribution",
      `p50 ${num(spread.p50)?.toFixed(2)} / p90 ${num(spread.p90)?.toFixed(2)} / p99 ${num(spread.p99)?.toFixed(2)} pips (n=${spread.n})`,
    );
  }
  if (input.shadow_would_block) {
    add("Shadow liquidity gate", `would-block ${input.shadow_would_block} context（未適用）`);
  }
}

function renderFiles(data) {
  const target = $("fileList");
  target.replaceChildren();
  Object.entries(data.files).forEach(([name, info]) => {
    const div = document.createElement("div");
    div.className = `file-item ${info.exists ? "" : "missing"}`;
    const left = document.createElement("div");
    left.textContent = name;
    const right = document.createElement("div");
    right.textContent = info.exists ? `${Math.round(info.size / 1024)}KB / ${shortDate(info.mtime)}` : "未作成";
    div.append(left, right);
    target.appendChild(div);
  });
}

function renderRecent(data) {
  const target = $("recentRows");
  target.replaceChildren();
  const rows = (data.journal.recent || []).slice().reverse();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent = "判断ログはまだありません";
    tr.appendChild(td);
    target.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const cells = [
      shortDate(row.ts),
      row.symbol || "--",
      TIMEFRAME_LABEL[row.timeframe] || row.timeframe || "融合24h",
      row.direction || "--",
      row.conviction ?? "--",
      row.data_quality === undefined || row.data_quality === null ? "--" : pct(Number(row.data_quality)),
      row.close === undefined || row.close === null ? "--" : Number(row.close).toFixed(String(row.symbol || "").endsWith("JPY") ? 3 : 5),
    ];
    cells.forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    target.appendChild(tr);
  });
}

const yenFormatter = new Intl.NumberFormat("ja-JP", {
  style: "currency",
  currency: "JPY",
  maximumFractionDigits: 0,
});

function yen(value) {
  const parsed = num(value);
  return parsed === null ? "--" : yenFormatter.format(parsed);
}

function signedYen(value) {
  const parsed = num(value);
  if (parsed === null) return "--";
  return `${parsed > 0 ? "+" : ""}${yenFormatter.format(parsed)}`;
}

function portfolioTone(node, value) {
  node.classList.remove("is-positive", "is-negative");
  if (typeof value !== "number") return;
  node.classList.add(value >= 0 ? "is-positive" : "is-negative");
}

function detailLine(label, value) {
  const row = document.createElement("div");
  const key = document.createElement("span");
  const text = document.createElement("strong");
  key.textContent = label;
  text.textContent = value;
  row.append(key, text);
  return row;
}

function portfolioObjectRows(title, value) {
  const section = document.createElement("div");
  section.className = "portfolio-structured";
  const heading = document.createElement("strong");
  heading.textContent = title;
  section.appendChild(heading);
  const entries = value && typeof value === "object" ? Object.entries(value) : [];
  if (!entries.length) {
    section.appendChild(empty("記録なし"));
    return section;
  }
  entries.forEach(([key, raw]) => {
    const row = document.createElement("p");
    row.textContent = `${key}: ${
      raw && typeof raw === "object" ? JSON.stringify(raw) : String(raw ?? "--")
    }`;
    section.appendChild(row);
  });
  return section;
}

function drawPortfolioCurve(portfolio) {
  const canvas = $("portfolioCurveCanvas");
  const emptyNote = $("portfolioCurveEmpty");
  if (!canvas) return;
  const rows = Array.isArray(portfolio.balance_curve) ? portfolio.balance_curve : [];
  if (!rows.length) {
    canvas.hidden = true;
    emptyNote.hidden = false;
    emptyNote.textContent = "正規台帳イベントがまだありません。";
    return;
  }
  canvas.hidden = false;
  emptyNote.hidden = true;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth || 900);
  const height = 260;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const left = 58;
  const right = 18;
  const top = 18;
  const balanceBottom = 182;
  const drawdownTop = 205;
  const bottom = 245;
  const balances = rows.map((row) => Number(row.balance_jpy || 0));
  const drawdowns = rows.map((row) => Number(row.drawdown_jpy || 0));
  const minimum = Math.min(...balances, Number(portfolio.initial_capital_jpy || 0));
  const maximum = Math.max(...balances, minimum + 1);
  const range = maximum - minimum || 1;
  const x = (index) =>
    left + (rows.length === 1 ? 0 : (index / (rows.length - 1)) * (width - left - right));
  const balanceY = (value) =>
    balanceBottom - ((value - minimum) / range) * (balanceBottom - top);
  const maxDrawdown = Math.max(...drawdowns, 1);
  const drawdownY = (value) =>
    drawdownTop + (value / maxDrawdown) * (bottom - drawdownTop);

  context.strokeStyle = "rgba(255,255,255,0.12)";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(left, balanceBottom);
  context.lineTo(width - right, balanceBottom);
  context.moveTo(left, drawdownTop);
  context.lineTo(width - right, drawdownTop);
  context.stroke();

  context.strokeStyle = "#5dc98c";
  context.lineWidth = 2;
  context.beginPath();
  balances.forEach((value, index) => {
    const px = x(index);
    const py = balanceY(value);
    if (index === 0) context.moveTo(px, py);
    else context.lineTo(px, py);
  });
  if (rows.length === 1) {
    context.lineTo(width - right, balanceY(balances[0]));
  }
  context.stroke();

  context.strokeStyle = "#de6e64";
  context.lineWidth = 2;
  context.beginPath();
  drawdowns.forEach((value, index) => {
    const px = x(index);
    const py = drawdownY(value);
    if (index === 0) context.moveTo(px, py);
    else context.lineTo(px, py);
  });
  if (rows.length === 1) {
    context.lineTo(width - right, drawdownY(drawdowns[0]));
  }
  context.stroke();

  context.fillStyle = "rgba(235,235,225,0.72)";
  context.font = "11px system-ui, sans-serif";
  context.fillText("残高", 8, top + 4);
  context.fillText(yen(maximum), 8, top + 18);
  context.fillText("DD", 8, drawdownTop + 12);
  context.fillText(yen(maxDrawdown), 8, bottom);
}

function renderPortfolioTradeDetail(trade) {
  const target = $("portfolioTradeDetail");
  target.replaceChildren();
  if (!trade) {
    target.appendChild(empty("表示できる取引明細はまだありません"));
    return;
  }
  const head = document.createElement("div");
  head.className = "portfolio-detail-head";
  const title = document.createElement("strong");
  title.textContent = `${trade.symbol || "--"} ${trade.direction || "--"} / ${trade.trade_id || "--"}`;
  const status = document.createElement("span");
  status.className = "badge";
  status.textContent = trade.status === "closed" ? "終了" : "保有中";
  head.append(title, status);

  const rationale = document.createElement("p");
  rationale.className = "note";
  rationale.textContent = `判断理由: ${trade.rationale?.reason || "構造化理由なし"}`;
  target.append(head, rationale);

  const grid = document.createElement("div");
  grid.className = "portfolio-detail-grid";
  grid.append(
    detailLine("decision_id", trade.decision_id || "--"),
    detailLine("時間足", trade.timeframe || "--"),
    detailLine("数量", String(trade.units ?? "--")),
    detailLine("判断Bid / Ask", `${trade.entry_bid ?? "--"} / ${trade.entry_ask ?? "--"}`),
    detailLine("開始価格", String(trade.entry_executable ?? "--")),
    detailLine("ストップ", String(trade.stop_price ?? "--")),
    detailLine("目標", String(trade.target_price ?? "--")),
    detailLine("計画リスク", yen(trade.planned_risk_jpy)),
    detailLine("想定元本", yen(trade.notional_jpy)),
    detailLine("JPY換算", `${trade.quote_to_jpy_rate ?? "--"} / ${trade.quote_conversion_source || "--"}`),
  );
  if (trade.status === "closed") {
    grid.append(
      detailLine("終了理由", trade.close_reason || "--"),
      detailLine("決済Bid / Ask", `${trade.exit_bid ?? "--"} / ${trade.exit_ask ?? "--"}`),
      detailLine("決済価格", String(trade.exit_executable ?? "--")),
      detailLine("市場損益(mid)", signedYen(trade.gross_market_pnl_jpy)),
      detailLine("bid/askコスト", yen(trade.spread_quote_cost_jpy)),
      detailLine("スリッページ", yen(trade.slippage_cost_jpy)),
      detailLine("手数料", yen(trade.commission_jpy)),
      detailLine("金利", yen(trade.financing_jpy)),
      detailLine("換算コスト", yen(trade.conversion_cost_jpy)),
      detailLine("純損益", signedYen(trade.net_pnl_jpy)),
      detailLine(
        "純R",
        typeof trade.realized_net_r === "number" ? trade.realized_net_r.toFixed(2) : "--",
      ),
      detailLine(
        "最大含み益",
        typeof trade.maximum_favorable_excursion_r === "number"
          ? `${trade.maximum_favorable_excursion_r.toFixed(2)}R`
          : "--",
      ),
      detailLine(
        "最大含み損",
        typeof trade.maximum_adverse_excursion_r === "number"
          ? `${trade.maximum_adverse_excursion_r.toFixed(2)}R`
          : "--",
      ),
      detailLine(
        "約定観測",
        trade.attribution?.execution_observation_mode ||
          "contemporaneous_quote",
      ),
    );
  } else {
    grid.append(
      detailLine("評価Bid / Ask", `${trade.mark_bid ?? "--"} / ${trade.mark_ask ?? "--"}`),
      detailLine("評価実行価格", String(trade.mark_executable ?? "--")),
      detailLine("評価時刻", shortDate(trade.mark_time)),
      detailLine("含み市場損益(mid)", signedYen(trade.gross_unrealized_pnl_jpy)),
      detailLine("含み純損益(bid/ask)", signedYen(trade.unrealized_pnl_jpy)),
      detailLine("評価bid/askコスト", yen(trade.mark_spread_quote_cost_jpy)),
      detailLine("評価状態", trade.valuation_status || "unavailable"),
    );
  }
  target.appendChild(grid);
  target.appendChild(portfolioObjectRows("判断時の寄与要因", trade.components));
  target.appendChild(portfolioObjectRows("判断時の構造化特徴", trade.features));

  const counterfactuals = trade.attribution?.counterfactuals;
  if (counterfactuals && typeof counterfactuals === "object") {
    const comparable = Object.fromEntries(
      Object.entries(counterfactuals).filter(
        ([key]) => !["contract", "causal_claim", "interpretation"].includes(key),
      ),
    );
    target.appendChild(portfolioObjectRows("反実仮想（因果断定なし）", comparable));
    const explanation = document.createElement("p");
    explanation.className = "note";
    explanation.textContent =
      counterfactuals.interpretation || "同一観測価格に対する機械的比較です。";
    target.appendChild(explanation);
  }

  const failures = Array.isArray(trade.failures) ? trade.failures : [];
  const failureBox = document.createElement("div");
  failureBox.className = "portfolio-failures";
  const failureTitle = document.createElement("strong");
  failureTitle.textContent = failures.length ? "失敗分類" : "失敗分類なし";
  failureBox.appendChild(failureTitle);
  failures.forEach((failure) => {
    const item = document.createElement("p");
    item.textContent = `${failure.category || "unknown"} / ${failure.code || "unknown"}: ${
      failure.evidence || "--"
    }（${failure.confidence || "unknown"} / ${failure.classifier_version || "unversioned"}）`;
    failureBox.appendChild(item);
  });
  target.appendChild(failureBox);

  const hashes = document.createElement("small");
  hashes.className = "portfolio-hashes";
  hashes.textContent = `data ${trade.data_sha256 || "--"} / model ${
    trade.model_sha256 || "--"
  } / policy ${trade.policy_sha256 || "--"}`;
  target.appendChild(hashes);
}

function renderVirtualPortfolio(data) {
  const portfolio = data.virtual_portfolio || {};
  const configured = portfolio.configured === true;
  const banner = $("portfolioReality");
  banner.classList.toggle("is-good", configured);
  banner.classList.toggle("is-waiting", !configured);
  setText("portfolioMode", "delayed bid/ask replay / broker disconnected");
  setText(
    "portfolioRealityTitle",
    configured ? "100万円の仮想口座を追記専用台帳で監視中" : "仮想口座はまだ初期化されていません",
  );
  setText(
    "portfolioRealityBody",
    configured
      ? "15m/1hだけを既存リスク上限内の仮想取引評価枠にし、4h/1dはポジションを持たない観測専用として記録します。遅延履歴bid/askによる分析で、実時間約定ではありません。"
      : portfolio.risk_gate?.reason || "tools/virtual_portfolio.py init を実行してください。",
  );

  setText("portfolioBalance", yen(portfolio.equity_jpy));
  portfolioTone($("portfolioBalance"), num(portfolio.unrealized_pnl_jpy));
  setText(
    "portfolioBalanceChange",
    portfolio.equity_jpy === null
      ? "評価額: 実行可能Bid/Ask不足"
      : `確定残高 ${yen(portfolio.balance_jpy)} / 含み ${signedYen(
          portfolio.unrealized_pnl_jpy,
        )}`,
  );
  setText("portfolioTodayPnl", signedYen(portfolio.today_pnl_jpy));
  portfolioTone($("portfolioTodayPnl"), num(portfolio.today_pnl_jpy));
  const horizonAllocation = portfolio.horizon_allocation || {};
  const dailyTask = portfolio.daily_profit_task || {};
  const dailyTaskStatus =
    dailyTask.status === "completed"
      ? "必須タスク完了"
      : dailyTask.status === "unavailable"
        ? "進捗取得不可"
        : "必須タスク進行中";
  setText(
    "portfolioTargetText",
    `${dailyTaskStatus} / 残り ${yen(
      dailyTask.remaining_jpy ?? portfolio.remaining_daily_target_jpy,
    )} / 目標 ${yen(dailyTask.target_jpy ?? portfolio.daily_target_jpy)}`,
  );
  const researchKpis = portfolio.research_kpis || {};
  const rollingKpi = researchKpis.rolling_20d || {};
  const rollingNetR = Number(rollingKpi.net_r);
  const rollingTradeCount = Number(rollingKpi.trade_count || 0);
  const minimumLearningRows = Number(researchKpis.minimum_learning_rows || 150);
  setText(
    "portfolioTargetProgress",
    Number.isFinite(rollingNetR) ? `${rollingNetR.toFixed(2)} R` : "--",
  );
  setBar(
    "portfolioTargetBar",
    minimumLearningRows > 0 ? rollingTradeCount / minimumLearningRows : 0,
  );
  const expectancy =
    typeof rollingKpi.cost_adjusted_expectancy_net_r === "number"
      ? rollingKpi.cost_adjusted_expectancy_net_r
      : null;
  const confidence = rollingKpi.expectancy_95pct_ci || {};
  const ciAvailable =
    confidence.status === "available" &&
    typeof confidence.lower === "number" &&
    typeof confidence.upper === "number";
  setText(
    "portfolioTargetBasis",
    `期待値 ${
      expectancy !== null ? `${expectancy.toFixed(2)} R` : "--"
    } / 95% CI ${
      ciAvailable
        ? `${Number(confidence.lower).toFixed(2)}〜${Number(confidence.upper).toFixed(2)} R`
        : "データ不足"
    } / n=${rollingTradeCount}`,
  );
  setText(
    "portfolioDrawdown",
    `${yen(portfolio.equity_drawdown_jpy)} ${
      typeof portfolio.equity_drawdown_pct === "number"
        ? `(${precisePct(portfolio.equity_drawdown_pct)})`
        : ""
    }`,
  );
  setText(
    "portfolioDrawdownLimit",
    `確定DD ${yen(portfolio.drawdown_jpy)} / 停止上限 ${yen(
      portfolio.hard_drawdown_limit_jpy,
    )}`,
  );
  setText("portfolioCash", yen(portfolio.cash_jpy));
  setText(
    "portfolioEquity",
    `初期 ${yen(portfolio.initial_capital_jpy)} / 累計確定 ${signedYen(
      portfolio.cumulative_net_pnl_jpy,
    )}`,
  );

  const risk = portfolio.risk_gate || {};
  setText("portfolioRemainingRisk", yen(risk.remaining_daily_loss_budget_jpy));
  setText("portfolioNextTradeRisk", `次回リスク ${yen(risk.next_trade_risk_jpy)}`);
  setText(
    "portfolioLeverage",
    typeof portfolio.gross_leverage === "number"
      ? `${portfolio.gross_leverage.toFixed(2)}x`
      : "0.00x",
  );
  setText("portfolioNotional", `想定元本 ${yen(portfolio.gross_notional_jpy)}`);
  const qualityLabels = {
    waiting_for_session_close_quote: "日次決済気配待ち",
    waiting_for_delayed_quotes: "判断の履歴気配待ち",
    waiting_for_position_marks: "評価用気配待ち",
    eligible_inputs_consumed: "取込・評価済み",
    session_close_lifecycle_error: "日次決済ライフサイクル不整合",
  };
  const qualityState = qualityLabels[portfolio.data_quality_state?.status] || "確認不能";
  const openPositionCount = portfolio.open_position_count || 0;
  const intradayOpenCount = horizonAllocation.intraday_open_positions || 0;
  const legacyOpenCount = horizonAllocation.legacy_positions_outside_policy || 0;
  const maxOpenPositions =
    portfolio.horizon_allocation?.capacity_limit ??
    portfolio.policy?.max_open_positions ??
    "--";
  setText(
    "portfolioDataState",
    risk.can_open ? "新規取引可" : "新規停止",
  );
  $("portfolioDataState").classList.toggle("is-positive", risk.can_open === true);
  $("portfolioDataState").classList.toggle("is-negative", risk.can_open !== true);
  setText(
    "portfolioReconciliation",
    portfolio.reconciliation?.passed
      ? `容量対象 ${intradayOpenCount}/${maxOpenPositions}件${
          legacyOpenCount ? ` / 移行前長期 ${legacyOpenCount}件` : ""
        } / 台帳照合OK`
      : `容量対象 ${intradayOpenCount}/${maxOpenPositions}件 / 台帳照合NG`,
  );
  setText("portfolioDataQuality", `データ: ${qualityState}`);
  const riskBadge = $("portfolioRiskBadge");
  riskBadge.textContent = risk.can_open ? "新規可" : "新規停止";
  riskBadge.classList.toggle("is-good", risk.can_open === true);
  riskBadge.classList.toggle("is-bad", risk.can_open !== true);
  setText(
    "portfolioDailyRisk",
    `${signedYen(portfolio.today_pnl_jpy)} / -${yen(risk.daily_loss_limit_jpy)}`,
  );
  setText(
    "portfolioWeeklyRisk",
    `${signedYen(portfolio.weekly_pnl_jpy)} / -${yen(risk.weekly_loss_limit_jpy)}`,
  );
  setText(
    "portfolioMonthlyRisk",
    `${signedYen(portfolio.monthly_pnl_jpy)} / -${yen(risk.monthly_loss_limit_jpy)}`,
  );
  setText(
    "portfolioOpenCount",
    `${intradayOpenCount} / ${maxOpenPositions}${
      legacyOpenCount ? `（移行前長期 ${legacyOpenCount}）` : ""
    }`,
  );
  setText("portfolioRiskReason", risk.reason || "全リスクゲート内です");
  const pendingClose = portfolio.pending_session_close_request;
  if (pendingClose) {
    setText(
      "portfolioRiskReason",
      `日次決済要求 ${shortDate(pendingClose.cutoff_time)} を保持中。実行可能な遅延Bid/Ask到着後に全建玉を決済します。`,
    );
  }
  drawPortfolioCurve(portfolio);

  const learning = portfolio.learning || {};
  const canonicalNetR = learning.canonical_net_r || {};
  const canonicalEligible = Number(canonicalNetR.eligible_rows || 0);
  const canonicalSamples = Number(canonicalNetR.net_label_samples || 0);
  const canonicalCoverage = num(canonicalNetR.net_label_coverage);
  const canonicalConnected =
    (canonicalEligible === 0 && canonicalNetR.status === "no_eligible_rows") ||
    (canonicalNetR.status === "connected" &&
      canonicalSamples === canonicalEligible &&
      canonicalCoverage === 1);
  setText(
    "portfolioLearningBadge",
    `${learning.eligible_rows || 0} / ${learning.minimum_rows || 150}`,
  );
  setText(
    "portfolioLearningState",
    !canonicalConnected
      ? "正準ラベル接続エラー"
      : learning.ready_for_validation
        ? "週次検証を開始可能"
        : "検証データ待ち",
  );
  setText(
    "portfolioLearningBody",
    !canonicalConnected
      ? "bid/ask・全費用・時刻証跡・正準JSONLストア接続のいずれかが欠けたため、学習をfail-closedで停止しています。"
      : learning.ready_for_validation
      ? "成熟したPITサンプルをtrain/tune/calibration/test/lockboxへ分離します。安全停止以外の方針変更は検証ゲートを通過するまで適用しません。"
      : "確定損失は同一区分の安全停止へ還流します。収益方針は最低件数と時間分割を満たすまで変更しません。",
  );
  setText(
    "portfolioCanonicalNetR",
    `正準store ${canonicalSamples} / ${canonicalEligible} (${pct(canonicalCoverage)}) / ${
      canonicalNetR.label_version || "--"
    } / bid/ask・全費用後 / research-only`,
  );
  const multiAxis = learning.multi_axis || {};
  const multiAxisBadge = $("portfolioMultiAxisBadge");
  const axisInputsReady = multiAxis.status === "axis_inputs_ready";
  multiAxisBadge.textContent = axisInputsReady
    ? "入力準備済み / 判断不使用"
    : "PIT入力を収集中";
  multiAxisBadge.classList.toggle("is-good", axisInputsReady);
  const axisGrid = $("portfolioMultiAxisGrid");
  axisGrid.replaceChildren();
  const axisOrder = [
    "net_return_distribution",
    "market_regime",
    "liquidity_proxy",
    "macro_surprise",
    "cross_asset_relationship",
    "uncertainty",
    "execution_quality",
    "portfolio_risk",
  ];
  const axisFallbackLabels = {
    net_return_distribution: "コスト控除後の収益分布",
    market_regime: "市場レジーム",
    liquidity_proxy: "流動性・公開気配proxy",
    macro_surprise: "マクロサプライズ",
    cross_asset_relationship: "クロスアセット関係",
    uncertainty: "不確実性",
    execution_quality: "執行品質",
    portfolio_risk: "ポートフォリオリスク",
  };
  axisOrder.forEach((name) => {
    const axis = multiAxis.axes?.[name] || {};
    const card = document.createElement("div");
    card.className = "portfolio-axis-card";
    const head = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = axis.label_ja || axisFallbackLabels[name];
    const status = document.createElement("span");
    status.className = `badge ${axis.ready ? "is-good" : ""}`;
    status.textContent = axis.ready ? "入力あり" : "収集中";
    head.append(title, status);
    const coverage = document.createElement("span");
    coverage.textContent =
      typeof axis.coverage === "number"
        ? `coverage ${(axis.coverage * 100).toFixed(0)}%（${axis.available_rows || 0}/${
            axis.development_rows || 0
          }）`
        : "成熟PIT行を待機中";
    const role = document.createElement("small");
    role.textContent =
      axis.role === "prediction_head"
        ? "予測出力"
        : axis.role === "auxiliary_target"
          ? "補助教師（予測特徴には不使用）"
          : "判断時点の入力特徴";
    card.append(head, coverage, role);
    axisGrid.appendChild(card);
  });

  const series = $("portfolioDailySeries");
  series.replaceChildren();
  const dailyRows = Array.isArray(portfolio.daily_series) ? portfolio.daily_series.slice(-30) : [];
  if (!dailyRows.length) {
    series.appendChild(empty("終了済み取引がないため、日次純損益はまだありません"));
  } else {
    const maximum = Math.max(...dailyRows.map((row) => Math.abs(Number(row.net_pnl_jpy || 0))), 1);
    dailyRows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "portfolio-series-row";
      const date = document.createElement("span");
      date.textContent = row.session_date_jst || "--";
      const track = document.createElement("div");
      track.className = "portfolio-series-track";
      const bar = document.createElement("i");
      const value = Number(row.net_pnl_jpy || 0);
      bar.className = value >= 0 ? "positive" : "negative";
      bar.style.width = `${Math.max(2, Math.round((Math.abs(value) / maximum) * 100))}%`;
      track.appendChild(bar);
      const amount = document.createElement("strong");
      amount.textContent = signedYen(value);
      portfolioTone(amount, value);
      item.append(date, track, amount);
      series.appendChild(item);
    });
  }

  const pending = $("portfolioPendingDecisions");
  pending.replaceChildren();
  const pendingRows = Array.isArray(portfolio.pending_decisions)
    ? portfolio.pending_decisions
    : [];
  setText("portfolioPendingCount", `${portfolio.pending_decision_count || 0}件待機`);
  if (!pendingRows.length) {
    pending.appendChild(empty("履歴bid/ask待ちの判断はありません"));
  } else {
    pendingRows.forEach((decision) => {
      const card = document.createElement("div");
      card.className = "portfolio-decision-card";
      const title = document.createElement("strong");
      title.textContent = `${shortDate(decision.decision_time)} ${
        decision.symbol || "--"
      } / ${decision.timeframe || "--"}`;
      const detail = document.createElement("p");
      detail.textContent = "遅延履歴bid/askの到着と価格経路の成熟を待機中";
      card.append(title, detail);
      pending.appendChild(card);
    });
  }

  const positions = $("portfolioOpenPositions");
  positions.replaceChildren();
  const openPositions = Array.isArray(portfolio.open_positions)
    ? portfolio.open_positions
    : [];
  if (!openPositions.length) {
    positions.appendChild(empty("現在の仮想ポジションはありません"));
  } else {
    openPositions.forEach((trade) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "portfolio-position-card";
      const main = document.createElement("strong");
      main.textContent = `${trade.symbol || "--"} ${trade.direction || "--"} / ${
        trade.timeframe || "--"
      }`;
      const detail = document.createElement("span");
      detail.textContent = `開始 ${trade.entry_executable ?? "--"} / 現在 ${
        trade.mark_executable ?? "--"
      } / 含み ${signedYen(trade.unrealized_pnl_jpy)} / stop ${
        trade.stop_price ?? "--"
      } / target ${trade.target_price ?? "--"} / 評価 ${shortDate(trade.mark_time)}`;
      card.append(main, detail);
      card.addEventListener("click", () => renderPortfolioTradeDetail(trade));
      positions.appendChild(card);
    });
  }

  const allTrades = Array.isArray(portfolio.trades) ? portfolio.trades : [];
  const search = String($("portfolioTradeSearch")?.value || "").trim().toLowerCase();
  const directionFilter = String($("portfolioDirectionFilter")?.value || "");
  const statusFilter = String($("portfolioStatusFilter")?.value || "");
  const trades = allTrades.filter((trade) => {
    if (directionFilter && trade.direction !== directionFilter) return false;
    if (statusFilter && trade.status !== statusFilter) return false;
    if (!search) return true;
    const searchable = [
      trade.trade_id,
      trade.decision_id,
      trade.symbol,
      trade.timeframe,
      trade.direction,
      trade.close_reason,
      trade.rationale?.reason,
      ...(Array.isArray(trade.failures)
        ? trade.failures.flatMap((failure) => [failure.category, failure.code, failure.evidence])
        : []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return searchable.includes(search);
  });
  setText("portfolioTradeCount", `${portfolio.closed_trade_count || 0}件終了`);
  const tradeRows = $("portfolioTradeRows");
  tradeRows.replaceChildren();
  if (!trades.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 10;
    cell.textContent = allTrades.length
      ? "条件に一致する取引明細はありません"
      : "仮想取引明細はまだありません";
    row.appendChild(cell);
    tradeRows.appendChild(row);
    renderPortfolioTradeDetail(null);
  } else {
    trades.forEach((trade) => {
      const row = document.createElement("tr");
      const failureCodes = (Array.isArray(trade.failures) ? trade.failures : [])
        .map((failure) => failure.code)
        .join(", ");
      const totalCosts =
        Number(trade.spread_quote_cost_jpy || 0) +
        Number(trade.slippage_cost_jpy || 0) +
        Number(trade.commission_jpy || 0) +
        Number(trade.financing_jpy || 0) +
        Number(trade.conversion_cost_jpy || 0);
      const leverage =
        Number(portfolio.balance_jpy || 0) > 0
          ? Number(trade.notional_jpy || 0) / Number(portfolio.balance_jpy)
          : null;
      const values = [
        `${shortDate(trade.open_time)}\n${trade.decision_id || "--"}`,
        `${trade.symbol || "--"} / ${trade.timeframe || "--"} / ${trade.direction || "--"}`,
        `${trade.units ?? "--"} / ${yen(trade.planned_risk_jpy)} / ${
          leverage === null ? "--" : `${leverage.toFixed(2)}x`
        }`,
        `${trade.entry_bid ?? "--"} / ${trade.entry_ask ?? "--"}`,
        `${trade.entry_executable ?? "--"} / ${trade.exit_executable ?? "--"}`,
        trade.status === "closed" ? "終了" : "保有",
        trade.status === "closed"
          ? `${signedYen(trade.gross_market_pnl_jpy)} / -${yen(totalCosts)}`
          : `${signedYen(trade.gross_unrealized_pnl_jpy)} / -${yen(
              trade.mark_spread_quote_cost_jpy,
            )}`,
        trade.status === "closed"
          ? `${signedYen(trade.net_pnl_jpy)} / ${
              typeof trade.realized_net_r === "number"
                ? `${trade.realized_net_r.toFixed(2)}R`
                : "--"
            }`
          : `${signedYen(trade.unrealized_pnl_jpy)} / 評価中`,
        trade.status === "closed"
          ? `${trade.maximum_favorable_excursion_r ?? "--"} / ${
              trade.maximum_adverse_excursion_r ?? "--"
            }`
          : "--",
        `${trade.data_sha256 ? "PIT hashあり" : "PIT証拠不足"} / ${
          trade.status === "closed"
            ? trade.attribution?.cost_contract?.costs_complete
              ? "コスト完全"
              : "コスト未確定"
            : trade.valuation_status === "delayed_executable_mark"
              ? "実行可能評価あり"
              : "評価気配待ち"
        }`,
      ];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index === 7) portfolioTone(cell, num(trade.net_pnl_jpy));
        row.appendChild(cell);
      });
      const action = document.createElement("button");
      action.type = "button";
      action.className = "portfolio-detail-button";
      action.textContent = `${trade.close_reason || "詳細"}${
        failureCodes ? ` / ${failureCodes}` : ""
      }`;
      action.addEventListener("click", () => renderPortfolioTradeDetail(trade));
      row.lastElementChild?.appendChild(document.createElement("br"));
      row.lastElementChild?.appendChild(action);
      tradeRows.appendChild(row);
    });
    renderPortfolioTradeDetail(trades[0]);
  }

  const decisions = $("portfolioDecisionList");
  decisions.replaceChildren();
  const decisionRows = Array.isArray(portfolio.recent_decisions)
    ? portfolio.recent_decisions.slice(0, 20)
    : [];
  if (!decisionRows.length) {
    decisions.appendChild(empty("判断サイクルの記録はまだありません"));
  } else {
    decisionRows.forEach((decision) => {
      const card = document.createElement("div");
      card.className = "portfolio-decision-card";
      const head = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = `${shortDate(decision.decision_time)} ${decision.symbol || "--"} ${
        decision.direction || "--"
      } / ${decision.timeframe || "--"}`;
      const disposition = document.createElement("span");
      disposition.className = "badge";
      const allocationClass = decision.horizon_allocation?.allocation_class;
      disposition.textContent =
        allocationClass === "observation_only"
          ? "観測専用"
          : allocationClass === "unsupported"
            ? "対象外"
            : decision.disposition === "opened"
              ? "開始"
              : "見送り";
      head.append(title, disposition);
      const reason = document.createElement("p");
      const rejection = Array.isArray(decision.rejection_reasons)
        ? decision.rejection_reasons.join(" / ")
        : "";
      reason.textContent = rejection || decision.reason || "理由なし";
      card.append(head, reason);
      decisions.appendChild(card);
    });
  }
}

function render(data) {
  lastDashboardState = data;
  renderVirtualPortfolio(data);
  renderReality(data);
  renderLearnedSummary(data);
  renderMetrics(data);
  renderFlow(data);
  renderCurve(data);
  renderWeights(data);
  renderStages(data);
  renderSymbolBars(data);
  renderTimeframeBars(data);
  renderHorizons(data);
  renderRecentOutcomes(data);
  renderActivity(data);
  renderConditionChart(data);
  renderOps(data);
  renderTradeMonitor(data);
  renderDecisionMonitor(data);
  renderCalibration(data);
  renderMl(data);
  renderConditions(data);
  renderDimensions(data);
  renderShadow(data);
  renderInputContext(data);
  renderFiles(data);
  renderRecent(data);
  if (lastForecastChart) {
    renderForecastChart(lastForecastChart);
  }
  loadForecastChart();
}

async function load() {
  if (window.location.protocol === "file:") {
    setReality({
      badge: "wrong url",
      title: "HTMLファイル直開きでは監視できません",
      body: "学習ログを読むにはWebサーバー経由で開く必要があります。http://127.0.0.1:8765/ を開いてください。",
      tone: "is-bad",
      reasons: [
        "ファイル直開きでは /api/state にアクセスできません。",
        "Mac miniで見る場合は dashboard server を起動して、そのURLをブラウザで開きます。",
      ],
    });
    return;
  }
  $("refreshBtn").disabled = true;
  try {
    const activeView = document.querySelector(".nav-item.active")?.dataset.view;
    const portfolioOnly = activeView === "portfolio";
    const response = await fetch(
      portfolioOnly ? "/api/virtual-portfolio" : "/api/state",
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (portfolioOnly) {
      lastDashboardState = {
        ...(lastDashboardState || {}),
        virtual_portfolio: data,
      };
      renderVirtualPortfolio(lastDashboardState);
    } else {
      render(data);
    }
  } catch (error) {
    console.error(error);
    alert(`読み込みに失敗しました: ${error.message}`);
  } finally {
    $("refreshBtn").disabled = false;
  }
}

$("refreshBtn").addEventListener("click", load);
["portfolioTradeSearch", "portfolioDirectionFilter", "portfolioStatusFilter"].forEach((id) => {
  $(id)?.addEventListener(id === "portfolioTradeSearch" ? "input" : "change", () => {
    if (lastDashboardState) renderVirtualPortfolio(lastDashboardState);
  });
});

// ===== 左サイドバーによる7画面の切り替え =====
const VIEW_EYEBROW = {
  portfolio: "分析専用シミュレーション",
  now: "現在の状況",
  performance: "精度の検証",
  learning: "学習の中身",
  forensics: "外れた原因の分析",
  data: "データ基盤",
  governance: "昇格と監査",
};

function switchView(key) {
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== key;
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === key;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const activeSection = document.querySelector(`.view[data-view="${key}"]`);
  const title = activeSection?.getAttribute("aria-label") || "";
  setText("topbarTitle", title);
  setText("topbarEyebrow", VIEW_EYEBROW[key] || "");
  try {
    window.localStorage.setItem("fxDashboardView", key);
  } catch (error) {
    // localStorage が使えない環境(プライベートモード等)でも画面切り替えは動かす
  }
  // hidden の間は幅が 0 で canvas を描けないため、表示された時点で描き直す
  if (lastCurve.length) drawCurve($("curveCanvas"), lastCurve);
  if (key === "now" && lastForecastChart) {
    drawForecastChart($("forecastChartCanvas"), lastForecastChart);
  }
  if (key === "portfolio" && lastDashboardState?.virtual_portfolio) {
    drawPortfolioCurve(lastDashboardState.virtual_portfolio);
  }
  window.scrollTo({ top: 0 });
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    switchView(button.dataset.view);
    load();
  });
});

// ウィンドウ幅が変わったら学習推移グラフだけ再描画(canvasは物理px依存のため)
let curveResizeTimer = null;
window.addEventListener("resize", () => {
  window.clearTimeout(curveResizeTimer);
  curveResizeTimer = window.setTimeout(() => {
    if (lastCurve.length) drawCurve($("curveCanvas"), lastCurve);
    if (lastForecastChart) drawForecastChart($("forecastChartCanvas"), lastForecastChart);
    if (lastDashboardState?.virtual_portfolio) {
      drawPortfolioCurve(lastDashboardState.virtual_portfolio);
    }
  }, 150);
});
// 前回開いていた画面を復元する(再読み込みのたびに先頭画面へ戻らないように)
let initialView = "portfolio";
try {
  const saved = window.localStorage.getItem("fxDashboardView");
  if (saved && document.querySelector(`.view[data-view="${saved}"]`)) initialView = saved;
} catch (error) {
  // 読めなければ既定の画面のまま
}
restoreChartSelection();
syncChartControls();
bindForecastChartControls();
switchView(initialView);

load();
setInterval(load, 30000);
setTimeout(() => {
  if (!document.hidden) loadForecastChart({ force: true });
  setInterval(() => {
    if (!document.hidden) loadForecastChart({ force: true });
  }, FORECAST_CHART_REFRESH_MS);
}, FORECAST_CHART_REFRESH_OFFSET_MS);
