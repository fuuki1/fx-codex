"""Multi-horizon forecast constants shared by generation, scoring, and UI.

The production set is the eight horizons approved in design A.  The 5-minute
horizon is recorded and evaluated forever in shadow; it can never be promoted.
All durations are market-open hours.  There is deliberately no 9h horizon.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ATR_FLAT_FRACTION = 0.1
SPREAD_FLAT_MULTIPLE = 2.0
DEFAULT_HORIZON_SYMBOLS: tuple[str, ...] = ("USDJPY", "EURUSD", "GBPUSD")
ANALYSIS_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")


@dataclass(frozen=True)
class HorizonSpec:
    label: str
    hours: float
    tolerance_hours: float
    learn_thin_gap_hours: float
    ml_thin_gap_hours: float
    shadow_only: bool = False


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
HORIZON_BY_LABEL = {spec.label: spec for spec in HORIZON_SPECS}
PRODUCTION_HORIZON_LABELS = tuple(spec.label for spec in HORIZON_SPECS if not spec.shadow_only)

# Initial priors.  Each row sums to one.  Learned weights replace these only
# within the same symbol x horizon cell.
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

_TIMEFRAME_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}
VOL_BUCKET_BOUNDS = (0.10, 0.25)


def flat_threshold(atr_h: float | None, spread: float | None) -> float:
    """Approved flat band: max(ATR_h * 0.1, measured spread * 2)."""
    atr_term = ATR_FLAT_FRACTION * atr_h if atr_h is not None and atr_h > 0 else 0.0
    spread_term = SPREAD_FLAT_MULTIPLE * spread if spread is not None and spread > 0 else 0.0
    return max(atr_term, spread_term)


def atr_for_horizon(view_atrs: dict[str, float], horizon_hours: float) -> float | None:
    """Scale the nearest available timeframe ATR with the square-root-time prior."""
    candidates = [
        (abs(math.log(horizon_hours / _TIMEFRAME_HOURS[tf])), tf, atr)
        for tf, atr in view_atrs.items()
        if tf in _TIMEFRAME_HOURS and atr > 0 and horizon_hours > 0
    ]
    if not candidates:
        return None
    _, timeframe, atr = min(candidates)
    return atr * math.sqrt(horizon_hours / _TIMEFRAME_HOURS[timeframe])


def vol_bucket(atr_pct: float | None) -> str:
    if atr_pct is None or not math.isfinite(atr_pct):
        return "mid"
    if atr_pct < VOL_BUCKET_BOUNDS[0]:
        return "low"
    if atr_pct < VOL_BUCKET_BOUNDS[1]:
        return "mid"
    return "high"
