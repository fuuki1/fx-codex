"""TrueFX unauthenticated LIVE top-of-book poller (credential-free real data).

TrueFX (Integral's retail data arm) serves a real-time top-of-book snapshot for
the major pairs over plain HTTPS with no account or credentials:

    https://webrates.truefx.com/rates/connect.html?f=csv

Each response line is
``PAIR,ms_epoch,bid_big,bid_points,ask_big,ask_points,high,low,open`` where the
full bid/ask prices are the string CONCATENATION of the big figure and the
points fields (``"1.13","983" -> 1.13983``; ``"162.","211" -> 162.211``).

Honesty rules:
- This is LIVE market data (``collection_mode="live_stream"``) but TrueFX is a
  market-data AGGREGATOR, not a broker — the quotes are indicative interbank
  rates, not tradable account quotes, so every quote carries
  ``tradable=False`` and ``account_environment="datafeed"``. The scorecard
  must NOT count this as a live *broker* stream.
- The provider supplies no sizes and no sequence numbers — flagged, never
  zero-filled.
- A quote whose event time lags its receive time beyond the stale window is
  quarantined by the standard raw-first stale gate; a poll cycle whose payload
  cannot be parsed is quarantined whole with its raw bytes retained.
- Consecutive polls returning an unchanged timestamp for a pair are duplicate
  identities and are quarantined as duplicates by the quote log — accepted
  rows therefore represent actual observed updates.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
import random
import socket

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload
from data_platform.collect.reconnect import BackoffPolicy, ConnectionState
from data_platform.raw.immutable_store import ImmutableRawStore

RATES_URL = "https://webrates.truefx.com/rates/connect.html?f=csv"
# TrueFX quotes only refresh while the market is open and liquid; 30s without a
# fresh event time on a major pair means the snapshot is stale, matching the
# platform's default staleness SLO (quality.state.QualityThresholds).
DEFAULT_STALE_AFTER_SECONDS = 30.0
# One snapshot every ~2s is well under the endpoint's informal rate limits and
# is enough to catch every top-of-book update the endpoint exposes.
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

Fetcher = Callable[[str], tuple[int, bytes]]


class TruefxFetchError(RuntimeError):
    """The rates endpoint could not be fetched. Fail closed (gap, no fill)."""


def requests_fetcher(url: str) -> tuple[int, bytes]:  # pragma: no cover - network
    import requests

    response = requests.get(
        url,
        headers={"User-Agent": "fx-codex-collect/1.0 (research; read-only)"},
        timeout=10,
    )
    return response.status_code, response.content


def _slash_name(instrument: str) -> str:
    upper = instrument.upper().replace("/", "").replace("_", "")
    if len(upper) != 6:
        raise ValueError(f"cannot map instrument {instrument!r} to a TrueFX pair name")
    return f"{upper[:3]}/{upper[3:]}"


@dataclass(frozen=True)
class TruefxContext:
    received_at: datetime
    connection_id: str


def parse_rates_payload(
    payload: bytes,
    context: TruefxContext,
    instruments: Sequence[str],
) -> list[CollectedQuote]:
    """Decode one rates snapshot into contract quotes for ``instruments``."""

    wanted = {
        _slash_name(instrument): instrument.upper().replace("/", "").replace("_", "")
        for instrument in instruments
    }
    writer = f"{socket.gethostname()}:{os.getpid()}"
    raw_sha = hashlib.sha256(payload).hexdigest()
    quotes: list[CollectedQuote] = []
    for line in payload.decode("utf-8", errors="strict").splitlines():
        fields = line.strip().split(",")
        if len(fields) < 6 or fields[0] not in wanted:
            continue
        pair, stamp_ms, bid_big, bid_points, ask_big, ask_points = fields[:6]
        event = datetime.fromtimestamp(int(stamp_ms) / 1000.0, tz=UTC)
        quotes.append(
            CollectedQuote(
                provider="truefx",
                account_environment="datafeed",
                instrument=wanted[pair],
                provider_event_time=event,
                received_at=context.received_at,
                bid=float(bid_big + bid_points),
                ask=float(ask_big + ask_points),
                bid_size=None,
                ask_size=None,
                tradable=False,  # indicative aggregator rate, never a tradable quote
                sequence_id=None,
                connection_id=context.connection_id,
                writer_id=writer,
                revision_id=None,
                raw_payload_sha256=raw_sha,
                source_endpoint_class="streaming_pricing",
                collection_mode="live_stream",
            )
        )
    return quotes


def poll_once(
    *,
    fetcher: Fetcher,
    store: ImmutableRawStore,
    log: QuoteLog,
    instruments: Sequence[str],
    connection_id: str,
    received_at: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> IngestResult:
    """Fetch one snapshot and run it through the raw-first ingest order."""

    status, body = fetcher(RATES_URL)
    if status != 200 or not body.strip():
        raise TruefxFetchError(f"{RATES_URL}: HTTP {status} ({len(body)} bytes)")
    # stamp AFTER the response is in hand: a snapshot the server produced
    # while a slow fetch was in flight is fresh data, not future data
    received = received_at or datetime.now(UTC)
    context = TruefxContext(received_at=received, connection_id=connection_id)
    return ingest_payload(
        body,
        parser=lambda raw: parse_rates_payload(raw, context, instruments),
        store=store,
        log=log,
        stale_after_seconds=stale_after_seconds,
    )


def run_poller(
    *,
    fetcher: Fetcher,
    store: ImmutableRawStore,
    log: QuoteLog,
    instruments: Sequence[str],
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_polls: int | None = None,
    max_duration: timedelta | None = None,
    should_stop: Callable[[], bool] | None = None,
    sleeper: Callable[[float], None] | None = None,
    backoff: BackoffPolicy | None = None,
    rng: random.Random | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[ConnectionState, list[IngestResult]]:
    """Poll the live snapshot on an interval until stopped.

    Transport failures open an explicit gap (never back-filled) and retry with
    backoff; the poller never fabricates a snapshot for a missed cycle.
    """

    clock = now or (lambda: datetime.now(UTC))
    wait = sleeper or (lambda _s: None)
    stop = should_stop or (lambda: False)
    policy = backoff or BackoffPolicy(initial_seconds=2.0, max_seconds=60.0)
    randomness = rng or random.Random()
    state = ConnectionState(heartbeat_timeout_seconds=max(poll_interval_seconds * 5, 30.0))
    connection_id = state.mark_connected(clock())
    started = clock()
    results: list[IngestResult] = []
    polls = 0
    failures = 0
    while not stop():
        if max_polls is not None and polls >= max_polls:
            break
        if max_duration is not None and clock() - started >= max_duration:
            break
        try:
            # received_at is stamped INSIDE poll_once after the response
            # arrives — stamping before the fetch would make server events
            # that occur during a slow fetch look like future data.
            result = poll_once(
                fetcher=fetcher,
                store=store,
                log=log,
                instruments=instruments,
                connection_id=connection_id,
            )
        except (TruefxFetchError, OSError, ConnectionError) as error:
            state.mark_disconnected(clock(), reason=f"transport: {str(error)[:120]}")
            wait(policy.delay(failures, randomness))
            failures += 1
            connection_id = state.mark_connected(clock())
            continue
        failures = 0
        polls += 1
        results.append(result)
        state.heartbeat(clock())
        wait(poll_interval_seconds)
    return state, results
