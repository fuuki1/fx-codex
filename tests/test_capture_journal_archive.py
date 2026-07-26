from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_platform.collect import journal_archive
from data_platform.collect.contract import CollectedQuote
from data_platform.collect.capture_journal import CaptureJournal
from data_platform.collect.journal_archive import (
    JournalArchiveError,
    restore_archive_set,
    seal_historical_shard,
    verify_archive,
)
from data_platform.collect.raw_first import QuoteLog, ingest_payload

DAY_ONE = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _ingest(log: QuoteLog, *, when: datetime, sequence: int) -> None:
    payload = f"provider-payload-{sequence}".encode()

    def parser(raw: bytes) -> list[CollectedQuote]:
        return [
            CollectedQuote(
                provider="oanda",
                account_environment="practice",
                instrument="USDJPY",
                provider_event_time=when,
                received_at=when,
                bid=155.0 + sequence / 100,
                ask=155.01 + sequence / 100,
                bid_size=1_000_000,
                ask_size=1_000_000,
                tradable=False,
                sequence_id=sequence,
                connection_id="archive-test",
                writer_id="archive-test",
                revision_id=None,
                raw_payload_sha256=hashlib.sha256(raw).hexdigest(),
                source_endpoint_class="replay_fixture",
                collection_mode="replay",
            )
        ]

    result = ingest_payload(payload, parser=parser, log=log, now=lambda: when)
    assert result.accepted_count == 1


def _three_day_journal(tmp_path: Path) -> tuple[QuoteLog, Path]:
    log = QuoteLog(tmp_path / "log", legacy_jsonl=False)
    for offset in range(3):
        _ingest(
            log,
            when=DAY_ONE + timedelta(days=offset),
            sequence=offset + 1,
        )
    log.journal.checkpoint()
    return log, tmp_path / "archive"


def test_seal_and_restore_complete_prefix(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    source_paths = tuple(log.journal.shard_paths())
    source_identity = log.journal.identity()
    first = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
        sealed_at=DAY_ONE + timedelta(days=3),
    )
    second = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-21",
        sealed_at=DAY_ONE + timedelta(days=3),
    )

    assert verify_archive(first.manifest_path) == first.manifest
    assert verify_archive(second.manifest_path) == second.manifest
    assert first.archive_path.stat().st_mode & 0o777 == 0o440
    assert first.manifest_path.stat().st_mode & 0o777 == 0o440
    assert tuple(log.journal.shard_paths()) == source_paths
    assert log.journal.identity() == source_identity
    assert (
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date="2026-07-20",
            sealed_at=DAY_ONE + timedelta(days=4),
        )
        == first
    )

    restored_root = tmp_path / "restored"
    report = restore_archive_set(
        [second.manifest_path, first.manifest_path],
        restored_root,
    )

    assert report.shard_dates == ("2026-07-20", "2026-07-21")
    assert report.head_sequence == 4
    restored_quotes = CaptureJournal(restored_root).read_quotes()
    source_quotes = log.journal.read_quotes()
    assert len(restored_quotes) == 2
    assert [row.quote.to_dict() for row in restored_quotes] == [
        row.quote.to_dict() for row in source_quotes[:2]
    ]


def test_latest_shard_cannot_be_sealed(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)

    with pytest.raises(JournalArchiveError, match="active/latest"):
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date="2026-07-22",
        )


def test_restore_requires_genesis_anchored_prefix(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    second = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-21",
    )

    with pytest.raises(JournalArchiveError, match="journal genesis"):
        restore_archive_set([second.manifest_path], tmp_path / "restored")


def test_archive_tampering_fails_closed(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    sealed = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
    )
    sealed.archive_path.chmod(0o640)
    with sealed.archive_path.open("ab") as handle:
        handle.write(b"tampered")
    sealed.archive_path.chmod(0o440)

    with pytest.raises(JournalArchiveError, match="SHA-256 mismatch"):
        verify_archive(sealed.manifest_path)


def test_archive_mode_change_fails_closed(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    sealed = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
    )
    sealed.archive_path.chmod(0o640)

    with pytest.raises(JournalArchiveError, match="mode mismatch"):
        verify_archive(sealed.manifest_path)


def test_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    sealed = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
    )
    manifest = sealed.manifest.copy()
    manifest["archive_sha256"] = "not-a-sha256"
    sealed.manifest_path.chmod(0o640)
    sealed.manifest_path.write_text(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n",
        encoding="ascii",
    )
    sealed.manifest_path.chmod(0o440)

    with pytest.raises(JournalArchiveError, match="lowercase SHA-256"):
        verify_archive(sealed.manifest_path)


def test_existing_restore_target_cannot_be_replaced(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    sealed = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
    )
    destination = tmp_path / "restored"
    destination.mkdir()
    (destination / "capture-2026-07-20.sqlite3").write_bytes(b"not-the-archive")

    with pytest.raises(JournalArchiveError, match="create-only artifact collision"):
        restore_archive_set([sealed.manifest_path], destination)


def test_restore_target_symlink_is_rejected(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    sealed = seal_historical_shard(
        log.journal.root,
        archive_root,
        shard_date="2026-07-20",
    )
    destination = tmp_path / "restored"
    destination.mkdir()
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"must-not-be-touched")
    (destination / "capture-2026-07-20.sqlite3").symlink_to(external)

    with pytest.raises(JournalArchiveError, match="cannot be a symlink"):
        restore_archive_set([sealed.manifest_path], destination)
    assert external.read_bytes() == b"must-not-be-touched"


def test_seal_rejects_naive_timestamp(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)

    with pytest.raises(JournalArchiveError, match="timezone-aware"):
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date="2026-07-20",
            sealed_at=datetime(2026, 7, 23),
        )


def test_seal_rejects_insufficient_disk_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    monkeypatch.setattr(
        journal_archive.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(JournalArchiveError, match="insufficient disk headroom"):
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date="2026-07-20",
        )


def test_orphan_archive_requires_manual_quarantine(tmp_path: Path) -> None:
    log, archive_root = _three_day_journal(tmp_path)
    archive_root.mkdir()
    (archive_root / "capture-2026-07-20.sqlite3.gz").write_bytes(b"orphan")

    with pytest.raises(JournalArchiveError, match="orphan archive"):
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date="2026-07-20",
        )
