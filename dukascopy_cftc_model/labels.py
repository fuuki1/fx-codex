"""将来リターンラベル(パイプライン5段目)。

Ridge が予測する対象 = 各バー t から horizon バー先までの将来 log-return。

    future_return(t) = log(close[t + horizon]) - log(close[t])

これは未来を見る値なので、末尾 horizon 本は NaN になる(まだ将来が無い)。
学習時はこの NaN 行を特徴量と揃えて落とす。

volatility_normalized=True の場合は、その時点の ATR(=判断時に確定している
ボラ)で割り、相場ボラの大小に依らない「シャープ的」ラベルにする。ATRは
過去情報なのでリークしない(将来リターンを過去ボラでスケールするだけ)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fx_backtester.indicators import average_true_range

from .config import LabelConfig


def future_return(prices: pd.DataFrame, horizon: int) -> pd.Series:
    """各バーの horizon バー先までの log-return(末尾 horizon 本は NaN)。"""
    if horizon < 1:
        raise ValueError("horizon は1以上")
    log_close = np.log(prices["close"])
    fwd = log_close.shift(-horizon) - log_close
    fwd.name = "future_return"
    return fwd


def build_labels(prices: pd.DataFrame, cfg: LabelConfig | None = None) -> pd.Series:
    """設定に従ってラベル系列を返す(index=価格timestamp)。"""
    cfg = cfg or LabelConfig()
    fwd = future_return(prices, cfg.horizon)
    if not cfg.volatility_normalized:
        return fwd
    # ボラ正規化: 判断時に確定している ATR(=t 時点)で割る
    atr = average_true_range(prices, window=14)
    atr_ret = atr / prices["close"]  # ATRをリターン単位に
    normalized = fwd / atr_ret.replace(0, np.nan)
    normalized.name = "future_return_vol_norm"
    return normalized


def align_xy(
    features: pd.DataFrame,
    labels: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    """特徴量とラベルを共通の有効行(両方finite)に揃える。

    ウォームアップ(特徴量NaN)と末尾ホライズン(ラベルNaN)、および無限値を落とす。
    戻り値の index は一致し、学習にそのまま使える。
    """
    joined = features.copy()
    joined["__label__"] = labels
    joined = joined.replace([np.inf, -np.inf], np.nan).dropna()
    y = joined.pop("__label__")
    return joined, y
