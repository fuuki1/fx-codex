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
  const mlExists = Boolean(files[ML_FILE]?.exists);
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
  if (!mlExists) {
    reasons.push("GBDTのMLモデルはまだ保存されていません。");
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
    summaryEl.textContent = `採点 ${last.scored}件 / 累積的中率 ${pct(last.hit_rate)}`;
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

  // 最新値のラベル
  const lp = curve[curve.length - 1];
  ctx.fillStyle = text;
  ctx.textAlign = "right";
  ctx.textBaseline = "bottom";
  ctx.font = "600 12px system-ui, sans-serif";
  ctx.fillText(pct(lp.hit_rate), padL + plotW, yRate(lp.hit_rate) - 6);
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
  setText("mlRows", `学習${data.ml.n_train || 0} / 検証${data.ml.n_valid || 0}`);
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

function renderSymbolBars(data) {
  const target = $("symbolBars");
  target.replaceChildren();
  const rows = data.learning.symbols || [];
  if (!rows.length) {
    target.appendChild(empty("ペア別に採点できる判断がまだありません"));
    return;
  }
  rows.forEach((row) => {
    target.appendChild(
      barRow(
        row.symbol,
        row.hit_rate || 0,
        `${pct(row.hit_rate)} (${row.hits}/${row.evaluated}) 係数×${Number(row.factor || 1).toFixed(2)}`,
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
    processList.appendChild(
      tradeItem(
        process.label_ja || process.key || "--",
        running ? `稼働中 pid=${(process.pids || []).join(", ")}` : "停止中",
        running ? "ok" : "warn",
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

function renderBins(data) {
  const target = $("binBars");
  target.replaceChildren();
  const bins = data.learning.bins || [];
  if (!bins.length) {
    target.appendChild(empty("確信度帯別の採点はまだありません"));
    return;
  }
  bins.forEach((bin) => {
    const evaluated = Number(bin.evaluated || 0);
    const hits = Number(bin.hits || 0);
    const rate = evaluated ? hits / evaluated : null;
    target.appendChild(
      barRow(
        `${bin.low}-${bin.high}`,
        rate || 0,
        `${pct(rate)} (${hits}/${evaluated})`,
        rate >= 0.5 ? "green" : "amber",
      ),
    );
  });
}

function renderMl(data) {
  setText("mlTrainedAt", shortDate(data.ml.trained_at));
  setText("mlUsable", String(Boolean(data.ml.usable)));
  const metrics = data.ml.metrics || {};
  const brier = num(metrics.val_brier);
  const base = num(metrics.baseline_brier);
  setText("mlBrier", brier === null ? "--" : brier.toFixed(3));
  setText("mlBaseBrier", base === null ? "--" : base.toFixed(3));
  setText("mlReasons", (data.ml.reasons || []).join(" / "));

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
  renderMetrics(data);
  renderFlow(data);
  renderCurve(data);
  renderWeights(data);
  renderStages(data);
  renderSymbolBars(data);
  renderTimeframeBars(data);
  renderOps(data);
  renderTradeMonitor(data);
  renderDecisionMonitor(data);
  renderBins(data);
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

// ウィンドウ幅が変わったら学習推移グラフだけ再描画(canvasは物理px依存のため)
let curveResizeTimer = null;
window.addEventListener("resize", () => {
  if (!lastCurve.length) return;
  window.clearTimeout(curveResizeTimer);
  curveResizeTimer = window.setTimeout(() => drawCurve($("curveCanvas"), lastCurve), 150);
});
$("logDir").addEventListener("keydown", (event) => {
  if (event.key === "Enter") load();
});

load();
setInterval(load, 30000);
