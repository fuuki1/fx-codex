"""Massive FX BBO collector: contract, PIT, reconnect, and daemon safety."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

from data_platform.collect.contract import QuoteContractError
from data_platform.collect.massive import (
    ENV_API_KEY,
    CollectorConfigError,
    MassiveConfig,
    parse_message,
    stream_quotes,
    websocket_transport,
)
from data_platform.collect.raw_first import QuoteLog
from data_platform.collect.reconnect import TokenExpiredError
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.fx_massive_quote_collector import EX_CONFIG, EX_OK, main as daemon_main

NOW = datetime(2026, 8, 10, 3, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _quote_frame(
    *, pair: str = "USD/JPY", event_time: datetime | None = None, bid: float = 147.001
) -> bytes:
    stamp = event_time or datetime.now(UTC) - timedelta(milliseconds=100)
    return json.dumps(
        [
            {
                "ev": "C",
                "p": pair,
                "x": 48,
                "a": bid + 0.003,
                "b": bid,
                "t": int(stamp.timestamp() * 1000),
            }
        ]
    ).encode()


class TestMassiveConfig:
    def test_missing_key_fails_closed(self) -> None:
        with pytest.raises(CollectorConfigError, match=ENV_API_KEY):
            MassiveConfig.from_env({})

    def test_repr_masks_key(self) -> None:
        config = MassiveConfig.from_env({ENV_API_KEY: "do-not-print-this-key"})
        assert "do-not-print-this-key" not in repr(config)
        assert "***masked***" in repr(config)

    def test_topics_are_quote_only_and_normalized(self) -> None:
        config = MassiveConfig(api_key="secret")
        assert config.subscription_topics(["USDJPY", "EUR/USD", "GBP_USD"]) == (
            "C.USD-JPY",
            "C.EUR-USD",
            "C.GBP-USD",
        )

    def test_invalid_or_duplicate_pair_rejected(self) -> None:
        config = MassiveConfig(api_key="secret")
        with pytest.raises(CollectorConfigError):
            config.subscription_topics(["USDJPY", "USD/JPY"])
        with pytest.raises(CollectorConfigError):
            config.subscription_topics(["BTCUSD1"])


class TestMassiveParsing:
    def test_status_frames_are_preserved_but_emit_no_quote(self) -> None:
        raw = json.dumps(
            [{"ev": "status", "status": "auth_success", "message": "authenticated"}]
        ).encode()
        assert parse_message(raw, connection_id="c1", tradable_allowed=True, received_at=NOW) == []

    def test_quote_is_normalized_with_honest_missing_fields(self) -> None:
        raw = _quote_frame(event_time=NOW - timedelta(milliseconds=250))
        quote = parse_message(
            raw,
            connection_id="c1",
            tradable_allowed=True,
            received_at=NOW,
        )[0]
        assert quote.provider == "massive"
        assert quote.account_environment == "datafeed"
        assert quote.instrument == "USDJPY"
        assert quote.bid == pytest.approx(147.001)
        assert quote.ask == pytest.approx(147.004)
        assert quote.bid_size is None and quote.ask_size is None
        assert quote.sequence_id is None
        assert quote.tradable is True
        assert quote.revision_id == "exchange:48"
        assert "provider_does_not_supply_bid_size" in quote.quality_flags
        assert "market_data_not_broker_execution_evidence" in quote.quality_flags

    def test_auth_rejection_is_terminal_not_quarantined_as_schema_noise(self) -> None:
        raw = json.dumps(
            [{"ev": "status", "status": "auth_failed", "message": "authentication failed"}]
        ).encode()
        with pytest.raises(TokenExpiredError):
            parse_message(raw, connection_id="c", tradable_allowed=False, received_at=NOW)

    def test_future_and_crossed_quotes_fail_contract(self) -> None:
        future = _quote_frame(event_time=NOW + timedelta(seconds=3))
        with pytest.raises(QuoteContractError, match="future provider_event_time"):
            parse_message(future, connection_id="c", tradable_allowed=True, received_at=NOW)

        crossed = json.dumps(
            [{"ev": "C", "p": "USD/JPY", "x": 1, "a": 147.0, "b": 147.1, "t": 1_786_329_600_000}]
        ).encode()
        with pytest.raises(QuoteContractError, match="crossed"):
            parse_message(crossed, connection_id="c", tradable_allowed=True, received_at=NOW)


class TestMassiveStream:
    def test_transport_waits_for_connected_then_authenticates_and_subscribes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connected = json.dumps([{"ev": "status", "status": "connected"}])
        authenticated = json.dumps([{"ev": "status", "status": "auth_success"}])
        quote = _quote_frame().decode()

        class FakeConnection:
            def __init__(self) -> None:
                self.frames = iter([connected, authenticated, quote])
                self.sent: list[str] = []
                self.closed = False

            def recv(self) -> str:
                return next(self.frames)

            def send(self, payload: str) -> None:
                self.sent.append(payload)

            def close(self) -> None:
                self.closed = True

        class FakeWebSocketError(Exception):
            pass

        connection = FakeConnection()
        fake_websocket = SimpleNamespace(
            create_connection=lambda *_args, **_kwargs: connection,
            WebSocketBadStatusException=FakeWebSocketError,
            WebSocketException=FakeWebSocketError,
        )
        monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

        generator = websocket_transport(
            "wss://socket.massive.com/forex",
            "secret-key",
            ("C.USD-JPY",),
        )
        assert next(generator).decode() == connected
        assert json.loads(connection.sent[0]) == {"action": "auth", "params": "secret-key"}
        assert next(generator).decode() == authenticated
        assert json.loads(connection.sent[1]) == {
            "action": "subscribe",
            "params": "C.USD-JPY",
        }
        assert next(generator).decode() == quote
        generator.close()
        assert connection.closed is True

    def test_replay_ingests_quote_and_stops(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def transport(url: str, key: str, topics: tuple[str, ...]) -> Iterator[bytes]:
            assert url == "wss://socket.massive.com/forex"
            assert key == "secret"
            assert topics == ("C.USD-JPY",)
            yield json.dumps([{"ev": "status", "status": "auth_success"}]).encode()
            yield _quote_frame()

        state, results = stream_quotes(
            MassiveConfig(api_key="secret"),
            ["USDJPY"],
            store=store,
            log=log,
            transport=transport,
            max_messages=2,
        )

        assert state.stopped_reason == "max_messages_reached"
        assert sum(result.accepted_count for result in results) == 1
        accepted = results[-1].accepted[0]
        assert accepted.collection_mode == "replay"
        assert accepted.source_endpoint_class == "replay_fixture"

    def test_authorization_failure_stops_without_reconnect(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        calls = {"count": 0}

        def rejected(_url: str, _key: str, _topics: tuple[str, ...]) -> Iterator[bytes]:
            calls["count"] += 1
            yield json.dumps([{"ev": "status", "status": "auth_failed"}]).encode()

        state, results = stream_quotes(
            MassiveConfig(api_key="secret"),
            ["USDJPY"],
            store=store,
            log=log,
            transport=rejected,
            sleeper=lambda _seconds: None,
        )

        assert calls["count"] == 1
        assert state.stopped_reason is not None and "token_expired" in state.stopped_reason
        assert results == []


class TestMassiveDaemon:
    def test_dry_run_requires_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        assert daemon_main(["--output-root", str(tmp_path), "--dry-run"]) == EX_CONFIG

    def test_dry_run_masks_key_and_does_not_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(ENV_API_KEY, "TOP-SECRET-MASSIVE-KEY")
        output = tmp_path / "not-created"
        assert daemon_main(["--output-root", str(output), "--dry-run"]) == EX_OK
        text = capsys.readouterr().out
        assert "TOP-SECRET-MASSIVE-KEY" not in text
        assert "***masked***" in text
        assert not output.exists()

    def test_mode_600_env_file_only_accepts_massive_key(self, tmp_path: Path) -> None:
        env_file = tmp_path / "massive.env"
        env_file.write_text(f"{ENV_API_KEY}=secret\n", encoding="utf-8")
        env_file.chmod(0o600)
        assert (
            daemon_main(
                [
                    "--output-root",
                    str(tmp_path / "out"),
                    "--env-file",
                    str(env_file),
                    "--dry-run",
                ]
            )
            == EX_OK
        )
        env_file.write_text(f"{ENV_API_KEY}=secret\nOTHER=value\n", encoding="utf-8")
        assert (
            daemon_main(
                [
                    "--output-root",
                    str(tmp_path / "out"),
                    "--env-file",
                    str(env_file),
                    "--dry-run",
                ]
            )
            == EX_CONFIG
        )

    def test_launchd_template_contains_no_secret(self) -> None:
        template = REPO_ROOT / "ops/launchd/com.fx-codex.massive-quote-collector.plist.tmpl"
        text = template.read_text(encoding="utf-8")
        payload = plistlib.loads(
            text.replace("__ROOT__", "/tmp/fx").replace("__HOME__", "/Users/example").encode()
        )
        assert payload["Label"] == "com.fx-codex.massive-quote-collector"
        assert payload["ProgramArguments"][1] == "/tmp/fx/scripts/run_massive_quote_collector.sh"
        assert "FX_MASSIVE_API_KEY" not in text
