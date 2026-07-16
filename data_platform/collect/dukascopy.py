"""Dukascopy historical bid/ask tick source (credential-free real data).

Dukascopy Bank's public datafeed serves hourly LZMA-compressed tick files:

    https://datafeed.dukascopy.com/datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

(the month path segment is ZERO-indexed). Each decompressed record is 20
bytes big-endian: ``ms_offset u32, ask u32, bid u32, ask_volume f32,
bid_volume f32`` — prices are integer points (JPY pairs 10^-3, others 10^-5),
volumes are provider-reported in millions of units.

This is REAL bid/ask market data from a real bank's feed, but it is a
HISTORICAL DOWNLOAD, not a live stream, and Dukascopy is not the intended
trading broker — every quote is tagged ``collection_mode="historical_download"``
/ ``account_environment="datafeed"`` so the scorecard can cap it honestly.
The endpoint is flaky (503/timeouts observed); fetches retry with backoff and
fail closed after exhausting attempts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import lzma
import os
import random
import socket
import struct

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload
from data_platform.collect.reconnect import BackoffPolicy
from data_platform.raw.immutable_store import ImmutableRawStore

_TICK_STRUCT = struct.Struct(">IIIff")
_JPY_POINT = 1e-3
_DEFAULT_POINT = 1e-5
_BASE_URL = "https://datafeed.dukascopy.com/datafeed"


class DukascopyFetchError(RuntimeError):
    """The datafeed could not be fetched after retries. Fail closed."""


def point_size(instrument: str) -> float:
    return _JPY_POINT if instrument.upper().endswith("JPY") else _DEFAULT_POINT


def hour_url(instrument: str, hour_start_utc: datetime) -> str:
    stamp = hour_start_utc.astimezone(UTC)
    return (
        f"{_BASE_URL}/{instrument.upper()}/{stamp.year}/"
        f"{stamp.month - 1:02d}/{stamp.day:02d}/{stamp.hour:02d}h_ticks.bi5"
    )


Fetcher = Callable[[str], tuple[int, bytes]]
"""url -> (http_status, body). Injectable for tests; production uses requests."""


def requests_fetcher(url: str) -> tuple[int, bytes]:  # pragma: no cover - network
    import requests

    response = requests.get(
        url,
        headers={"User-Agent": "fx-codex-collect/1.0 (research; read-only)"},
        timeout=60,
    )
    return response.status_code, response.content


def fetch_hour_payload(
    instrument: str,
    hour_start_utc: datetime,
    *,
    fetcher: Fetcher,
    backoff: BackoffPolicy | None = None,
    max_attempts: int = 4,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bytes:
    """Fetch one hour file with retry/backoff. Empty file = no ticks (weekend)."""

    policy = backoff or BackoffPolicy(initial_seconds=2.0, max_seconds=30.0)
    randomness = rng or random.Random()
    wait = sleeper or (lambda _s: None)
    url = hour_url(instrument, hour_start_utc)
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
            return b""  # hour genuinely absent (e.g. market closed); NOT fabricated
        last_status = status
        if attempt + 1 >= max_attempts:
            break
        wait(policy.delay(attempt, randomness))
    raise DukascopyFetchError(f"{url}: HTTP {last_status} after {max_attempts} attempts")


@dataclass(frozen=True)
class HourContext:
    instrument: str
    hour_start_utc: datetime
    received_at: datetime
    connection_id: str


def parse_hour_payload(payload: bytes, context: HourContext) -> list[CollectedQuote]:
    """Decode one raw .bi5 payload into contract quotes (no repair, no fill)."""

    if not payload:
        return []
    raw_sha = hashlib.sha256(payload).hexdigest()
    decompressed = lzma.decompress(payload)
    if len(decompressed) % _TICK_STRUCT.size != 0:
        raise ValueError(f"bi5 payload is not a whole number of {_TICK_STRUCT.size}-byte ticks")
    point = point_size(context.instrument)
    hour_start = context.hour_start_utc.astimezone(UTC)
    writer = f"{socket.gethostname()}:{os.getpid()}"
    quotes: list[CollectedQuote] = []
    for offset in range(0, len(decompressed), _TICK_STRUCT.size):
        ms, ask_points, bid_points, ask_volume, bid_volume = _TICK_STRUCT.unpack_from(
            decompressed, offset
        )
        quotes.append(
            CollectedQuote(
                provider="dukascopy",
                account_environment="datafeed",
                instrument=context.instrument.upper(),
                provider_event_time=hour_start + timedelta(milliseconds=int(ms)),
                received_at=context.received_at,
                bid=bid_points * point,
                ask=ask_points * point,
                bid_size=float(bid_volume),
                ask_size=float(ask_volume),
                tradable=False,  # historical download is never a tradable live quote
                sequence_id=None,  # provider supplies no sequence number
                connection_id=context.connection_id,
                writer_id=writer,
                revision_id=None,
                raw_payload_sha256=raw_sha,
                source_endpoint_class="historical_datafeed",
                collection_mode="historical_download",
            )
        )
    return quotes


def ingest_hour(
    instrument: str,
    hour_start_utc: datetime,
    *,
    fetcher: Fetcher,
    store: ImmutableRawStore,
    log: QuoteLog,
    connection_id: str,
    received_at: datetime | None = None,
    stale_after_seconds: float = float("inf"),
) -> IngestResult | None:
    """Fetch + raw-first ingest one hour. Returns None when the hour is empty.

    ``stale_after_seconds`` defaults to +inf here because a historical
    download is *by definition* received long after the event time — the
    lag is provenance (``collection_mode``), not staleness of a live feed.
    """

    payload = fetch_hour_payload(instrument, hour_start_utc, fetcher=fetcher)
    if not payload:
        return None
    received = received_at or datetime.now(UTC)
    context = HourContext(
        instrument=instrument,
        hour_start_utc=hour_start_utc,
        received_at=received,
        connection_id=connection_id,
    )
    return ingest_payload(
        payload,
        parser=lambda raw: parse_hour_payload(raw, context),
        store=store,
        log=log,
        stale_after_seconds=stale_after_seconds,
    )
