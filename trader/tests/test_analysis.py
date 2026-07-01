from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd


def _frame(closes: list[float], *, freq: str = "5min") -> pd.DataFrame:
    n = len(closes)
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "time": pd.date_range("2024-01-03", periods=n, freq=freq, tz="UTC"),
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "complete": True,
        }
    )


# 平日（水）12:00 UTC = FX セッション内
WEEKDAY = datetime(2024, 1, 3, 12, tzinfo=UTC)
WEEKEND = datetime(2024, 1, 6, 12, tzinfo=UTC)  # 土曜
PARAMS = {"fast_window": 5, "slow_window": 20, "atr_window": 14, "atr_multiple": 1.5, "rr_target": 1.5}


def test_analyze_buy_when_both_timeframes_up():
    import analysis

    up = _frame(list(np.linspace(150.0, 151.0, 120)))
    up_htf = _frame(list(np.linspace(150.0, 151.0, 120)), freq="1h")
    r = analysis.analyze(up, up_htf, PARAMS, symbol="USDJPY", now=WEEKDAY)
    assert r.action == "BUY"
    assert r.trend_htf == 1 and r.signal_ltf == 1
    # 損切りは現値より下、利確は上、R:R = 目標
    assert r.stop < r.last_price < r.take_profit
    assert r.rr == 1.5
    assert r.stop_distance and r.stop_distance > 0


def test_analyze_sell_when_both_timeframes_down():
    import analysis

    down = _frame(list(np.linspace(151.0, 150.0, 120)))
    down_htf = _frame(list(np.linspace(151.0, 150.0, 120)), freq="1h")
    r = analysis.analyze(down, down_htf, PARAMS, symbol="USDJPY", now=WEEKDAY)
    assert r.action == "SELL"
    assert r.take_profit < r.last_price < r.stop        # ショートは損切り上・利確下


def test_analyze_wait_when_timeframes_disagree():
    import analysis

    up = _frame(list(np.linspace(150.0, 151.0, 120)))            # 下位足は上昇
    down_htf = _frame(list(np.linspace(151.0, 150.0, 120)), freq="1h")  # 上位足は下降
    r = analysis.analyze(up, down_htf, PARAMS, symbol="USDJPY", now=WEEKDAY)
    assert r.action == "WAIT"


def test_analyze_wait_when_session_closed():
    import analysis

    up = _frame(list(np.linspace(150.0, 151.0, 120)))
    up_htf = _frame(list(np.linspace(150.0, 151.0, 120)), freq="1h")
    r = analysis.analyze(up, up_htf, PARAMS, symbol="USDJPY", now=WEEKEND)
    assert r.action == "WAIT"
    assert r.session_open is False


def test_analyze_wait_when_insufficient_data():
    import analysis

    short = _frame([150.0, 150.1, 150.2])
    r = analysis.analyze(short, short, PARAMS, symbol="USDJPY", now=WEEKDAY)
    assert r.action == "WAIT"


def test_fresh_cross_detects_direction_change():
    import analysis

    # fresh_cross は「最新バーでの」クロスのみ検出する。fast=2/slow=3 で最終バーに上抜けが
    # 起きる系列を用意（sign(fast-slow) が -1→+1 に反転する）。
    assert analysis.fresh_cross(_frame([10.0, 8.0, 6.0, 5.0, 9.0]), 2, 3) == 1
    # 単調上昇はクロス無し（0）
    assert analysis.fresh_cross(_frame([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]), 2, 3) == 0


def test_indicators_sma_atr():
    import analysis

    df = _frame([1.0, 2.0, 3.0, 4.0, 5.0])
    assert analysis.sma(df["close"], 2).iloc[-1] == 4.5
    a = analysis.atr(df, 2).iloc[-1]
    assert a > 0


def test_build_chart_payload_shapes_and_markers():
    import dashboard

    closes = list(np.linspace(151.0, 150.0, 40)) + list(np.linspace(150.0, 151.5, 40))
    cp = dashboard.build_chart_payload(_frame(closes), PARAMS)
    assert len(cp["candles"]) == 80
    assert cp["candles"][0]["time"] < cp["candles"][-1]["time"]     # 昇順
    assert all("value" in p for p in cp["fast_ma"])                 # NaN は除外済み
    assert any(m["text"] == "BUY" for m in cp["markers"])           # 上抜けマーカーがある
