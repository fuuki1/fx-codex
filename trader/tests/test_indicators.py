from __future__ import annotations

import numpy as np
import pandas as pd


def _frame(closes: list[float]) -> pd.DataFrame:
    c = np.array(closes, dtype=float)
    return pd.DataFrame(
        {"time": pd.date_range("2024-01-03", periods=len(c), freq="5min", tz="UTC"),
         "open": c, "high": c + 0.05, "low": c - 0.05, "close": c}
    )


def test_sma_ema_basic():
    import indicators as ind

    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert ind.sma(s, 2).iloc[-1] == 4.5
    assert ind.ema(s, 2).iloc[-1] > ind.ema(s, 4).iloc[-1]   # 速い EMA ほど直近に寄る


def test_atr_positive_and_wilder():
    import indicators as ind

    df = _frame(list(np.linspace(150.0, 151.0, 50)))
    a = ind.atr(df, 14)
    assert a.iloc[-1] > 0


def test_rsi_bounds_and_direction():
    import indicators as ind

    up = pd.Series(list(np.linspace(100.0, 120.0, 60)))
    down = pd.Series(list(np.linspace(120.0, 100.0, 60)))
    assert ind.rsi(up, 14).iloc[-1] > 70      # 一貫した上昇は買われすぎ
    assert ind.rsi(down, 14).iloc[-1] < 30    # 一貫した下降は売られすぎ
    assert 0.0 <= ind.rsi(up, 14).iloc[-1] <= 100.0


def test_efficiency_ratio_trend_vs_range():
    import indicators as ind

    trend = pd.Series(list(np.linspace(100.0, 110.0, 60)))
    # 決定的なジグザグ（往復）: 窓内で行って戻る → 効率比はほぼ 0
    rng = pd.Series([100.0 + (0.5 if i % 2 == 0 else -0.5) for i in range(60)])
    assert ind.efficiency_ratio(trend, 10).iloc[-1] > 0.8    # 一方向 → 1 に近い
    assert ind.efficiency_ratio(rng, 10).iloc[-1] < 0.3      # 往復 → 低い


def test_adx_higher_in_trend_than_range():
    import indicators as ind

    trend = _frame(list(np.linspace(100.0, 120.0, 120)))
    t = np.arange(120)
    rng = _frame(list(100.0 + np.sin(t / 3.0)))
    assert ind.adx(trend, 14).iloc[-1] > ind.adx(rng, 14).iloc[-1]


def test_kama_follows_and_smooths():
    import indicators as ind

    s = pd.Series(list(np.linspace(100.0, 110.0, 60)))
    k = ind.kama(s, 10)
    assert pd.notna(k.iloc[-1])
    assert 100.0 <= k.iloc[-1] <= 110.0


def test_donchian_position_bounds():
    import indicators as ind

    df = _frame(list(np.linspace(100.0, 110.0, 40)))
    p = ind.donchian_position(df, 20).iloc[-1]
    assert -1.0 <= p <= 1.0
    assert p > 0.5      # 上昇の最終バーは上限付近


def test_fresh_cross_last_bar_only():
    import indicators as ind

    fast = pd.Series([1.0, 2.0, 3.0, 4.0, 10.0])   # 最終バーで slow を上抜け
    slow = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0])
    assert ind.fresh_cross(fast, slow) == 1
    # 既に上抜け済み（最終バーで変化なし）→ 0
    assert ind.fresh_cross(pd.Series([6.0, 7.0]), pd.Series([5.0, 5.0])) == 0
