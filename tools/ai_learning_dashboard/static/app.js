const state = {
  logDir: "",
};

const JOURNAL_FILE = "briefing_journal.jsonl";
const LEARNING_FILE = "briefing_learning.json";
const ML_FILE = "ml_model.json";

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
  const journalExists = Boolean(files[JOURNAL_FILE]?.exists);
  const learningExists = Boolean(files[LEARNING_FILE]?.exists);
  const mlExists = Boolean(files[ML_FILE]?.exists);
  const journalTotal = Number(data.journal?.total || 0);
  const evaluated = Number(data.learning?.evaluated || 0);
  const pending = Number(data.evaluation?.pending || 0);
  const reasons = [];

  if (!journalExists || journalTotal === 0) {
    reasons.push("判断ログが0件なので、まだ当たり外れを学習できません。");
    reasons.push("dry-runやno-journalでは学習用ログが残らない可能性があります。");
  } else if (evaluated === 0) {
    reasons.push(`判断ログは${journalTotal}件ありますが、約24時間後の比較がまだです。`);
    if (pending > 0) reasons.push(`${pending}件は採点待ちです。`);
  } else {
    reasons.push(`${evaluated}件を採点済みです。的中率と重み調整に使えます。`);
  }

  if (!learningExists) {
    reasons.push("重み調整ファイルはまだ作られていません。");
  }
  if (!mlExists) {
    reasons.push("GBDTのMLモデルはまだ保存されていません。");
  } else if (!data.ml.usable) {
    reasons.push("MLモデルはありますが、検証スコア不足などで判断参加は無効です。");
  }

  if (!journalExists || journalTotal === 0) {
    setReality({
      badge: "not trained",
      title: "今は学習していません",
      body: "この画面はAI本体ではなく監視画面です。読み取る学習ログが無いため、現在の状態は未学習です。",
      tone: "is-bad",
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
  $("flowProfile").classList.toggle("active", data.files["briefing_learning.json"].exists);
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
  setText("mlRows", `学習${data.ml.n_train || 0} / 検証${data.ml.n_valid || 0}`);
}

function renderWeights(data) {
  const lw = data.learning;
  setText("learningGenerated", shortDate(lw.generated_at));
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
  renderWeights(data);
  renderStages(data);
  renderSymbolBars(data);
  renderTimeframeBars(data);
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
$("logDir").addEventListener("keydown", (event) => {
  if (event.key === "Enter") load();
});

load();
setInterval(load, 30000);
