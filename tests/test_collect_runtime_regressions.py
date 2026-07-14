"""Regression tests for production collector runtime behavior."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random

import pytest

import data_platform.collect.oanda as oanda
from data_platform.collect.oanda import OandaConfig, stream_quotes
from data_platform.collect.raw_first import QuoteLog
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.fx_quote_collector import EX_TEMPFAIL, _write_state, main as daemon_main
from tools.run_exclusive import ExclusiveLock

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _price_line() -> bytes:
    return json.dumps(
        {
            "type": "PRICE",
            "instrument": "USD_JPY",
            "time": datetime.now(UTC).isoformat(),
            "bids": [{"price": "155.001", "liquidity": 1_000_000}],
            "asks": [{"price": "155.004", "liquidity": 500_000}],
            "tradeable": True,
        }
    ).encode()


def _config() -> OandaConfig:
    return OandaConfig(token="tok", account_id="acc", environment="practice")


def test_default_reconnect_path_calls_real_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")
    calls = {"count": 0}
    delays: list[float] = []

    def flaky(_url: str, _token: str) -> Iterator[bytes]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("temporary disconnect")
        yield _price_line()

    monkeypatch.setattr(oanda.time, "sleep", delays.append)
    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=log,
        transport=flaky,
        rng=random.Random(7),
        max_messages=1,
    )

    assert state.stopped_reason == "max_messages_reached"
    assert sum(result.accepted_count for result in results) == 1
    assert delays and delays[0] > 0.0


def test_transport_timeout_is_classified_as_heartbeat_gap(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")
    timestamps = iter([NOW, NOW + timedelta(seconds=16)])

    def timed_out(_url: str, _token: str) -> Iterator[bytes]:
        raise ConnectionError("read timeout")
        yield b""  # pragma: no cover

    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=log,
        transport=timed_out,
        clock=lambda: next(timestamps),
        max_reconnects=0,
    )

    assert results == []
    assert state.stopped_reason == "max_reconnects_exhausted"
    assert state.gaps
    assert state.gaps[-1].reason == "heartbeat_timeout"
    assert state.tradable is False


def test_stop_predicate_stops_before_next_message(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")
    stop_answers = iter([False, False, True])

    def transport(_url: str, _token: str) -> Iterator[bytes]:
        yield _price_line()
        yield _price_line()

    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=log,
        transport=transport,
        should_stop=lambda: next(stop_answers),
    )

    assert state.stopped_reason == "stop_requested"
    assert sum(result.accepted_count for result in results) == 1


def test_state_file_is_atomically_replaced(tmp_path: Path) -> None:
    path = tmp_path / "state" / "last_run.json"
    _write_state(path, {"status": "first", "count": 1})
    _write_state(path, {"status": "second", "count": 2})

    assert json.loads(path.read_text()) == {"count": 2, "status": "second"}
    assert list(path.parent.glob(".*.tmp")) == []


def test_duplicate_writer_creates_incident_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FX_OANDA_API_TOKEN", "secret")
    monkeypatch.setenv("FX_OANDA_ACCOUNT_ID", "account")
    monkeypatch.setenv("FX_OANDA_ENV", "practice")
    held = ExclusiveLock("quote-collector", locks_dir=tmp_path / "state")
    assert held.acquire() is True
    try:
        code = daemon_main(["--output-root", str(tmp_path)])
    finally:
        held.release()

    assert code == EX_TEMPFAIL
    incidents = list((tmp_path / "state" / "incidents").glob("lock_collision_*.json"))
    assert len(incidents) == 1
    payload = json.loads(incidents[0].read_text())
    assert payload["type"] == "duplicate_writer_rejected"
    assert payload["severity"] == "critical"
