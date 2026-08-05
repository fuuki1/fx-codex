(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const formatter = new Intl.NumberFormat("ja-JP", {
    style: "currency",
    currency: "JPY",
    maximumFractionDigits: 0,
  });

  function number(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function money(value, signed = false) {
    const parsed = number(value);
    if (parsed === null) return "--";
    return `${signed && parsed > 0 ? "+" : ""}${formatter.format(parsed)}`;
  }

  function date(value) {
    if (!value) return "未記録";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleString("ja-JP", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function text(id, value) {
    const node = byId(id);
    if (node) node.textContent = value;
  }

  function empty(message) {
    const node = document.createElement("div");
    node.className = "empty";
    node.textContent = message;
    return node;
  }

  function tone(node, value) {
    if (!node) return;
    node.classList.remove("is-positive", "is-negative");
    if (typeof value === "number") {
      node.classList.add(value >= 0 ? "is-positive" : "is-negative");
    }
  }

  function line(label, value) {
    const row = document.createElement("div");
    const key = document.createElement("span");
    const content = document.createElement("strong");
    key.textContent = label;
    content.textContent = value;
    row.append(key, content);
    return row;
  }

  function renderDetail(trade) {
    const target = byId("portfolioTradeDetail");
    if (!target) return;
    target.replaceChildren();
    if (!trade) {
      target.appendChild(empty("表示できる取引明細はまだありません"));
      return;
    }
    const head = document.createElement("div");
    head.className = "portfolio-detail-head";
    const title = document.createElement("strong");
    title.textContent = `${trade.symbol || "--"} ${trade.direction || "--"} / ${
      trade.trade_id || "--"
    }`;
    const status = document.createElement("span");
    status.className = "badge";
    status.textContent = trade.status === "closed" ? "終了" : "保有中";
    head.append(title, status);
    const rationale = document.createElement("p");
    rationale.className = "note";
    rationale.textContent = `判断理由: ${trade.rationale?.reason || "構造化理由なし"}`;
    const grid = document.createElement("div");
    grid.className = "portfolio-detail-grid";
    grid.append(
      line("開始価格", String(trade.entry_executable ?? "--")),
      line("ストップ", String(trade.stop_price ?? "--")),
      line("目標", String(trade.target_price ?? "--")),
      line("計画リスク", money(trade.planned_risk_jpy)),
    );
    if (trade.status === "closed") {
      grid.append(
        line("市場損益(mid)", money(trade.gross_market_pnl_jpy, true)),
        line("bid/askコスト", money(trade.spread_quote_cost_jpy)),
        line("スリッページ", money(trade.slippage_cost_jpy)),
        line("手数料", money(trade.commission_jpy)),
        line("金利", money(trade.financing_jpy)),
        line("換算コスト", money(trade.conversion_cost_jpy)),
        line("純損益", money(trade.net_pnl_jpy, true)),
        line(
          "純R",
          typeof trade.realized_net_r === "number" ? trade.realized_net_r.toFixed(2) : "--",
        ),
      );
    }
    const failures = document.createElement("div");
    failures.className = "portfolio-failures";
    const failureTitle = document.createElement("strong");
    const rows = Array.isArray(trade.failures) ? trade.failures : [];
    failureTitle.textContent = rows.length ? "失敗分類" : "失敗分類なし";
    failures.appendChild(failureTitle);
    rows.forEach((failure) => {
      const item = document.createElement("p");
      item.textContent = `${failure.category || "unknown"} / ${
        failure.code || "unknown"
      }: ${failure.evidence || "--"}（${failure.confidence || "unknown"}）`;
      failures.appendChild(item);
    });
    const hashes = document.createElement("small");
    hashes.className = "portfolio-hashes";
    hashes.textContent = `data ${trade.data_sha256 || "--"} / model ${
      trade.model_sha256 || "--"
    } / policy ${trade.policy_sha256 || "--"}`;
    target.append(head, rationale, grid, failures, hashes);
  }

  function render(payload) {
    const portfolio = payload || {};
    const configured = portfolio.configured === true;
    const banner = byId("portfolioReality");
    if (banner) {
      banner.classList.toggle("is-good", configured);
      banner.classList.toggle("is-waiting", !configured);
    }
    text("portfolioMode", "offline simulation / broker disconnected");
    text(
      "portfolioRealityTitle",
      configured
        ? "100万円の仮想口座を追記専用台帳で監視中"
        : "仮想口座はまだ初期化されていません",
    );
    text(
      "portfolioRealityBody",
      configured
        ? "15m/1hだけが仮想取引容量を使い、4h/1dは観測専用です。1日7万円は最適化に使わない参考値です。"
        : portfolio.risk_gate?.reason || "仮想口座を初期化してください。",
    );
    text("portfolioBalance", money(portfolio.balance_jpy));
    text(
      "portfolioBalanceChange",
      `初期 ${money(portfolio.initial_capital_jpy)} / 累計 ${money(
        number(portfolio.balance_jpy) === null
          ? null
          : Number(portfolio.balance_jpy) - Number(portfolio.initial_capital_jpy || 0),
        true,
      )}`,
    );
    text("portfolioTodayPnl", money(portfolio.today_pnl_jpy, true));
    tone(byId("portfolioTodayPnl"), number(portfolio.today_pnl_jpy));
    const horizon = portfolio.horizon_allocation || {};
    text(
      "portfolioTargetText",
      `容量対象 ${horizon.intraday_open_positions || 0}件 / 観測専用判断 ${
        horizon.counts?.observation_only || 0
      }件`,
    );
    const research = portfolio.research_kpis || {};
    const rolling = research.rolling_20d || {};
    const netR = Number(rolling.net_r || 0);
    text("portfolioTargetProgress", `${netR.toFixed(2)} R`);
    const progress =
      Number(research.minimum_learning_rows || 0) > 0
        ? Number(rolling.trade_count || 0) / Number(research.minimum_learning_rows)
        : 0;
    const progressBar = byId("portfolioTargetBar");
    if (progressBar) progressBar.style.width = `${Math.min(100, Math.round(progress * 100))}%`;
    text(
      "portfolioDrawdown",
      `${money(portfolio.drawdown_jpy)} ${
        typeof portfolio.drawdown_pct === "number"
          ? `(${Math.round(portfolio.drawdown_pct * 100)}%)`
          : ""
      }`,
    );
    text("portfolioDrawdownLimit", `停止上限 ${money(portfolio.hard_drawdown_limit_jpy)}`);

    const risk = portfolio.risk_gate || {};
    const riskBadge = byId("portfolioRiskBadge");
    if (riskBadge) {
      riskBadge.textContent = risk.can_open ? "新規可" : "停止";
      riskBadge.classList.toggle("is-good", risk.can_open === true);
      riskBadge.classList.toggle("is-bad", risk.can_open !== true);
    }
    text(
      "portfolioDailyRisk",
      `${money(portfolio.today_pnl_jpy, true)} / -${money(risk.daily_loss_limit_jpy)}`,
    );
    text(
      "portfolioWeeklyRisk",
      `${money(portfolio.weekly_pnl_jpy, true)} / -${money(risk.weekly_loss_limit_jpy)}`,
    );
    text(
      "portfolioMonthlyRisk",
      `${money(portfolio.monthly_pnl_jpy, true)} / -${money(risk.monthly_loss_limit_jpy)}`,
    );
    text(
      "portfolioOpenCount",
      `${portfolio.open_position_count || 0} / ${
        portfolio.horizon_allocation?.capacity_limit ??
        portfolio.policy?.max_open_positions ??
        "--"
      }`,
    );
    text("portfolioRiskReason", risk.reason || "全リスクゲート内です");

    const learning = portfolio.learning || {};
    const canonical = learning.canonical_net_r || {};
    const canonicalEligible = Number(canonical.eligible_rows || 0);
    const canonicalSamples = Number(canonical.net_label_samples || 0);
    const canonicalCoverage = Number(canonical.net_label_coverage);
    const canonicalConnected =
      (canonicalEligible === 0 && canonical.status === "no_eligible_rows") ||
      (canonical.status === "connected" &&
        canonicalSamples === canonicalEligible &&
        canonicalCoverage === 1);
    text(
      "portfolioLearningBadge",
      `${learning.eligible_rows || 0} / ${learning.minimum_rows || 150}`,
    );
    text(
      "portfolioLearningState",
      !canonicalConnected
        ? "正準ラベル接続エラー"
        : learning.ready_for_validation
          ? "週次検証を開始可能"
          : "検証データ待ち",
    );
    text(
      "portfolioLearningBody",
      !canonicalConnected
        ? "bid/ask・全費用・時刻証跡・正準JSONLストア接続が揃わないため、学習をfail-closedで停止しています。"
        : learning.ready_for_validation
        ? "成熟したPITサンプルを独立した時系列窓へ分離できます。"
        : "最低件数と時系列分割を満たすまで売買方針を変更しません。",
    );
    text(
      "portfolioCanonicalNetR",
      `正準store ${canonicalSamples} / ${canonicalEligible} (${pct(
        Number.isFinite(canonicalCoverage) ? canonicalCoverage : null,
      )}) / ${canonical.label_version || "--"} / bid/ask・全費用後 / research-only`,
    );

    const series = byId("portfolioDailySeries");
    if (series) {
      series.replaceChildren();
      const rows = Array.isArray(portfolio.daily_series)
        ? portfolio.daily_series.slice(-30)
        : [];
      if (!rows.length) {
        series.appendChild(empty("終了済み取引がないため、日次純損益はまだありません"));
      } else {
        const maximum = Math.max(
          ...rows.map((row) => Math.abs(Number(row.net_pnl_jpy || 0))),
          1,
        );
        rows.forEach((row) => {
          const item = document.createElement("div");
          item.className = "portfolio-series-row";
          const day = document.createElement("span");
          day.textContent = row.session_date_jst || "--";
          const track = document.createElement("div");
          track.className = "portfolio-series-track";
          const bar = document.createElement("i");
          const value = Number(row.net_pnl_jpy || 0);
          bar.className = value >= 0 ? "positive" : "negative";
          bar.style.width = `${Math.max(
            2,
            Math.round((Math.abs(value) / maximum) * 100),
          )}%`;
          track.appendChild(bar);
          const amount = document.createElement("strong");
          amount.textContent = money(value, true);
          tone(amount, value);
          item.append(day, track, amount);
          series.appendChild(item);
        });
      }
    }

    const positions = byId("portfolioOpenPositions");
    if (positions) {
      positions.replaceChildren();
      const rows = Array.isArray(portfolio.open_positions)
        ? portfolio.open_positions
        : [];
      if (!rows.length) {
        positions.appendChild(empty("現在の仮想ポジションはありません"));
      } else {
        rows.forEach((trade) => {
          const card = document.createElement("button");
          card.type = "button";
          card.className = "portfolio-position-card";
          const main = document.createElement("strong");
          main.textContent = `${trade.symbol || "--"} ${trade.direction || "--"} / ${
            trade.timeframe || "--"
          }`;
          const detail = document.createElement("span");
          detail.textContent = `開始 ${trade.entry_executable ?? "--"} / stop ${
            trade.stop_price ?? "--"
          } / target ${trade.target_price ?? "--"} / risk ${money(
            trade.planned_risk_jpy,
          )}`;
          card.append(main, detail);
          card.addEventListener("click", () => renderDetail(trade));
          positions.appendChild(card);
        });
      }
    }

    const trades = Array.isArray(portfolio.trades) ? portfolio.trades : [];
    text("portfolioTradeCount", `${portfolio.closed_trade_count || 0}件終了`);
    const tradeRows = byId("portfolioTradeRows");
    if (tradeRows) {
      tradeRows.replaceChildren();
      if (!trades.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 7;
        cell.textContent = "仮想取引明細はまだありません";
        row.appendChild(cell);
        tradeRows.appendChild(row);
        renderDetail(null);
      } else {
        trades.forEach((trade) => {
          const row = document.createElement("tr");
          [
            date(trade.open_time),
            trade.symbol || "--",
            trade.direction || "--",
            trade.status === "closed" ? "終了" : "保有",
            trade.status === "closed" ? money(trade.net_pnl_jpy, true) : "--",
            typeof trade.realized_net_r === "number"
              ? trade.realized_net_r.toFixed(2)
              : "--",
          ].forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            if (index === 4) tone(cell, number(trade.net_pnl_jpy));
            row.appendChild(cell);
          });
          const actionCell = document.createElement("td");
          const action = document.createElement("button");
          action.type = "button";
          action.className = "portfolio-detail-button";
          action.textContent =
            (Array.isArray(trade.failures) ? trade.failures : [])
              .map((failure) => failure.code)
              .join(", ") || "詳細";
          action.addEventListener("click", () => renderDetail(trade));
          actionCell.appendChild(action);
          row.appendChild(actionCell);
          tradeRows.appendChild(row);
        });
        renderDetail(trades[0]);
      }
    }

    const decisions = byId("portfolioDecisionList");
    if (decisions) {
      decisions.replaceChildren();
      const rows = Array.isArray(portfolio.recent_decisions)
        ? portfolio.recent_decisions.slice(0, 20)
        : [];
      if (!rows.length) {
        decisions.appendChild(empty("判断サイクルの記録はまだありません"));
      } else {
        rows.forEach((decision) => {
          const card = document.createElement("div");
          card.className = "portfolio-decision-card";
          const head = document.createElement("div");
          const title = document.createElement("strong");
          title.textContent = `${date(decision.decision_time)} ${
            decision.symbol || "--"
          } ${decision.direction || "--"}`;
          const disposition = document.createElement("span");
          disposition.className = "badge";
          disposition.textContent = decision.disposition === "opened" ? "開始" : "見送り";
          head.append(title, disposition);
          const reason = document.createElement("p");
          reason.textContent =
            (Array.isArray(decision.rejection_reasons)
              ? decision.rejection_reasons.join(" / ")
              : "") ||
            decision.reason ||
            "理由なし";
          card.append(head, reason);
          decisions.appendChild(card);
        });
      }
    }
  }

  async function load() {
    try {
      const response = await fetch("/api/virtual-portfolio", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      console.error("virtual portfolio load failed", error);
      text("portfolioRealityTitle", "仮想口座の読込みに失敗しました");
      text("portfolioRealityBody", String(error.message || error));
    }
  }

  function showPortfolioView() {
    document.querySelectorAll(".view").forEach((section) => {
      section.hidden = section.dataset.view !== "portfolio";
    });
    document.querySelectorAll(".nav-item").forEach((button) => {
      const active = button.dataset.view === "portfolio";
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    text("topbarTitle", "仮想口座と純損益");
    text("topbarEyebrow", "分析専用シミュレーション");
    try {
      window.localStorage.setItem("fxDashboardView", "portfolio");
    } catch (error) {
      // localStorageが使えなくても表示は継続する。
    }
    window.scrollTo({ top: 0 });
  }

  byId("refreshBtn")?.addEventListener("click", load);
  window.setTimeout(() => {
    showPortfolioView();
    load();
  }, 0);
  load();
  window.setInterval(load, 30_000);
})();
