from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.capture_journal import JournalState
from data_platform.collect.raw_first import QuoteLog, ingest_payload
from data_platform.raw.immutable_store import ImmutableRawStore
from data_platform.runtime.bid_ask_bridge import (
    BidAskBridgeError,
    ReplayPosition,
    materialize_increment,
)

T0 = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)


def _load_snapshot_entries(path) -> list[dict]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for row in rows:
        payload = {key: value for key, value in row.items() if key != "content_hash"}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert row["content_hash"] == hashlib.sha256(encoded).hexdigest()
    return rows


def _quote(
    payload: bytes,
    second: int,
    bid: float,
    *,
    instrument: str = "USD_JPY",
) -> CollectedQuote:
    observed = T0 + timedelta(seconds=second)
    return CollectedQuote(
        provider="oanda",
        account_environment="practice",
        instrument=instrument,
        provider_event_time=observed,
        received_at=observed + timedelta(milliseconds=100),
        bid=bid,
        ask=bid + 0.02,
        bid_size=None,
        ask_size=None,
        tradable=True,
        sequence_id=None,
        connection_id="connection-1",
        writer_id="collector-1",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(payload).hexdigest(),
        source_endpoint_class="streaming_pricing",
        collection_mode="continuous",
    )


def _commit_quotes(
    log: QuoteLog,
    store: ImmutableRawStore,
    *rows: tuple[int, float],
) -> tuple[int, int]:
    payload = json.dumps(rows, separators=(",", ":")).encode("ascii")
    result = ingest_payload(
        payload,
        parser=lambda raw: [_quote(raw, second, bid) for second, bid in rows],
        store=store,
        log=log,
        now=lambda: T0 + timedelta(seconds=max(second for second, _bid in rows) + 1),
    )
    commit = next(
        entry
        for entry in log.ledger.entries()
        if entry.capture_id == result.capture_id and entry.state is JournalState.COMMITTED
    )
    return commit.sequence, result.accepted_count


def test_bridge_materializes_only_completed_minutes_and_replays_idempotently(tmp_path) -> None:
    log_directory = tmp_path / "log"
    log = QuoteLog(log_directory)
    store = ImmutableRawStore(tmp_path / "raw")
    first_commit, _count = _commit_quotes(
        log,
        store,
        (5, 150.00),
        (50, 150.10),
        (65, 150.20),
    )
    state = tmp_path / "state.json"
    output = tmp_path / "prices.jsonl"

    first = materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=1, seconds=31),
    )

    assert first.completed_bars == 1
    assert first.output_rows == 4
    assert first.appended_rows == 4
    assert first.next_position == ReplayPosition(first_commit, 2)
    rows = _load_snapshot_entries(output)
    assert {row["timeframe"] for row in rows} == {"15m", "1h", "4h", "1d"}
    assert {row["ts"] for row in rows} == {(T0 + timedelta(minutes=1)).isoformat()}
    assert all(row["ohlc_scope"] == "closed_bar_after_prediction" for row in rows)
    assert all(row["open_time"] == T0.isoformat() for row in rows)
    assert all(row["bid"] == pytest.approx(150.10) for row in rows)
    assert all(row["ask"] == pytest.approx(150.12) for row in rows)
    assert all(row["quote_count"] == 2 for row in rows)
    assert all(row["account_environment"] == "practice" for row in rows)
    assert all(row["collection_mode"] == "continuous" for row in rows)
    assert all(row["source_endpoint_class"] == "streaming_pricing" for row in rows)
    assert all(row["source_quality_states"] == ["usable"] for row in rows)
    assert all("source_coverage" not in row for row in rows)
    assert all(
        row["coverage_measurement"] == "unmeasured_provider_sampling_cadence" for row in rows
    )
    assert all("provider_sampling_cadence_unmeasured" in row["data_quality_flags"] for row in rows)

    # Simulate a crash after output fsync but before checkpoint replacement.
    state.unlink()
    replay = materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=1, seconds=31),
    )
    assert replay.completed_bars == 1
    assert replay.appended_rows == 0
    assert len(_load_snapshot_entries(output)) == 4

    second_commit, _count = _commit_quotes(log, store, (125, 150.30))
    advanced = materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=2, seconds=31),
    )
    assert advanced.completed_bars == 1
    assert advanced.appended_rows == 4
    assert advanced.next_position == ReplayPosition(second_commit, 0)
    assert len(_load_snapshot_entries(output)) == 8


def test_bridge_retains_first_open_quote_until_its_minute_is_complete(tmp_path) -> None:
    log_directory = tmp_path / "log"
    log = QuoteLog(log_directory)
    commit_sequence, accepted_count = _commit_quotes(
        log,
        ImmutableRawStore(tmp_path / "raw"),
        (5, 150.0),
        (65, 150.2),
    )
    state = tmp_path / "state.json"
    output = tmp_path / "prices.jsonl"

    result = materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=1, seconds=31),
    )
    assert result.next_position == ReplayPosition(commit_sequence, 1)
    assert result.appended_rows == 4

    resumed = materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=2, seconds=31),
    )
    assert resumed.start_position == ReplayPosition(commit_sequence, 1)
    assert resumed.next_position == ReplayPosition(commit_sequence, accepted_count)
    assert resumed.appended_rows == 4


def test_bridge_fails_closed_on_configuration_change(tmp_path) -> None:
    log_directory = tmp_path / "log"
    log = QuoteLog(log_directory)
    _commit_quotes(log, ImmutableRawStore(tmp_path / "raw"), (5, 150.0))
    state = tmp_path / "state.json"
    output = tmp_path / "prices.jsonl"
    materialize_increment(
        ingest_log_dir=log_directory,
        state_path=state,
        output_path=output,
        instruments=["USDJPY"],
        as_of=T0 + timedelta(minutes=1, seconds=31),
    )

    with pytest.raises(BidAskBridgeError, match="source/config changed"):
        materialize_increment(
            ingest_log_dir=log_directory,
            state_path=state,
            output_path=output,
            instruments=["USDJPY", "EURUSD"],
            as_of=T0 + timedelta(minutes=2),
        )
