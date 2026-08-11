from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from data_platform.collect.contract import CollectedQuote
from fx_intel.quote_tape_index import (
    QuoteIndexError,
    load_indexed_quote_payloads,
    sync_quote_index,
)
from fx_intel.virtual_portfolio_replay import load_quote_tape

START = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)


def _tick(moment: datetime, *, symbol: str = "USDJPY") -> dict[str, object]:
    identity = f"{symbol}:{moment.isoformat()}".encode()
    return CollectedQuote(
        provider="dukascopy",
        account_environment="datafeed",
        instrument=symbol,
        provider_event_time=moment,
        received_at=START + timedelta(hours=2),
        bid=150.0,
        ask=150.02,
        bid_size=1.0,
        ask_size=1.0,
        tradable=False,
        sequence_id=None,
        connection_id="historical-fixture",
        writer_id="fixture",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(identity).hexdigest(),
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    ).to_dict()


def _encoded(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_quote_index_bootstrap_then_reads_only_appended_bytes(tmp_path) -> None:
    source = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    first = _encoded(_tick(START + timedelta(minutes=1)))
    second = _encoded(_tick(START + timedelta(minutes=2)))
    source.write_bytes(first)

    bootstrap = sync_quote_index(source, database, now=START + timedelta(hours=3))
    with source.open("ab") as handle:
        handle.write(second)
    incremental = sync_quote_index(source, database, now=START + timedelta(hours=3, minutes=1))

    assert bootstrap["scanned_bytes"] == len(first)
    assert incremental["scanned_bytes"] == len(second)
    assert incremental["started_offset"] == len(first)
    selected = load_indexed_quote_payloads(
        source,
        database,
        symbols={"USDJPY"},
        start=START,
        end=START + timedelta(minutes=3),
        as_of=START + timedelta(hours=3, minutes=1),
    )
    assert len(selected.payloads) == 2
    assert selected.index_lag_bytes == 0


def test_quote_index_waits_for_a_complete_jsonl_line(tmp_path) -> None:
    source = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    complete = _encoded(_tick(START + timedelta(minutes=1)))
    pending = _encoded(_tick(START + timedelta(minutes=2)))
    split = len(pending) // 2
    source.write_bytes(complete + pending[:split])

    first = sync_quote_index(source, database, now=START + timedelta(hours=3))
    assert first["indexed_offset"] == len(complete)
    assert first["partial_tail_bytes"] == split

    with source.open("ab") as handle:
        handle.write(pending[split:])
    second = sync_quote_index(
        source,
        database,
        now=START + timedelta(hours=3, minutes=1),
    )
    assert second["indexed_rows"] == 1
    assert second["partial_tail_bytes"] == 0


def test_quote_index_fails_closed_after_source_replacement(tmp_path) -> None:
    source = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    source.write_bytes(_encoded(_tick(START + timedelta(minutes=1))))
    sync_quote_index(source, database, now=START + timedelta(hours=3))
    rotated = tmp_path / "quotes.rotated.jsonl"
    source.rename(rotated)
    source.write_bytes(rotated.read_bytes())

    with pytest.raises(QuoteIndexError, match="identity changed"):
        sync_quote_index(source, database, now=START + timedelta(hours=3, minutes=1))


def test_quote_index_reader_fails_closed_when_source_has_unindexed_bytes(tmp_path) -> None:
    source = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    source.write_bytes(_encoded(_tick(START + timedelta(minutes=1))))
    as_of = START + timedelta(hours=3)
    sync_quote_index(source, database, now=as_of)
    with source.open("ab") as handle:
        handle.write(_encoded(_tick(START + timedelta(minutes=2))))

    with pytest.raises(QuoteIndexError, match="behind source"):
        load_indexed_quote_payloads(
            source,
            database,
            symbols={"USDJPY"},
            start=START,
            end=START + timedelta(minutes=3),
            as_of=as_of,
        )


def test_replay_index_matches_the_raw_tape_window(tmp_path) -> None:
    source = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    source.write_bytes(
        b"".join(_encoded(_tick(START + timedelta(minutes=index))) for index in range(1, 4))
    )
    as_of = START + timedelta(hours=3)
    sync_quote_index(source, database, now=as_of)

    raw = load_quote_tape(
        source,
        symbols={"USDJPY"},
        start=START,
        end=START + timedelta(minutes=4),
        as_of=as_of,
    )
    indexed = load_quote_tape(
        source,
        symbols={"USDJPY"},
        start=START,
        end=START + timedelta(minutes=4),
        as_of=as_of,
        index_path=database,
    )
    assert indexed.ticks == raw.ticks
    assert indexed.watermark == raw.watermark
    assert indexed.index_path == str(database.resolve())
