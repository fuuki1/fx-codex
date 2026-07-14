"""Source-adapter tests: OANDA stream (replay + failure injection), Dukascopy
bi5 decoding/retry, and reconnect policy semantics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
import lzma
from pathlib import Path
import random
import struct

import pytest

from data_platform.collect import dukascopy
from data_platform.collect.oanda import (
    ENV_ACCOUNT,
    ENV_ENVIRONMENT,
    ENV_TOKEN,
    CollectorConfigError,
    OandaConfig,
    parse_price_line,
    stream_quotes,
)
from data_platform.collect.raw_first import QuoteLog
from data_platform.collect.reconnect import (
    BackoffPolicy,
    ConnectionState,
    TokenExpiredError,
)
from data_platform.raw.immutable_store import ImmutableRawStore

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _price_line(instrument: str = "USD_JPY", time_offset: float = 0.0) -> bytes:
    # Stream-loop tests must stamp relative to the WALL CLOCK: ingest assigns
    # received_at = now(), and the stale gate (correctly) quarantines quotes
    # whose event time lags received_at by more than the stale window.
    stamp = (datetime.now(UTC) + timedelta(seconds=time_offset)).isoformat()
    return json.dumps(
        {
            "type": "PRICE",
            "instrument": instrument,
            "time": stamp,
            "bids": [{"price": "155.001", "liquidity": 1000000}],
            "asks": [{"price": "155.004", "liquidity": 500000}],
            "tradeable": True,
        }
    ).encode()


class TestOandaConfig:
    def test_fail_closed_without_credentials(self) -> None:
        with pytest.raises(CollectorConfigError, match="never logged"):
            OandaConfig.from_env({})

    def test_partial_credentials_still_fail(self) -> None:
        with pytest.raises(CollectorConfigError):
            OandaConfig.from_env({ENV_TOKEN: "x", ENV_ACCOUNT: "a"})

    def test_invalid_environment_rejected(self) -> None:
        with pytest.raises(CollectorConfigError, match="practice"):
            OandaConfig.from_env({ENV_TOKEN: "x", ENV_ACCOUNT: "a", ENV_ENVIRONMENT: "sandbox"})

    def test_repr_masks_token(self) -> None:
        config = OandaConfig.from_env(
            {ENV_TOKEN: "SECRET-TOKEN-VALUE", ENV_ACCOUNT: "acc-1", ENV_ENVIRONMENT: "practice"}
        )
        assert "SECRET-TOKEN-VALUE" not in repr(config)
        assert "***masked***" in repr(config)

    def test_stream_url_is_pricing_only(self) -> None:
        config = OandaConfig(token="t", account_id="acc-1", environment="practice")
        url = config.stream_url(["USD_JPY", "EUR_USD"])
        assert "/pricing/stream" in url
        assert "practice" in url
        for forbidden in ("/orders", "/trades", "/positions"):
            assert forbidden not in url


class TestOandaParsing:
    def test_price_line_parsed_with_sizes(self) -> None:
        quotes = parse_price_line(
            _price_line(time_offset=-0.1),
            environment="practice",
            connection_id="c1",
            tradable_allowed=True,
        )
        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.instrument == "USDJPY"
        assert quote.bid == pytest.approx(155.001)
        assert quote.bid_size == pytest.approx(1000000.0)
        assert quote.tradable is True
        assert quote.collection_mode == "live_stream"

    def test_heartbeat_produces_no_quote(self) -> None:
        line = json.dumps({"type": "HEARTBEAT", "time": NOW.isoformat()}).encode()
        assert (
            parse_price_line(line, environment="practice", connection_id="c", tradable_allowed=True)
            == []
        )

    def test_tradable_forced_false_while_disconnected(self) -> None:
        quotes = parse_price_line(
            _price_line(time_offset=-0.1),
            environment="practice",
            connection_id="c1",
            tradable_allowed=False,  # reconnecting
        )
        assert quotes[0].tradable is False

    def test_malformed_price_line_raises(self) -> None:
        bad = json.dumps({"type": "PRICE", "instrument": "USD_JPY", "time": NOW.isoformat()})
        with pytest.raises(ValueError, match="top-of-book"):
            parse_price_line(
                bad.encode(), environment="practice", connection_id="c", tradable_allowed=True
            )


class TestOandaStreamLoop:
    def _config(self) -> OandaConfig:
        return OandaConfig(token="tok", account_id="acc", environment="practice")

    def test_replay_stream_ingests_and_stops(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def transport(url: str, token: str) -> Iterator[bytes]:
            assert "/pricing/stream" in url
            yield _price_line(time_offset=-1.0)
            yield json.dumps({"type": "HEARTBEAT"}).encode()
            yield _price_line(time_offset=-0.5)

        state, results = stream_quotes(
            self._config(),
            ["USD_JPY"],
            store=store,
            log=log,
            transport=transport,
            max_messages=3,
        )
        accepted = sum(r.accepted_count for r in results)
        assert accepted == 2
        assert state.stopped_reason == "max_messages_reached"

    def test_disconnect_reconnects_with_gap_and_counter(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        calls = {"n": 0}

        def flaky(url: str, token: str) -> Iterator[bytes]:
            calls["n"] += 1
            if calls["n"] == 1:
                yield _price_line(time_offset=-2.0)
                raise ConnectionError("network dropped")
            yield _price_line(time_offset=-0.2)

        delays: list[float] = []
        state, results = stream_quotes(
            self._config(),
            ["USD_JPY"],
            store=store,
            log=log,
            transport=flaky,
            max_messages=2,
            rng=random.Random(7),
            sleeper=delays.append,
        )
        assert calls["n"] == 2
        assert state.reconnect_count >= 1
        assert delays and delays[0] > 0.0  # backoff actually waited
        assert any(gap.reason.startswith("transport_error") for gap in state.gaps)
        assert sum(r.accepted_count for r in results) == 2

    def test_token_expiry_stops_fail_closed(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def dead(url: str, token: str) -> Iterator[bytes]:
            raise TokenExpiredError("HTTP 401")
            yield b""  # pragma: no cover

        state, results = stream_quotes(
            self._config(), ["USD_JPY"], store=store, log=log, transport=dead
        )
        assert state.stopped_reason is not None
        assert "token_expired" in state.stopped_reason
        assert results == []

    def test_reconnect_budget_exhausts_and_stops(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def always_down(url: str, token: str) -> Iterator[bytes]:
            raise ConnectionError("still down")
            yield b""  # pragma: no cover

        state, _ = stream_quotes(
            self._config(),
            ["USD_JPY"],
            store=store,
            log=log,
            transport=always_down,
            max_reconnects=2,
            sleeper=lambda _s: None,
        )
        assert state.stopped_reason == "max_reconnects_exhausted"
        assert state.reconnect_count >= 2


class TestReconnectPolicy:
    def test_backoff_grows_and_caps(self) -> None:
        policy = BackoffPolicy(
            initial_seconds=1.0, factor=2.0, max_seconds=8.0, jitter_fraction=0.0
        )
        rng = random.Random(0)
        delays = [policy.delay(a, rng) for a in range(6)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    def test_jitter_stays_in_bounds(self) -> None:
        policy = BackoffPolicy(
            initial_seconds=4.0, factor=1.0, max_seconds=4.0, jitter_fraction=0.25
        )
        rng = random.Random(42)
        for attempt in range(50):
            delay = policy.delay(attempt, rng)
            assert 3.0 <= delay <= 5.0

    def test_heartbeat_timeout_opens_gap_and_blocks_tradable(self) -> None:
        state = ConnectionState(heartbeat_timeout_seconds=5.0)
        state.mark_connected(NOW)
        assert state.tradable is True
        assert state.check_alive(NOW + timedelta(seconds=3)) is True
        assert state.check_alive(NOW + timedelta(seconds=10)) is False
        assert state.tradable is False
        assert state.gaps and state.gaps[-1].reason == "heartbeat_timeout"
        assert state.gaps[-1].ended_at is None  # gap stays open until reconnect
        state.mark_connected(NOW + timedelta(seconds=20))
        assert state.gaps[-1].ended_at is not None  # gap explicitly closed, not backfilled


class TestDukascopy:
    def _payload(self, ticks: list[tuple[int, int, int, float, float]]) -> bytes:
        raw = b"".join(struct.pack(">IIIff", *tick) for tick in ticks)
        return lzma.compress(raw)

    def test_decode_jpy_point_scaling(self) -> None:
        payload = self._payload([(1500, 155004, 155001, 0.75, 1.5)])
        context = dukascopy.HourContext(
            instrument="USDJPY",
            hour_start_utc=NOW.replace(minute=0, second=0, microsecond=0),
            received_at=NOW,
            connection_id="d1",
        )
        quotes = dukascopy.parse_hour_payload(payload, context)
        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.bid == pytest.approx(155.001)
        assert quote.ask == pytest.approx(155.004)
        assert quote.bid_size == pytest.approx(1.5)
        assert quote.tradable is False
        assert quote.collection_mode == "historical_download"

    def test_decode_five_digit_point_scaling(self) -> None:
        payload = self._payload([(10, 108505, 108500, 0.1, 0.2)])
        context = dukascopy.HourContext(
            instrument="EURUSD",
            hour_start_utc=NOW.replace(minute=0, second=0, microsecond=0),
            received_at=NOW,
            connection_id="d1",
        )
        quote = dukascopy.parse_hour_payload(payload, context)[0]
        assert quote.bid == pytest.approx(1.08500)
        assert quote.ask == pytest.approx(1.08505)

    def test_month_is_zero_indexed_in_url(self) -> None:
        url = dukascopy.hour_url("USDJPY", datetime(2024, 1, 10, 12, tzinfo=UTC))
        assert "/USDJPY/2024/00/10/12h_ticks.bi5" in url

    def test_truncated_payload_rejected(self) -> None:
        broken = lzma.compress(b"\x00" * 19)
        context = dukascopy.HourContext(
            instrument="USDJPY", hour_start_utc=NOW, received_at=NOW, connection_id="d1"
        )
        with pytest.raises(ValueError, match="whole number"):
            dukascopy.parse_hour_payload(broken, context)

    def test_retry_on_503_then_success(self) -> None:
        attempts = {"n": 0}

        def fetcher(url: str) -> tuple[int, bytes]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return 503, b"unavailable"
            return 200, self._payload([(1, 155004, 155001, 0.1, 0.1)])

        payload = dukascopy.fetch_hour_payload(
            "USDJPY", NOW, fetcher=fetcher, sleeper=lambda _s: None
        )
        assert attempts["n"] == 3 and payload

    def test_persistent_503_fails_closed(self) -> None:
        def fetcher(url: str) -> tuple[int, bytes]:
            return 503, b""

        with pytest.raises(dukascopy.DukascopyFetchError, match="HTTP 503"):
            dukascopy.fetch_hour_payload(
                "USDJPY", NOW, fetcher=fetcher, max_attempts=2, sleeper=lambda _s: None
            )

    def test_404_means_no_hour_not_fabricated(self) -> None:
        payload = dukascopy.fetch_hour_payload(
            "USDJPY", NOW, fetcher=lambda _u: (404, b"x"), sleeper=lambda _s: None
        )
        assert payload == b""
