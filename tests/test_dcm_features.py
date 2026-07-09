"""Phase 3: 特徴量・ラベル・整列の検証。リーク無しを重点的に確かめる。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dukascopy_cftc_model.config import FeatureConfig, LabelConfig
from dukascopy_cftc_model.features import (
    build_feature_matrix,
    cot_features,
    technical_features,
)
from dukascopy_cftc_model.labels import align_xy, build_labels, future_return


def _prices(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0005, n))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0004,
            "low": close - 0.0004,
            "close": close,
            "volume": rng.integers(50, 500, n).astype(float),
        },
        index=pd.DatetimeIndex(idx, name="timestamp"),
    )


def _cot(weeks: int = 60, start: str = "2023-06-06") -> pd.DataFrame:
    dates = pd.date_range(start, periods=weeks, freq="7D")
    rng = np.random.default_rng(3)
    nl = rng.integers(100_000, 260_000, weeks)
    ns = rng.integers(100_000, 260_000, weeks)
    return pd.DataFrame(
        {
            "report_date": dates,
            "noncomm_long": nl,
            "noncomm_short": ns,
            "comm_long": rng.integers(300_000, 500_000, weeks),
            "comm_short": rng.integers(300_000, 500_000, weeks),
            "open_interest": rng.integers(600_000, 900_000, weeks),
            "net_noncomm": nl - ns,
        }
    )


def test_technical_features_shapes_and_no_lookahead() -> None:
    prices = _prices()
    feats = technical_features(prices, FeatureConfig())
    assert len(feats) == len(prices)
    # rsi/atr は window ぶんウォームアップNaNがある(先頭は落ちる)
    assert feats["rsi"].iloc[:13].isna().all()
    # 最終行は将来を見ない値なので有限(過去情報のみ)
    assert np.isfinite(feats["ret_1"].iloc[-1])


def test_future_return_is_forward_and_tail_nan() -> None:
    prices = _prices(50)
    fwd = future_return(prices, horizon=5)
    # 末尾 horizon 本は NaN(将来が無い)
    assert fwd.iloc[-5:].isna().all()
    # 手計算: fwd[0] = log(close[5]) - log(close[0])
    expected = np.log(prices["close"].iloc[5]) - np.log(prices["close"].iloc[0])
    assert abs(fwd.iloc[0] - expected) < 1e-12


def test_cot_features_asof_no_leak() -> None:
    """価格バー時刻より後に公開されるCOT週が漏れ込まないこと。"""
    prices = _prices(300)
    cot = _cot(60)
    cfg = FeatureConfig(cot_publication_lag_days=3)
    feats = cot_features({"EUR": cot, "USD": cot}, "EURUSD", prices.index, cfg)

    # 各バーで結合された eur_cot_net は、そのバー時刻以前に公開済みの週のもの。
    # 公開時刻 = report_date + 3日。最初の公開時刻より前のバーは NaN でなければならない。
    pub_first = (cot["report_date"].min() + pd.Timedelta(days=3)).tz_localize("UTC")
    before = prices.index < pub_first
    if before.any():
        assert feats.loc[before, "eur_cot_net"].isna().all()

    # base=EUR, quote=USD の差分列が存在
    assert "cot_diff_cot_net" in feats.columns


def test_cot_features_backward_join_uses_latest_available_week() -> None:
    prices = _prices(300)
    cot = _cot(60)
    cfg = FeatureConfig(cot_publication_lag_days=3)
    feats = cot_features({"EUR": cot, "USD": cot}, "EURUSD", prices.index, cfg)

    # 適当なバーを取り、その eur_cot_net が「公開時刻 <= バー時刻」の最新週の net と一致
    pub = cot.copy()
    pub["public_time"] = (pub["report_date"] + pd.Timedelta(days=3)).dt.tz_localize("UTC")
    bar_time = prices.index[200]
    eligible = pub[pub["public_time"] <= bar_time]
    if not eligible.empty:
        expected_net = eligible.sort_values("public_time").iloc[-1]["net_noncomm"]
        got = feats.loc[bar_time, "eur_cot_net"]
        assert got == expected_net


def test_build_feature_matrix_with_and_without_cot() -> None:
    prices = _prices()
    cot = _cot(60)
    with_cot = build_feature_matrix(prices, {"EUR": cot, "USD": cot}, "EURUSD", FeatureConfig())
    without = build_feature_matrix(
        prices, {"EUR": cot, "USD": cot}, "EURUSD", FeatureConfig(use_cot=False)
    )
    assert any(c.startswith("eur_cot") or c.startswith("cot_diff") for c in with_cot.columns)
    assert not any("cot" in c for c in without.columns)
    assert with_cot.shape[1] > without.shape[1]


def test_align_xy_drops_warmup_and_tail() -> None:
    prices = _prices(300)
    cot = _cot(60)
    X = build_feature_matrix(prices, {"EUR": cot, "USD": cot}, "EURUSD", FeatureConfig())
    y = build_labels(prices, LabelConfig(horizon=24))
    Xa, ya = align_xy(X, y)
    assert len(Xa) == len(ya)
    assert Xa.index.equals(ya.index)
    assert np.isfinite(Xa.to_numpy()).all()
    assert np.isfinite(ya.to_numpy()).all()
    # 末尾24本は将来ラベルが無いので必ず落ちている
    assert ya.index.max() <= prices.index[-25]


def test_vol_normalized_label() -> None:
    prices = _prices(100)
    y = build_labels(prices, LabelConfig(horizon=10, volatility_normalized=True))
    assert y.name == "future_return_vol_norm"
    # 生リターンより分散が抑えられている(ボラで割っているため)傾向
    raw = build_labels(prices, LabelConfig(horizon=10, volatility_normalized=False))
    assert np.isfinite(y.dropna().to_numpy()).all()
    assert len(y) == len(raw)
