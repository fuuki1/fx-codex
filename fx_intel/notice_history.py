"""Historical OHLC sourcing for detailed trade notices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from . import dukascopy
from .briefing import TradePlan
from .market_structure import EntryLevels, OhlcBar, build_entry_levels


@dataclass
class NoticeHistoryResult:
    bars_by_symbol: dict[str, list[OhlcBar]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def dukascopy_notice_bars(
    symbols: Sequence[str],
    *,
    now: datetime,
    cache_dir: str | Path,
    timeframe: str = "15m",
    hours_back: float = 18.0,
    session=None,
) -> NoticeHistoryResult:
    """Fetch recent finalized OHLC bars from Dukascopy for notice structure.

    This is intentionally opt-in from the CLI because it can touch the network.
    Bars that are not fully closed at ``now`` are dropped to avoid leaking the
    still-forming candle into entry conditions.
    """
    if timeframe not in dukascopy.TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported Dukascopy timeframe: {timeframe}")
    now_utc = now if now.tzinfo else now.replace(tzinfo=UTC)
    now_utc = now_utc.astimezone(UTC)
    start = now_utc - timedelta(hours=max(hours_back, 1.0))
    warnings: list[str] = []
    output: dict[str, list[OhlcBar]] = {}
    for symbol in symbols:
        local_warnings: list[str] = []
        try:
            ticks = dukascopy.fetch_ticks(
                symbol,
                start,
                now_utc,
                cache_dir,
                warnings=local_warnings,
                session=session,
            )
            bars = dukascopy.ticks_to_bars(ticks, timeframe)
        except Exception as error:  # noqa: BLE001 - external source degradation
            warnings.append(f"Dukascopy通知OHLC取得失敗({symbol}): {error}")
            continue
        warnings.extend(local_warnings)
        finalized = _finalized_bars(bars, timeframe, now_utc)
        if not finalized:
            warnings.append(f"Dukascopy通知OHLC: {symbol} {timeframe} の確定バーなし")
            continue
        output[symbol] = [
            OhlcBar(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in finalized
        ]
    return NoticeHistoryResult(bars_by_symbol=output, warnings=warnings)


def entry_levels_from_bars(
    plans: Sequence[TradePlan],
    bars_by_symbol: Mapping[str, Sequence[OhlcBar]],
    *,
    lookback_bars: int,
) -> dict[str, EntryLevels]:
    """Build entry levels for plans from preloaded OHLC bars."""
    levels: dict[str, EntryLevels] = {}
    for plan in plans:
        bars = bars_by_symbol.get(plan.symbol)
        if not bars or plan.close is None:
            continue
        built = build_entry_levels(
            plan.symbol,
            plan.direction,
            bars,
            current_price=float(plan.close),
            atr=plan.atr,
            lookback_bars=lookback_bars,
        )
        if built is not None:
            levels[plan.symbol] = built
    return levels


def merge_entry_levels(
    primary: Mapping[str, EntryLevels],
    fallback: Mapping[str, EntryLevels],
) -> dict[str, EntryLevels]:
    """Keep primary levels and fill missing symbols from fallback levels."""
    merged = dict(primary)
    for symbol, levels in fallback.items():
        merged.setdefault(symbol, levels)
    return merged


def _finalized_bars(
    bars: Sequence[dukascopy.Bar], timeframe: str, now: datetime
) -> list[dukascopy.Bar]:
    minutes = dukascopy.TIMEFRAME_MINUTES[timeframe]
    now_utc = now if now.tzinfo else now.replace(tzinfo=UTC)
    now_utc = now_utc.astimezone(UTC)
    return [
        bar for bar in bars if bar.timestamp.astimezone(UTC) + timedelta(minutes=minutes) <= now_utc
    ]
