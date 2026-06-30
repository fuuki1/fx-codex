from __future__ import annotations

import pandas as pd
from fx_backtester.engine import BacktestResult, Trade
from fx_backtester.metrics import compute_metrics


def _result(returns, trades):
    idx = pd.date_range("2021-01-01", periods=len(returns), freq="D", tz="UTC")
    r = pd.Series(returns, index=idx)
    e = (1 + r).cumprod()
    return BacktestResult(bar_returns=r, equity=e, trades=trades)


def test_profit_factor_from_trades():
    ts = pd.Timestamp("2021-01-01", tz="UTC")
    trades = [
        Trade(ts, ts, 1, 100, 102, 0.02, "signal"),
        Trade(ts, ts, 1, 100, 101, 0.01, "signal"),
        Trade(ts, ts, 1, 100, 99, -0.01, "signal"),
    ]
    res = _result([0.02, 0.01, -0.01], trades)
    m = compute_metrics(res, 252.0)
    assert m["profit_factor"] == 3.0          # 0.03 / 0.01
    assert m["num_trades"] == 3
    assert m["win_rate"] == round(2 / 3, 4)


def test_sharpe_sign_and_drawdown():
    # 平均>0 かつ ばらつき>0（定数列は分散0で Sharpe=0 になるため避ける）
    res = _result([0.02, 0.01, 0.015, 0.005], [])
    m = compute_metrics(res, 252.0)
    assert m["sharpe_ratio"] > 0
    assert m["max_drawdown_pct"] == 0.0       # 全て正なら DD なし

    res2 = _result([0.05, -0.10, 0.0], [])
    m2 = compute_metrics(res2, 252.0)
    assert m2["max_drawdown_pct"] > 0


def test_profit_factor_no_losses_capped():
    ts = pd.Timestamp("2021-01-01", tz="UTC")
    res = _result([0.01], [Trade(ts, ts, 1, 100, 101, 0.01, "signal")])
    m = compute_metrics(res, 252.0)
    assert m["profit_factor"] == 999.0
