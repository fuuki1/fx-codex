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
    else:
        net = trades["net_pnl"].astype(float)
        wins = net[net > 0]
        losses = net[net < 0]
        expectancy_usd = float(net.mean())
        expectancy_r = float(trades["r_multiple"].astype(float).mean())
        win_rate = float((net > 0).mean())
        gross_profit = float(wins.sum())
        gross_loss = abs(float(losses.sum()))
        profit_factor = float("inf") if gross_loss == 0 and gross_profit > 0 else (
            gross_profit / gross_loss if gross_loss > 0 else 0.0
        )
        average_win = float(wins.mean()) if not wins.empty else 0.0
        average_loss = abs(float(losses.mean())) if not losses.empty else 0.0
        largest_win = float(wins.max()) if not wins.empty else 0.0
        largest_loss = abs(float(losses.min())) if not losses.empty else 0.0

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
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "average_win": average_win,
        "average_loss": average_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "payoff_ratio": average_win / average_loss if average_loss > 0 else 0.0,
        "sharpe_ratio": sharpe,
        "calmar_ratio": annualized_return / max_drawdown_pct if max_drawdown_pct > 0 else 0.0,
        "recovery_factor": (final_equity - initial_cash) / max_drawdown_usd if max_drawdown_usd > 0 else 0.0,
        "exposure_pct": exposure_pct,
    }


def _annualized_return(index: pd.DatetimeIndex, initial_cash: float, final_equity: float) -> float:
    if len(index) < 2 or initial_cash <= 0 or final_equity <= 0:
        return 0.0
    elapsed_seconds = (index[-1] - index[0]).total_seconds()
    years = elapsed_seconds / (365.25 * 24 * 60 * 60)
    if years <= 0:
        return 0.0
    return float((final_equity / initial_cash) ** (1 / years) - 1)
