"""Tiingo Forex WebSocket top-of-book collector.

Official interfaces and storage terms:

- Forex WebSocket: https://www.tiingo.com/documentation/websockets/forex
- Terms of Service: https://api.tiingo.com/tos/
- Plans: https://www.tiingo.com/about/pricing

Tiingo permits local storage for internal consumption while the applicable
subscription remains active. Its terms require deletion of Tiingo data when
that subscription ends and express written approval to create or retain
Derived Data. The collector therefore requires both an explicit usage
attestation and a written-approval reference, while logging neither the API
token nor approval reference. This module is market-data-only and has no
account or broker API.

The Forex WebSocket publishes vendor-normalized aggregate top-of-book updates,
not venue-native dealer flow.  Quotes retain that limitation in quality flags.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
import random
import re
import socket
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload
from data_platform.collect.reconnect import (
    BackoffPolicy,
    ConnectionState,
    TokenExpiredError,
)
from data_platform.raw.immutable_store import ImmutableRawStore
from fx_intel.universe import normalize_mvp_symbols

ENV_TOKEN = "FX_TIINGO_API_TOKEN"
ENV_PLAN = "FX_TIINGO_PLAN"
ENV_USAGE_SCOPE = "FX_TIINGO_USAGE_SCOPE"
ENV_DERIVED_DATA_APPROVAL = "FX_TIINGO_DERIVED_DATA_APPROVAL_REF"
REQUIRED_USAGE_SCOPE = "internal_nonredisplay_active_subscription"

PROVIDER = "tiingo"
ACCOUNT_ENVIRONMENT = "datafeed"
SOURCE_ENDPOINT_CLASS: Literal["tiingo_fx_websocket"] = "tiingo_fx_websocket"
CANONICAL_SOURCE = "tiingo_fx_top_of_book_bid_ask"
SOURCE_RECORD_PREFIX = "tiingo-fx"
WEBSOCKET_URL = "wss://api.tiingo.com/fx"
HEARTBEAT_TIMEOUT_SECONDS = 45.0
THRESHOLD_LEVEL = 5
ALLOWED_PLANS = frozenset({"free", "power", "commercial"})
_APPROVAL_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}")

CollectionMode = Literal["live_stream", "replay"]
EndpointClass = Literal["tiingo_fx_websocket", "replay_fixture"]


class CollectorConfigError(RuntimeError):
    """Missing or invalid collector configuration."""


@dataclass(frozen=True)
class TiingoConfig:
    token: str
    plan: str
    usage_scope: str
    derived_data_approval_ref: str

    def __repr__(self) -> str:
        return (
            "TiingoConfig(token='***masked***', "
            f"plan='{self.plan}', usage_scope='{self.usage_scope}', "
            "derived_data_approval_ref='***masked***')"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TiingoConfig:
        source = os.environ if env is None else env
        token = source.get(ENV_TOKEN, "").strip()
        plan = source.get(ENV_PLAN, "").strip().lower()
        usage_scope = source.get(ENV_USAGE_SCOPE, "").strip().lower()
        approval_ref = source.get(ENV_DERIVED_DATA_APPROVAL, "").strip()
        missing = [
            name
            for name, value in (
                (ENV_TOKEN, token),
                (ENV_PLAN, plan),
                (ENV_USAGE_SCOPE, usage_scope),
                (ENV_DERIVED_DATA_APPROVAL, approval_ref),
            )
            if not value
        ]
        if missing:
            raise CollectorConfigError(
                f"Tiingo collector is not configured; set {', '.join(missing)} "
                "(values are never logged). Refusing to run without an explicit "
                "active-subscription storage attestation and Derived Data approval."
            )
        if plan not in ALLOWED_PLANS:
            raise CollectorConfigError(
                f"{ENV_PLAN} must be one of {sorted(ALLOWED_PLANS)} "
                "(the supplied value is not logged)"
            )
        if usage_scope != REQUIRED_USAGE_SCOPE:
            raise CollectorConfigError(
                f"{ENV_USAGE_SCOPE} must be exactly {REQUIRED_USAGE_SCOPE!r}; "
                "this attests internal non-display use and an active subscription"
            )
        if _APPROVAL_REF_PATTERN.fullmatch(approval_ref) is None:
            raise CollectorConfigError(
                f"{ENV_DERIVED_DATA_APPROVAL} must be a 3-128 character reference "
                "to Tiingo's express written Derived Data approval "
                "(the supplied value is not logged)"
            )
        return cls(
            token=token,
            plan=plan,
            usage_scope=usage_scope,
            derived_data_approval_ref=approval_ref,
        )

    def subscription_message(self, instruments: Sequence[str]) -> str:
        tickers = to_tiingo_tickers(instruments)
        payload = {
            "eventName": "subscribe",
            "authorization": self.token,
            "eventData": {
                "thresholdLevel": THRESHOLD_LEVEL,
                "tickers": list(tickers),
            },
        }
        return json.dumps(payload, separators=(",", ":"), allow_nan=False)


def to_tiingo_tickers(instruments: Sequence[str]) -> tuple[str, ...]:
    """Normalize the supported operational universe to Tiingo ticker syntax."""

    try:
        symbols = normalize_mvp_symbols(instruments)
    except ValueError as error:
        raise CollectorConfigError(str(error)) from error
    return tuple(symbol.lower() for symbol in symbols)


def _validate_websocket_url(url: str) -> None:
    """Allow the token to reach only Tiingo's exact encrypted Forex socket."""

    parsed = urlsplit(url)
    if (
        parsed.scheme != "wss"
        or parsed.hostname != "api.tiingo.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/fx"
        or parsed.query
        or parsed.fragment
    ):
        raise CollectorConfigError(
            "refusing to send the Tiingo token outside the exact WSS Forex endpoint"
        )


def _writer_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _message_payload(line: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Tiingo WebSocket message is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Tiingo WebSocket message must be a JSON object")
    return payload


def _message_type(line: bytes) -> str | None:
    try:
        value = _message_payload(line).get("messageType")
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _control_exception(line: bytes) -> Exception | None:
    """Classify provider error envelopes without returning provider text."""

    try:
        payload = _message_payload(line)
    except ValueError:
        return None
    kind = payload.get("messageType")
    if kind not in ("E", "I"):
        return None
    if kind == "I":
        response = payload.get("response")
        code = response.get("code") if isinstance(response, dict) else None
        if isinstance(code, bool) or not isinstance(code, int) or code < 400:
            return None
    detail = json.dumps(payload, sort_keys=True).lower()
    authorization_markers = (
        "auth",
        "token",
        "api key",
        "apikey",
        "unauthorized",
        "forbidden",
    )
    if any(marker in detail for marker in authorization_markers):
        return TokenExpiredError("provider rejected authorization")
    return ConnectionError("provider reported a WebSocket error")


def _optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be numeric or null")
    return float(value)


def parse_price_message(
    line: bytes,
    *,
    connection_id: str,
    received_at: datetime | None = None,
    source_endpoint_class: EndpointClass = SOURCE_ENDPOINT_CLASS,
    collection_mode: CollectionMode = "live_stream",
) -> list[CollectedQuote]:
    """Parse one exact Tiingo WebSocket message.

    ``H`` heartbeat and ``I`` informational messages produce no quote.  Error
    messages are quarantined by the raw-first pipeline and then classified by
    :func:`stream_quotes`.
    """

    received = received_at or datetime.now(UTC)
    payload = _message_payload(line)
    kind = payload.get("messageType")
    if kind == "H":
        return []
    if kind == "I":
        if _control_exception(line) is not None:
            raise ValueError("Tiingo WebSocket information message reported an error")
        return []
    if kind == "E":
        raise ValueError("Tiingo WebSocket reported an error")
    if kind != "A":
        raise ValueError(f"unsupported Tiingo messageType: {kind!r}")
    if payload.get("service") != "fx":
        raise ValueError("Tiingo message service must be 'fx'")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 8:
        raise ValueError("Tiingo FX quote data must contain exactly 8 fields")
    if data[0] != "Q":
        raise ValueError("Tiingo FX message is not a top-of-book quote")

    try:
        provider_time = datetime.fromisoformat(str(data[2]))
    except ValueError as error:
        raise ValueError("Tiingo quote datetime is invalid") from error
    if provider_time.tzinfo is None or provider_time.utcoffset() is None:
        raise ValueError("Tiingo quote datetime must be timezone-aware")

    bid = _optional_float(data[4], "bidPrice")
    mid = _optional_float(data[5], "midPrice")
    ask = _optional_float(data[6], "askPrice")
    if bid is None or ask is None or mid is None:
        raise ValueError("Tiingo quote requires bid, mid and ask prices")
    if not bid <= mid <= ask:
        raise ValueError("Tiingo mid price must lie between bid and ask")
    try:
        instrument = normalize_mvp_symbols([str(data[1])])[0]
    except ValueError as error:
        raise ValueError("Tiingo quote ticker is outside the subscribed MVP universe") from error

    raw_sha = hashlib.sha256(line).hexdigest()
    return [
        CollectedQuote(
            provider=PROVIDER,
            account_environment=ACCOUNT_ENVIRONMENT,
            instrument=instrument,
            provider_event_time=provider_time,
            received_at=received,
            bid=bid,
            ask=ask,
            bid_size=_optional_float(data[3], "bidSize"),
            ask_size=_optional_float(data[7], "askSize"),
            tradable=False,
            sequence_id=None,
            connection_id=connection_id,
            writer_id=_writer_id(),
            revision_id=None,
            raw_payload_sha256=raw_sha,
            source_endpoint_class=source_endpoint_class,
            collection_mode=collection_mode,
            quality_flags=(
                "provider_does_not_supply_tradability",
                "vendor_normalized_aggregated_feed",
            ),
        )
    ]


def websocket_transport(
    config: TiingoConfig,
    instruments: Sequence[str],
) -> Iterator[bytes]:  # pragma: no cover - exercised through an injected socket
    """Connect to Tiingo's exact WSS endpoint and yield message bytes."""

    import websocket

    _validate_websocket_url(WEBSOCKET_URL)
    connection = None
    try:
        connection = websocket.create_connection(
            WEBSOCKET_URL,
            timeout=HEARTBEAT_TIMEOUT_SECONDS,
            redirect_limit=0,
            suppress_origin=True,
        )
        connection.send(config.subscription_message(instruments))
        while True:
            message = connection.recv()
            if isinstance(message, str):
                yield message.encode("utf-8")
            elif isinstance(message, bytes):
                yield message
            elif message is None:
                raise ConnectionError("Tiingo WebSocket closed")
            else:
                raise ConnectionError("Tiingo WebSocket returned an unsupported frame")
    except TokenExpiredError:
        raise
    except websocket.WebSocketBadStatusException as error:
        if getattr(error, "status_code", None) in (401, 403):
            raise TokenExpiredError("provider rejected authorization") from error
        raise ConnectionError(f"Tiingo handshake failed: {type(error).__name__}") from error
    except (
        websocket.WebSocketException,
        OSError,
    ) as error:
        raise ConnectionError(f"Tiingo transport failed: {type(error).__name__}") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


Transport = Callable[[TiingoConfig, Sequence[str]], Iterator[bytes]]
Clock = Callable[[], datetime]
StopPredicate = Callable[[], bool]


def stream_quotes(
    config: TiingoConfig,
    instruments: Sequence[str],
    *,
    store: ImmutableRawStore | None,
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
    retain_results: bool = True,
    on_result: Callable[[IngestResult], None] | None = None,
) -> tuple[ConnectionState, list[IngestResult]]:
    """Run Tiingo messages through raw-first ingestion and bounded reconnects."""

    tickers = to_tiingo_tickers(instruments)
    conn = state or ConnectionState(heartbeat_timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS)
    policy = backoff or BackoffPolicy()
    randomness = rng or random.Random()
    wait = sleeper or time.sleep
    now_fn = clock or (lambda: datetime.now(UTC))
    stop_requested = should_stop or (lambda: False)
    results: list[IngestResult] = []
    consecutive_failures = 0
    messages_seen = 0
    token_bytes = config.token.encode("utf-8")
    escaped_token_bytes = json.dumps(config.token)[1:-1].encode("utf-8")

    while conn.stopped_reason is None:
        if stop_requested():
            conn.stop("stop_requested")
            break
        connection_id = conn.mark_connected(now_fn())
        received_healthy_message = False
        try:
            for line in transport(config, tickers):
                if stop_requested():
                    conn.stop("stop_requested")
                    break
                if not line:
                    continue
                if len(token_bytes) >= 16 and (token_bytes in line or escaped_token_bytes in line):
                    # Security exception to raw-first: a provider response that
                    # reflects the credential must never make the secret durable.
                    raise TokenExpiredError("provider response contained credential material")
                now = now_fn()
                alive = conn.check_alive(now)
                kind = _message_type(line)
                if alive and kind in ("A", "H", "I"):
                    conn.heartbeat(now)
                    if not received_healthy_message:
                        consecutive_failures = 0
                        received_healthy_message = True
                messages_seen += 1

                def _parser(raw: bytes, _cid: str = connection_id) -> list[CollectedQuote]:
                    return parse_price_message(
                        raw,
                        connection_id=_cid,
                        received_at=now,
                        source_endpoint_class=source_endpoint_class,
                        collection_mode=collection_mode,
                    )

                control_error = _control_exception(line)
                result = ingest_payload(line, parser=_parser, store=store, log=log, now=lambda: now)
                if on_result is not None:
                    on_result(result)
                if retain_results:
                    results.append(result)
                if control_error is not None:
                    raise control_error
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
