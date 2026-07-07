"""Market-structure levels for detailed trade notices.

The functions here extract support/resistance style entry levels from already
loaded OHLC bars.  They are deterministic and side-effect free; data loading is
kept in the caller so tests can use small synthetic bar sequences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence


@dataclass(frozen=True)
class OhlcBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class EntryLevels:
    symbol: str
    direction: str
    pullback_low: float
    pullback_high: float
    reclaim_level: float
    breakout_level: float
    support: float | None
    resistance: float | None
    recent_low: float
    recent_high: float
    source: str
    bars_used: int


def build_entry_levels(
    symbol: str,
    direction: str,
    bars: Sequence[OhlcBar],
    *,
    current_price: float,
    atr: float | None,
    lookback_bars: int = 48,
) -> EntryLevels | None:
    """Build direction-specific entry levels from recent OHLC bars.

    ``None`` means the caller should use the ATR-only fallback.  The algorithm
    intentionally stays conservative: it only overrides fallback levels when it
    can inspect at least five valid bars.
    """
    if direction not in ("long", "short") or current_price <= 0:
        return None
    valid = [
        bar
        for bar in sorted(bars, key=lambda item: item.timestamp)
        if bar.high >= bar.low and bar.low > 0 and bar.close > 0
    ]
    if len(valid) < 5:
        return None
    recent = valid[-max(5, lookback_bars) :]
    recent_high = max(bar.high for bar in recent)
    recent_low = min(bar.low for bar in recent)
    atr_value = atr if atr is not None and atr > 0 else max(current_price * 0.001, 1e-9)

    swing_highs, swing_lows = _swing_points(recent)
    resistance = _nearest_above(swing_highs + [recent_high], current_price)
    support = _nearest_below(swing_lows + [recent_low], current_price)

    if direction == "long":
        support_level = (
            support if support is not None else max(recent_low, current_price - atr_value)
        )
        pullback_low = max(recent_low, min(support_level, current_price))
        pullback_high = min(current_price, max(pullback_low, current_price - atr_value * 0.15))
        reclaim_level = max(current_price + atr_value * 0.25, pullback_high + atr_value * 0.25)
        if resistance is not None and resistance > current_price:
            breakout_level = resistance
        else:
            breakout_level = current_price + atr_value
    else:
        resistance_level = (
            resistance if resistance is not None else min(recent_high, current_price + atr_value)
        )
        pullback_low = max(current_price, min(resistance_level, current_price + atr_value * 0.15))
        pullback_high = min(recent_high, max(resistance_level, current_price))
        reclaim_level = min(current_price - atr_value * 0.25, pullback_low - atr_value * 0.25)
        if support is not None and support < current_price:
            breakout_level = support
        else:
            breakout_level = current_price - atr_value

    return EntryLevels(
        symbol=symbol,
        direction=direction,
        pullback_low=pullback_low,
        pullback_high=pullback_high,
        reclaim_level=reclaim_level,
        breakout_level=breakout_level,
        support=support,
        resistance=resistance,
        recent_low=recent_low,
        recent_high=recent_high,
        source="recent_ohlc",
        bars_used=len(recent),
    )


def _swing_points(bars: Sequence[OhlcBar], radius: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    if len(bars) < radius * 2 + 1:
        return highs, lows
    for index in range(radius, len(bars) - radius):
        window = bars[index - radius : index + radius + 1]
        center = bars[index]
        if center.high == max(bar.high for bar in window):
            highs.append(center.high)
        if center.low == min(bar.low for bar in window):
            lows.append(center.low)
    return highs, lows


def _nearest_above(levels: Sequence[float], price: float) -> float | None:
    candidates = [level for level in levels if level > price]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(level - price))


def _nearest_below(levels: Sequence[float], price: float) -> float | None:
    candidates = [level for level in levels if level < price]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(level - price))
