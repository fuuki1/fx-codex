"""OANDA v3 read-only pricing-stream collector (Primary live source).

Official API: https://developer.oanda.com/rest-live-v20/pricing-ep/
Stream endpoint (the ONLY endpoint class this module may call):

    GET {stream_host}/v3/accounts/{account_id}/pricing/stream?instruments=...

Read-only guarantee: this module builds URLs exclusively under
``/pricing`` — it contains no order/trade/position endpoint and exposes no
mutating method. ``tests/test_collect_no_order_path.py`` scans for violations.

Credentials are read from environment variables and are REQUIRED — without
them the collector fails closed (``CollectorConfigError``) instead of
degrading to a mock. Values are never logged; ``repr`` masks the token.

    FX_OANDA_API_TOKEN     personal access token (read-only scope suffices)
    FX_OANDA_ACCOUNT_ID    account id the pricing stream is scoped to
    FX_OANDA_ENV           "practice" or "live"

practice/demo streams are collected honestly as ``account_environment=
"practice"`` — the scorecard caps a practice-only dataset at 90 and it never
counts as production evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
import random
import socket
import time

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload
from data_platform.collect.reconnect import (
    BackoffPolicy,
    ConnectionState,
    TokenExpiredError,
)
from data_platform.raw.immutable_store import ImmutableRawStore

ENV_TOKEN = "FX_OANDA_API_TOKEN"
ENV_ACCOUNT = "FX_OANDA_ACCOUNT_ID"
ENV_ENVIRONMENT = "FX_OANDA_ENV"
_STREAM_HOSTS = {
    "practice": "https://stream-fxpractice.oanda.com",
    "live": "https://stream-fxtrade.oanda.com",
}
_READ_ONLY_PATH = "/v3/accounts/{account_id}/pricing/stream"
HEARTBEAT_TIMEOUT_SECONDS = 15.0  # OANDA emits HEARTBEAT every ~5s


class CollectorConfigError(RuntimeError):
    """Missing/invalid configuration. Fail closed — never fall back to mocks."""


@dataclass(frozen=True)
class OandaConfig:
    token: str
    account_id: str
    environment: str

    def __repr__(self) -> str:  # never leak the token via logs/repr
        return (
            f"OandaConfig(token='***masked***', account_id='{self.account_id}', "
            f"environment='{self.environment}')"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OandaConfig:
        source = os.environ if env is None else env
        token = source.get(ENV_TOKEN, "").strip()
        account = source.get(ENV_ACCOUNT, "").strip()
        environment = source.get(ENV_ENVIRONMENT, "").strip().lower()
        missing = [
            name
            for name, value in (
                (ENV_TOKEN, token),
                (ENV_ACCOUNT, account),
                (ENV_ENVIRONMENT, environment),
            )
            if not value
        ]
        if missing:
            raise CollectorConfigError(
                f"OANDA collector is not configured; set {', '.join(missing)} "
                "(values are never logged). Refusing to run without credentials."
            )
        if environment not in _STREAM_HOSTS:
            raise CollectorConfigError(
                f"{ENV_ENVIRONMENT} must be 'practice' or 'live', got {environment!r}"
            )
        return cls(token=token, account_id=account, environment=environment)

    def stream_url(self, instruments: Sequence[str]) -> str:
        if not instruments:
            raise CollectorConfigError("at least one instrument is required")
        path = _READ_ONLY_PATH.format(account_id=self.account_id)
        joined = "%2C".join(instruments)
        return f"{_STREAM_HOSTS[self.environment]}{path}?instruments={joined}"


def _writer_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def parse_price_line(
    line: bytes,
    *,
    environment: str,
    connection_id: str,
    tradable_allowed: bool,
    received_at: datetime | None = None,
) -> list[CollectedQuote]:
    """Parse ONE stream line. HEARTBEAT lines yield no quotes.

    OANDA PRICE lines carry ``bids``/``asks`` ladders with ``liquidity`` —
    we take the top of book and keep its liquidity as the size (documented
    provider semantics, not a guess). ``tradeable`` comes from the provider
    but is forced ``False`` while our connection state is not live.
    """

    received = received_at or datetime.now(UTC)
    payload = json.loads(line.decode("utf-8"))
    kind = payload.get("type")
    if kind == "HEARTBEAT":
        return []
    if kind != "PRICE":
        raise ValueError(f"unsupported stream message type: {kind!r}")
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    if not bids or not asks:
        raise ValueError("PRICE message without top-of-book bid/ask")
    top_bid, top_ask = bids[0], asks[0]
    raw_sha = hashlib.sha256(line).hexdigest()
    return [
        CollectedQuote(
            provider="oanda",
            account_environment=environment,
            instrument=str(payload["instrument"]).replace("_", ""),
            provider_event_time=datetime.fromisoformat(str(payload["time"])),
            received_at=received,
            bid=float(top_bid["price"]),
            ask=float(top_ask["price"]),
            bid_size=(float(top_bid["liquidity"]) if "liquidity" in top_bid else None),
            ask_size=(float(top_ask["liquidity"]) if "liquidity" in top_ask else None),
            tradable=bool(payload.get("tradeable", False)) and tradable_allowed,
            sequence_id=None,  # OANDA does not expose a sequence number
            connection_id=connection_id,
            writer_id=_writer_id(),
            revision_id=None,
            raw_payload_sha256=raw_sha,
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
        )
    ]


Transport = Callable[[str, str], Iterator[bytes]]
Clock = Callable[[], datetime]
StopPredicate = Callable[[], bool]
"""(url, token) -> iterator of raw stream lines. Injectable for replay tests.

The production transport lives in :func:`requests_transport`; tests inject
fakes that yield fixture lines or raise to simulate failures. Quotes produced
through a fake transport are ``collection_mode='live_stream'`` ONLY when the
transport is the production one — replayed fixtures must be re-tagged by the
caller and are never counted as real connection evidence.
"""


def requests_transport(url: str, token: str) -> Iterator[bytes]:  # pragma: no cover
    """Production streaming transport with a heartbeat-bounded read timeout."""

    import requests

    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=(10, HEARTBEAT_TIMEOUT_SECONDS),
        )
        if response.status_code in (401, 403):
            raise TokenExpiredError(f"authorization rejected (HTTP {response.status_code})")
        response.raise_for_status()
        yield from response.iter_lines()
    except TokenExpiredError:
        raise
    except requests.exceptions.RequestException as error:
        raise ConnectionError(
            f"requests transport failed: {type(error).__name__}"
        ) from error


def stream_quotes(
    config: OandaConfig,
    instruments: Sequence[str],
    *,
    store: ImmutableRawStore,
    log: QuoteLog,
    transport: Transport,
    state: ConnectionState | None = None,
    backoff: BackoffPolicy | None = None,
    max_reconnects: int = 5,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Clock | None = None,
    should_stop: StopPredicate | None = None,
    max_messages: int | None = None,
) -> tuple[ConnectionState, list[IngestResult]]:
    """Run the stream through raw-first ingest with reconnect semantics.

    Returns the final connection state (gaps, reconnect count, stop reason)
    and the per-payload ingest results. Token expiry stops the collector
    (fail-closed); transient transport errors reconnect with real
    backoff+jitter up to ``max_reconnects``. Heartbeat expiry opens an explicit
    gap before any late quote can be treated as tradable. ``should_stop`` is
    polled at connection and message boundaries for graceful daemon shutdown.
    """

    conn = state or ConnectionState(heartbeat_timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS)
    policy = backoff or BackoffPolicy()
    randomness = rng or random.Random()
    wait = sleeper or time.sleep
    now_fn = clock or (lambda: datetime.now(UTC))
    stop_requested = should_stop or (lambda: False)
    results: list[IngestResult] = []
    url = config.stream_url(instruments)
    attempt = 0
    messages_seen = 0
    while conn.stopped_reason is None:
        if stop_requested():
            conn.stop("stop_requested")
            break
        connection_id = conn.mark_connected(now_fn())
        try:
            for line in transport(url, config.token):
                if stop_requested():
                    conn.stop("stop_requested")
                    break
                if not line:
                    continue
                now = now_fn()
                alive = conn.check_alive(now)
                if alive:
                    conn.heartbeat(now)
                messages_seen += 1

                def _parser(raw: bytes, _cid: str = connection_id) -> list[CollectedQuote]:
                    return parse_price_line(
                        raw,
                        environment=config.environment,
                        connection_id=_cid,
                        tradable_allowed=alive and conn.tradable,
                    )

                results.append(ingest_payload(line, parser=_parser, store=store, log=log))
                if not alive:
                    break
                if max_messages is not None and messages_seen >= max_messages:
                    conn.stop("max_messages_reached")
                    break
            else:  # stream ended without error -> provider closed it
                conn.mark_disconnected(now_fn(), reason="stream_closed")
        except TokenExpiredError as error:
            conn.mark_disconnected(now_fn(), reason="token_expired")
            conn.stop(f"token_expired: {error}")
            break
        except (OSError, ConnectionError) as error:
            now = now_fn()
            if conn.connected and not conn.check_alive(now):
                pass  # check_alive already opened a heartbeat_timeout gap
            elif conn.connected:
                conn.mark_disconnected(now, reason=f"transport_error: {error}")
        if conn.stopped_reason is not None:
            break
        if stop_requested():
            conn.stop("stop_requested")
            break
        if attempt >= max_reconnects:
            conn.stop("max_reconnects_exhausted")
            break
        wait(policy.delay(attempt, randomness))
        attempt += 1
    return conn, results
