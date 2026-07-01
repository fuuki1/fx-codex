"""テクニカル指標（純粋関数・外部 I/O 非依存）。

レジーム判定と多因子合議のための土台。すべて「そのバーまで」の情報だけで計算し（先読み無し）、
pandas Series で返す。Wilder 平滑（RMA）を用いる指標は業界標準の定義に合わせる。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def fresh_cross(fast: pd.Series, slow: pd.Series) -> int:
    """最新バーで fast が slow をクロスしたら新方向（+1/-1）、無ければ 0。"""
    if len(fast) < 2 or len(slow) < 2:
        return 0
    prev = fast.iloc[-2] - slow.iloc[-2]
    now = fast.iloc[-1] - slow.iloc[-1]
    if pd.isna(prev) or pd.isna(now):
        return 0
    ps = 1 if prev > 0 else -1 if prev < 0 else 0
    ns = 1 if now > 0 else -1 if now < 0 else 0
    return ns if (ns != 0 and ps != ns) else 0


def rma(series: pd.Series, window: int) -> pd.Series:
    """Wilder 平滑移動平均（RSI/ATR/ADX の標準平滑）。alpha=1/window の指数移動平均。"""
    return series.ewm(alpha=1.0 / window, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder ATR（True Range の RMA）。"""
    return rma(true_range(df), window)


def efficiency_ratio(series: pd.Series, window: int = 10) -> pd.Series:
    """Kaufman 効率比（0..1）。1 に近いほど一方向（トレンド）、0 に近いほど往復（レンジ）。"""
    change = (series - series.shift(window)).abs()
    volatility = series.diff().abs().rolling(window).sum()
    return change / volatility.replace(0.0, np.nan)


def kama(series: pd.Series, er_window: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman 適応移動平均。効率比が高いほど速く、低いほど鈍く追随する。"""
    er = efficiency_ratio(series, er_window)
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
    values = series.to_numpy(dtype=float)
    sc_v = sc.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    start = er_window
    if start < len(values):
        out[start] = values[start]
        for i in range(start + 1, len(values)):
            s = sc_v[i]
            out[i] = out[i - 1] if np.isnan(s) else out[i - 1] + s * (values[i] - out[i - 1])
    return pd.Series(out, index=series.index)


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder RSI（0..100）。"""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, window)
    avg_loss = rma(loss, window)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.where(avg_loss != 0.0, 100.0)


def roc(series: pd.Series, window: int = 10) -> pd.Series:
    """変化率(%)。モメンタムの符号・強さ。"""
    return (series / series.shift(window) - 1.0) * 100.0


def bollinger_z(series: pd.Series, window: int = 20) -> pd.Series:
    """終値の z スコア（(価格 - 移動平均) / 標準偏差）。平均回帰の入力。"""
    mean = series.rolling(window).mean()
    std = series.rolling(window).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def donchian_position(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """ドンチャン内の相対位置（-1=下限, 0=中央, +1=上限）。ブレイクアウトの入力。"""
    hh = df["high"].rolling(window).max()
    ll = df["low"].rolling(window).min()
    mid = (hh + ll) / 2.0
    half = (hh - ll) / 2.0
    return ((df["close"] - mid) / half.replace(0.0, np.nan)).clip(-1.0, 1.0)


def adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder ADX（トレンド強度・0..100）。方向は問わず「トレンドの強さ」だけを測る。"""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )
    atr_ = rma(true_range(df), window).replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, window) / atr_
    minus_di = 100.0 * rma(minus_dm, window) / atr_
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return rma(dx, window)


def atr_percentile(df: pd.DataFrame, window: int = 14, lookback: int = 100) -> float:
    """直近 ATR が過去 lookback 本の中で何パーセンタイルか（0..1）。ボラ・レジームの入力。"""
    a = atr(df, window).dropna()
    if len(a) < 10:
        return 0.5
    recent = a.tail(lookback)
    last = a.iloc[-1]
    return float((recent < last).mean())


def realized_vol(series: pd.Series, window: int = 20) -> pd.Series:
    """対数リターンの標準偏差（ボラティリティ）。"""
    logret = np.log(series / series.shift(1))
    return logret.rolling(window).std(ddof=0)


def slope_sign(series: pd.Series, window: int = 3) -> int:
    """直近 window 本での傾きの符号（+1/-1/0）。適応 MA の向き判定に使う。"""
    s = series.dropna()
    if len(s) < window + 1:
        return 0
    diff = s.iloc[-1] - s.iloc[-1 - window]
    return 1 if diff > 0 else -1 if diff < 0 else 0
