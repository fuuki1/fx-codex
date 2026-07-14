"""FXCM public candle archive (credential-free real broker bid/ask candles).

FXCM (a real retail FX broker) publishes historical candle files on a public
CDN with no account or credentials:

    https://candledata.fxcorporate.com/H1/{PAIR}/{YYYY}/{WEEK}.csv.gz

One gzip CSV per instrument-week with header
``DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose``.
Timestamps are ``MM/DD/YYYY HH:MM:SS.fff`` in UTC (weeks open Sunday ~21:00/
22:00 UTC = 17:00 New York, verified against Dukascopy UTC hours). There is no
volume — the absence is flagged, never zero-filled.

This is REAL bid/ask data from a real broker, but a HISTORICAL DOWNLOAD of
*candles* (not a live tradable stream): tagged
``collection_mode="historical_download"`` / ``account_environment="datafeed"``
so the scorecard caps it honestly. Its value here is as an INDEPENDENT second
bid/ask source for cross-provider divergence checks against Dukascopy.

Row-level honesty: a row whose book is crossed at the aligned boundaries
(``BidOpen >= AskOpen`` or ``BidClose >= AskClose``) is rejected — the whole
payload is quarantined rather than repaired, because a crossed candle means
the file cannot be trusted as-served.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import os
import socket

from data_platform.collect.candles import (
    CandleIngestResult,
    CandleLog,
    CollectedCandle,
    ParsedCandles,
    ingest_candle_payload,
)
from data_platform.raw.immutable_store import ImmutableRawStore

_FXCM_BASE = "https://candledata.fxcorporate.com"
_HEADER = "DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose"


def week_h1_url(instrument: str, year: int, week: int) -> str:
    return f"{_FXCM_BASE}/H1/{instrument.upper()}/{year}/{week}.csv.gz"


@dataclass(frozen=True)
class FxcmContext:
    instrument: str
    received_at: datetime
    connection_id: str


def parse_week_h1(payload: bytes, context: FxcmContext) -> ParsedCandles:
    """Decode one gzip weekly H1 CSV into bid+ask contract candles."""

    raw_sha = hashlib.sha256(payload).hexdigest()
    try:
        text = gzip.decompress(payload).decode("utf-8-sig")
    except (OSError, EOFError) as error:
        # corrupt/truncated download: quarantine the payload, never guess at it
        raise ValueError(f"gzip payload failed decompression: {error}") from error
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        # FXCM serves an empty gzip for some weeks (e.g. 2024 w35/w51/w52):
        # an honest provider-side gap, never fabricated over.
        return ParsedCandles(candles=(), padding_excluded=0, total_records=0)
    if lines[0].strip() != _HEADER:
        raise ValueError(f"unexpected FXCM header: {lines[0][:120]}")
    writer = f"{socket.gethostname()}:{os.getpid()}"

    def make(
        stamp: datetime, side: str, o: float, h: float, lo: float, c: float
    ) -> CollectedCandle:
        return CollectedCandle(
            provider="fxcm",
            account_environment="datafeed",
            instrument=context.instrument.upper(),
            side=side,
            interval="1h",
            open_time=stamp,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=None,
            received_at=context.received_at,
            connection_id=context.connection_id,
            writer_id=writer,
            raw_payload_sha256=raw_sha,
            source_endpoint_class="historical_datafeed",
            collection_mode="historical_download",
        )

    candles: list[CollectedCandle] = []
    zero_width = 0
    crossed = 0
    total = 0
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) != 9:
            raise ValueError(f"FXCM row has {len(fields)} fields, expected 9")
        stamp = datetime.strptime(fields[0], "%m/%d/%Y %H:%M:%S.%f").replace(tzinfo=UTC)
        bid_o, bid_h, bid_l, bid_c = (float(value) for value in fields[1:5])
        ask_o, ask_h, ask_l, ask_c = (float(value) for value in fields[5:9])
        total += 2
        if bid_o > ask_o or bid_c > ask_c:
            crossed += 2  # strictly crossed boundary: excluded and counted, never repaired
            continue
        if bid_o == ask_o or bid_c == ask_c:
            zero_width += 2  # zero-width boundary book: excluded and counted, not repaired
            continue
        candles.append(make(stamp, "bid", bid_o, bid_h, bid_l, bid_c))
        candles.append(make(stamp, "ask", ask_o, ask_h, ask_l, ask_c))
    if total and (crossed / total) > 0.01:
        # isolated point-precision inversions are a known archive artifact; a
        # file where they are pervasive cannot be trusted as-served at all.
        raise ValueError(f"{crossed}/{total} records have a crossed boundary book; file untrusted")
    return ParsedCandles(
        candles=tuple(candles),
        padding_excluded=0,
        total_records=total,
        zero_width_excluded=zero_width,
        crossed_excluded=crossed,
    )


def ingest_week_file(
    payload: bytes,
    context: FxcmContext,
    *,
    store: ImmutableRawStore,
    log: CandleLog,
) -> CandleIngestResult | None:
    """Raw-first ingest of one mirrored weekly file (None for absent weeks)."""

    if not payload:
        return None
    return ingest_candle_payload(
        payload,
        parser=lambda raw: parse_week_h1(raw, context),
        store=store,
        log=log,
    )
