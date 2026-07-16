const state = {
  logDir: "",
};

const JOURNAL_FILE = "briefing_journal.jsonl";
const LEARNING_FILE = "briefing_learning.json";
const TF_JOURNAL_FILE = "briefing_tf_journal.jsonl";
const TF_LEARNING_FILE = "briefing_tf_learning.json";
const ML_FILE = "ml_model.json";
const DECISION_MONITOR_FILE = "decision_expectancy_monitor.json";

const $ = (id) => document.getElementById(id);

function pct(value, fallback = "--") {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 100)}%`
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
    reasons.push(`判断ログは${journalTotal}件ありますが、約24時間後の比較がまだです。`);
    if (pending > 0) reasons.push(`${pending}件は採点待ちです。`);
  } else {
    reasons.push(`${evaluated}件を採点済みです。的中率と重み調整に使えます。`);
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

function renderMetrics(data) {
  setText("journalTotal", String(data.journal.total));
  setText("journalLatest", shortDate(data.journal.latest_ts));
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
  const paperReady = trade.paper_ready || [];
  const paused = trade.auto_paused || [];
  if (!paperReady.length && !paused.length) {
    actions.appendChild(empty("承認待ち・自動停止中の改善候補はありません"));
  }
  paperReady.slice(0, 5).forEach((row) => {
    actions.appendChild(
      tradeItem(
        `承認待ち ${row.priority || ""}`,
        `${row.title_ja || row.candidate_id || "--"} / seen ${row.seen_count || 0}`,
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
    const expectancy = num(row.expectancy_r);
    policyStats.appendChild(
      tradeItem(
        `${row.stage || "--"} ${row.candidate_id || "--"}`,
        `期待R ${signedR(expectancy)} / PF ${num(row.profit_factor_r)?.toFixed(2) || "--"} / n=${row.tradable || 0}`,
        expectancy !== null && expectancy < 0 ? "red-text" : "green-text",
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
  const expectancy = num(overall.expectancy_r);
  const profitFactor = num(overall.profit_factor_r);
  const netR = num(performance.net_R);
  const deltaExpected = num(modelDelta.delta_expected_R);
  const cellCount = Object.values(counts).reduce((total, value) => total + Number(value || 0), 0);

  setText("decisionMonitorUpdated", shortDate(decision.generated_at));
  setText("decisionHealth", decision.status || "unknown");
  setText(
    "decisionExpectancy",
    `期待R ${signedR(expectancy)} / net ${signedR(netR)} / Δ ${signedR(deltaExpected)} / PF ${
      profitFactor === null ? "--" : profitFactor.toFixed(2)
    }`,
  );
  setText(
    "decisionCounts",
    `events ${decision.decision_events || 0} / scored ${decision.scored_outcomes || 0} / cells ${cellCount}`,
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
  setText("donutHitCount", `${hits} / ${evaluated} 採点`);
  const note = $("donutSampleNote");
  if (note) {
    note.textContent =
      evaluated === 0
        ? "採点済みの判断がまだありません"
        : evaluated < 20
          ? `重み学習の目安20件まであと${20 - evaluated}件`
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
        { hit_rate: rate, hits: row.hits, evaluated: row.evaluated },
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
  const color = hitColor(rate);
  cell.className = `matrix-cell filled${evaluated < 3 ? " dim" : ""}`;
  cell.tabIndex = 0;
  cell.style.background = color;
  cell.style.color = inkOn(color);
  cell.style.borderLeftColor = rate >= 0.5 ? "rgba(9,42,28,0.55)" : "rgba(60,12,12,0.55)";
  const rateEl = document.createElement("span");
  rateEl.className = "cell-rate";
  rateEl.textContent = pct(rate);
  const nEl = document.createElement("span");
  nEl.className = "cell-n";
  nEl.textContent = `${stat.hits}/${evaluated}`;
  cell.append(rateEl, nEl);
  attachTooltip(cell, () => ({
    title: `${ctx.tf || ""} × ${ctx.symbol || ""}`,
    rows: [
      { label: "的中率", value: pct(rate), color },
      { label: "的中 / 採点", value: `${stat.hits} / ${evaluated}`, muted: true },
      ...(!ctx.isTotal && evaluated < 3
        ? [{ label: "サンプル少", value: "参考値", muted: true }]
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
  const reasons = data.ml.reasons || [];
  const training = data.ml.training || {};
  const progress = data.ml.has_model
    ? ""
    : `融合24時間判断のPIT適格な初期件数ゲート: 4時間間引き後 ` +
      `${training.eligible_after_thinning || 0} / ${training.minimum_required || 150}件` +
      `（採点待ち ${training.pending || 0}件、旧形式除外 ${training.pit_ineligible || 0}件、` +
      `通過後も追加検証あり）`;
  setText("mlReasons", reasons.length ? reasons.join(" / ") : progress);

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
    td.colSpan = 6;
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

function render(data) {
  state.logDir = data.log_dir || state.logDir;
  $("logDir").value = state.logDir;
  renderReality(data);
  renderLearnedSummary(data);
  renderMetrics(data);
  renderFlow(data);
  renderWeights(data);
  renderStages(data);
  renderSymbolBars(data);
  renderTimeframeBars(data);
  renderActivity(data);
  renderConditionChart(data);
  renderOps(data);
  renderTradeMonitor(data);
  renderDecisionMonitor(data);
  renderCalibration(data);
  renderMl(data);
  renderConditions(data);
  renderFiles(data);
  renderRecent(data);
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
  const logDir = $("logDir").value.trim();
  const url = logDir ? `/api/state?logDir=${encodeURIComponent(logDir)}` : "/api/state";
  $("refreshBtn").disabled = true;
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    render(data);
  } catch (error) {
    console.error(error);
    alert(`読み込みに失敗しました: ${error.message}`);
  } finally {
    $("refreshBtn").disabled = false;
  }
}

$("refreshBtn").addEventListener("click", load);
$("logDir").addEventListener("keydown", (event) => {
  if (event.key === "Enter") load();
});

load();
setInterval(load, 30000);
