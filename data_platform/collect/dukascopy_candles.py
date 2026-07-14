"""Dukascopy historical bid/ask CANDLE files (credential-free real data).

Dukascopy Bank's public datafeed also serves LZMA-compressed candle files —
minute candles per instrument-day and hour candles per instrument-month, one
file per side of the book:

    .../datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{DD:02d}/{BID|ASK}_candles_min_1.bi5
    .../datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{BID|ASK}_candles_hour_1.bi5

(the month path segment is ZERO-indexed). Each decompressed record is 24 bytes
big-endian: ``sec_offset u32, open u32, close u32, low u32, high u32,
volume f32`` — note the provider's OPEN, CLOSE, LOW, HIGH field order, verified
against tick-derived hours and independent sources. Prices are integer points
(JPY pairs 10^-3, others 10^-5); the offset is seconds from the period start
(day start for m1, month start for h1). Files are padded to the full period:
closed-market rows carry ``volume == 0`` with flat OHLC. Those padding rows are
provider file-format artifacts, not market observations — the parser excludes
them with a per-payload counted reason (see ``ParsedCandles.padding_excluded``);
they are never turned into flat "prices" for a closed market.

This is REAL bid/ask market data from a real bank's datafeed, but a HISTORICAL
DOWNLOAD — every candle is tagged ``collection_mode="historical_download"`` /
``account_environment="datafeed"`` so the scorecard caps it honestly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import calendar
import hashlib
import lzma
import os
import random
import socket
import struct

from data_platform.collect.candles import (
    CandleIngestResult,
    CandleLog,
    CollectedCandle,
    ParsedCandles,
    ingest_candle_payload,
)
from data_platform.collect.dukascopy import _BASE_URL, DukascopyFetchError, point_size
from data_platform.collect.reconnect import BackoffPolicy
from data_platform.raw.immutable_store import ImmutableRawStore

_CANDLE_STRUCT = struct.Struct(">IIIIIf")

Fetcher = Callable[[str], tuple[int, bytes]]


def day_m1_url(instrument: str, day: date, side: str) -> str:
    return (
        f"{_BASE_URL}/{instrument.upper()}/{day.year}/{day.month - 1:02d}/"
        f"{day.day:02d}/{side.upper()}_candles_min_1.bi5"
    )


def month_h1_url(instrument: str, year: int, month: int, side: str) -> str:
    return (
        f"{_BASE_URL}/{instrument.upper()}/{year}/{month - 1:02d}/"
        f"{side.upper()}_candles_hour_1.bi5"
    )


def fetch_payload(
    url: str,
    *,
    fetcher: Fetcher,
    backoff: BackoffPolicy | None = None,
    max_attempts: int = 4,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bytes:
    """Fetch one candle file with retry/backoff. Empty bytes = honest absence."""

    policy = backoff or BackoffPolicy(initial_seconds=2.0, max_seconds=30.0)
    randomness = rng or random.Random()
    wait = sleeper or (lambda _s: None)
    last_status = -1
    for attempt in range(max_attempts):
        try:
            status, body = fetcher(url)
        except (OSError, ConnectionError) as error:
            last_status = -1
            if attempt + 1 >= max_attempts:
                raise DukascopyFetchError(f"{url}: {error}") from error
            wait(policy.delay(attempt, randomness))
            continue
        if status == 200:
            return body
        if status == 404:
            return b""  # period genuinely absent; NOT fabricated
        last_status = status
        if attempt + 1 >= max_attempts:
            break
        wait(policy.delay(attempt, randomness))
    raise DukascopyFetchError(f"{url}: HTTP {last_status} after {max_attempts} attempts")


@dataclass(frozen=True)
class CandleContext:
    """Provenance for one candle payload: which period/side it claims to be."""

    instrument: str
    side: str  # "bid" | "ask"
    interval: str  # "1m" | "1h"
    period_start_utc: datetime  # day start for 1m files, month start for 1h files
    expected_records: int
    received_at: datetime
    connection_id: str


def m1_day_context(
    instrument: str,
    day: date,
    side: str,
    *,
    received_at: datetime,
    connection_id: str,
) -> CandleContext:
    return CandleContext(
        instrument=instrument.upper(),
        side=side.lower(),
        interval="1m",
        period_start_utc=datetime(day.year, day.month, day.day, tzinfo=UTC),
        expected_records=24 * 60,
        received_at=received_at,
        connection_id=connection_id,
    )


def h1_month_context(
    instrument: str,
    year: int,
    month: int,
    side: str,
    *,
    received_at: datetime,
    connection_id: str,
) -> CandleContext:
    days = calendar.monthrange(year, month)[1]
    return CandleContext(
        instrument=instrument.upper(),
        side=side.lower(),
        interval="1h",
        period_start_utc=datetime(year, month, 1, tzinfo=UTC),
        expected_records=days * 24,
        received_at=received_at,
        connection_id=connection_id,
    )


def parse_candle_payload(payload: bytes, context: CandleContext) -> ParsedCandles:
    """Decode one raw candle .bi5 into contract candles (no repair, no fill)."""

    if not payload:
        return ParsedCandles(candles=(), padding_excluded=0, total_records=0)
    raw_sha = hashlib.sha256(payload).hexdigest()
    try:
        decompressed = lzma.decompress(payload)
    except lzma.LZMAError as error:
        # corrupt/truncated download: quarantine the payload, never guess at it
        raise ValueError(f"bi5 payload failed LZMA decompression: {error}") from error
    if len(decompressed) % _CANDLE_STRUCT.size != 0:
        raise ValueError(f"bi5 payload is not a whole number of {_CANDLE_STRUCT.size}-byte candles")
    count = len(decompressed) // _CANDLE_STRUCT.size
    if count != context.expected_records:
        raise ValueError(
            f"{context.instrument} {context.interval} {context.side} payload has {count} "
            f"records, expected {context.expected_records} for a complete period"
        )
    step = timedelta(minutes=1) if context.interval == "1m" else timedelta(hours=1)
    point = point_size(context.instrument)
    writer = f"{socket.gethostname()}:{os.getpid()}"
    candles: list[CollectedCandle] = []
    padding = 0
    for index in range(count):
        sec, opened, closed, low, high, volume = _CANDLE_STRUCT.unpack_from(
            decompressed, index * _CANDLE_STRUCT.size
        )
        if volume == 0.0 and opened == closed == low == high:
            padding += 1  # provider padding for a closed-market period; counted, not priced
            continue
        open_time = context.period_start_utc + timedelta(seconds=int(sec))
        expected_offset = index * step
        if open_time != context.period_start_utc + expected_offset:
            raise ValueError(
                f"record {index} offset {sec}s does not sit on the {context.interval} grid"
            )
        candles.append(
            CollectedCandle(
                provider="dukascopy",
                account_environment="datafeed",
                instrument=context.instrument,
                side=context.side,
                interval=context.interval,
                open_time=open_time,
                open=opened * point,
                high=high * point,
                low=low * point,
                close=closed * point,
                volume=float(volume),
                received_at=context.received_at,
                connection_id=context.connection_id,
                writer_id=writer,
                raw_payload_sha256=raw_sha,
                source_endpoint_class="historical_datafeed",
                collection_mode="historical_download",
            )
        )
    return ParsedCandles(candles=tuple(candles), padding_excluded=padding, total_records=count)


def ingest_candle_file(
    payload: bytes,
    context: CandleContext,
    *,
    store: ImmutableRawStore,
    log: CandleLog,
) -> CandleIngestResult | None:
    """Raw-first ingest of one mirrored/fetched candle file.

    Returns ``None`` for an absent period (empty payload) — absence is recorded
    by the mirror manifest, never fabricated here.
    """

    if not payload:
        return None
    return ingest_candle_payload(
        payload,
        parser=lambda raw: parse_candle_payload(raw, context),
        store=store,
        log=log,
    )
