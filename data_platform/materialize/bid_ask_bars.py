"""Bid/ask bar materialization from a quote stream.

Bars are aggregated from :class:`~data_platform.contracts.market_quote.MarketQuote`
records — never invented. Each bar carries bid, ask and mid OHLC plus a spread
distribution (open/mean/median/p95/max) and quality fields (``quote_count``,
``stale_seconds``, ``source_coverage``). The pipeline is one-directional:

    raw quotes -> normalized quotes -> bid/ask bars -> research feature views

You can always reconstruct bars from the raw quotes (given the same quotes and
interval you get the same bars, deterministically). You can *not* reconstruct
quotes from bars — a bar is a lossy summary, so this module never fabricates
per-quote data from bar data.

Bars are timestamped at the *close* of their interval and are only emitted for
intervals that actually contain quotes; an interval with no quotes is a gap (see
:func:`gap_audit`), not a zero-volume bar with a made-up price.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from data_platform.adapters.broker import normalize_quotes
from data_platform.contracts.market_quote import MarketQuote
from data_platform.contracts.pit_record import PITContractError

# Canonical bar intervals the platform materializes.
BAR_INTERVALS: dict[str, timedelta] = {
    "5s": timedelta(seconds=5),
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True)
class BidAskBar:
    """One aggregated bar. All prices are derived from real quotes."""

    instrument: str
    interval: str
    open_time: datetime
    close_time: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    mid_open: float
    mid_high: float
    mid_low: float
    mid_close: float
    spread_open: float
    spread_mean: float
    spread_median: float
    spread_p95: float
    spread_max: float
    quote_count: int
    stale_seconds: float
    source_coverage: float

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "interval": self.interval,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "bid_open": self.bid_open,
            "bid_high": self.bid_high,
            "bid_low": self.bid_low,
            "bid_close": self.bid_close,
            "ask_open": self.ask_open,
            "ask_high": self.ask_high,
            "ask_low": self.ask_low,
            "ask_close": self.ask_close,
            "mid_open": self.mid_open,
            "mid_high": self.mid_high,
            "mid_low": self.mid_low,
            "mid_close": self.mid_close,
            "spread_open": self.spread_open,
            "spread_mean": self.spread_mean,
            "spread_median": self.spread_median,
            "spread_p95": self.spread_p95,
            "spread_max": self.spread_max,
            "quote_count": self.quote_count,
            "stale_seconds": self.stale_seconds,
            "source_coverage": self.source_coverage,
        }


def _bucket_start(timestamp: datetime, interval: timedelta) -> datetime:
    """Floor ``timestamp`` to the start of its interval bucket (UTC epoch grid)."""

    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (timestamp.astimezone(UTC) - epoch) // interval
    return epoch + elapsed * interval


def materialize_bars(
    quotes: Sequence[MarketQuote],
    interval: str,
    *,
    expected_quote_interval: timedelta | None = None,
) -> list[BidAskBar]:
    """Aggregate ``quotes`` into bars of the named ``interval``.

    ``expected_quote_interval`` (if given) sets the denominator for
    ``source_coverage`` — the fraction of the expected number of quotes actually
    seen in the bar. Without it, coverage is reported as 1.0 for any bar that has
    at least one quote (we cannot claim a coverage we cannot compute, so we do
    not under-report either; callers that care must supply the expectation).
    """

    if interval not in BAR_INTERVALS:
        raise PITContractError(f"unknown bar interval {interval!r}; valid: {sorted(BAR_INTERVALS)}")
    span = BAR_INTERVALS[interval]
    ordered = normalize_quotes(quotes)
    if not ordered:
        return []

    buckets: dict[datetime, list[MarketQuote]] = {}
    for quote in ordered:
        start = _bucket_start(quote.source_timestamp, span)
        buckets.setdefault(start, []).append(quote)

    bars: list[BidAskBar] = []
    for start in sorted(buckets):
        window = sorted(buckets[start], key=lambda q: (q.source_timestamp, q.sequence_id))
        bars.append(_bar_from_window(interval, start, span, window, expected_quote_interval))
    return bars


def _bar_from_window(
    interval: str,
    start: datetime,
    span: timedelta,
    window: list[MarketQuote],
    expected_quote_interval: timedelta | None,
) -> BidAskBar:
    bids = np.array([q.bid for q in window], dtype=float)
    asks = np.array([q.ask for q in window], dtype=float)
    mids = (bids + asks) / 2.0
    spreads = asks - bids

    # stale_seconds: the largest gap between consecutive quotes inside the bar,
    # i.e. the longest the book went without a fresh quote. A single-quote bar
    # has, by definition, no measured intra-bar staleness.
    times = [q.source_timestamp.astimezone(UTC) for q in window]
    if len(times) > 1:
        gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        stale_seconds = float(max(gaps))
    else:
        stale_seconds = 0.0

    if expected_quote_interval is not None and expected_quote_interval > timedelta(0):
        expected = max(1, int(span / expected_quote_interval))
        source_coverage = min(1.0, len(window) / expected)
    else:
        source_coverage = 1.0

    return BidAskBar(
        instrument=window[0].instrument,
        interval=interval,
        open_time=start,
        close_time=start + span,
        bid_open=float(bids[0]),
        bid_high=float(bids.max()),
        bid_low=float(bids.min()),
        bid_close=float(bids[-1]),
        ask_open=float(asks[0]),
        ask_high=float(asks.max()),
        ask_low=float(asks.min()),
        ask_close=float(asks[-1]),
        mid_open=float(mids[0]),
        mid_high=float(mids.max()),
        mid_low=float(mids.min()),
        mid_close=float(mids[-1]),
        spread_open=float(spreads[0]),
        spread_mean=float(spreads.mean()),
        spread_median=float(np.median(spreads)),
        spread_p95=float(np.percentile(spreads, 95)),
        spread_max=float(spreads.max()),
        quote_count=len(window),
        stale_seconds=stale_seconds,
        source_coverage=source_coverage,
    )
