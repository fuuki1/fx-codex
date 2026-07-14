"""Regression tests for production collector runtime behavior."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace

import pytest

import data_platform.collect.oanda as oanda
from data_platform.collect.oanda import OandaConfig, requests_transport, stream_quotes
from data_platform.collect.raw_first import QuoteLog, RawFirstError
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.fx_quote_collector import (
    EX_SOFTWARE,
    EX_TEMPFAIL,
    _write_state,
    main as daemon_main,
)
from tools.run_exclusive import ExclusiveLock

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _heartbeat_line() -> bytes:
    return json.dumps({"type": "HEARTBEAT", "time": datetime.now(UTC).isoformat()}).encode()


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


def test_reconnect_budget_resets_after_a_healthy_message(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")
    calls = {"count": 0}
    delays: list[float] = []

    def intermittent(_url: str, _token: str) -> Iterator[bytes]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("temporary disconnect")
        if calls["count"] in (2, 3):
            yield _heartbeat_line()
            raise ConnectionError("disconnect after a healthy message")
        yield _price_line()

    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=log,
        transport=intermittent,
        max_reconnects=1,
        max_messages=3,
        sleeper=delays.append,
        rng=random.Random(2),
    )

    assert state.stopped_reason == "max_messages_reached"
    assert calls["count"] == 4
    assert state.reconnect_count == 3
    assert len(delays) == 3
    assert sum(result.accepted_count for result in results) == 1


def test_injected_transport_defaults_to_replay_provenance(tmp_path: Path) -> None:
    store = ImmutableRawStore(tmp_path / "raw")
    log = QuoteLog(tmp_path / "log")

    state, results = stream_quotes(
        _config(),
        ["USD_JPY"],
        store=store,
        log=log,
        transport=lambda _url, _token: iter([_price_line()]),
        max_messages=1,
    )

    assert state.stopped_reason == "max_messages_reached"
    quote = results[0].accepted[0]
    assert quote.collection_mode == "replay"
    assert quote.source_endpoint_class == "replay_fixture"


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


def test_requests_transport_closes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self) -> None:
            self.closed = False

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            self.closed = True

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[bytes]:
            yield b"line"

    response = FakeResponse()
    fake_requests = SimpleNamespace(
        get=lambda *_args, **_kwargs: response,
        exceptions=SimpleNamespace(RequestException=RuntimeError),
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    assert list(requests_transport("https://example.invalid", "masked")) == [b"line"]
    assert response.closed is True


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


def test_log_bootstrap_fails_closed_on_malformed_row(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "quotes.jsonl").write_text('{"provider": "oanda"\n', encoding="utf-8")

    with pytest.raises(RawFirstError, match="malformed at line 1"):
        QuoteLog(log_dir)


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
    incidents = list((tmp_path / "state" / "incidents").glob("duplicate_writer_rejected_*.json"))
    assert len(incidents) == 1
    payload = json.loads(incidents[0].read_text())
    assert payload["type"] == "duplicate_writer_rejected"
    assert payload["severity"] == "critical"
    terminal = json.loads((tmp_path / "state" / "last_run.json").read_text())
    assert terminal["status"] == "duplicate_writer_rejected"
    assert terminal["exit_code"] == EX_TEMPFAIL


def test_unexpected_runtime_error_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FX_OANDA_API_TOKEN", "secret")
    monkeypatch.setenv("FX_OANDA_ACCOUNT_ID", "account")
    monkeypatch.setenv("FX_OANDA_ENV", "practice")
    monkeypatch.setattr(
        "tools.fx_quote_collector.stream_quotes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    code = daemon_main(["--output-root", str(tmp_path)])

    assert code == EX_SOFTWARE
    terminal = json.loads((tmp_path / "state" / "last_run.json").read_text())
    assert terminal["status"] == "runtime_failure"
    assert terminal["error_type"] == "RuntimeError"
    incidents = list((tmp_path / "state" / "incidents").glob("collector_runtime_failure_*.json"))
    assert len(incidents) == 1


def test_launchd_wrapper_exit_mapping_and_env_loading(tmp_path: Path) -> None:
    wrapper = REPO_ROOT / "scripts" / "run_quote_collector.sh"
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    environment = {**os.environ, "HOME": str(empty_home)}

    manual = subprocess.run(
        ["/bin/sh", str(wrapper), "--output-root", str(tmp_path / "out"), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert manual.returncode == 78

    launchd = subprocess.run(
        [
            "/bin/sh",
            str(wrapper),
            "--launchd",
            "--output-root",
            str(tmp_path / "out"),
            "--dry-run",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert launchd.returncode == 0

    configured_home = tmp_path / "configured-home"
    env_dir = configured_home / ".config" / "fx-codex"
    env_dir.mkdir(parents=True)
    env_file = env_dir / "collector.env"
    env_file.write_text(
        "FX_OANDA_API_TOKEN=TOP-SECRET\n"
        "FX_OANDA_ACCOUNT_ID=ACCOUNT-SECRET\n"
        "FX_OANDA_ENV=practice\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    configured_env = {
        **os.environ,
        "HOME": str(configured_home),
        "FX_CODEX_COLLECTOR_PYTHON": sys.executable,
    }
    configured = subprocess.run(
        ["/bin/sh", str(wrapper), "--output-root", str(tmp_path / "out"), "--dry-run"],
        env=configured_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0
    assert "TOP-SECRET" not in configured.stdout
    assert "ACCOUNT-SECRET" not in configured.stdout
    assert "***masked***" in configured.stdout
