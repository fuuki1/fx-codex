from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fx_backtester.artifacts import audit_run_artifacts
from fx_backtester.data import load_economic_events_csv, load_price_csvs
from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig
from fx_backtester.metrics import calculate_metrics
from fx_backtester.models import (
    UnsupportedConversionError,
    instrument_for,
    quote_amount_to_usd,
)
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies.baselines import (
    BuyAndHoldLongStrategy,
    FlatStrategy,
    RandomDirectionStrategy,
)
from fx_backtester.trade_quality import TradeQualityConfig, evaluate_trade_quality


@dataclass(frozen=True)
class RunAnalysisConfig:
    oos_ratio: float = 0.30
    monte_carlo_paths: int = 2_000
    monte_carlo_seed: int = 42
    ruin_threshold_pct: float = 0.30
    cost_multipliers: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)
    min_period_days: int = 365
    min_oos_trades: int = 30
    min_expectancy_trades: int = 30
    full_confidence_trades: int = 100
    min_segment_trades: int = 20
    expectancy_bootstrap_samples: int = 1_000
    expectancy_bootstrap_seed: int = 42
    min_expectancy_r: float = 0.0
    min_tp_sl_score: float = 55.0
    max_unpriced_trade_pct: float = 0.05
    min_walk_forward_folds: int = 3
    min_forward_days: int = 30
    min_monte_carlo_paths: int = 1_000
    max_acceptable_ruin_probability: float = 0.05
    monthly_target_return_pct: float = 0.08
    html_rows: int = 12


def analyze_run_artifacts(
    run_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    config: RunAnalysisConfig | None = None,
    walk_forward_summary_path: str | Path | None = None,
    forward_trades_path: str | Path | None = None,
    write_html: bool = True,
) -> dict[str, Path]:
    analysis_config = config or RunAnalysisConfig()
    source = Path(run_dir)
    destination = Path(output_dir) if output_dir else source
    destination.mkdir(parents=True, exist_ok=True)

    trades = _read_trades(source / "trade_log.csv")
    equity = _read_equity(source / "equity_curve.csv")
    metrics = _read_json(source / "metrics.json")
    run_config = _read_json(source / "config.json")
    qa_report = _read_optional_csv(source / "data_qa.csv")
    price_data, data_load_error = _load_run_price_data(source)
    initial_cash = float(metrics.get("initial_cash", run_config.get("initial_cash", 100_000.0)))

    walk_forward_path = _default_existing_path(
        walk_forward_summary_path,
        source / "walk_forward_summary.csv",
    )
    forward_path = _default_existing_path(forward_trades_path, source / "forward_trade_log.csv")

    pair_performance = pair_performance_summary(trades)
    monthly_pnl = monthly_pnl_summary(equity, trades)
    monthly_target = monthly_target_summary(
        monthly_pnl,
        analysis_config.monthly_target_return_pct,
    )
    drawdowns = drawdown_periods(equity)
    periods = period_performance_summary(equity, trades)
    oos = out_of_sample_summary(equity, trades, initial_cash, analysis_config.oos_ratio)
    cost = cost_sensitivity_summary(
        trades,
        run_config,
        analysis_config.cost_multipliers,
    )
    pnl = pnl_breakdown_summary(trades, run_config)
    lot = lot_control_summary(trades, run_config)
    monte_carlo = monte_carlo_summary(
        trades,
        initial_cash,
        analysis_config.monte_carlo_paths,
        analysis_config.monte_carlo_seed,
        analysis_config.ruin_threshold_pct,
    )
    forward = forward_test_summary(forward_path, analysis_config.min_forward_days)
    paper_diff = paper_backtest_diff_summary(
        trades,
        forward_path,
    )
    trade_quality = evaluate_trade_quality(
        trades,
        price_data,
        qa_report=qa_report,
        config=TradeQualityConfig(
            min_trades=analysis_config.min_expectancy_trades,
            full_confidence_trades=analysis_config.full_confidence_trades,
            min_segment_trades=analysis_config.min_segment_trades,
            expectancy_bootstrap_samples=analysis_config.expectancy_bootstrap_samples,
            expectancy_bootstrap_seed=analysis_config.expectancy_bootstrap_seed,
            min_expectancy_r=analysis_config.min_expectancy_r,
            min_tp_sl_score=analysis_config.min_tp_sl_score,
            max_unpriced_trade_pct=analysis_config.max_unpriced_trade_pct,
        ),
        conversion_rates=run_config.get("conversion_rates", {}),
        data_load_error=data_load_error,
    )
    baseline = baseline_comparison_summary(source, metrics, run_config)
    readiness = commercial_readiness_summary(
        source,
        equity,
        trades,
        pair_performance,
        monthly_pnl,
        monthly_target,
        drawdowns,
        periods,
        oos,
        cost,
        lot,
        monte_carlo,
        forward,
        walk_forward_path,
        trade_quality,
        analysis_config,
    )
    diagnosis = strategy_diagnosis_summary(
        metrics,
        pnl,
        pair_performance,
        monthly_pnl,
        monthly_target,
        drawdowns,
        oos,
        cost,
        baseline,
        paper_diff,
        readiness,
        trade_quality,
    )

    paths = {
        "pair_performance": destination / "pair_performance.csv",
        "monthly_pnl": destination / "monthly_pnl.csv",
        "monthly_target": destination / "monthly_target.csv",
        "drawdown_periods": destination / "drawdown_periods.csv",
        "period_performance": destination / "period_performance.csv",
        "cost_sensitivity": destination / "cost_sensitivity.csv",
        "pnl_breakdown": destination / "pnl_breakdown.csv",
        "pnl_by_side": destination / "pnl_by_side.csv",
        "pnl_by_hour": destination / "pnl_by_hour.csv",
        "pnl_by_pair": destination / "pnl_by_pair.csv",
        "pnl_by_strategy": destination / "pnl_by_strategy.csv",
        "pnl_breakdown_summary": destination / "pnl_breakdown_summary.json",
        "usable_segments": destination / "usable_segments.csv",
        "strategy_diagnosis": destination / "strategy_diagnosis.json",
        "baseline_comparison": destination / "baseline_comparison.csv",
        "paper_backtest_diff": destination / "paper_backtest_diff.json",
        "mfe_mae_by_trade": destination / "mfe_mae_by_trade.csv",
        "edge_segments": destination / "edge_segments.csv",
        "trade_expectancy": destination / "trade_expectancy.json",
        "sample_guard": destination / "sample_guard.json",
        "mfe_mae_summary": destination / "mfe_mae_summary.json",
        "tp_sl_score": destination / "tp_sl_score.json",
        "data_quality_monitor": destination / "data_quality_monitor.json",
        "ai_trade_decision": destination / "ai_trade_decision.json",
        "monte_carlo_quantiles": destination / "monte_carlo_quantiles.csv",
        "oos_summary": destination / "oos_summary.json",
        "lot_control": destination / "lot_control_summary.json",
        "monte_carlo": destination / "monte_carlo_summary.json",
        "forward_test": destination / "forward_test_summary.json",
        "commercial_readiness": destination / "commercial_readiness.json",
        "analysis_manifest": destination / "analysis_manifest.json",
    }

    pair_performance.to_csv(paths["pair_performance"], index=False)
    monthly_pnl.to_csv(paths["monthly_pnl"], index=False)
    monthly_target.to_csv(paths["monthly_target"], index=False)
    drawdowns.to_csv(paths["drawdown_periods"], index=False)
    periods.to_csv(paths["period_performance"], index=False)
    cost.to_csv(paths["cost_sensitivity"], index=False)
    pnl["breakdown"].to_csv(paths["pnl_breakdown"], index=False)
    pnl["by_side"].to_csv(paths["pnl_by_side"], index=False)
    pnl["by_hour"].to_csv(paths["pnl_by_hour"], index=False)
    pnl["by_pair"].to_csv(paths["pnl_by_pair"], index=False)
    pnl["by_strategy"].to_csv(paths["pnl_by_strategy"], index=False)
    monte_carlo["quantiles"].to_csv(paths["monte_carlo_quantiles"], index=False)
    _write_json(paths["pnl_breakdown_summary"], pnl["summary"])
    diagnosis["usable_segments"].to_csv(paths["usable_segments"], index=False)
    _write_json(paths["strategy_diagnosis"], diagnosis["summary"])
    baseline.to_csv(paths["baseline_comparison"], index=False)
    _write_json(paths["paper_backtest_diff"], paper_diff)
    trade_quality["by_trade"].to_csv(paths["mfe_mae_by_trade"], index=False)
    trade_quality["segments"].to_csv(paths["edge_segments"], index=False)
    _write_json(paths["trade_expectancy"], trade_quality["expectancy"])
    _write_json(paths["sample_guard"], trade_quality["sample_guard"])
    _write_json(paths["mfe_mae_summary"], trade_quality["mfe_mae"])
    _write_json(paths["tp_sl_score"], trade_quality["tp_sl"])
    _write_json(paths["data_quality_monitor"], trade_quality["data_quality"])
    _write_json(paths["ai_trade_decision"], trade_quality["ai_decision"])
    _write_json(paths["oos_summary"], oos)
    _write_json(paths["lot_control"], lot)
    _write_json(paths["monte_carlo"], monte_carlo["summary"])
    _write_json(paths["forward_test"], forward)
    _write_json(paths["commercial_readiness"], readiness)

    manifest = {
        "schema_version": 1,
        "source_run_dir": str(source),
        "analysis_config": asdict(analysis_config),
        "inputs": {
            "walk_forward_summary": str(walk_forward_path) if walk_forward_path else None,
            "forward_trades": str(forward_path) if forward_path else None,
        },
        "outputs": {key: str(path) for key, path in paths.items()},
    }
    _write_json(paths["analysis_manifest"], manifest)

    if write_html:
        html_path = destination / "index.html"
        write_analysis_dashboard(
            html_path,
            run_dir=source,
            metrics=metrics,
            readiness=readiness,
            pair_performance=pair_performance,
            monthly_pnl=monthly_pnl,
            monthly_target=monthly_target,
            drawdowns=drawdowns,
            cost_sensitivity=cost,
            pnl=pnl,
            diagnosis=diagnosis,
            baseline=baseline,
            trade_quality=trade_quality,
            oos=oos,
            monte_carlo=monte_carlo["summary"],
            forward=forward,
            html_rows=analysis_config.html_rows,
        )
        paths["dashboard"] = html_path

    return paths


def pair_performance_summary(trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "symbol",
        "trade_count",
        "net_pnl",
        "gross_pnl",
        "fees",
        "win_rate",
        "profit_factor",
        "expectancy_usd",
        "expectancy_r",
        "average_units",
        "largest_win",
        "largest_loss",
        "long_trades",
        "short_trades",
    ]
    if trades.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for symbol, group in trades.groupby("symbol", sort=True):
        net = group["net_pnl"].astype(float)
        wins = net[net > 0]
        losses = net[net < 0]
        gross_loss = abs(float(losses.sum()))
        gross_profit = float(wins.sum())
        rows.append(
            {
                "symbol": symbol,
                "trade_count": int(len(group)),
                "net_pnl": float(net.sum()),
                "gross_pnl": float(group["gross_pnl"].astype(float).sum()),
                "fees": float(group["fees"].astype(float).sum()),
                "win_rate": float((net > 0).mean()),
                "profit_factor": _profit_factor(gross_profit, gross_loss),
                "expectancy_usd": float(net.mean()),
                "expectancy_r": float(group["r_multiple"].astype(float).mean()),
                "average_units": float(group["units"].astype(float).mean()),
                "largest_win": float(wins.max()) if not wins.empty else 0.0,
                "largest_loss": abs(float(losses.min())) if not losses.empty else 0.0,
                "long_trades": int((group["direction"].astype(int) == 1).sum()),
                "short_trades": int((group["direction"].astype(int) == -1).sum()),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def monthly_pnl_summary(equity: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "month",
        "start_equity",
        "end_equity",
        "return_pct",
        "max_drawdown_pct",
        "trade_count",
        "net_pnl",
        "win_rate",
    ]
    if equity.empty:
        return pd.DataFrame(columns=columns)

    prepared_trades = trades.copy()
    if not prepared_trades.empty:
        prepared_trades["exit_month"] = pd.to_datetime(
            prepared_trades["exit_time"],
            errors="coerce",
        ).dt.to_period("M")

    rows: list[dict[str, Any]] = []
    for month, month_equity in equity.groupby(equity.index.to_period("M")):
        month_trades = (
            prepared_trades[prepared_trades["exit_month"] == month]
            if not prepared_trades.empty
            else prepared_trades
        )
        equity_values = month_equity["equity"].astype(float)
        start_equity = float(equity_values.iloc[0])
        end_equity = float(equity_values.iloc[-1])
        net = (
            month_trades["net_pnl"].astype(float)
            if not month_trades.empty
            else pd.Series(dtype=float)
        )
        rows.append(
            {
                "month": str(month),
                "start_equity": start_equity,
                "end_equity": end_equity,
                "return_pct": end_equity / start_equity - 1 if start_equity else 0.0,
                "max_drawdown_pct": _max_drawdown_pct(equity_values),
                "trade_count": int(len(month_trades)),
                "net_pnl": float(net.sum()) if not net.empty else 0.0,
                "win_rate": float((net > 0).mean()) if not net.empty else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def monthly_target_summary(
    monthly_pnl: pd.DataFrame,
    target_return_pct: float = 0.08,
) -> pd.DataFrame:
    columns = [
        "month",
        "return_pct",
        "target_return_pct",
        "target_met",
        "shortfall_pct",
        "surplus_pct",
        "max_drawdown_pct",
        "trade_count",
        "net_pnl",
    ]
    if monthly_pnl.empty:
        return pd.DataFrame(columns=columns)
    if target_return_pct <= 0:
        raise ValueError("target_return_pct must be positive")

    output = monthly_pnl.copy()
    output["return_pct"] = output["return_pct"].astype(float)
    output["target_return_pct"] = float(target_return_pct)
    output["target_met"] = output["return_pct"] >= float(target_return_pct)
    output["shortfall_pct"] = (float(target_return_pct) - output["return_pct"]).clip(lower=0.0)
    output["surplus_pct"] = (output["return_pct"] - float(target_return_pct)).clip(lower=0.0)
    return output.reindex(columns=columns)


def drawdown_periods(equity: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "peak_time",
        "start_time",
        "valley_time",
        "recovery_time",
        "recovered",
        "peak_equity",
        "valley_equity",
        "drawdown_pct",
        "drawdown_usd",
        "underwater_days",
        "recovery_days",
    ]
    if equity.empty:
        return pd.DataFrame(columns=columns)

    values = equity["equity"].astype(float)
    peak_time = values.index[0]
    peak_equity = float(values.iloc[0])
    active: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []

    for timestamp, value in values.items():
        current_equity = float(value)
        if current_equity >= peak_equity:
            if active is not None:
                active["recovery_time"] = timestamp
                active["recovered"] = True
                active["underwater_days"] = _days_between(active["peak_time"], timestamp)
                active["recovery_days"] = _days_between(active["valley_time"], timestamp)
                rows.append(active)
                active = None
            peak_time = timestamp
            peak_equity = current_equity
            continue

        drawdown_pct = abs(current_equity / peak_equity - 1) if peak_equity else 0.0
        drawdown_usd = peak_equity - current_equity
        if active is None:
            active = {
                "peak_time": peak_time,
                "start_time": timestamp,
                "valley_time": timestamp,
                "recovery_time": None,
                "recovered": False,
                "peak_equity": peak_equity,
                "valley_equity": current_equity,
                "drawdown_pct": drawdown_pct,
                "drawdown_usd": drawdown_usd,
                "underwater_days": None,
                "recovery_days": None,
            }
        elif drawdown_pct > float(active["drawdown_pct"]):
            active["valley_time"] = timestamp
            active["valley_equity"] = current_equity
            active["drawdown_pct"] = drawdown_pct
            active["drawdown_usd"] = drawdown_usd

    if active is not None:
        final_time = values.index[-1]
        active["underwater_days"] = _days_between(active["peak_time"], final_time)
        rows.append(active)

    output = pd.DataFrame(rows, columns=columns)
    if output.empty:
        return output
    return output.sort_values("drawdown_pct", ascending=False).reset_index(drop=True)


def period_performance_summary(equity: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "period_type",
        "period",
        "start",
        "end",
        "days",
        "trade_count",
        "net_pnl",
        "return_pct",
        "max_drawdown_pct",
    ]
    if equity.empty:
        return pd.DataFrame(columns=columns)

    prepared_trades = trades.copy()
    if not prepared_trades.empty:
        prepared_trades["exit_time"] = pd.to_datetime(prepared_trades["exit_time"], errors="coerce")

    rows: list[dict[str, Any]] = []
    for period_type, freq in (("year", "Y"), ("quarter", "Q"), ("month", "M")):
        for period, period_equity in equity.groupby(equity.index.to_period(freq)):
            start = period_equity.index[0]
            end = period_equity.index[-1]
            period_trades = _trades_between(prepared_trades, start, end, "exit_time")
            start_equity = float(period_equity["equity"].iloc[0])
            end_equity = float(period_equity["equity"].iloc[-1])
            rows.append(
                {
                    "period_type": period_type,
                    "period": str(period),
                    "start": start,
                    "end": end,
                    "days": _days_between(start, end),
                    "trade_count": int(len(period_trades)),
                    "net_pnl": (
                        float(period_trades["net_pnl"].astype(float).sum())
                        if not period_trades.empty
                        else 0.0
                    ),
                    "return_pct": end_equity / start_equity - 1 if start_equity else 0.0,
                    "max_drawdown_pct": _max_drawdown_pct(period_equity["equity"].astype(float)),
                }
            )
    return pd.DataFrame(rows, columns=columns)


def out_of_sample_summary(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
    oos_ratio: float,
) -> dict[str, Any]:
    if equity.empty:
        return {
            "available": False,
            "reason": "equity_curve is empty",
            "split_time": None,
            "in_sample": {},
            "out_of_sample": {},
        }
    if not 0 < oos_ratio < 1:
        raise ValueError("oos_ratio must be between 0 and 1")

    split_position = max(1, min(len(equity) - 1, int(len(equity) * (1 - oos_ratio))))
    split_time = equity.index[split_position]
    in_equity = equity.loc[equity.index < split_time]
    out_equity = equity.loc[equity.index >= split_time]
    in_trades = _trades_before(trades, split_time, "entry_time")
    out_trades = _trades_at_or_after(trades, split_time, "entry_time")

    return {
        "available": not in_equity.empty and not out_equity.empty,
        "split_time": _json_value(split_time),
        "oos_ratio": oos_ratio,
        "in_sample": _metrics_for_window(in_equity, in_trades, initial_cash),
        "out_of_sample": _metrics_for_window(
            out_equity,
            out_trades,
            float(out_equity["equity"].iloc[0]) if not out_equity.empty else initial_cash,
        ),
    }


def cost_sensitivity_summary(
    trades: pd.DataFrame,
    run_config: dict[str, Any],
    multipliers: tuple[float, ...],
) -> pd.DataFrame:
    columns = [
        "spread_multiplier",
        "slippage_multiplier",
        "adjusted_net_pnl",
        "delta_vs_baseline",
        "adjusted_profit_factor",
        "trade_count",
        "unsupported_cost_rows",
    ]
    if trades.empty:
        return pd.DataFrame(
            [
                {
                    "spread_multiplier": spread_multiplier,
                    "slippage_multiplier": slippage_multiplier,
                    "adjusted_net_pnl": 0.0,
                    "delta_vs_baseline": 0.0,
                    "adjusted_profit_factor": 0.0,
                    "trade_count": 0,
                    "unsupported_cost_rows": 0,
                }
                for spread_multiplier in multipliers
                for slippage_multiplier in multipliers
            ],
            columns=columns,
        )

    conversion_rates = {
        str(key).upper(): float(value)
        for key, value in run_config.get("conversion_rates", {}).items()
    }
    base_net = trades["net_pnl"].astype(float)
    spread_cost = trades.apply(
        lambda row: _trade_spread_cost_usd(row, conversion_rates),
        axis=1,
    )
    slippage_cost = trades.apply(
        lambda row: _trade_slippage_cost_usd(row, conversion_rates),
        axis=1,
    )
    unsupported = int(spread_cost.isna().sum() + slippage_cost.isna().sum())
    spread_cost = spread_cost.fillna(0.0)
    slippage_cost = slippage_cost.fillna(0.0)

    rows: list[dict[str, Any]] = []
    baseline = float(base_net.sum())
    for spread_multiplier in multipliers:
        for slippage_multiplier in multipliers:
            adjusted = (
                base_net
                - (spread_multiplier - 1.0) * spread_cost
                - (slippage_multiplier - 1.0) * slippage_cost
            )
            wins = adjusted[adjusted > 0]
            losses = adjusted[adjusted < 0]
            gross_profit = float(wins.sum())
            gross_loss = abs(float(losses.sum()))
            adjusted_net = float(adjusted.sum())
            rows.append(
                {
                    "spread_multiplier": float(spread_multiplier),
                    "slippage_multiplier": float(slippage_multiplier),
                    "adjusted_net_pnl": adjusted_net,
                    "delta_vs_baseline": adjusted_net - baseline,
                    "adjusted_profit_factor": _profit_factor(gross_profit, gross_loss),
                    "trade_count": int(len(trades)),
                    "unsupported_cost_rows": unsupported,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def pnl_breakdown_summary(trades: pd.DataFrame, run_config: dict[str, Any]) -> dict[str, Any]:
    enriched = _enriched_pnl_trades(trades, run_config)
    breakdown_columns = ["component", "amount_usd", "description"]
    group_columns = [
        "group",
        "trade_count",
        "pre_cost_pnl",
        "spread_loss",
        "slippage_loss",
        "commission",
        "swap",
        "net_pnl",
        "win_rate",
    ]
    if enriched.empty:
        empty_groups = pd.DataFrame(columns=group_columns)
        return {
            "summary": {
                "trade_count": 0,
                "total_net_pnl": 0.0,
                "pre_cost_pnl": 0.0,
                "spread_loss": 0.0,
                "slippage_loss": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "execution_gross_pnl": 0.0,
                "winning_trade_profit": 0.0,
                "losing_trade_loss": 0.0,
                "unsupported_cost_rows": 0,
                "swap_modeled": False,
            },
            "breakdown": pd.DataFrame(columns=breakdown_columns),
            "by_side": empty_groups.copy(),
            "by_hour": empty_groups.copy(),
            "by_pair": empty_groups.copy(),
            "by_strategy": empty_groups.copy(),
        }

    summary = {
        "trade_count": int(len(enriched)),
        "total_net_pnl": float(enriched["net_pnl"].sum()),
        "pre_cost_pnl": float(enriched["pre_cost_pnl"].sum()),
        "spread_loss": float(enriched["spread_loss"].sum()),
        "slippage_loss": float(enriched["slippage_loss"].sum()),
        "commission": float(enriched["commission"].sum()),
        "swap": float(enriched["swap"].sum()),
        "execution_gross_pnl": float(enriched["gross_pnl"].sum()),
        "winning_trade_profit": float(enriched.loc[enriched["net_pnl"] > 0, "net_pnl"].sum()),
        "losing_trade_loss": abs(float(enriched.loc[enriched["net_pnl"] < 0, "net_pnl"].sum())),
        "unsupported_cost_rows": int(enriched["cost_supported"].eq(False).sum()),
        "swap_modeled": bool(enriched["swap_modeled"].any()),
    }
    breakdown = pd.DataFrame(
        [
            {
                "component": "pre_cost_pnl",
                "amount_usd": summary["pre_cost_pnl"],
                "description": "PnL reconstructed before spread, slippage, commission, and swap.",
            },
            {
                "component": "spread_loss",
                "amount_usd": summary["spread_loss"],
                "description": "Estimated spread cost. Negative values reduce PnL.",
            },
            {
                "component": "slippage_loss",
                "amount_usd": summary["slippage_loss"],
                "description": "Estimated adverse slippage cost. Negative values reduce PnL.",
            },
            {
                "component": "commission",
                "amount_usd": summary["commission"],
                "description": "Broker commission and fixed/minimum fees. Negative values reduce PnL.",
            },
            {
                "component": "swap",
                "amount_usd": summary["swap"],
                "description": "Swap/financing. Zero unless swap_usd or swap is present in trade_log.",
            },
            {
                "component": "total_net_pnl",
                "amount_usd": summary["total_net_pnl"],
                "description": "Final modeled PnL after all available costs.",
            },
        ],
        columns=breakdown_columns,
    )
    return {
        "summary": summary,
        "breakdown": breakdown,
        "by_side": _aggregate_pnl_group(enriched, "side_label"),
        "by_hour": _aggregate_pnl_group(enriched, "entry_hour"),
        "by_pair": _aggregate_pnl_group(enriched, "symbol"),
        "by_strategy": _aggregate_pnl_group(enriched, "strategy"),
    }


def strategy_diagnosis_summary(
    metrics: dict[str, Any],
    pnl: dict[str, Any],
    pair_performance: pd.DataFrame,
    monthly_pnl: pd.DataFrame,
    monthly_target: pd.DataFrame,
    drawdowns: pd.DataFrame,
    oos: dict[str, Any],
    cost_sensitivity: pd.DataFrame,
    baseline: pd.DataFrame,
    paper_diff: dict[str, Any],
    readiness: dict[str, Any],
    trade_quality: dict[str, Any],
) -> dict[str, Any]:
    pnl_summary = pnl["summary"]
    net_pnl = float(pnl_summary["total_net_pnl"])
    pre_cost_pnl = float(pnl_summary["pre_cost_pnl"])
    total_cost = (
        float(pnl_summary["spread_loss"])
        + float(pnl_summary["slippage_loss"])
        + float(pnl_summary["commission"])
        + float(pnl_summary["swap"])
    )
    findings: list[dict[str, Any]] = []

    if net_pnl < 0 and pre_cost_pnl < 0:
        findings.append(
            _finding(
                "strategy_edge_negative",
                "high",
                "The strategy is negative before modeled costs.",
                {"pre_cost_pnl": pre_cost_pnl, "net_pnl": net_pnl},
            )
        )
    elif net_pnl < 0 and pre_cost_pnl > 0:
        findings.append(
            _finding(
                "cost_drag_negative",
                "high",
                "The raw edge is positive but costs turn the system negative.",
                {"pre_cost_pnl": pre_cost_pnl, "total_cost": total_cost, "net_pnl": net_pnl},
            )
        )

    if not monthly_target.empty:
        missed = monthly_target[monthly_target["target_met"].astype(bool).eq(False)]
        if not missed.empty:
            findings.append(
                _finding(
                    "monthly_target_missed",
                    "high",
                    "One or more months did not meet the monthly return target.",
                    {
                        "months_missed": int(len(missed)),
                        "months_total": int(len(monthly_target)),
                        "target_return_pct": float(monthly_target["target_return_pct"].iloc[0]),
                        "worst_shortfall_pct": float(missed["shortfall_pct"].astype(float).max()),
                        "missed_months": missed[
                            ["month", "return_pct", "shortfall_pct", "trade_count"]
                        ].to_dict(orient="records"),
                    },
                )
            )

    if not pair_performance.empty:
        losing_pairs = pair_performance[pair_performance["net_pnl"].astype(float) < 0]
        if not losing_pairs.empty:
            findings.append(
                _finding(
                    "pair_concentration_loss",
                    "medium",
                    "Some pairs lose money and should be filtered or redesigned.",
                    {
                        "losing_pairs": losing_pairs[["symbol", "net_pnl", "trade_count"]].to_dict(
                            orient="records"
                        )
                    },
                )
            )

    by_side = pnl["by_side"]
    if not by_side.empty and (by_side["net_pnl"].astype(float) < 0).any():
        findings.append(
            _finding(
                "directional_asymmetry",
                "medium",
                "Long and short performance is asymmetric.",
                {"by_side": by_side.to_dict(orient="records")},
            )
        )

    by_hour = pnl["by_hour"]
    if not by_hour.empty:
        losing_hours = by_hour[by_hour["net_pnl"].astype(float) < 0]
        if not losing_hours.empty:
            findings.append(
                _finding(
                    "time_of_day_decay",
                    "medium",
                    "Some entry hours have negative PnL.",
                    {
                        "losing_hours": losing_hours[["group", "net_pnl", "trade_count"]].to_dict(
                            orient="records"
                        )
                    },
                )
            )

    stressed = _cost_row(cost_sensitivity, 1.5, 1.5)
    if stressed and float(stressed["adjusted_net_pnl"]) < net_pnl:
        findings.append(
            _finding(
                "cost_sensitivity",
                "medium",
                "The system deteriorates under 1.5x spread/slippage.",
                {"stressed_1_5x_net_pnl": float(stressed["adjusted_net_pnl"])},
            )
        )

    strategy_row = _baseline_row(baseline, "strategy")
    random_row = _baseline_row(baseline, "random_direction_baseline")
    if (
        strategy_row
        and random_row
        and float(strategy_row["net_pnl"]) <= float(random_row["net_pnl"])
    ):
        findings.append(
            _finding(
                "baseline_underperformance",
                "high",
                "The strategy does not beat the random baseline on net PnL.",
                {
                    "strategy_net_pnl": float(strategy_row["net_pnl"]),
                    "random_net_pnl": float(random_row["net_pnl"]),
                },
            )
        )

    oos_trades = int(oos.get("out_of_sample", {}).get("trade_count", 0))
    if oos_trades < 30:
        findings.append(
            _finding(
                "oos_sample_too_small",
                "high",
                "Out-of-sample trade count is too small for commercial judgment.",
                {"oos_trade_count": oos_trades},
            )
        )

    if paper_diff.get("status") == "not_provided":
        findings.append(
            _finding(
                "paper_trading_missing",
                "high",
                "Paper trading or forward evidence is missing.",
                paper_diff,
            )
        )

    quality_decision = trade_quality["ai_decision"]
    if not bool(quality_decision.get("deployable")):
        findings.append(
            _finding(
                "trade_decision_quality_blocked",
                "high",
                "Trade-level expectancy, sample, TP/SL, or data-quality gates block AI deployment.",
                quality_decision,
            )
        )

    expectancy = trade_quality["expectancy"]
    if expectancy.get("status") in {"negative_or_zero", "weak_positive"}:
        findings.append(
            _finding(
                "expectancy_not_confirmed",
                "high",
                "Expected value is not confirmed by confidence-adjusted R-multiple analysis.",
                expectancy,
            )
        )

    mfe_mae = trade_quality["mfe_mae"]
    if (
        float(mfe_mae.get("avg_mfe_r", 0.0)) >= 1.0
        and float(mfe_mae.get("avg_capture_efficiency", 0.0)) < 0.35
    ):
        findings.append(
            _finding(
                "low_mfe_capture",
                "medium",
                "Trades often produce favorable excursion, but exits capture too little of it.",
                mfe_mae,
            )
        )

    if float(mfe_mae.get("mae_reached_1r_rate", 0.0)) > 0.35:
        findings.append(
            _finding(
                "high_mae_pressure",
                "medium",
                "A large share of trades reaches at least 1R adverse excursion.",
                mfe_mae,
            )
        )

    if not drawdowns.empty:
        worst = drawdowns.iloc[0].to_dict()
        findings.append(
            _finding(
                "drawdown_profile",
                "info",
                "Worst drawdown period is tracked for recovery risk.",
                worst,
            )
        )

    usable_segments = usable_segments_summary(
        pnl["by_pair"],
        pnl["by_hour"],
        pnl["by_side"],
        pnl["by_strategy"],
    )
    return {
        "summary": {
            "commercial_ready": readiness["commercial_ready"],
            "net_pnl": net_pnl,
            "pre_cost_pnl": pre_cost_pnl,
            "total_modeled_cost": total_cost,
            "primary_cause": _primary_cause(findings),
            "findings": findings,
            "suggested_filters": _suggested_filters(usable_segments),
        },
        "usable_segments": usable_segments,
    }


def usable_segments_summary(
    by_pair: pd.DataFrame,
    by_hour: pd.DataFrame,
    by_side: pd.DataFrame,
    by_strategy: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for segment_type, frame in (
        ("pair", by_pair),
        ("hour", by_hour),
        ("side", by_side),
        ("strategy", by_strategy),
    ):
        if frame.empty:
            continue
        for _, row in frame.iterrows():
            net_pnl = float(row["net_pnl"])
            trade_count = int(row["trade_count"])
            keep = bool(net_pnl > 0 and trade_count >= 2)
            rows.append(
                {
                    "segment_type": segment_type,
                    "segment": str(row["group"]),
                    "decision": "keep" if keep else "block",
                    "trade_count": trade_count,
                    "net_pnl": net_pnl,
                    "pre_cost_pnl": float(row["pre_cost_pnl"]),
                    "win_rate": float(row["win_rate"]),
                    "reason": (
                        "positive net_pnl with enough trades"
                        if keep
                        else "negative/weak net_pnl or too few trades"
                    ),
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "segment_type",
            "segment",
            "decision",
            "trade_count",
            "net_pnl",
            "pre_cost_pnl",
            "win_rate",
            "reason",
        ],
    )


def baseline_comparison_summary(
    run_dir: Path,
    strategy_metrics: dict[str, Any],
    run_config: dict[str, Any],
) -> pd.DataFrame:
    columns = [
        "name",
        "trade_count",
        "net_pnl",
        "total_return_pct",
        "max_drawdown_pct",
        "profit_factor",
        "win_rate",
        "expectancy_usd",
    ]
    rows = [_metric_row("strategy", strategy_metrics)]
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return pd.DataFrame(rows, columns=columns)

    try:
        manifest = _read_json(manifest_path)
        data_paths = [item["path"] for item in manifest["inputs"]["data"]]
        events_path = (manifest.get("inputs", {}).get("events", {}) or {}).get("path")
        data = load_price_csvs(data_paths)
        events = load_economic_events_csv(events_path)
        config = _config_from_dict(run_config)
        for baseline in (
            FlatStrategy(),
            BuyAndHoldLongStrategy(),
            RandomDirectionStrategy(seed=17),
        ):
            result = BacktestEngine(baseline, config, events).run(data)
            rows.append(_metric_row(baseline.name, result.metrics))
    except Exception as error:  # pragma: no cover - defensive artifact reporting
        rows.append(
            {
                "name": "baseline_error",
                "trade_count": 0,
                "net_pnl": 0.0,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "win_rate": 0.0,
                "expectancy_usd": 0.0,
                "error": str(error),
            }
        )
    return pd.DataFrame(rows).reindex(columns=columns + ["error"])


def paper_backtest_diff_summary(
    backtest_trades: pd.DataFrame,
    forward_trades_path: Path | None,
) -> dict[str, Any]:
    backtest_summary = _trade_summary(backtest_trades)
    if forward_trades_path is None:
        return {
            "status": "not_provided",
            "backtest": backtest_summary,
            "forward": None,
            "diff": None,
        }
    forward_trades = _read_trades(forward_trades_path)
    forward_summary = _trade_summary(forward_trades)
    return {
        "status": "provided",
        "path": str(forward_trades_path),
        "backtest": backtest_summary,
        "forward": forward_summary,
        "diff": {
            key: forward_summary[key] - backtest_summary[key]
            for key in (
                "net_pnl",
                "expectancy_usd",
                "win_rate",
                "average_spread_pips",
                "average_slippage_pips",
            )
        },
    }


def lot_control_summary(trades: pd.DataFrame, run_config: dict[str, Any]) -> dict[str, Any]:
    risk = run_config.get("risk", {})
    units = trades["units"].astype(float) if not trades.empty else pd.Series(dtype=float)
    return {
        "risk_per_trade_pct": risk.get("risk_per_trade_pct"),
        "risk_cap_pct": risk.get("risk_cap_pct"),
        "max_leverage": risk.get("max_leverage"),
        "max_position_units": risk.get("max_position_units"),
        "allow_fractional_units": bool(risk.get("allow_fractional_units", False)),
        "trade_count": int(len(trades)),
        "average_units": float(units.mean()) if not units.empty else 0.0,
        "max_units": float(units.max()) if not units.empty else 0.0,
        "min_units": float(units.min()) if not units.empty else 0.0,
        "unit_std": float(units.std(ddof=0)) if len(units) > 1 else 0.0,
        "appears_fixed_lot": bool(units.nunique() <= 1) if not units.empty else False,
        "uses_risk_percent_sizing": risk.get("risk_per_trade_pct") is not None
        and risk.get("risk_cap_pct") is not None,
    }


def monte_carlo_summary(
    trades: pd.DataFrame,
    initial_cash: float,
    paths: int,
    seed: int,
    ruin_threshold_pct: float,
) -> dict[str, Any]:
    quantile_columns = ["metric", "q05", "q25", "q50", "q75", "q95"]
    if paths <= 0:
        raise ValueError("monte_carlo_paths must be positive")
    if not 0 < ruin_threshold_pct < 1:
        raise ValueError("ruin_threshold_pct must be between 0 and 1")
    if trades.empty:
        return {
            "summary": {
                "paths": int(paths),
                "seed": int(seed),
                "trade_count": 0,
                "ruin_threshold_pct": ruin_threshold_pct,
                "ruin_probability": 0.0,
                "median_final_equity": initial_cash,
                "p05_final_equity": initial_cash,
                "p95_final_equity": initial_cash,
                "median_max_drawdown_pct": 0.0,
                "p95_max_drawdown_pct": 0.0,
            },
            "quantiles": pd.DataFrame(columns=quantile_columns),
        }

    pnl = trades["net_pnl"].astype(float).to_numpy()
    rng = np.random.default_rng(seed)
    samples = rng.choice(pnl, size=(paths, len(pnl)), replace=True)
    equity_paths = initial_cash + np.cumsum(samples, axis=1)
    initial_column = np.full((paths, 1), initial_cash)
    full_paths = np.concatenate([initial_column, equity_paths], axis=1)
    running_max = np.maximum.accumulate(full_paths, axis=1)
    drawdowns = full_paths / running_max - 1
    max_drawdown = np.abs(np.min(drawdowns, axis=1))
    final_equity = full_paths[:, -1]
    min_equity = np.min(full_paths, axis=1)
    ruin_level = initial_cash * (1 - ruin_threshold_pct)
    ruin_probability = float(np.mean(min_equity <= ruin_level))

    quantiles = pd.DataFrame(
        [
            _quantile_row("final_equity", final_equity),
            _quantile_row("min_equity", min_equity),
            _quantile_row("max_drawdown_pct", max_drawdown),
        ],
        columns=quantile_columns,
    )
    return {
        "summary": {
            "paths": int(paths),
            "seed": int(seed),
            "trade_count": int(len(pnl)),
            "ruin_threshold_pct": ruin_threshold_pct,
            "ruin_probability": ruin_probability,
            "median_final_equity": float(np.quantile(final_equity, 0.50)),
            "p05_final_equity": float(np.quantile(final_equity, 0.05)),
            "p95_final_equity": float(np.quantile(final_equity, 0.95)),
            "median_max_drawdown_pct": float(np.quantile(max_drawdown, 0.50)),
            "p95_max_drawdown_pct": float(np.quantile(max_drawdown, 0.95)),
        },
        "quantiles": quantiles,
    }


def forward_test_summary(
    forward_trades_path: Path | None,
    min_forward_days: int,
) -> dict[str, Any]:
    if forward_trades_path is None:
        return {
            "status": "not_provided",
            "passed": False,
            "path": None,
            "reason": "No forward trade log was provided. Backtest-only evidence is not sale-ready.",
        }
    trades = _read_trades(forward_trades_path)
    if trades.empty:
        return {
            "status": "empty",
            "passed": False,
            "path": str(forward_trades_path),
            "reason": "Forward trade log has no trades.",
        }
    start = pd.to_datetime(trades["entry_time"], errors="coerce").min()
    end = pd.to_datetime(trades["exit_time"], errors="coerce").max()
    days = _days_between(start, end)
    net_pnl = float(trades["net_pnl"].astype(float).sum())
    return {
        "status": "provided",
        "passed": bool(days >= min_forward_days and len(trades) > 0),
        "path": str(forward_trades_path),
        "start": _json_value(start),
        "end": _json_value(end),
        "days": days,
        "trade_count": int(len(trades)),
        "net_pnl": net_pnl,
        "minimum_required_days": int(min_forward_days),
    }


def commercial_readiness_summary(
    run_dir: Path,
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    pair_performance: pd.DataFrame,
    monthly_pnl: pd.DataFrame,
    monthly_target: pd.DataFrame,
    drawdowns: pd.DataFrame,
    periods: pd.DataFrame,
    oos: dict[str, Any],
    cost: pd.DataFrame,
    lot: dict[str, Any],
    monte_carlo: dict[str, Any],
    forward: dict[str, Any],
    walk_forward_summary_path: Path | None,
    trade_quality: dict[str, Any],
    config: RunAnalysisConfig,
) -> dict[str, Any]:
    audit = audit_run_artifacts(run_dir)
    coverage_days = _days_between(equity.index[0], equity.index[-1]) if not equity.empty else 0.0
    month_count = int(len(monthly_pnl))
    target_month_count = int(len(monthly_target))
    target_months_met = (
        int(monthly_target["target_met"].astype(bool).sum()) if not monthly_target.empty else 0
    )
    target_months_missed = target_month_count - target_months_met
    worst_monthly_shortfall = (
        float(monthly_target["shortfall_pct"].astype(float).max())
        if not monthly_target.empty
        else 0.0
    )
    pair_count = int(len(pair_performance))
    oos_trade_count = int(oos.get("out_of_sample", {}).get("trade_count", 0))
    walk_forward_folds = _walk_forward_fold_count(walk_forward_summary_path)
    stressed = _cost_row(cost, 1.5, 1.5)
    stressed_net = float(stressed["adjusted_net_pnl"]) if stressed else 0.0
    mc_summary = monte_carlo["summary"]
    quality_decision = trade_quality["ai_decision"]
    sample_guard = trade_quality["sample_guard"]
    expectancy = trade_quality["expectancy"]
    tp_sl = trade_quality["tp_sl"]
    data_quality = trade_quality["data_quality"]

    gates = [
        _gate(
            "artifact_audit",
            bool(audit["passed"]),
            "required",
            "Backtest artifacts must pass structural audit.",
            audit,
        ),
        _gate(
            "data_quality_monitor",
            bool(data_quality.get("passed")),
            "required",
            "Price QA and trade-window coverage must be valid for trade-level scoring.",
            data_quality,
        ),
        _gate(
            "sample_guard",
            bool(sample_guard.get("passed")),
            "required",
            f"Trade-level expectancy requires at least {config.min_expectancy_trades} trades.",
            sample_guard,
        ),
        _gate(
            "expectancy_edge",
            bool(expectancy.get("passed")),
            "required",
            "R-multiple expectancy must remain positive after bootstrap confidence adjustment.",
            expectancy,
        ),
        _gate(
            "tp_sl_score",
            bool(tp_sl.get("passed")),
            "advisory",
            f"TP/SL behavior score should be at least {config.min_tp_sl_score:.1f}.",
            tp_sl,
        ),
        _gate(
            "ai_trade_decision",
            bool(quality_decision.get("deployable")),
            "required",
            "AI trade decision quality gate must be deployable.",
            quality_decision,
        ),
        _gate(
            "multiple_periods",
            coverage_days >= config.min_period_days,
            "required",
            f"Coverage must be at least {config.min_period_days} days.",
            {"coverage_days": coverage_days},
        ),
        _gate(
            "out_of_sample",
            bool(oos.get("available")) and oos_trade_count >= config.min_oos_trades,
            "required",
            f"OOS section must have at least {config.min_oos_trades} trades.",
            {"split_time": oos.get("split_time"), "oos_trade_count": oos_trade_count},
        ),
        _gate(
            "walk_forward",
            walk_forward_folds >= config.min_walk_forward_folds,
            "required",
            f"Walk-forward must have at least {config.min_walk_forward_folds} folds.",
            {
                "path": str(walk_forward_summary_path) if walk_forward_summary_path else None,
                "folds": walk_forward_folds,
            },
        ),
        _gate(
            "pair_performance",
            pair_count >= 2,
            "required",
            "Pair-level performance must be available for at least two pairs.",
            {"pair_count": pair_count},
        ),
        _gate(
            "monthly_pnl",
            month_count >= 12,
            "required",
            "Monthly PnL should cover at least 12 months for sale-grade evidence.",
            {"month_count": month_count},
        ),
        _gate(
            "monthly_return_target",
            target_month_count > 0 and target_months_missed == 0,
            "required",
            f"Every evaluated month must meet the {config.monthly_target_return_pct:.2%} return target.",
            {
                "target_return_pct": config.monthly_target_return_pct,
                "months_evaluated": target_month_count,
                "months_met": target_months_met,
                "months_missed": target_months_missed,
                "worst_shortfall_pct": worst_monthly_shortfall,
            },
        ),
        _gate(
            "drawdown_duration",
            not drawdowns.empty,
            "required",
            "Drawdown periods and recovery time must be calculated.",
            {"drawdown_periods": int(len(drawdowns))},
        ),
        _gate(
            "cost_sensitivity",
            not cost.empty,
            "required",
            "Spread and slippage sensitivity matrix must be calculated.",
            {"rows": int(len(cost)), "stressed_1_5x_net_pnl": stressed_net},
        ),
        _gate(
            "cost_robustness",
            stressed_net > 0,
            "advisory",
            "1.5x spread and 1.5x slippage should remain profitable.",
            {"stressed_1_5x_net_pnl": stressed_net},
        ),
        _gate(
            "lot_control",
            bool(lot.get("uses_risk_percent_sizing")) and not bool(lot.get("appears_fixed_lot")),
            "required",
            "Lot sizing must use risk-percent controls rather than a fixed lot.",
            lot,
        ),
        _gate(
            "monte_carlo",
            int(mc_summary["paths"]) >= config.min_monte_carlo_paths
            and float(mc_summary["ruin_probability"]) <= config.max_acceptable_ruin_probability,
            "required",
            "Monte Carlo paths and ruin probability must be within thresholds.",
            {
                "paths": mc_summary["paths"],
                "ruin_probability": mc_summary["ruin_probability"],
            },
        ),
        _gate(
            "forward_test",
            bool(forward.get("passed")),
            "required",
            "Forward or paper test evidence is mandatory before sale or live allocation.",
            forward,
        ),
    ]
    required_gates = [gate for gate in gates if gate["severity"] == "required"]
    return {
        "commercial_ready": all(bool(gate["passed"]) for gate in required_gates),
        "required_passed": sum(1 for gate in required_gates if gate["passed"]),
        "required_total": len(required_gates),
        "trade_count": int(len(trades)),
        "coverage_days": coverage_days,
        "monthly_target_return_pct": config.monthly_target_return_pct,
        "monthly_target_months_met": target_months_met,
        "monthly_target_months_missed": target_months_missed,
        "gates": gates,
    }


def write_analysis_dashboard(
    path: str | Path,
    *,
    run_dir: Path,
    metrics: dict[str, Any],
    readiness: dict[str, Any],
    pair_performance: pd.DataFrame,
    monthly_pnl: pd.DataFrame,
    monthly_target: pd.DataFrame,
    drawdowns: pd.DataFrame,
    cost_sensitivity: pd.DataFrame,
    pnl: dict[str, Any],
    diagnosis: dict[str, Any],
    baseline: pd.DataFrame,
    trade_quality: dict[str, Any],
    oos: dict[str, Any],
    monte_carlo: dict[str, Any],
    forward: dict[str, Any],
    html_rows: int = 12,
) -> None:
    destination = Path(path)
    status = "READY" if readiness["commercial_ready"] else "BLOCKED"
    status_class = "good" if readiness["commercial_ready"] else "bad"
    gates = pd.DataFrame(readiness["gates"])
    pnl_summary = pnl["summary"]
    diagnosis_summary = diagnosis["summary"]
    expectancy = trade_quality["expectancy"]
    sample_guard = trade_quality["sample_guard"]
    mfe_mae = trade_quality["mfe_mae"]
    tp_sl = trade_quality["tp_sl"]
    data_quality = trade_quality["data_quality"]
    ai_decision = trade_quality["ai_decision"]
    target_months = int(len(monthly_target))
    target_months_met = int(monthly_target["target_met"].astype(bool).sum()) if target_months else 0
    target_label = f"{target_months_met}/{target_months}" if target_months else "0/0"
    cards = [
        (
            "Final equity",
            _money(metrics.get("final_equity", 0.0)),
            "Initial: " + _money(metrics.get("initial_cash", 0.0)),
        ),
        (
            "Total return",
            _pct(metrics.get("total_return_pct", 0.0)),
            "Annualized: " + _pct(metrics.get("annualized_return_pct", 0.0)),
        ),
        (
            "Max drawdown",
            _pct(metrics.get("max_drawdown_pct", 0.0)),
            _money(metrics.get("max_drawdown_usd", 0.0)),
        ),
        (
            "Trades",
            str(metrics.get("trade_count", 0)),
            "Win rate: " + _pct(metrics.get("win_rate", 0.0)),
        ),
        (
            "Profit factor",
            _number(metrics.get("profit_factor", 0.0)),
            "Expectancy: " + _money(metrics.get("expectancy_usd", 0.0)),
        ),
        (
            "EV confidence",
            str(expectancy.get("status", "unknown")),
            "R CI low: " + _number(expectancy.get("expectancy_ci_low", 0.0)),
        ),
        (
            "Sample guard",
            str(sample_guard.get("status", "unknown")),
            "Weight: " + _pct(sample_guard.get("confidence_weight", 0.0)),
        ),
        (
            "TP/SL score",
            _number(tp_sl.get("score", 0.0)),
            "Minimum: " + _number(tp_sl.get("minimum_score", 0.0)),
        ),
        (
            "MFE / MAE",
            _number(mfe_mae.get("mfe_to_mae_ratio", 0.0)),
            "Avg MFE: " + _number(mfe_mae.get("avg_mfe_r", 0.0)) + "R",
        ),
        (
            "Data quality",
            str(data_quality.get("status", "unknown")),
            "Pricing: " + _pct(data_quality.get("pricing_coverage", 0.0)),
        ),
        (
            "AI decision",
            str(ai_decision.get("verdict", "unknown")),
            "Deployable: " + str(ai_decision.get("deployable", False)),
        ),
        (
            "MC ruin probability",
            _pct(monte_carlo.get("ruin_probability", 0.0)),
            f"Paths: {monte_carlo.get('paths', 0)}",
        ),
        (
            "OOS trades",
            str(oos.get("out_of_sample", {}).get("trade_count", 0)),
            "Split: " + str(oos.get("split_time")),
        ),
        (
            "Monthly target",
            target_label,
            "Target: " + _pct(readiness.get("monthly_target_return_pct", 0.08)),
        ),
        ("Forward test", str(forward.get("status", "missing")), "Required before sale"),
        (
            "Net PnL",
            _money(pnl_summary.get("total_net_pnl", 0.0)),
            "After modeled costs",
        ),
        (
            "Pre-cost PnL",
            _money(pnl_summary.get("pre_cost_pnl", 0.0)),
            "Before spread/slippage/fees",
        ),
        (
            "Spread loss",
            _money(pnl_summary.get("spread_loss", 0.0)),
            "Estimated from fills",
        ),
        (
            "Slippage loss",
            _money(pnl_summary.get("slippage_loss", 0.0)),
            "Always adverse",
        ),
        (
            "Primary cause",
            str(diagnosis_summary.get("primary_cause", "none")),
            "See strategy_diagnosis.json",
        ),
    ]

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FX Backtest Commercial Validation</title>
  <style>
    :root {{
      --bg: #f5f7fa;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #5d6b7a;
      --line: #d9e0e7;
      --good: #087f5b;
      --bad: #c92a2a;
      --warn: #b7791f;
      --accent: #2454d6;
      --soft: #eef4ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      background: #111827;
      color: white;
      padding: 28px 40px;
      border-bottom: 4px solid var(--accent);
    }}
    header h1 {{ margin: 0 0 6px; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 0; color: #cbd5e1; font-size: 14px; }}
    main {{ max-width: 1220px; margin: 0 auto; padding: 28px 24px 48px; }}
    .status {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px 20px;
      margin-bottom: 18px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      border-radius: 999px;
      padding: 0 14px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .badge.good {{ background: #e6fcf5; color: var(--good); }}
    .badge.bad {{ background: #fff5f5; color: var(--bad); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{ padding: 16px; min-height: 106px; }}
    .card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 8px;
    }}
    .card strong {{ display: block; font-size: 24px; line-height: 1.1; letter-spacing: 0; }}
    .card small {{ display: block; color: var(--muted); margin-top: 8px; font-size: 13px; }}
    section {{ padding: 20px; margin-bottom: 18px; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; letter-spacing: 0; }}
    .links {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }}
    a.file {{
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      color: var(--accent);
      background: var(--soft);
      text-decoration: none;
      font-weight: 700;
      min-height: 56px;
    }}
    a.file small {{ display: block; color: var(--muted); font-weight: 500; margin-top: 2px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 9px 8px; white-space: nowrap; }}
    th {{ color: var(--muted); background: #fafbfc; font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    .good-text {{ color: var(--good); font-weight: 700; }}
    .bad-text {{ color: var(--bad); font-weight: 700; }}
    .warn-text {{ color: var(--warn); font-weight: 700; }}
    .note {{ color: var(--muted); font-size: 13px; margin: 8px 0 0; }}
    @media (max-width: 900px) {{
      header {{ padding: 22px; }}
      main {{ padding: 20px 14px 36px; }}
      .status {{ display: block; }}
      .badge {{ margin-top: 12px; }}
      .grid, .links {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 560px) {{
      .grid, .links {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>FX Backtest Commercial Validation</h1>
    <p>{html.escape(str(run_dir))}</p>
  </header>
  <main>
    <div class="status">
      <div>
        <strong>Commercial readiness: {status}</strong>
        <div class="note">Required gates passed: {readiness["required_passed"]} / {readiness["required_total"]}. Forward evidence is mandatory before sale or live allocation.</div>
      </div>
      <span class="badge {status_class}">{status}</span>
    </div>
    <div class="grid">
      {''.join(_card(title, value, subtitle) for title, value, subtitle in cards)}
    </div>
    <section>
      <h2>Artifacts</h2>
      <div class="links">
        {_file_link("trade_log.csv", "Execution log")}
        {_file_link("equity_curve.csv", "Equity curve")}
        {_file_link("pair_performance.csv", "By pair")}
        {_file_link("monthly_pnl.csv", "Monthly PnL")}
        {_file_link("monthly_target.csv", "Monthly target")}
        {_file_link("drawdown_periods.csv", "DD duration")}
        {_file_link("period_performance.csv", "Periods")}
        {_file_link("cost_sensitivity.csv", "Costs")}
        {_file_link("pnl_breakdown.csv", "P&L tree")}
        {_file_link("pnl_by_side.csv", "Long/short P&L")}
        {_file_link("pnl_by_hour.csv", "Hourly P&L")}
        {_file_link("pnl_by_pair.csv", "Pair P&L")}
        {_file_link("pnl_by_strategy.csv", "Strategy P&L")}
        {_file_link("strategy_diagnosis.json", "Cause diagnosis")}
        {_file_link("usable_segments.csv", "Keep/block segments")}
        {_file_link("baseline_comparison.csv", "Baselines")}
        {_file_link("paper_backtest_diff.json", "Paper diff")}
        {_file_link("mfe_mae_by_trade.csv", "MFE/MAE trades")}
        {_file_link("edge_segments.csv", "EV segments")}
        {_file_link("trade_expectancy.json", "Expected value")}
        {_file_link("sample_guard.json", "Sample guard")}
        {_file_link("mfe_mae_summary.json", "MFE/MAE summary")}
        {_file_link("tp_sl_score.json", "TP/SL score")}
        {_file_link("data_quality_monitor.json", "Data monitor")}
        {_file_link("ai_trade_decision.json", "AI decision")}
        {_file_link("monte_carlo_summary.json", "Ruin risk")}
        {_file_link("oos_summary.json", "IS/OOS")}
        {_file_link("commercial_readiness.json", "Gates")}
      </div>
    </section>
    <section>
      <h2>Readiness Gates</h2>
      {_html_table(_gate_table(gates), html_rows)}
    </section>
    <section>
      <h2>Strategy Diagnosis</h2>
      {_html_table(_findings_table(diagnosis_summary.get("findings", [])), html_rows)}
    </section>
    <section>
      <h2>AI Trade Decision</h2>
      {_html_table(_dict_table(ai_decision, exclude=("blockers", "improvement_actions")), html_rows)}
    </section>
    <section>
      <h2>Improvement Actions</h2>
      {_html_table(pd.DataFrame(ai_decision.get("improvement_actions", [])), html_rows)}
    </section>
    <section>
      <h2>Expected Value</h2>
      {_html_table(_dict_table(expectancy), html_rows)}
    </section>
    <section>
      <h2>MFE / MAE Summary</h2>
      {_html_table(_dict_table(mfe_mae), html_rows)}
    </section>
    <section>
      <h2>TP / SL Score</h2>
      {_html_table(_dict_table(tp_sl, exclude=("components",)), html_rows)}
    </section>
    <section>
      <h2>Edge Segments</h2>
      {_html_table(trade_quality["segments"], html_rows)}
    </section>
    <section>
      <h2>MFE / MAE By Trade</h2>
      {_html_table(trade_quality["by_trade"], html_rows)}
    </section>
    <section>
      <h2>Usable Segments</h2>
      {_html_table(diagnosis["usable_segments"], html_rows)}
    </section>
    <section>
      <h2>Baseline Comparison</h2>
      {_html_table(baseline, html_rows)}
    </section>
    <section>
      <h2>P&amp;L Breakdown</h2>
      <p class="note">Gross profit is winning-trade profit only. Negative values in gross_pnl mean execution PnL before fees, not gross profit.</p>
      {_html_table(pnl["breakdown"], html_rows)}
    </section>
    <section>
      <h2>Long / Short P&amp;L</h2>
      {_html_table(pnl["by_side"], html_rows)}
    </section>
    <section>
      <h2>Hourly P&amp;L</h2>
      {_html_table(pnl["by_hour"], html_rows)}
    </section>
    <section>
      <h2>Strategy P&amp;L</h2>
      {_html_table(pnl["by_strategy"], html_rows)}
    </section>
    <section>
      <h2>Pair Performance</h2>
      {_html_table(pair_performance, html_rows)}
    </section>
    <section>
      <h2>Monthly PnL</h2>
      {_html_table(monthly_pnl, html_rows)}
    </section>
    <section>
      <h2>Monthly Target</h2>
      {_html_table(monthly_target, html_rows)}
    </section>
    <section>
      <h2>Drawdown Periods</h2>
      {_html_table(drawdowns, html_rows)}
    </section>
    <section>
      <h2>Cost Sensitivity</h2>
      {_html_table(cost_sensitivity, html_rows)}
    </section>
  </main>
</body>
</html>
"""
    destination.write_text(document, encoding="utf-8")


def _enriched_pnl_trades(trades: pd.DataFrame, run_config: dict[str, Any]) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()

    conversion_rates = {
        str(key).upper(): float(value)
        for key, value in run_config.get("conversion_rates", {}).items()
    }
    output = trades.copy()
    spread_cost = output.apply(
        lambda row: _trade_spread_cost_usd(row, conversion_rates),
        axis=1,
    )
    slippage_cost = output.apply(
        lambda row: _trade_slippage_cost_usd(row, conversion_rates),
        axis=1,
    )
    output["cost_supported"] = spread_cost.notna() & slippage_cost.notna()
    output["spread_loss"] = -spread_cost.fillna(0.0).astype(float)
    output["slippage_loss"] = -slippage_cost.fillna(0.0).astype(float)
    output["commission"] = -output["fees"].astype(float)
    output["swap"], output["swap_modeled"] = _swap_values(output)
    output["net_pnl"] = output["net_pnl"].astype(float)
    output["gross_pnl"] = output["gross_pnl"].astype(float)
    output["pre_cost_pnl"] = (
        output["net_pnl"]
        - output["spread_loss"]
        - output["slippage_loss"]
        - output["commission"]
        - output["swap"]
    )
    output["side_label"] = (
        output["direction"].astype(int).map({1: "long", -1: "short"}).fillna("unknown")
    )
    output["entry_hour"] = (
        pd.to_datetime(output["entry_time"], errors="coerce").dt.hour.fillna(-1).astype(int)
    )
    return output


def _swap_values(trades: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    if "swap_usd" in trades.columns:
        return trades["swap_usd"].astype(float), pd.Series(True, index=trades.index)
    if "swap" in trades.columns:
        return trades["swap"].astype(float), pd.Series(True, index=trades.index)
    return (
        pd.Series(0.0, index=trades.index, dtype=float),
        pd.Series(False, index=trades.index),
    )


def _aggregate_pnl_group(trades: pd.DataFrame, column: str) -> pd.DataFrame:
    columns = [
        "group",
        "trade_count",
        "pre_cost_pnl",
        "spread_loss",
        "slippage_loss",
        "commission",
        "swap",
        "net_pnl",
        "win_rate",
    ]
    if trades.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for group, frame in trades.groupby(column, sort=True):
        net = frame["net_pnl"].astype(float)
        rows.append(
            {
                "group": str(group),
                "trade_count": int(len(frame)),
                "pre_cost_pnl": float(frame["pre_cost_pnl"].sum()),
                "spread_loss": float(frame["spread_loss"].sum()),
                "slippage_loss": float(frame["slippage_loss"].sum()),
                "commission": float(frame["commission"].sum()),
                "swap": float(frame["swap"].sum()),
                "net_pnl": float(net.sum()),
                "win_rate": float((net > 0).mean()) if not net.empty else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _config_from_dict(raw: dict[str, Any]) -> BacktestConfig:
    risk = raw.get("risk", {})
    execution = raw.get("execution", {})
    return BacktestConfig(
        initial_cash=float(raw.get("initial_cash", 100_000.0)),
        risk=RiskConfig(
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 0.01)),
            risk_cap_pct=float(risk.get("risk_cap_pct", 0.01)),
            max_daily_loss_pct=float(risk.get("max_daily_loss_pct", 0.02)),
            max_weekly_loss_pct=_optional_float(risk.get("max_weekly_loss_pct")),
            max_monthly_drawdown_pct=_optional_float(risk.get("max_monthly_drawdown_pct")),
            monthly_profit_target_pct=_optional_float(risk.get("monthly_profit_target_pct")),
            hard_drawdown_pct=_optional_float(risk.get("hard_drawdown_pct")),
            min_stop_pips=float(risk.get("min_stop_pips", 5.0)),
            max_leverage=float(risk.get("max_leverage", 10.0)),
            max_currency_exposure_pct=_optional_float(risk.get("max_currency_exposure_pct")),
            max_position_units=_optional_float(risk.get("max_position_units")),
            allow_fractional_units=bool(risk.get("allow_fractional_units", False)),
        ),
        execution=ExecutionConfig(
            spread_pips={
                str(key).upper(): float(value)
                for key, value in execution.get("spread_pips", {}).items()
            },
            slippage_pips={
                str(key).upper(): float(value)
                for key, value in execution.get("slippage_pips", {}).items()
            },
            spread_time_multipliers={
                int(key): float(value)
                for key, value in execution.get("spread_time_multipliers", {}).items()
            },
            slippage_time_multipliers={
                int(key): float(value)
                for key, value in execution.get("slippage_time_multipliers", {}).items()
            },
            commission_per_million_usd=float(execution.get("commission_per_million_usd", 30.0)),
            fixed_fee_usd=float(execution.get("fixed_fee_usd", 0.0)),
            minimum_fee_usd=float(execution.get("minimum_fee_usd", 0.0)),
        ),
        no_trade_minutes_before=int(raw.get("no_trade_minutes_before", 30)),
        no_trade_minutes_after=int(raw.get("no_trade_minutes_after", 30)),
        min_event_impact=str(raw.get("min_event_impact", "medium")),
        max_open_positions=(
            int(raw["max_open_positions"]) if raw.get("max_open_positions") is not None else None
        ),
        cooldown_bars_after_stop=int(raw.get("cooldown_bars_after_stop", 0)),
        trading_start_time=raw.get("trading_start_time"),
        trading_end_time=raw.get("trading_end_time"),
        blocked_weekdays=tuple(int(day) for day in raw.get("blocked_weekdays", [])),
        conversion_rates={
            str(key).upper(): float(value) for key, value in raw.get("conversion_rates", {}).items()
        },
        close_positions_on_daily_stop=bool(raw.get("close_positions_on_daily_stop", True)),
        close_positions_on_portfolio_stop=bool(raw.get("close_positions_on_portfolio_stop", True)),
        force_close_on_end=bool(raw.get("force_close_on_end", True)),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _metric_row(name: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "trade_count": int(metrics.get("trade_count", 0)),
        "net_pnl": float(metrics.get("final_equity", 0.0))
        - float(metrics.get("initial_cash", 0.0)),
        "total_return_pct": float(metrics.get("total_return_pct", 0.0)),
        "max_drawdown_pct": float(metrics.get("max_drawdown_pct", 0.0)),
        "profit_factor": _finite_float(metrics.get("profit_factor", 0.0)),
        "win_rate": float(metrics.get("win_rate", 0.0)),
        "expectancy_usd": float(metrics.get("expectancy_usd", 0.0)),
    }


def _trade_summary(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "trade_count": 0,
            "net_pnl": 0.0,
            "expectancy_usd": 0.0,
            "win_rate": 0.0,
            "average_spread_pips": 0.0,
            "average_slippage_pips": 0.0,
        }
    net = trades["net_pnl"].astype(float)
    spread_columns = [column for column in ("spread_pips", "exit_spread_pips") if column in trades]
    slippage_columns = [
        column for column in ("slippage_pips", "exit_slippage_pips") if column in trades
    ]
    return {
        "trade_count": int(len(trades)),
        "net_pnl": float(net.sum()),
        "expectancy_usd": float(net.mean()),
        "win_rate": float((net > 0).mean()),
        "average_spread_pips": (
            float(trades[spread_columns].astype(float).mean().mean()) if spread_columns else 0.0
        ),
        "average_slippage_pips": (
            float(trades[slippage_columns].astype(float).mean().mean()) if slippage_columns else 0.0
        ),
    }


def _finding(
    cause: str,
    severity: str,
    message: str,
    evidence: Any,
) -> dict[str, Any]:
    return {
        "cause": cause,
        "severity": severity,
        "message": message,
        "evidence": evidence,
    }


def _primary_cause(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "none"
    severity_rank = {"high": 0, "medium": 1, "info": 2}
    ordered = sorted(findings, key=lambda item: severity_rank.get(str(item["severity"]), 99))
    return str(ordered[0]["cause"])


def _suggested_filters(usable_segments: pd.DataFrame) -> dict[str, list[str]]:
    if usable_segments.empty:
        return {"keep_pairs": [], "keep_hours": [], "keep_sides": [], "keep_strategies": []}
    kept = usable_segments[usable_segments["decision"] == "keep"]
    return {
        "keep_pairs": _kept_segments(kept, "pair"),
        "keep_hours": _kept_segments(kept, "hour"),
        "keep_sides": _kept_segments(kept, "side"),
        "keep_strategies": _kept_segments(kept, "strategy"),
    }


def _kept_segments(frame: pd.DataFrame, segment_type: str) -> list[str]:
    return frame.loc[frame["segment_type"] == segment_type, "segment"].astype(str).tolist()


def _baseline_row(frame: pd.DataFrame, name: str) -> dict[str, Any] | None:
    if frame.empty or "name" not in frame.columns:
        return None
    matched = frame[frame["name"] == name]
    if matched.empty:
        return None
    return matched.iloc[0].to_dict()


def _finite_float(value: Any) -> float:
    if value == "inf" or value == float("inf"):
        return float("inf")
    return float(value)


def _read_optional_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _load_run_price_data(run_dir: Path) -> tuple[dict[str, pd.DataFrame], str | None]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {}, "manifest.json is missing"
    try:
        manifest = _read_json(manifest_path)
        data_paths = [
            str(item["path"])
            for item in manifest.get("inputs", {}).get("data", [])
            if isinstance(item, dict) and item.get("path")
        ]
        if not data_paths:
            return {}, "manifest has no input data paths"
        return load_price_csvs(data_paths), None
    except Exception as error:  # pragma: no cover - defensive artifact reporting
        return {}, str(error)


def _read_trades(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    for column in ("signal_time", "order_time", "fill_time", "entry_time", "exit_time"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def _read_equity(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    timestamp_column = "timestamp" if "timestamp" in frame.columns else frame.columns[0]
    frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], errors="coerce")
    return frame.set_index(timestamp_column).sort_index()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_json_value(value), indent=2, ensure_ascii=False), encoding="utf-8")


def _json_value(value: Any) -> Any:
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        if np.isnan(value):
            return None
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, float) and (value == float("inf") or value == float("-inf")):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _default_existing_path(raw_path: str | Path | None, default: Path) -> Path | None:
    if raw_path is not None:
        path = Path(raw_path)
        return path if path.exists() else None
    return default if default.exists() else None


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    if gross_loss == 0 and gross_profit > 0:
        return float("inf")
    if gross_loss > 0:
        return gross_profit / gross_loss
    return 0.0


def _max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    return abs(float((equity / running_max - 1).min()))


def _days_between(start: Any, end: Any) -> float:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if pd.isna(start_ts) or pd.isna(end_ts):
        return 0.0
    return max((end_ts - start_ts).total_seconds() / 86_400, 0.0)


def _trades_between(
    trades: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    column: str,
) -> pd.DataFrame:
    if trades.empty or column not in trades.columns:
        return trades.iloc[0:0].copy()
    timestamps = pd.to_datetime(trades[column], errors="coerce")
    return trades[(timestamps >= start) & (timestamps <= end)].copy()


def _trades_before(trades: pd.DataFrame, split_time: pd.Timestamp, column: str) -> pd.DataFrame:
    if trades.empty or column not in trades.columns:
        return trades.iloc[0:0].copy()
    timestamps = pd.to_datetime(trades[column], errors="coerce")
    return trades[timestamps < split_time].copy()


def _trades_at_or_after(
    trades: pd.DataFrame, split_time: pd.Timestamp, column: str
) -> pd.DataFrame:
    if trades.empty or column not in trades.columns:
        return trades.iloc[0:0].copy()
    timestamps = pd.to_datetime(trades[column], errors="coerce")
    return trades[timestamps >= split_time].copy()


def _metrics_for_window(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
) -> dict[str, Any]:
    if equity.empty:
        return {"trade_count": 0}
    metrics = calculate_metrics(equity, trades, initial_cash)
    return _json_value(metrics)


def _trade_spread_cost_usd(row: pd.Series, conversion_rates: dict[str, float]) -> float:
    symbol = str(row["symbol"])
    units = float(row["units"])
    entry = _pip_cost_usd(
        symbol,
        units,
        float(row["entry_price"]),
        float(row["spread_pips"]) / 2,
        conversion_rates,
    )
    exit_cost = _pip_cost_usd(
        symbol,
        units,
        float(row["exit_price"]),
        float(row["exit_spread_pips"]) / 2,
        conversion_rates,
    )
    return entry + exit_cost


def _trade_slippage_cost_usd(row: pd.Series, conversion_rates: dict[str, float]) -> float:
    symbol = str(row["symbol"])
    units = float(row["units"])
    entry = _pip_cost_usd(
        symbol,
        units,
        float(row["entry_price"]),
        float(row["slippage_pips"]),
        conversion_rates,
    )
    exit_cost = _pip_cost_usd(
        symbol,
        units,
        float(row["exit_price"]),
        float(row["exit_slippage_pips"]),
        conversion_rates,
    )
    return entry + exit_cost


def _pip_cost_usd(
    symbol: str,
    units: float,
    price: float,
    pips: float,
    conversion_rates: dict[str, float],
) -> float:
    inst = instrument_for(symbol)
    amount_quote = abs(units) * inst.pip_size * pips
    try:
        return abs(quote_amount_to_usd(symbol, amount_quote, price, conversion_rates))
    except UnsupportedConversionError:
        return float("nan")


def _quantile_row(metric: str, values: np.ndarray) -> dict[str, Any]:
    return {
        "metric": metric,
        "q05": float(np.quantile(values, 0.05)),
        "q25": float(np.quantile(values, 0.25)),
        "q50": float(np.quantile(values, 0.50)),
        "q75": float(np.quantile(values, 0.75)),
        "q95": float(np.quantile(values, 0.95)),
    }


def _walk_forward_fold_count(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    frame = pd.read_csv(path)
    return int(len(frame))


def _cost_row(
    frame: pd.DataFrame, spread_multiplier: float, slippage_multiplier: float
) -> dict[str, Any] | None:
    if frame.empty:
        return None
    matched = frame[
        (frame["spread_multiplier"].astype(float) == spread_multiplier)
        & (frame["slippage_multiplier"].astype(float) == slippage_multiplier)
    ]
    if matched.empty:
        return None
    return matched.iloc[0].to_dict()


def _gate(
    name: str,
    passed: bool,
    severity: str,
    requirement: str,
    evidence: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "severity": severity,
        "requirement": requirement,
        "evidence": evidence,
    }


def _card(title: str, value: str, subtitle: str) -> str:
    return (
        '<div class="card">'
        f"<span>{html.escape(title)}</span>"
        f"<strong>{html.escape(value)}</strong>"
        f"<small>{html.escape(subtitle)}</small>"
        "</div>"
    )


def _file_link(path: str, label: str) -> str:
    return (
        f'<a class="file" href="{html.escape(path)}">'
        f"{html.escape(path)}<small>{html.escape(label)}</small></a>"
    )


def _gate_table(gates: pd.DataFrame) -> pd.DataFrame:
    if gates.empty:
        return gates
    output = gates[["name", "passed", "severity", "requirement"]].copy()
    output["passed"] = output["passed"].map(lambda value: "PASS" if bool(value) else "BLOCK")
    return output


def _findings_table(findings: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "cause": finding.get("cause"),
                "severity": finding.get("severity"),
                "message": finding.get("message"),
            }
            for finding in findings
        ],
        columns=["cause", "severity", "message"],
    )


def _dict_table(value: dict[str, Any], exclude: tuple[str, ...] = ()) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if key in exclude or isinstance(item, (dict, list, tuple)):
            continue
        rows.append({"metric": key, "value": item})
    return pd.DataFrame(rows, columns=["metric", "value"])


def _html_table(frame: pd.DataFrame, rows: int) -> str:
    if frame.empty:
        return '<p class="note">No rows.</p>'
    limited = frame.head(rows).copy()
    for column in limited.columns:
        if pd.api.types.is_float_dtype(limited[column]):
            limited[column] = limited[column].map(_number)
        else:
            limited[column] = limited[column].map(_cell_text)
    table = limited.to_html(index=False, escape=True)
    table = table.replace('<table border="1" class="dataframe">', "<table>")
    table = table.replace("<td>PASS</td>", '<td class="good-text">PASS</td>')
    table = table.replace("<td>BLOCK</td>", '<td class="bad-text">BLOCK</td>')
    return f'<div class="table-wrap">{table}</div>'


def _cell_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_value(value), ensure_ascii=False)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == float("inf"):
        return "inf"
    if abs(number) >= 100:
        return f"{number:,.2f}"
    return f"{number:.4f}"
