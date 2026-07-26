"""Durability, recovery, integrity, rotation, and bounded-file journal tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from data_platform.collect.capture_journal import (
    CaptureJournal,
    CaptureJournalError,
    JournalState,
)
from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import QuoteLog, ingest_payload

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _quote(payload: bytes, *, when: datetime = NOW, sequence: int = 1) -> CollectedQuote:
    return CollectedQuote(
        provider="oanda",
        account_environment="practice",
        instrument="USDJPY",
        provider_event_time=when - timedelta(milliseconds=50),
        received_at=when,
        bid=155.001 + sequence / 100_000,
        ask=155.004 + sequence / 100_000,
        bid_size=1_000_000,
        ask_size=1_000_000,
        tradable=False,
        sequence_id=sequence,
        connection_id="connection-1",
        writer_id="writer-1",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(payload).hexdigest(),
        source_endpoint_class="replay_fixture",
        collection_mode="replay",
    )


def _ingest(log: QuoteLog, payload: bytes, *, when: datetime = NOW, sequence: int = 1):
    return ingest_payload(
        payload,
        parser=lambda raw: [_quote(raw, when=when, sequence=sequence)],
        log=log,
        now=lambda: when,
    )


def test_raw_is_durable_before_terminal_and_incomplete_is_invisible(tmp_path: Path) -> None:
    journal = CaptureJournal(tmp_path / "journal")
    capture = journal.begin(b"interrupted", occurred_at=NOW)

    assert journal.read_raw(capture) == b"interrupted"
    assert journal.read_quotes() == ()
    assert [entry.state for entry in journal.entries()] == [JournalState.RAW_DURABLE]


def test_recovery_appends_unavailable_without_mutating_raw(tmp_path: Path) -> None:
    journal = CaptureJournal(tmp_path / "journal")
    capture = journal.begin(b"interrupted", occurred_at=NOW)
    raw_before = journal.read_raw(capture)

    recovered = journal.recover_incomplete(occurred_at=NOW + timedelta(seconds=1))

    assert recovered == (capture.capture_id,)
    assert journal.read_raw(capture) == raw_before
    assert [entry.state for entry in journal.entries()] == [
        JournalState.RAW_DURABLE,
        JournalState.UNAVAILABLE,
    ]
    assert journal.read_quotes() == ()


def test_commit_atomically_publishes_only_accepted_rows(tmp_path: Path) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    first = _ingest(log, b"one")
    duplicate = _ingest(log, b"one", sequence=1)

    records = log.journal.read_quotes()
    assert first.accepted_count == 1
    assert duplicate.accepted_count == 0
    assert len(records) == 1
    assert records[0].capture_id == first.capture_id
    assert log.journal.entries()[-1].metadata["quality_flag_counts"] == {"duplicate_quote": 1}
    assert not log.accepted_path.exists()
    assert not log.quarantine_path.exists()


def test_parse_failure_retains_raw_and_is_terminal_quarantine(tmp_path: Path) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    result = ingest_payload(
        b"malformed",
        parser=lambda _raw: (_ for _ in ()).throw(ValueError("bad schema")),
        log=log,
        now=lambda: NOW,
    )

    entries = log.journal.entries()
    assert result.accepted_count == 0
    assert [entry.state for entry in entries] == [
        JournalState.RAW_DURABLE,
        JournalState.QUARANTINED,
    ]
    assert entries[1].metadata["reason"] == "schema_validation_failed"


def test_daily_rotation_links_hash_chain_and_bounds_file_count(tmp_path: Path) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    _ingest(log, b"day-one", when=NOW, sequence=1)
    _ingest(log, b"day-two", when=NOW + timedelta(days=1), sequence=2)
    log.journal.checkpoint()

    shards = log.journal.shard_paths()
    assert len(shards) == 2
    # SQLite may retain one WAL and one SHM companion per daily shard.
    assert len([path for path in (tmp_path / "log").rglob("*") if path.is_file()]) <= 6
    entries = log.journal.entries()
    assert entries[2].previous_entry_sha256 == entries[1].entry_sha256
    log.journal.verify()


def test_evidence_tables_reject_update_and_delete(tmp_path: Path) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    _ingest(log, b"immutable")
    shard = log.journal.shard_paths()[0]

    with sqlite3.connect(shard) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only events"):
            connection.execute("UPDATE events SET raw_sha256 = ?", ("0" * 64,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only quote rows"):
            connection.execute("DELETE FROM quote_rows")


def test_shard_capacity_limit_fails_closed_before_raw_append(tmp_path: Path) -> None:
    journal = CaptureJournal(tmp_path / "journal", max_shard_bytes=1)

    with pytest.raises(CaptureJournalError, match="capacity exceeded"):
        journal.begin(b"provider-message", occurred_at=NOW)

    assert journal.entries() == ()


def test_non_sqlite_shard_fails_as_journal_error(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    root.mkdir()
    (root / "capture-2026-07-22.sqlite3").write_bytes(b"not-a-sqlite-database")

    with pytest.raises(CaptureJournalError, match="SQLite journal failure"):
        CaptureJournal(root)


def test_journal_shard_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    root.mkdir()
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"external")
    (root / "capture-2026-07-22.sqlite3").symlink_to(external)

    with pytest.raises(CaptureJournalError, match="cannot be a symlink"):
        CaptureJournal(root)


def test_reader_detects_external_row_tampering_even_if_trigger_is_removed(
    tmp_path: Path,
) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    _ingest(log, b"tamper")
    shard = log.journal.shard_paths()[0]
    with sqlite3.connect(shard) as connection:
        connection.execute("DROP TRIGGER quote_rows_no_update")
        connection.execute(
            "UPDATE quote_rows SET payload_json = ?",
            (json.dumps({"tampered": True}),),
        )

    with pytest.raises(CaptureJournalError, match="row hash mismatch"):
        log.journal.read_quotes()


def test_synthetic_soak_uses_constant_file_count(tmp_path: Path) -> None:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False, verify_journal=False)
    count = 1_000
    for index in range(count):
        payload = f"quote-{index}".encode()
        _ingest(
            log,
            payload,
            when=NOW + timedelta(milliseconds=index),
            sequence=index,
        )
    log.journal.checkpoint()
    files = [path for path in (tmp_path / "log").rglob("*") if path.is_file()]
    database_bytes = sum(path.stat().st_size for path in files)

    assert len(log.journal.read_quotes()) == count
    assert len(files) <= 3
    assert database_bytes / count < 16_384
