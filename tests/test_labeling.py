"""labeling.py(分数次差分・トリプルバリア・メタラベリング)のテスト。

すべてネットワーク非依存の純粋関数なので、合成した価格系列で不変条件を検証する。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fx_backtester.labeling import (
    cusum_filter,
    frac_diff_ffd,
    frac_diff_weights,
    meta_labels,
    min_ffd_order,
    sample_weights_by_return,
    triple_barrier_labels,
)


def _index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-06-01", periods=n, freq="h", name="timestamp")


# ---------------------------------------------------------------- 分数次差分


def test_frac_diff_weights_d_zero_is_identity() -> None:
    w = frac_diff_weights(0.0)
    assert w.tolist() == [1.0]


def test_frac_diff_weights_d_one_is_first_difference() -> None:
    w = frac_diff_weights(1.0, threshold=1e-9)
    # d=1: w_0=1, w_1=-1, 以降は0(閾値で打ち切り)
    assert w[0] == pytest.approx(1.0)
    assert w[1] == pytest.approx(-1.0)


def test_frac_diff_ffd_d_one_matches_diff() -> None:
    series = pd.Series(np.arange(1, 21, dtype=float), index=_index(20))
    ffd = frac_diff_ffd(series, 1.0, threshold=1e-9)
    expected = series.diff()
    # 窓が埋まる範囲で1次差分に一致(先頭NaNは共通)
    both = pd.concat([ffd, expected], axis=1).dropna()
    assert np.allclose(both.iloc[:, 0], both.iloc[:, 1])


def test_frac_diff_ffd_fractional_preserves_more_memory_than_diff() -> None:
    # トレンド+ノイズの系列で、分数次(d=0.4)は原系列との相関を1次差分より残す
    rng = np.random.default_rng(0)
    trend = np.cumsum(rng.normal(0, 1, 300)) + np.arange(300) * 0.1
    series = pd.Series(trend, index=_index(300))
    # threshold=1e-3 で窓を実用長(≈50本)に収める。既定の1e-5は窓が千本超になり
    # 300本の系列ではほぼ全てNaNになる(=FFDの使いどころの注意点そのもの)。
    frac = frac_diff_ffd(series, 0.4, threshold=1e-3)
    integer = series.diff()
    corr_frac = series.corr(frac)
    corr_int = series.corr(integer)
    assert abs(corr_frac) > abs(corr_int)  # 分数次のほうが原系列の記憶を保持


def test_min_ffd_order_returns_low_d_for_trending_series() -> None:
    rng = np.random.default_rng(1)
    series = pd.Series(np.cumsum(rng.normal(0, 1, 400)), index=_index(400))
    d = min_ffd_order(series)
    assert d is not None
    assert 0.0 < d <= 1.0


# ---------------------------------------------------------------- CUSUM


def test_cusum_filter_flags_large_moves_only() -> None:
    # 前半は微動(閾値未満)、途中で+5%の急騰 → その1点だけイベント
    prices = [100.0] * 10 + [105.0] + [105.0] * 9
    close = pd.Series(prices, index=_index(20))
    events = cusum_filter(close, threshold=0.03, use_log_returns=True)
    assert len(events) == 1
    assert events[0] == close.index[10]  # 急騰した点


def test_cusum_filter_resets_after_event() -> None:
    # 2回の急騰は2イベント(1回目でリセットされ、2回目も拾える)
    prices = [100.0] * 5 + [104.0] * 5 + [108.0] * 5
    close = pd.Series(prices, index=_index(15))
    events = cusum_filter(close, threshold=0.03)
    assert len(events) == 2


def test_cusum_filter_catches_downside() -> None:
    prices = [100.0] * 5 + [95.0] * 10  # -5%の急落
    close = pd.Series(prices, index=_index(15))
    events = cusum_filter(close, threshold=0.03)
    assert len(events) == 1
    assert events[0] == close.index[5]


def test_cusum_filter_series_threshold() -> None:
    # 閾値をSeriesで渡す(ボラ連動運用)。緩い閾値なら拾い、厳しいと拾わない
    prices = [100.0] * 5 + [102.0] * 10  # +2%
    close = pd.Series(prices, index=_index(15))
    loose = pd.Series(0.01, index=close.index)  # 1%閾値 → 拾う
    strict = pd.Series(0.05, index=close.index)  # 5%閾値 → 拾わない
    assert len(cusum_filter(close, loose)) == 1
    assert len(cusum_filter(close, strict)) == 0


def test_cusum_filter_rejects_nonpositive_float_threshold() -> None:
    close = pd.Series([100.0, 101.0], index=_index(2))
    with pytest.raises(ValueError):
        cusum_filter(close, threshold=0.0)


def test_cusum_filter_no_events_when_flat() -> None:
    close = pd.Series([100.0] * 20, index=_index(20))
    assert len(cusum_filter(close, threshold=0.01)) == 0


# ---------------------------------------------------------------- トリプルバリア


def test_triple_barrier_upper_hit_gives_plus_one() -> None:
    # 単調上昇で必ず上バリアに先着する
    close = pd.Series([100.0 * (1.01 ** i) for i in range(30)], index=_index(30))
    vol = pd.Series(0.01, index=close.index)  # σ=1%
    labels = triple_barrier_labels(
        close, upper_multiple=2.0, lower_multiple=2.0, vertical_bars=10, volatility=vol
    )
    assert (labels["label"].iloc[:5] == 1).all()
    assert (labels["ret"].iloc[:5] > 0).all()


def test_triple_barrier_lower_hit_gives_minus_one() -> None:
    close = pd.Series([100.0 * (0.99 ** i) for i in range(30)], index=_index(30))
    vol = pd.Series(0.01, index=close.index)
    labels = triple_barrier_labels(
        close, upper_multiple=2.0, lower_multiple=2.0, vertical_bars=10, volatility=vol
    )
    assert (labels["label"].iloc[:5] == -1).all()


def test_triple_barrier_vertical_timeout_gives_zero() -> None:
    # ほぼ横ばい(±0.1%)ではバリアに届かず時間切れ=0
    close = pd.Series([100.0 + 0.05 * (i % 2) for i in range(30)], index=_index(30))
    vol = pd.Series(0.02, index=close.index)  # バリア=4%(届かない)
    labels = triple_barrier_labels(
        close, upper_multiple=2.0, lower_multiple=2.0, vertical_bars=5, volatility=vol
    )
    assert (labels["label"].iloc[:5] == 0).all()


def test_triple_barrier_respects_side_for_short() -> None:
    # 下落系列でも side=-1(ショート)なら利確方向に届くので +1
    close = pd.Series([100.0 * (0.99 ** i) for i in range(30)], index=_index(30))
    vol = pd.Series(0.01, index=close.index)
    side = pd.Series(-1, index=close.index)
    labels = triple_barrier_labels(
        close, upper_multiple=2.0, lower_multiple=2.0, vertical_bars=10,
        volatility=vol, side=side,
    )
    assert (labels["label"].iloc[:5] == 1).all()  # ショートが当たった=張って正解


def test_triple_barrier_no_lookahead_only_future_bars() -> None:
    # 最後のバーはエントリ後に前方が無い→垂直満了(=自分自身)でlabel=0, ret=0
    close = pd.Series(np.linspace(100, 110, 20), index=_index(20))
    vol = pd.Series(0.01, index=close.index)
    labels = triple_barrier_labels(close, vertical_bars=5, volatility=vol)
    last_ts = close.index[-1]
    assert labels.loc[last_ts, "label"] == 0
    assert labels.loc[last_ts, "ret"] == pytest.approx(0.0)


def test_triple_barrier_skips_events_without_volatility() -> None:
    close = pd.Series(np.linspace(100, 110, 20), index=_index(20))
    vol = pd.Series(np.nan, index=close.index)  # σ全欠損
    labels = triple_barrier_labels(close, volatility=vol)
    assert labels.empty


# ---------------------------------------------------------------- メタラベリング


def test_meta_labels_map_profit_to_one() -> None:
    barriers = pd.DataFrame(
        {"label": [1, -1, 0, 1], "ret": [0.02, -0.02, 0.0, 0.03], "touch_ts": _index(4)},
        index=_index(4),
    )
    meta = meta_labels(barriers)
    assert meta.tolist() == [1, 0, 0, 1]  # 利確到達(label>0)だけ「張るべき=1」


def test_meta_labels_requires_label_column() -> None:
    with pytest.raises(ValueError):
        meta_labels(pd.DataFrame({"ret": [0.1]}))


def test_sample_weights_scale_with_absolute_return() -> None:
    barriers = pd.DataFrame(
        {"label": [1, 1, 1], "ret": [0.01, 0.02, 0.03]}, index=_index(3)
    )
    weights = sample_weights_by_return(barriers)
    assert weights.mean() == pytest.approx(1.0)  # 平均1に正規化
    assert weights.iloc[2] > weights.iloc[0]  # 大きく動いたサンプルほど重い


def test_sample_weights_degenerate_zero_returns_uniform() -> None:
    barriers = pd.DataFrame({"label": [0, 0], "ret": [0.0, 0.0]}, index=_index(2))
    weights = sample_weights_by_return(barriers)
    assert (weights == 1.0).all()
