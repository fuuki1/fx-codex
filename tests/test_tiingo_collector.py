"""Tiingo Forex collector contract and failure-mode tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from data_platform.collect.contract import QuoteContractError
from data_platform.collect.raw_first import QuoteLog
from data_platform.collect.reconnect import TokenExpiredError
from data_platform.collect.tiingo import (
    ACCOUNT_ENVIRONMENT,
    CANONICAL_SOURCE,
    ENV_DERIVED_DATA_APPROVAL,
    ENV_PLAN,
    ENV_TOKEN,
    ENV_USAGE_SCOPE,
    PROVIDER,
    REQUIRED_USAGE_SCOPE,
    SOURCE_ENDPOINT_CLASS,
    THRESHOLD_LEVEL,
    WEBSOCKET_URL,
    CollectorConfigError,
    TiingoConfig,
    _validate_websocket_url,
    parse_price_message,
    stream_quotes,
    to_tiingo_tickers,
    websocket_transport,
)

NOW = datetime(2026, 7, 26, 5, 0, 1, tzinfo=UTC)


def _config(token: str = "not-logged") -> TiingoConfig:
    return TiingoConfig(
        token=token,
        plan="power",
        usage_scope=REQUIRED_USAGE_SCOPE,
        derived_data_approval_ref="tiingo-approval-2026-001",
    )


def _quote_message(
    *,
    ticker: str = "usdjpy",
    event_time: datetime = NOW - timedelta(seconds=1),
    bid: float = 155.001,
    mid: float = 155.0025,
    ask: float = 155.004,
) -> bytes:
    return json.dumps(
        {
            "messageType": "A",
            "service": "fx",
            "data": [
                "Q",
                ticker,
                event_time.isoformat(),
                1_000_000,
                bid,
                mid,
                ask,
                500_000,
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_config_requires_explicit_active_subscription_storage_attestation() -> None:
    with pytest.raises(CollectorConfigError, match=ENV_USAGE_SCOPE):
        TiingoConfig.from_env(
            {
                ENV_TOKEN: "secret",
                ENV_PLAN: "power",
                ENV_USAGE_SCOPE: "internal_use",
                ENV_DERIVED_DATA_APPROVAL: "tiingo-approval-2026-001",
            }
        )


def test_config_repr_masks_token_and_validates_plan() -> None:
    token = "TOP-SECRET-TIINGO-TOKEN"
    config = TiingoConfig.from_env(
        {
            ENV_TOKEN: token,
            ENV_PLAN: "power",
            ENV_USAGE_SCOPE: REQUIRED_USAGE_SCOPE,
            ENV_DERIVED_DATA_APPROVAL: "tiingo-approval-2026-001",
        }
    )

    assert token not in repr(config)
    assert "***masked***" in repr(config)
    with pytest.raises(CollectorConfigError, match=ENV_PLAN):
        TiingoConfig.from_env(
            {
                ENV_TOKEN: token,
                ENV_PLAN: "unknown",
                ENV_USAGE_SCOPE: REQUIRED_USAGE_SCOPE,
                ENV_DERIVED_DATA_APPROVAL: "tiingo-approval-2026-001",
            }
        )


def test_config_requires_written_derived_data_approval_reference() -> None:
    with pytest.raises(CollectorConfigError, match=ENV_DERIVED_DATA_APPROVAL):
        TiingoConfig.from_env(
            {
                ENV_TOKEN: "secret",
                ENV_PLAN: "power",
                ENV_USAGE_SCOPE: REQUIRED_USAGE_SCOPE,
                ENV_DERIVED_DATA_APPROVAL: "",
            }
        )


def test_subscription_requests_all_top_of_book_updates_for_mvp_universe() -> None:
    payload = json.loads(_config().subscription_message(["USD_JPY", "eur/usd"]))

    assert payload == {
        "eventName": "subscribe",
        "authorization": "not-logged",
        "eventData": {
            "thresholdLevel": THRESHOLD_LEVEL,
            "tickers": ["usdjpy", "eurusd"],
        },
    }
    assert to_tiingo_tickers(["GBPUSD", "AUD-USD"]) == ("gbpusd", "audusd")
    with pytest.raises(CollectorConfigError, match="unsupported operational"):
        to_tiingo_tickers(["NZDUSD"])


def test_quote_message_preserves_bid_ask_sizes_timestamps_and_limitations() -> None:
    raw = _quote_message()
    quote = parse_price_message(raw, connection_id="connection-1", received_at=NOW)[0]

    assert quote.provider == PROVIDER
    assert quote.account_environment == ACCOUNT_ENVIRONMENT
    assert quote.instrument == "USDJPY"
    assert quote.provider_event_time == NOW - timedelta(seconds=1)
    assert quote.received_at == NOW
    assert quote.bid == 155.001
    assert quote.ask == 155.004
    assert quote.bid_size == 1_000_000
    assert quote.ask_size == 500_000
    assert quote.tradable is False
    assert quote.source_endpoint_class == SOURCE_ENDPOINT_CLASS
    assert quote.collection_mode == "live_stream"
    assert "provider_does_not_supply_tradability" in quote.quality_flags
    assert "vendor_normalized_aggregated_feed" in quote.quality_flags
    assert CANONICAL_SOURCE == "tiingo_fx_top_of_book_bid_ask"


@pytest.mark.parametrize("message_type", ["H", "I"])
def test_heartbeat_and_info_messages_produce_no_quotes(message_type: str) -> None:
    raw = json.dumps({"messageType": message_type, "service": "fx", "data": []}).encode()
    assert parse_price_message(raw, connection_id="connection-1", received_at=NOW) == []


def test_invalid_quote_is_rejected_without_repair() -> None:
    with pytest.raises(QuoteContractError, match="crossed"):
        parse_price_message(
            _quote_message(bid=155.004, mid=155.004, ask=155.004),
            connection_id="connection-1",
            received_at=NOW,
        )
    with pytest.raises(QuoteContractError, match="future provider_event_time"):
        parse_price_message(
            _quote_message(event_time=NOW + timedelta(seconds=3)),
            connection_id="connection-1",
            received_at=NOW,
        )
    with pytest.raises(ValueError, match="mid price"):
        parse_price_message(
            _quote_message(mid=155.100),
            connection_id="connection-1",
            received_at=NOW,
        )
    with pytest.raises(ValueError, match="subscribed MVP universe"):
        parse_price_message(
            _quote_message(ticker="nzdusd"),
            connection_id="connection-1",
            received_at=NOW,
        )


def test_authorization_error_is_raw_first_quarantined_then_stops(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "messageType": "E",
            "service": "fx",
            "data": "Authorization token rejected",
        }
    ).encode()

    state, results = stream_quotes(
        _config(),
        ["USDJPY"],
        store=None,
        log=QuoteLog(tmp_path / "log", legacy_jsonl=False),
        transport=lambda _config, _tickers: iter([raw]),
    )

    assert state.stopped_reason == "token_expired: provider rejected authorization"
    assert len(results) == 1
    assert results[0].accepted == []
    assert len(results[0].quarantined) == 1


def test_information_envelope_with_auth_failure_stops_fail_closed(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "messageType": "I",
            "service": "fx",
            "response": {"code": 401, "message": "API token is invalid"},
        }
    ).encode()

    state, results = stream_quotes(
        _config(),
        ["USDJPY"],
        store=None,
        log=QuoteLog(tmp_path / "log", legacy_jsonl=False),
        transport=lambda _config, _tickers: iter([raw]),
    )

    assert state.stopped_reason == "token_expired: provider rejected authorization"
    assert len(results) == 1
    assert results[0].accepted == []
    assert len(results[0].quarantined) == 1


def test_reflected_token_is_never_written_to_raw_journal(tmp_path: Path) -> None:
    token = "credential-that-must-never-be-stored"
    raw = json.dumps(
        {
            "messageType": "E",
            "service": "fx",
            "data": f"rejected authorization={token}",
        }
    ).encode()
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)

    state, results = stream_quotes(
        _config(token),
        ["USDJPY"],
        store=None,
        log=log,
        transport=lambda _config, _tickers: iter([raw]),
    )

    assert state.stopped_reason == (
        "token_expired: provider response contained credential material"
    )
    assert results == []
    assert log.journal.entries() == ()


def test_stream_defaults_injected_transport_to_replay_provenance(tmp_path: Path) -> None:
    state, results = stream_quotes(
        _config(),
        ["USDJPY"],
        store=None,
        log=QuoteLog(tmp_path / "log", legacy_jsonl=False),
        transport=lambda _config, _tickers: iter([_quote_message()]),
        clock=lambda: NOW,
        max_messages=1,
    )

    assert state.stopped_reason == "max_messages_reached"
    quote = results[0].accepted[0]
    assert quote.source_endpoint_class == "replay_fixture"
    assert quote.collection_mode == "replay"


def test_transport_rejects_any_non_exact_token_destination() -> None:
    _validate_websocket_url(WEBSOCKET_URL)
    for url in (
        "ws://api.tiingo.com/fx",
        "wss://example.invalid/fx",
        "wss://api.tiingo.com/crypto",
        "wss://api.tiingo.com/fx?redirect=1",
    ):
        with pytest.raises(CollectorConfigError, match="exact WSS Forex endpoint"):
            _validate_websocket_url(url)


def test_transport_disables_redirects_sends_subscription_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WebSocketError(Exception):
        pass

    class ClosedError(WebSocketError):
        pass

    class TimeoutError(WebSocketError):
        pass

    class BadStatusError(WebSocketError):
        pass

    class FakeConnection:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.closed = False

        def send(self, value: str) -> None:
            self.sent.append(value)

        def recv(self) -> str:
            return _quote_message().decode()

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    observed: dict[str, object] = {}

    def create_connection(url: str, **kwargs: object) -> FakeConnection:
        observed["url"] = url
        observed.update(kwargs)
        return connection

    fake_websocket = SimpleNamespace(
        create_connection=create_connection,
        WebSocketBadStatusException=BadStatusError,
        WebSocketException=WebSocketError,
        WebSocketConnectionClosedException=ClosedError,
        WebSocketTimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

    generator: Iterator[bytes] = websocket_transport(_config(), ["USDJPY"])
    assert next(generator) == _quote_message()
    generator.close()

    assert observed["url"] == WEBSOCKET_URL
    assert observed["redirect_limit"] == 0
    assert observed["suppress_origin"] is True
    subscription = json.loads(connection.sent[0])
    assert subscription["eventData"]["tickers"] == ["usdjpy"]
    assert connection.closed is True


def test_transport_maps_handshake_unauthorized_without_leaking_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WebSocketError(Exception):
        pass

    class BadStatusError(WebSocketError):
        def __init__(self) -> None:
            super().__init__("server body containing a secret")
            self.status_code = 401

    fake_websocket = SimpleNamespace(
        create_connection=lambda *_args, **_kwargs: (_ for _ in ()).throw(BadStatusError()),
        WebSocketBadStatusException=BadStatusError,
        WebSocketException=WebSocketError,
    )
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

    with pytest.raises(TokenExpiredError, match="provider rejected authorization") as captured:
        next(websocket_transport(_config(), ["USDJPY"]))

    assert "server body" not in str(captured.value)
