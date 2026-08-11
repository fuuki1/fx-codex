"""Massive read-only real-time FX BBO WebSocket collector.

Official contracts checked 2026-08-10:

* https://massive.com/docs/websocket/forex/quotes
* https://massive.com/docs/websocket/quickstart

The only network destination is ``wss://socket.massive.com/forex``.  This
module authenticates a market-data WebSocket and subscribes to ``C.<PAIR>``
quote topics.  It has no broker account, execution endpoint, or mutation API.

Massive's documented FX quote payload exposes bid, ask, provider timestamp,
pair, and exchange identifier.  The documented response schema does not expose
quote sizes or a sequence number, so those values remain ``None`` and are
quality-flagged rather than invented.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
import random
import re
import socket
import time
from typing import Any, Literal

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload
from data_platform.collect.reconnect import BackoffPolicy, ConnectionState, TokenExpiredError
from data_platform.raw.immutable_store import ImmutableRawStore

ENV_API_KEY = "FX_MASSIVE_API_KEY"
WEBSOCKET_URL = "wss://socket.massive.com/forex"
RECEIVE_TIMEOUT_SECONDS = 30.0
LIVENESS_TIMEOUT_SECONDS = 45.0

CollectionMode = Literal["live_stream", "replay"]
EndpointClass = Literal["forex_bbo_websocket", "replay_fixture"]

_PAIR_RE = re.compile(r"^[A-Z]{6}$")
_AUTH_FAILURE_STATUSES = frozenset(
    {"auth_failed", "authentication_failed", "not_authorized", "unauthorized"}
)


class CollectorConfigError(RuntimeError):
    """Massive market-data configuration is missing or invalid."""


@dataclass(frozen=True)
class MassiveConfig:
    api_key: str

    def __repr__(self) -> str:
        return "MassiveConfig(api_key='***masked***')"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MassiveConfig:
        source = os.environ if env is None else env
        api_key = str(source.get(ENV_API_KEY, "")).strip()
        if not api_key:
            raise CollectorConfigError(
                f"Massive collector is not configured; set {ENV_API_KEY} "
                "(the value is never logged). Refusing to run without credentials."
            )
        return cls(api_key=api_key)

    def subscription_topics(self, instruments: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(_normalize_pair(value) for value in instruments)
        if not normalized:
            raise CollectorConfigError("at least one instrument is required")
        if len(set(normalized)) != len(normalized):
            raise CollectorConfigError("duplicate Massive instruments are not allowed")
        return tuple(f"C.{pair[:3]}-{pair[3:]}" for pair in normalized)


def _normalize_pair(value: object) -> str:
    normalized = str(value).strip().upper()
    for separator in ("/", "-", "_"):
        normalized = normalized.replace(separator, "")
    if not _PAIR_RE.fullmatch(normalized):
        raise CollectorConfigError(f"invalid six-letter FX pair: {value!r}")
    return normalized


def _writer_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _records(payload: object) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        return [payload]
    if isinstance(payload, list) and all(isinstance(item, Mapping) for item in payload):
        return list(payload)
    raise ValueError("Massive WebSocket frame must be a JSON object or object array")


def _is_auth_failure(record: Mapping[str, Any]) -> bool:
    if str(record.get("ev") or "").lower() != "status":
        return False
    status = str(record.get("status") or "").strip().lower()
    message = str(record.get("message") or "").strip().lower()
    if status in _AUTH_FAILURE_STATUSES:
        return True
    return ("auth" in message or "authoriz" in message) and any(
        term in message for term in ("fail", "denied", "invalid", "not authorized")
    )


def _event_time_from_ms(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Massive quote timestamp must be numeric Unix milliseconds")
    milliseconds = float(value)
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        raise ValueError("Massive quote timestamp must be finite and positive")
    try:
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("Massive quote timestamp is outside the supported range") from error


def parse_message(
    raw: bytes,
    *,
    connection_id: str,
    tradable_allowed: bool,
    received_at: datetime | None = None,
    source_endpoint_class: EndpointClass = "forex_bbo_websocket",
    collection_mode: CollectionMode = "live_stream",
) -> list[CollectedQuote]:
    """Normalize one Massive WebSocket frame into zero or more BBO quotes."""

    received = received_at or datetime.now(UTC)
    payload = json.loads(raw.decode("utf-8"))
    records = _records(payload)
    for record in records:
        if _is_auth_failure(record):
            raise TokenExpiredError("Massive authentication or plan authorization rejected")

    raw_sha = hashlib.sha256(raw).hexdigest()
    quotes: list[CollectedQuote] = []
    for record in records:
        event_type = str(record.get("ev") or "")
        if event_type == "status":
            continue
        if event_type != "C":
            raise ValueError(f"unsupported Massive WebSocket event type: {event_type!r}")
        exchange = record.get("x")
        revision_id = None if exchange is None else f"exchange:{exchange}"
        try:
            instrument = _normalize_pair(record.get("p"))
        except CollectorConfigError as error:
            raise ValueError("Massive quote contains an invalid FX pair") from error
        quotes.append(
            CollectedQuote(
                provider="massive",
                account_environment="datafeed",
                instrument=instrument,
                provider_event_time=_event_time_from_ms(record.get("t")),
                received_at=received,
                bid=float(record["b"]),
                ask=float(record["a"]),
                bid_size=None,
                ask_size=None,
                tradable=tradable_allowed,
                sequence_id=None,
                connection_id=connection_id,
                writer_id=_writer_id(),
                revision_id=revision_id,
                raw_payload_sha256=raw_sha,
                source_endpoint_class=source_endpoint_class,
                collection_mode=collection_mode,
                quality_flags=("market_data_not_broker_execution_evidence",),
            )
        )
    return quotes


def _frame_has_status(raw: bytes, expected: str) -> bool:
    try:
        records = _records(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return any(
        str(record.get("ev") or "").lower() == "status"
        and str(record.get("status") or "").lower() == expected
        for record in records
    )


def websocket_transport(
    url: str,
    api_key: str,
    topics: tuple[str, ...],
) -> Iterator[bytes]:  # pragma: no cover - exercised with injected transports
    """Connect, authenticate, then subscribe using websocket-client."""

    import websocket

    connection = None
    auth_sent = False
    subscribed = False
    try:
        connection = websocket.create_connection(
            url,
            timeout=RECEIVE_TIMEOUT_SECONDS,
            enable_multithread=False,
        )
        while True:
            message = connection.recv()
            if message in (None, "", b""):
                raise ConnectionError("Massive WebSocket closed without a frame")
            raw = message if isinstance(message, bytes) else str(message).encode("utf-8")
            if not auth_sent and _frame_has_status(raw, "connected"):
                connection.send(json.dumps({"action": "auth", "params": api_key}))
                auth_sent = True
            if auth_sent and not subscribed and _frame_has_status(raw, "auth_success"):
                connection.send(json.dumps({"action": "subscribe", "params": ",".join(topics)}))
                subscribed = True
            yield raw
    except websocket.WebSocketBadStatusException as error:
        if getattr(error, "status_code", None) in (401, 403):
            raise TokenExpiredError("Massive WebSocket authorization rejected") from error
        raise ConnectionError("Massive WebSocket handshake failed") from error
    except TokenExpiredError:
        raise
    except (OSError, websocket.WebSocketException) as error:
        raise ConnectionError(
            f"Massive WebSocket transport failed: {type(error).__name__}"
        ) from error
    finally:
        if connection is not None:
            connection.close()


Transport = Callable[[str, str, tuple[str, ...]], Iterator[bytes]]
Clock = Callable[[], datetime]
StopPredicate = Callable[[], bool]


def stream_quotes(
    config: MassiveConfig,
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
    source_endpoint_class: EndpointClass = "replay_fixture",
    collection_mode: CollectionMode = "replay",
) -> tuple[ConnectionState, list[IngestResult]]:
    """Run Massive BBO frames through the immutable raw-first pipeline."""

    conn = state or ConnectionState(heartbeat_timeout_seconds=LIVENESS_TIMEOUT_SECONDS)
    policy = backoff or BackoffPolicy()
    randomness = rng or random.Random()
    wait = sleeper or time.sleep
    now_fn = clock or (lambda: datetime.now(UTC))
    stop_requested = should_stop or (lambda: False)
    results: list[IngestResult] = []
    topics = config.subscription_topics(instruments)
    consecutive_failures = 0
    messages_seen = 0

    while conn.stopped_reason is None:
        if stop_requested():
            conn.stop("stop_requested")
            break
        connection_id = conn.mark_connected(now_fn())
        received_healthy_message = False
        try:
            for raw in transport(WEBSOCKET_URL, config.api_key, topics):
                if stop_requested():
                    conn.stop("stop_requested")
                    break
                if not raw:
                    continue
                now = now_fn()
                alive = conn.check_alive(now)
                if alive:
                    conn.heartbeat(now)
                    if not received_healthy_message:
                        consecutive_failures = 0
                        received_healthy_message = True
                messages_seen += 1

                def _parser(frame: bytes, _cid: str = connection_id) -> list[CollectedQuote]:
                    return parse_message(
                        frame,
                        connection_id=_cid,
                        tradable_allowed=alive and conn.tradable,
                        received_at=now,
                        source_endpoint_class=source_endpoint_class,
                        collection_mode=collection_mode,
                    )

                results.append(
                    ingest_payload(raw, parser=_parser, store=store, log=log, now=lambda: now)
                )
                if not alive:
                    break
                if max_messages is not None and messages_seen >= max_messages:
                    conn.stop("max_messages_reached")
                    break
            else:
                conn.mark_disconnected(now_fn(), reason="stream_closed")
        except TokenExpiredError as error:
            conn.mark_disconnected(now_fn(), reason="token_expired")
            conn.stop(f"token_expired: {error}")
            break
        except (OSError, ConnectionError) as error:
            now = now_fn()
            if conn.connected and not conn.check_alive(now):
                pass
            elif conn.connected:
                conn.mark_disconnected(now, reason=f"transport_error: {error}")
        if conn.stopped_reason is not None:
            break
        if stop_requested():
            conn.stop("stop_requested")
            break
        if consecutive_failures >= max_reconnects:
            conn.stop("max_reconnects_exhausted")
            break
        wait(policy.delay(consecutive_failures, randomness))
        consecutive_failures += 1
    return conn, results


__all__ = [
    "ENV_API_KEY",
    "LIVENESS_TIMEOUT_SECONDS",
    "MassiveConfig",
    "CollectorConfigError",
    "WEBSOCKET_URL",
    "parse_message",
    "stream_quotes",
    "websocket_transport",
]
