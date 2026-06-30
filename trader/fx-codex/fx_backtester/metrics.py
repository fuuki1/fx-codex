"""パフォーマンス指標。

mission-critical の観点で「過大評価しない・ゼロ割で壊れない」ことを重視。
profit_factor は損失ゼロ時に発散するため上限 999 でクリップ（JSON 化のため inf を避ける）。
"""
from __future__ import annotations

import math

import numpy as np

from .engine import BacktestResult

_PF_CAP = 999.0


def _ann_factor(periods_per_year: float) -> float:
    return math.sqrt(max(periods_per_year, 1e-9))


def compute_metrics(result: BacktestResult, periods_per_year: float) -> dict[str, float]:
    rets = result.bar_returns
    equity = result.equity
    trades = result.trades
    n = len(rets)

    total_return = float(equity.iloc[-1] - 1.0) if n else 0.0
    mean = float(rets.mean()) if n else 0.0
    std = float(rets.std(ddof=1)) if n > 1 else 0.0
    ann = _ann_factor(periods_per_year)

    sharpe = (mean / std) * ann if std > 0 else 0.0
    downside = rets[rets < 0]
    dstd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (mean / dstd) * ann if dstd > 0 else 0.0

    # 最大ドローダウン（%）
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min()) if n else 0.0

    # トレードベースの指標
    trade_rets = np.array([t.return_pct for t in trades], dtype=float)
    num_trades = int(len(trade_rets))
    wins = trade_rets[trade_rets > 0]
    losses = trade_rets[trade_rets < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    if gross_loss > 0:
        profit_factor = min(gross_profit / gross_loss, _PF_CAP)
    else:
        profit_factor = _PF_CAP if gross_profit > 0 else 0.0
    win_rate = float(len(wins) / num_trades) if num_trades else 0.0
    expectancy = float(trade_rets.mean()) if num_trades else 0.0

    years = n / periods_per_year if periods_per_year > 0 else 0.0
    final_equity = float(equity.iloc[-1]) if n else 1.0
    cagr = (final_equity ** (1.0 / years) - 1.0) if years > 0 and final_equity > 0 else 0.0

    # エクスポージャ（建玉を持っていたバーの割合）
    bars_in_pos = 0
    idx = equity.index
    for t in trades:
        try:
            a = idx.get_indexer([t.entry_ts])[0]
            b = idx.get_indexer([t.exit_ts])[0]
            if a >= 0 and b >= 0:
                bars_in_pos += max(b - a, 0)
        except Exception:
            pass
    exposure = float(bars_in_pos / n) if n else 0.0

    return {
        # 互換キー（auto_optimize / score が参照）
        "sharpe_ratio": round(sharpe, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown_pct": round(abs(max_dd) * 100.0, 4),
        # 追加指標
        "sortino_ratio": round(sortino, 4),
        "total_return_pct": round(total_return * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "volatility_annual_pct": round(std * ann * 100.0, 4),
        "win_rate": round(win_rate, 4),
        "expectancy_pct": round(expectancy * 100.0, 6),
        "num_trades": num_trades,
        "exposure": round(exposure, 4),
        "bars": n,
    }
