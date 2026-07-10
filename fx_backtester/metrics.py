from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _annualization_factor(index: pd.DatetimeIndex) -> float:
    if len(index) < 3:
        return math.sqrt(252)
    deltas = index.to_series().diff().dropna().dt.total_seconds()
    median_seconds = float(deltas.median())
    if median_seconds <= 0:
        return math.sqrt(252)
    periods_per_year = 365.25 * 24 * 60 * 60 / median_seconds
    return math.sqrt(periods_per_year)


def calculate_metrics(
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
) -> dict[str, float | int]:
    if equity_curve.empty:
        raise ValueError("equity_curve is empty")

    equity = equity_curve["equity"].astype(float)
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    drawdown_usd = equity - running_max
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = 0.0
    if len(returns) > 1 and returns.std(ddof=1) > 0:
        sharpe = float(returns.mean() / returns.std(ddof=1) * _annualization_factor(equity.index))
    downside = returns.clip(upper=0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside)))) if not downside.empty else 0.0
    sortino = 0.0
    if downside_deviation > 0:
        sortino = float(returns.mean() / downside_deviation * _annualization_factor(equity.index))

    if trades.empty:
        expectancy_usd = 0.0
        expectancy_r = 0.0
        win_rate = 0.0
        profit_factor = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        average_win = 0.0
        average_loss = 0.0
        largest_win = 0.0
        largest_loss = 0.0
        median_trade_usd = 0.0
        median_r = 0.0
        expected_shortfall_r_05 = 0.0
        longest_loss_streak = 0
        average_holding_hours = 0.0
        median_holding_hours = 0.0
        total_fees_usd = 0.0
        round_trip_turnover_units = 0.0
    else:
        net = trades["net_pnl"].astype(float)
        r_multiples = trades["r_multiple"].astype(float)
        wins = net[net > 0]
        losses = net[net < 0]
        expectancy_usd = float(net.mean())
        expectancy_r = float(r_multiples.mean())
        win_rate = float((net > 0).mean())
        gross_profit = float(wins.sum())
        gross_loss = abs(float(losses.sum()))
        profit_factor = (
            float("inf")
            if gross_loss == 0 and gross_profit > 0
            else (gross_profit / gross_loss if gross_loss > 0 else 0.0)
        )
        average_win = float(wins.mean()) if not wins.empty else 0.0
        average_loss = abs(float(losses.mean())) if not losses.empty else 0.0
        largest_win = float(wins.max()) if not wins.empty else 0.0
        largest_loss = abs(float(losses.min())) if not losses.empty else 0.0
        median_trade_usd = float(net.median())
        median_r = float(r_multiples.median())
        tail_count = max(1, math.ceil(len(r_multiples) * 0.05))
        expected_shortfall_r_05 = float(r_multiples.nsmallest(tail_count).mean())
        longest_loss_streak = _longest_true_streak((net < 0).tolist())
        average_holding_hours, median_holding_hours = _holding_period_hours(trades)
        total_fees_usd = (
            float(trades["fees"].astype(float).sum()) if "fees" in trades.columns else 0.0
        )
        round_trip_turnover_units = (
            float(2.0 * trades["units"].astype(float).abs().sum())
            if "units" in trades.columns
            else 0.0
        )

    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_cash - 1
    max_drawdown_pct = abs(float(drawdown.min()))
    max_drawdown_usd = abs(float(drawdown_usd.min()))
    annualized_return = _annualized_return(equity.index, initial_cash, final_equity)
    exposure_pct = 0.0
    if "open_positions" in equity_curve.columns:
        exposure_pct = float((equity_curve["open_positions"].astype(float) > 0).mean())

    return {
        "initial_cash": float(initial_cash),
        "final_equity": final_equity,
        "total_return_pct": total_return,
        "annualized_return_pct": annualized_return,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_usd": max_drawdown_usd,
        "trade_count": int(len(trades)),
        "win_rate": win_rate,
        "expectancy_usd": expectancy_usd,
        "expectancy_r": expectancy_r,
        "median_trade_usd": median_trade_usd,
        "median_r": median_r,
        "expected_shortfall_r_05": expected_shortfall_r_05,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "average_win": average_win,
        "average_loss": average_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "payoff_ratio": average_win / average_loss if average_loss > 0 else 0.0,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "downside_deviation": downside_deviation,
        "calmar_ratio": annualized_return / max_drawdown_pct if max_drawdown_pct > 0 else 0.0,
        "recovery_factor": (
            (final_equity - initial_cash) / max_drawdown_usd if max_drawdown_usd > 0 else 0.0
        ),
        "exposure_pct": exposure_pct,
        "longest_loss_streak": longest_loss_streak,
        "average_holding_hours": average_holding_hours,
        "median_holding_hours": median_holding_hours,
        "total_fees_usd": total_fees_usd,
        "round_trip_turnover_units": round_trip_turnover_units,
    }


def _longest_true_streak(flags: list[bool]) -> int:
    longest = 0
    current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def _holding_period_hours(trades: pd.DataFrame) -> tuple[float, float]:
    if "entry_time" not in trades.columns or "exit_time" not in trades.columns:
        return 0.0, 0.0
    entries = pd.to_datetime(trades["entry_time"], utc=True, errors="coerce")
    exits = pd.to_datetime(trades["exit_time"], utc=True, errors="coerce")
    hours = (exits - entries).dt.total_seconds().div(3600.0)
    hours = hours[(hours >= 0) & np.isfinite(hours)]
    if hours.empty:
        return 0.0, 0.0
    return float(hours.mean()), float(hours.median())


def _annualized_return(index: pd.DatetimeIndex, initial_cash: float, final_equity: float) -> float:
    if len(index) < 2 or initial_cash <= 0 or final_equity <= 0:
        return 0.0
    elapsed_seconds = (index[-1] - index[0]).total_seconds()
    years = elapsed_seconds / (365.25 * 24 * 60 * 60)
    if years <= 0:
        return 0.0
    return float((final_equity / initial_cash) ** (1 / years) - 1)
