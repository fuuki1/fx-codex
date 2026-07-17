"""マルチホライズン予測トラックの共有定数(設計A: docs/design/A_HORIZON_FORECASTS.md)。

分析時間足(15m/1h/4h/1d)は特徴量、ホライズンは予測対象として分離する。
ホライズン集合は本番8本 + 5m(恒久shadow)の計9本。9hは採用しない(ユーザー決定)。

- horizon_hours は市場オープン時間換算(週末49hは時計停止、fx_intel.market の規約)。
- 学習間引きgapは「horizon/2、下限=記録周期5分」を原則に、5分周期×長ホライズンの
  評価窓重複によるサンプル水増しを抑える。MLはさらに広い間引きを使う。
- flat(横ばい)の閾値は max(ATR_h×0.1, 実測スプレッド×2)。スプレッドが取れない行は
  ATR項のみで判定する(閾値が狭くなる=flat判定が減る方向の保守誤差)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, UTC

ATR_FLAT_FRACTION = 0.1  # learning.DEFAULT_ATR_FRACTION と同値(乖離させない)
SPREAD_FLAT_MULTIPLE = 2.0
DEFAULT_HORIZON_SYMBOLS: tuple[str, ...] = ("USDJPY", "EURUSD", "GBPUSD")


@dataclass(frozen=True)
class HorizonSpec:
    """予測ホライズン1本の定義。"""

    label: str
    hours: float  # 市場オープン時間換算
    tolerance_hours: float  # 採点許容誤差(±)
    learn_thin_gap_hours: float  # 学習サンプルの同一(symbol,horizon)間引き
    ml_thin_gap_hours: float  # ML学習のさらに広い間引き
    shadow_only: bool = False  # Trueは恒久shadow(統合・昇格の対象外)


HORIZON_SPECS: tuple[HorizonSpec, ...] = (
    HorizonSpec("5m", 5 / 60, 2 / 60, 5 / 60, 20 / 60, shadow_only=True),
    HorizonSpec("15m", 0.25, 0.10, 0.25, 1.0),
    HorizonSpec("30m", 0.50, 0.15, 0.50, 2.0),
    HorizonSpec("1h", 1.0, 0.25, 1.0, 4.0),
    HorizonSpec("3h", 3.0, 0.50, 1.5, 6.0),
    HorizonSpec("6h", 6.0, 1.0, 3.0, 12.0),
    HorizonSpec("12h", 12.0, 2.0, 6.0, 24.0),
    HorizonSpec("24h", 24.0, 2.0, 12.0, 48.0),
    HorizonSpec("3d", 72.0, 6.0, 36.0, 144.0),
)

HORIZON_BY_LABEL: dict[str, HorizonSpec] = {spec.label: spec for spec in HORIZON_SPECS}
PRODUCTION_HORIZON_LABELS: tuple[str, ...] = tuple(
    spec.label for spec in HORIZON_SPECS if not spec.shadow_only
)

# 分析時間足レーティングとニュースの、ホライズン別prior重み(学習前の既定)。
# 行の合計は1.0。学習(A2)はこの重みをセル単位で再推定して上書きする。
ANALYSIS_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")
PRIOR_WEIGHTS: dict[str, dict[str, float]] = {
    "5m": {"15m": 0.50, "1h": 0.22, "4h": 0.08, "1d": 0.05, "news": 0.15},
    "15m": {"15m": 0.45, "1h": 0.25, "4h": 0.10, "1d": 0.05, "news": 0.15},
    "30m": {"15m": 0.45, "1h": 0.25, "4h": 0.10, "1d": 0.05, "news": 0.15},
    "1h": {"15m": 0.25, "1h": 0.35, "4h": 0.15, "1d": 0.05, "news": 0.20},
    "3h": {"15m": 0.25, "1h": 0.35, "4h": 0.15, "1d": 0.05, "news": 0.20},
    "6h": {"15m": 0.10, "1h": 0.25, "4h": 0.30, "1d": 0.10, "news": 0.25},
    "12h": {"15m": 0.10, "1h": 0.25, "4h": 0.30, "1d": 0.10, "news": 0.25},
    "24h": {"15m": 0.05, "1h": 0.15, "4h": 0.30, "1d": 0.25, "news": 0.25},
    "3d": {"15m": 0.05, "1h": 0.15, "4h": 0.30, "1d": 0.25, "news": 0.25},
}

# 分析時間足の名目時間(ATRのホライズンスケーリング用)
_TIMEFRAME_HOURS: dict[str, float] = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}

# ボラバケット(learning.FEATURE_SPECS の atr_pct 境界と同値。乖離させない)
VOL_BUCKET_BOUNDS = (0.10, 0.25)  # ATR/価格 %


def vol_bucket(atr_pct: float | None) -> str:
    """ATR/価格(%)を low/mid/high の3バケットへ。不明は mid 扱い。"""
    if atr_pct is None or not math.isfinite(atr_pct):
        return "mid"
    if atr_pct < VOL_BUCKET_BOUNDS[0]:
        return "low"
    if atr_pct < VOL_BUCKET_BOUNDS[1]:
        return "mid"
    return "high"


# セッション帯(UTC時)。境界の近似は設計docに明記。週末はゲート側で closed になる。
_SESSION_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 7, "tokyo"),
    (7, 12, "london"),
    (12, 17, "ldn_ny"),
    (17, 22, "ny"),
)


def session_label(moment: datetime) -> str:
    """UTC時刻から市場セッションラベルを返す(off=どの帯にも属さない)。"""
    hour = moment.astimezone(UTC).hour
    for start, end, label in _SESSION_BANDS:
        if start <= hour < end:
            return label
    return "off"


def flat_threshold(atr_h: float | None, spread: float | None) -> float:
    """横ばい判定の閾値: max(ATR_h×0.1, スプレッド×2)。両方欠測なら0。"""
    atr_term = ATR_FLAT_FRACTION * atr_h if atr_h is not None and atr_h > 0 else 0.0
    spread_term = SPREAD_FLAT_MULTIPLE * spread if spread is not None and spread > 0 else 0.0
    return max(atr_term, spread_term)


def atr_for_horizon(view_atrs: dict[str, float], horizon_hours: float) -> float | None:
    """ホライズン適合ATR。最も時間の近い分析時間足のATRを√時間則でスケール。

    view_atrs は {timeframe: atr>0} のみを渡す。空なら None。
    """
    candidates = [
        (abs(math.log(horizon_hours / _TIMEFRAME_HOURS[tf])), tf, atr)
        for tf, atr in view_atrs.items()
        if tf in _TIMEFRAME_HOURS and atr > 0 and horizon_hours > 0
    ]
    if not candidates:
        return None
    _, tf, atr = min(candidates)
    return atr * math.sqrt(horizon_hours / _TIMEFRAME_HOURS[tf])
