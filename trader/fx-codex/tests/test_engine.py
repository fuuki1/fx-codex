from __future__ import annotations

import pandas as pd
from fx_backtester.costs import CostModel
from fx_backtester.engine import run


def _df(closes, lows=None, highs=None, opens=None):
    n = len(closes)
    idx = pd.date_range("2021-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens or closes,
            "high": highs or closes,
            "low": lows or closes,
            "close": closes,
        },
        index=idx,
    )


def _signal(df, target, stop):
    return pd.DataFrame(
        {"target_position": target, "stop_distance": [stop] * len(df)}, index=df.index
    )


def test_no_lookahead_entry_bar_earns_nothing():
    df = _df([100, 101, 102, 103, 104])
    sig = _signal(df, [1, 1, 1, 1, 1], stop=1000)  # stop は発火しない
    res = run(df, sig, CostModel("USDJPY"))
    # i=0,1 は 0（エントリは前バー判断を当バー終値で執行 → エントリバーでは収益なし）
    assert res.bar_returns.iloc[0] == 0.0
    assert res.bar_returns.iloc[1] == 0.0
    # i=2 で初めて close1->close2 のリターンが乗る（先読みなし）
    assert res.bar_returns.iloc[2] == 102 / 101 - 1


def test_costs_reduce_returns():
    df = _df([100, 101, 102, 103, 104])
    sig = _signal(df, [1, 1, 1, 1, 1], stop=1000)
    free = run(df, sig, CostModel("USDJPY", spread_pips=0, slippage_pips=0))
    costed = run(df, sig, CostModel("USDJPY", spread_pips=1, slippage_pips=1))
    assert costed.equity.iloc[-1] < free.equity.iloc[-1]
    assert costed.bar_returns.iloc[1] < 0  # エントリでコストを払う


def test_stop_triggers_and_records_trade():
    df = _df([100, 100, 100, 100], lows=[100, 100, 96, 100])
    sig = _signal(df, [1, 1, 1, 1], stop=3)  # entry 100, stop=97
    res = run(df, sig, CostModel("USDJPY"))
    assert any(t.reason == "stop" for t in res.trades)


def test_profitable_long_on_uptrend():
    df = _df([100, 101, 102, 103, 104, 105])
    sig = _signal(df, [1] * 6, stop=1000)
    res = run(df, sig, CostModel("USDJPY"))
    assert res.equity.iloc[-1] > 1.0
