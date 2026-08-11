from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import stat

import pytest

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.oanda import OandaConfig, parse_price_line, stream_quotes
from data_platform.collect.oanda_raw_shadow import OandaRawShadowHooks
from data_platform.collect.raw_first import QuoteLog, ingest_payload
from data_platform.raw.event_ledger import RawEventLedger
from data_platform.raw.immutable_store import ImmutableRawStore
from data_platform.raw.shadow import RawLedgerShadowWriter
from tools.raw_event_ledger_health import main as health_main
from tools import fx_quote_collector as collector_tool

NOW = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)


def _price_line(symbol: str = "USD_JPY") -> bytes:
    return json.dumps(
        {
            "type": "PRICE",
            "instrument": symbol,
            "time": NOW.isoformat(),
            "bids": [{"price": "150.001", "liquidity": 1_000_000}],
            "asks": [{"price": "150.004", "liquidity": 500_000}],
            "tradeable": True,
        }
    ).encode("utf-8")


def _config() -> OandaConfig:
    return OandaConfig(token="masked", account_id="masked", environment="practice")


def test_raw_shadow_hook_runs_after_blob_durability_and_before_parse(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")
    calls: list[str] = []

    class Hooks:
        def capture(
            self, _payload: bytes, *, raw_sha256: str, observed_at: datetime
        ) -> SimpleNamespace:
            assert store.exists(raw_sha256)
            assert observed_at == NOW
            calls.append("capture")
            return SimpleNamespace(status="queued", event_id="event-1")

        def quarantine(
            self, event_id: str, *, reason: str, observed_at: datetime
        ) -> SimpleNamespace:
            calls.append(f"quarantine:{event_id}:{reason}")
            return SimpleNamespace(status="queued", event_id=event_id)

    def parser(_payload: bytes) -> list[object]:
        assert calls == ["capture"]
        raise ValueError("malformed")

    result = ingest_payload(
        b"malformed",
        parser=parser,  # type: ignore[arg-type]
        store=store,
        log=log,
        now=lambda: NOW,
        raw_shadow=Hooks(),
    )
    assert result.raw_shadow_status == "queued"
    assert result.raw_shadow_event_id == "event-1"
    assert calls == ["capture", "quarantine:event-1:schema_validation_failed"]


def test_enabled_oanda_shadow_records_symbol_and_health(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "raw-ledger", blob_store=store)
    writer = RawLedgerShadowWriter(ledger, enabled=True)
    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=QuoteLog(tmp_path / "log"),
        transport=lambda _url, _token: iter([_price_line()]),
        clock=lambda: NOW,
        max_messages=1,
        raw_shadow_writer=writer,
    )
    assert state.stopped_reason == "max_messages_reached"
    assert results[0].raw_shadow_status == "queued"
    assert writer.drain()
    health = ledger.health_snapshot(
        now=NOW + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
        required_symbols=("USD_JPY",),
    )
    assert health["status"] == "ok"
    assert health["symbol_counts"] == {"USDJPY": 1}
    assert health["missing_required_symbols"] == []
    assert (
        health_main(
            [
                "--collection-root",
                str(tmp_path),
                "--stale-after-seconds",
                "1000000000",
                "--max-duplicate-ratio",
                "1",
                "--max-quarantine-ratio",
                "1",
                "--required-symbol",
                "USD_JPY",
            ]
        )
        == 0
    )
    assert '"status": "ok"' in capsys.readouterr().out


def test_enabled_oanda_shadow_covers_every_default_symbol_and_measures_loss(
    tmp_path: Path,
) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "raw-ledger", blob_store=store)
    writer = RawLedgerShadowWriter(ledger, enabled=True)
    required = collector_tool.DEFAULT_INSTRUMENTS

    state, results = stream_quotes(
        _config(),
        required,
        store=store,
        log=QuoteLog(tmp_path / "log"),
        transport=lambda _url, _token: iter(_price_line(symbol) for symbol in required),
        clock=lambda: NOW,
        max_messages=len(required),
        raw_shadow_writer=writer,
    )

    assert state.stopped_reason == "max_messages_reached"
    assert all(result.raw_shadow_status == "queued" for result in results)
    assert writer.drain()
    health = ledger.health_snapshot(
        now=NOW + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
        required_symbols=required,
    )
    assert health["status"] == "ok"
    assert health["symbol_counts"] == {"EURUSD": 1, "GBPUSD": 1, "USDJPY": 1}
    assert health["missing_required_symbols"] == []
    metrics = writer.metrics()
    assert metrics["raw_attempted"] == len(required)
    assert metrics["raw_stored"] == len(required)
    assert metrics["raw_failed"] == 0
    assert metrics["raw_failure_ratio"] == 0
    assert metrics["raw_queue_overflow_ratio"] == 0


def test_disabled_oanda_shadow_creates_no_ledger_output(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    writer = RawLedgerShadowWriter(RawEventLedger(ledger_root), enabled=False)
    _state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=ImmutableRawStore(tmp_path / "raw"),
        log=QuoteLog(tmp_path / "log"),
        transport=lambda _url, _token: iter([_price_line()]),
        clock=lambda: NOW,
        max_messages=1,
        raw_shadow_writer=writer,
    )
    assert results[0].accepted_count == 1
    assert results[0].raw_shadow_status == "disabled"
    assert not ledger_root.exists()


def test_disabled_hook_preserves_existing_quote_output_byte_for_byte(tmp_path: Path) -> None:
    def parser(payload: bytes) -> list[CollectedQuote]:
        return parse_price_line(
            payload,
            environment="practice",
            connection_id="fixed-connection",
            tradable_allowed=True,
            received_at=NOW,
            source_endpoint_class="replay_fixture",
            collection_mode="replay",
        )

    baseline_root = tmp_path / "baseline"
    disabled_root = tmp_path / "disabled"
    ingest_payload(
        _price_line(),
        parser=parser,
        store=ImmutableRawStore(baseline_root / "raw"),
        log=QuoteLog(baseline_root / "log"),
        now=lambda: NOW,
    )
    writer = RawLedgerShadowWriter(
        RawEventLedger(disabled_root / "ledger"),
        enabled=False,
    )
    ingest_payload(
        _price_line(),
        parser=parser,
        store=ImmutableRawStore(disabled_root / "raw"),
        log=QuoteLog(disabled_root / "log"),
        now=lambda: NOW,
        raw_shadow=OandaRawShadowHooks(
            writer,
            environment="practice",
            account_id="masked-account",
            connection_id="fixed-connection",
            source_endpoint_class="replay_fixture",
            collection_mode="replay",
            received_at=NOW,
        ),
    )
    assert (baseline_root / "log" / "quotes.jsonl").read_bytes() == (
        disabled_root / "log" / "quotes.jsonl"
    ).read_bytes()
    assert not (disabled_root / "ledger").exists()


def test_malformed_oanda_payload_is_retained_then_quarantined(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "ledger", blob_store=store)
    writer = RawLedgerShadowWriter(ledger, enabled=True)
    _state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=QuoteLog(tmp_path / "log"),
        transport=lambda _url, _token: iter([b"not-json"]),
        clock=lambda: NOW,
        max_messages=1,
        raw_shadow_writer=writer,
    )
    assert results[0].accepted_count == 0
    assert results[0].quarantined
    assert writer.drain()
    stats = ledger.stats()
    assert stats.total_events == 1
    assert stats.quarantined_events == 1


def test_shadow_restart_replays_same_delivery_idempotently(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "ledger", blob_store=store)
    payload = _price_line()
    raw_hash = store.put(payload).sha256

    first_writer = RawLedgerShadowWriter(ledger, enabled=True)
    first_hooks = OandaRawShadowHooks(
        first_writer,
        environment="practice",
        account_id="masked-account",
        connection_id="stable-connection",
        source_endpoint_class="replay_fixture",
        collection_mode="replay",
        received_at=NOW,
    )
    assert first_hooks.capture(payload, raw_sha256=raw_hash, observed_at=NOW).status == "queued"
    assert first_writer.drain()

    restarted_writer = RawLedgerShadowWriter(ledger, enabled=True)
    restarted_hooks = OandaRawShadowHooks(
        restarted_writer,
        environment="practice",
        account_id="masked-account",
        connection_id="stable-connection",
        source_endpoint_class="replay_fixture",
        collection_mode="replay",
        received_at=NOW,
    )
    assert restarted_hooks.capture(payload, raw_sha256=raw_hash, observed_at=NOW).status == "queued"
    assert restarted_writer.drain()
    assert ledger.stats().total_events == 1
    assert restarted_writer.metrics()["idempotent"] == 1


def test_reconnect_redelivery_is_retained_and_classified_duplicate(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "ledger", blob_store=store)
    payload = _price_line()
    raw_hash = store.put(payload).sha256

    first = RawLedgerShadowWriter(ledger, enabled=True)
    assert (
        OandaRawShadowHooks(
            first,
            environment="practice",
            account_id="masked-account",
            connection_id="connection-1",
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
            received_at=NOW,
        )
        .capture(payload, raw_sha256=raw_hash, observed_at=NOW)
        .status
        == "queued"
    )
    assert first.drain()

    redelivery_time = NOW + timedelta(seconds=1)
    restarted = RawLedgerShadowWriter(ledger, enabled=True)
    assert (
        OandaRawShadowHooks(
            restarted,
            environment="practice",
            account_id="masked-account",
            connection_id="connection-2",
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
            received_at=redelivery_time,
        )
        .capture(
            payload,
            raw_sha256=raw_hash,
            observed_at=redelivery_time,
        )
        .status
        == "queued"
    )
    assert restarted.drain()
    stats = ledger.stats()
    assert stats.total_events == 2
    assert stats.unique_payloads == 1
    assert stats.duplicate_events == 1


def test_same_provider_event_from_different_account_scope_is_not_duplicate(
    tmp_path: Path,
) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "ledger", blob_store=store)
    payload = _price_line()
    raw_hash = store.put(payload).sha256

    for offset, account_id in enumerate(("account-a", "account-b")):
        received_at = NOW + timedelta(seconds=offset)
        writer = RawLedgerShadowWriter(ledger, enabled=True)
        result = OandaRawShadowHooks(
            writer,
            environment="practice",
            account_id=account_id,
            connection_id=f"connection-{offset}",
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
            received_at=received_at,
        ).capture(payload, raw_sha256=raw_hash, observed_at=received_at)
        assert result.status == "queued"
        assert writer.drain()

    stats = ledger.stats()
    assert stats.total_events == 2
    assert stats.duplicate_events == 0


def test_shadow_ingest_time_is_observed_after_raw_blob_durability(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    ledger = RawEventLedger(tmp_path / "ledger", blob_store=store)
    writer = RawLedgerShadowWriter(ledger, enabled=True)
    ingest_observed_at = NOW + timedelta(milliseconds=250)
    stamps = iter((NOW - timedelta(seconds=1), NOW, ingest_observed_at))

    _state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=QuoteLog(tmp_path / "log"),
        transport=lambda _url, _token: iter([_price_line()]),
        clock=lambda: next(stamps),
        max_messages=1,
        raw_shadow_writer=writer,
    )

    assert results[0].accepted_count == 1
    assert writer.drain()
    assert ledger.stats().last_ingest_time == ingest_observed_at


def test_enabled_daemon_secures_existing_0755_root_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "collect"
    output_root.mkdir(mode=0o755)
    output_root.chmod(0o755)
    env_file = tmp_path / "collector.env"
    env_file.write_text(
        "FX_OANDA_API_TOKEN=token\n"
        "FX_OANDA_ACCOUNT_ID=account\n"
        "FX_OANDA_ENV=practice\n"
        "FX_RAW_LEDGER_ENABLED=true\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setattr(
        collector_tool,
        "isolated_requests_transport",
        lambda _url, _token: iter([_price_line()]),
    )
    monkeypatch.setattr(collector_tool, "python_runtime_isolated", lambda: True)
    monkeypatch.setattr(
        collector_tool,
        "critical_collector_code_sha256",
        lambda _root: "d" * 64,
    )
    monkeypatch.setattr(collector_tool, "file_sha256", lambda _path: "e" * 64)
    monkeypatch.setattr(
        collector_tool,
        "verified_collector_environment_identity",
        lambda *_args, **_kwargs: ("f" * 64, "1" * 64),
    )

    code = collector_tool.main(
        [
            "--output-root",
            str(output_root),
            "--env-file",
            str(env_file),
            "--max-messages",
            "1",
        ]
    )

    assert code == collector_tool.EX_OK
    assert stat.S_IMODE(output_root.stat().st_mode) == 0o700
    assert (output_root / "raw-ledger" / "events.sqlite3").is_file()
    metrics = json.loads((output_root / "state" / "raw_ledger_shadow_metrics.json").read_text())
    assert metrics["final"] is True
    assert metrics["drained"] is True
    assert metrics["writer_pid"] > 0
    assert metrics["observed_at"]
    assert metrics["queue_overflow_ratio"] == 0
