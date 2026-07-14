"""Bid/ask bar materialization from provider CANDLE files.

Candle sources (Dukascopy datafeed, FXCM archive) publish per-side OHLC bars,
not ticks. This module pairs the bid-side and ask-side candles of the same
(instrument, interval, open_time), optionally aggregates them onto a coarser
grid, and emits :class:`CandleBar` rows suitable for spread-aware research.

Honesty rules:
- A *spread* is only quoted where bid and ask are known at the SAME instant —
  the candle boundaries (open and close). Per-side highs/lows occur at unknown,
  generally different instants, so ``ask_high - bid_high`` is NEVER presented
  as a spread, and no mid high/low is fabricated. Spread statistics are
  computed over the boundary samples of the constituent candles and labelled
  with their basis (``minute_boundaries`` / ``hour_boundaries``).
- A source candle missing its opposite side is an UNPAIRED gap — counted and
  reported, never synthesized. A pair whose book is crossed at a boundary is
  excluded and counted (``crossed_excluded``), never repaired.
- Buckets with no paired candles are gaps, not zero bars. ``completeness`` is
  the fraction of expected constituent slots actually present.
- Aggregation is deterministic: same candles in, byte-identical CSV out
  (:func:`bars_to_csv_bytes` fixes column order, float repr and gzip mtime).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import statistics
from typing import Any

from data_platform.collect.candles import CANDLE_INTERVALS, CollectedCandle

# Target intervals this module materializes from candle sources.
TARGET_INTERVALS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}


class CandleBarError(ValueError):
    """Inputs unusable for materialization (mixed instruments/providers…)."""


@dataclass(frozen=True)
class CandleBar:
    """One spread-aware bar derived from paired bid/ask candles."""

    instrument: str
    provider: str
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
    spread_open: float
    spread_close: float
    spread_boundary_mean: float
    spread_boundary_median: float
    spread_boundary_p95: float
    spread_boundary_max: float
    spread_samples: int
    spread_basis: str  # e.g. "minute_boundaries", "hour_boundaries"
    bid_volume_sum: float | None
    ask_volume_sum: float | None
    candle_count: int
    completeness: float

    @property
    def mid_open(self) -> float:
        return (self.bid_open + self.ask_open) / 2.0

    @property
    def mid_close(self) -> float:
        return (self.bid_close + self.ask_close) / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "provider": self.provider,
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
            "spread_open": self.spread_open,
            "spread_close": self.spread_close,
            "spread_boundary_mean": self.spread_boundary_mean,
            "spread_boundary_median": self.spread_boundary_median,
            "spread_boundary_p95": self.spread_boundary_p95,
            "spread_boundary_max": self.spread_boundary_max,
            "spread_samples": self.spread_samples,
            "spread_basis": self.spread_basis,
            "bid_volume_sum": self.bid_volume_sum,
            "ask_volume_sum": self.ask_volume_sum,
            "candle_count": self.candle_count,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class MaterializedBars:
    """Materialization output plus its counted exclusions (the audit trail)."""

    bars: tuple[CandleBar, ...]
    unpaired_bid: int
    unpaired_ask: int
    crossed_excluded: int
    source_interval: str
    target_interval: str


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        raise CandleBarError("percentile of empty sample")
    if len(ordered) == 1:
        return ordered[0]
    quantiles = statistics.quantiles(ordered, n=100, method="inclusive")
    index = max(1, min(99, round(fraction * 100)))
    return quantiles[index - 1]


def _bucket_start(timestamp: datetime, span: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (timestamp.astimezone(UTC) - epoch) // span
    return epoch + elapsed * span


def materialize_candle_bars(
    candles: Sequence[CollectedCandle],
    target_interval: str,
) -> MaterializedBars:
    """Pair bid/ask candles from ONE provider+instrument and aggregate them."""

    if target_interval not in TARGET_INTERVALS:
        raise CandleBarError(
            f"unknown target interval {target_interval!r}; valid: {sorted(TARGET_INTERVALS)}"
        )
    if not candles:
        raise CandleBarError("no candles supplied")
    instruments = {candle.instrument for candle in candles}
    providers = {candle.provider for candle in candles}
    intervals = {candle.interval for candle in candles}
    if len(instruments) != 1 or len(providers) != 1 or len(intervals) != 1:
        raise CandleBarError(
            "materialization requires exactly one instrument, provider and source interval; got "
            f"instruments={sorted(instruments)} providers={sorted(providers)} "
            f"intervals={sorted(intervals)}"
        )
    source_interval = next(iter(intervals))
    source_span = CANDLE_INTERVALS[source_interval]
    target_span = TARGET_INTERVALS[target_interval]
    if target_span < source_span:
        raise CandleBarError(
            f"cannot materialize {target_interval} bars from coarser {source_interval} candles"
        )

    bids: dict[datetime, CollectedCandle] = {}
    asks: dict[datetime, CollectedCandle] = {}
    for candle in candles:
        table = bids if candle.side == "bid" else asks
        if candle.open_time in table:
            raise CandleBarError(
                f"duplicate {candle.side} candle at {candle.open_time.isoformat()} "
                "(deduplicate at ingest before materializing)"
            )
        table[candle.open_time] = candle

    paired_times = sorted(set(bids) & set(asks))
    unpaired_bid = len(set(bids) - set(asks))
    unpaired_ask = len(set(asks) - set(bids))

    crossed = 0
    buckets: dict[datetime, list[tuple[CollectedCandle, CollectedCandle]]] = {}
    for stamp in paired_times:
        bid, ask = bids[stamp], asks[stamp]
        if bid.open >= ask.open or bid.close >= ask.close:
            crossed += 1  # crossed boundary book: excluded and counted, never repaired
            continue
        buckets.setdefault(_bucket_start(stamp, target_span), []).append((bid, ask))

    spread_basis = f"{'minute' if source_interval == '1m' else 'hour'}_boundaries"
    expected_slots = max(1, int(target_span / source_span))
    bars: list[CandleBar] = []
    for start in sorted(buckets):
        pairs = sorted(buckets[start], key=lambda pair: pair[0].open_time)
        bid_candles = [pair[0] for pair in pairs]
        ask_candles = [pair[1] for pair in pairs]
        spreads = sorted(
            [ask.open - bid.open for bid, ask in pairs]
            + [ask.close - bid.close for bid, ask in pairs]
        )
        bid_volumes = [candle.volume for candle in bid_candles]
        ask_volumes = [candle.volume for candle in ask_candles]
        bars.append(
            CandleBar(
                instrument=bid_candles[0].instrument,
                provider=bid_candles[0].provider,
                interval=target_interval,
                open_time=start,
                close_time=start + target_span,
                bid_open=bid_candles[0].open,
                bid_high=max(candle.high for candle in bid_candles),
                bid_low=min(candle.low for candle in bid_candles),
                bid_close=bid_candles[-1].close,
                ask_open=ask_candles[0].open,
                ask_high=max(candle.high for candle in ask_candles),
                ask_low=min(candle.low for candle in ask_candles),
                ask_close=ask_candles[-1].close,
                spread_open=ask_candles[0].open - bid_candles[0].open,
                spread_close=ask_candles[-1].close - bid_candles[-1].close,
                spread_boundary_mean=statistics.fmean(spreads),
                spread_boundary_median=statistics.median(spreads),
                spread_boundary_p95=_percentile(spreads, 0.95),
                spread_boundary_max=spreads[-1],
                spread_samples=len(spreads),
                spread_basis=spread_basis,
                bid_volume_sum=(
                    None
                    if any(volume is None for volume in bid_volumes)
                    else float(sum(volume for volume in bid_volumes if volume is not None))
                ),
                ask_volume_sum=(
                    None
                    if any(volume is None for volume in ask_volumes)
                    else float(sum(volume for volume in ask_volumes if volume is not None))
                ),
                candle_count=len(pairs),
                completeness=min(1.0, len(pairs) / expected_slots),
            )
        )
    return MaterializedBars(
        bars=tuple(bars),
        unpaired_bid=unpaired_bid,
        unpaired_ask=unpaired_ask,
        crossed_excluded=crossed,
        source_interval=source_interval,
        target_interval=target_interval,
    )


CSV_COLUMNS = (
    "instrument",
    "provider",
    "interval",
    "open_time",
    "close_time",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "spread_open",
    "spread_close",
    "spread_boundary_mean",
    "spread_boundary_median",
    "spread_boundary_p95",
    "spread_boundary_max",
    "spread_samples",
    "spread_basis",
    "bid_volume_sum",
    "ask_volume_sum",
    "candle_count",
    "completeness",
)


def bars_to_csv_bytes(bars: Iterable[CandleBar], *, compress: bool = False) -> bytes:
    """Serialize bars deterministically (fixed columns, repr floats, gzip mtime=0)."""

    lines = [",".join(CSV_COLUMNS)]
    for bar in sorted(bars, key=lambda b: (b.instrument, b.open_time)):
        row = bar.to_dict()
        cells: list[str] = []
        for column in CSV_COLUMNS:
            value = row[column]
            if value is None:
                cells.append("")
            elif isinstance(value, float):
                # 1e-8 is far below any FX point size; rounding keeps the
                # serialization compact without losing price information.
                cells.append(repr(round(value, 8)))
            else:
                cells.append(str(value))
        lines.append(",".join(cells))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if not compress:
        return payload
    return gzip.compress(payload, mtime=0)


def bars_sha256(bars: Iterable[CandleBar]) -> str:
    """Content hash of the canonical (uncompressed) CSV serialization."""

    return hashlib.sha256(bars_to_csv_bytes(bars)).hexdigest()


def bars_from_csv_bytes(payload: bytes) -> list[CandleBar]:
    """Parse the canonical CSV(.gz) serialization back into bars.

    Round-trips :func:`bars_to_csv_bytes` exactly: loading a dataset and
    re-serializing it reproduces the identical bytes (and thus the registered
    ``dataset_sha256``), which is how a consumer proves a committed dataset
    has not drifted from its lineage manifest.
    """

    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    lines = payload.decode("utf-8").splitlines()
    if not lines or tuple(lines[0].split(",")) != CSV_COLUMNS:
        raise CandleBarError("payload is not a canonical candle-bar CSV")
    bars: list[CandleBar] = []
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) != len(CSV_COLUMNS):
            raise CandleBarError(f"row has {len(cells)} cells, expected {len(CSV_COLUMNS)}")
        row = dict(zip(CSV_COLUMNS, cells, strict=True))
        bars.append(
            CandleBar(
                instrument=row["instrument"],
                provider=row["provider"],
                interval=row["interval"],
                open_time=datetime.fromisoformat(row["open_time"]),
                close_time=datetime.fromisoformat(row["close_time"]),
                bid_open=float(row["bid_open"]),
                bid_high=float(row["bid_high"]),
                bid_low=float(row["bid_low"]),
                bid_close=float(row["bid_close"]),
                ask_open=float(row["ask_open"]),
                ask_high=float(row["ask_high"]),
                ask_low=float(row["ask_low"]),
                ask_close=float(row["ask_close"]),
                spread_open=float(row["spread_open"]),
                spread_close=float(row["spread_close"]),
                spread_boundary_mean=float(row["spread_boundary_mean"]),
                spread_boundary_median=float(row["spread_boundary_median"]),
                spread_boundary_p95=float(row["spread_boundary_p95"]),
                spread_boundary_max=float(row["spread_boundary_max"]),
                spread_samples=int(row["spread_samples"]),
                spread_basis=row["spread_basis"],
                bid_volume_sum=(
                    None if row["bid_volume_sum"] == "" else float(row["bid_volume_sum"])
                ),
                ask_volume_sum=(
                    None if row["ask_volume_sum"] == "" else float(row["ask_volume_sum"])
                ),
                candle_count=int(row["candle_count"]),
                completeness=float(row["completeness"]),
            )
        )
    return bars


@dataclass(frozen=True)
class CandleGapAudit:
    """Missing target-interval buckets between the first and last observed bar."""

    interval: str
    expected_bars: int
    observed_bars: int
    missing_open_times: tuple[str, ...]

    @property
    def completeness(self) -> float:
        if self.expected_bars == 0:
            return 1.0
        return self.observed_bars / self.expected_bars


def candle_gap_audit(bars: Sequence[CandleBar], interval: str) -> CandleGapAudit:
    """Grid completeness from first to last bar (calendar closures reported,
    not hidden — a weekend reads as missing buckets and downstream policy
    decides what closure is legitimate)."""

    if interval not in TARGET_INTERVALS:
        raise CandleBarError(f"unknown target interval {interval!r}")
    span = TARGET_INTERVALS[interval]
    ordered = sorted(bars, key=lambda bar: bar.open_time)
    if len(ordered) < 2:
        return CandleGapAudit(
            interval=interval,
            expected_bars=len(ordered),
            observed_bars=len(ordered),
            missing_open_times=(),
        )
    present = {bar.open_time for bar in ordered}
    expected: list[datetime] = []
    cursor = ordered[0].open_time
    while cursor <= ordered[-1].open_time:
        expected.append(cursor)
        cursor += span
    missing = tuple(t.isoformat() for t in expected if t not in present)
    return CandleGapAudit(
        interval=interval,
        expected_bars=len(expected),
        observed_bars=len(present),
        missing_open_times=missing,
    )
