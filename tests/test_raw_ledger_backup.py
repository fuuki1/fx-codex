"""Create-only backup and restore probes for the raw event ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

import pytest

from data_platform.contracts.raw_event import RawEvent
from data_platform.raw.backup import (
    RawLedgerBackupError,
    create_backup,
    verify_backup,
)
from data_platform.raw import backup as backup_module
from data_platform.raw.event_ledger import RawEventLedger
from data_platform.raw.immutable_store import ImmutableRawStore

NOW = datetime.now(UTC).replace(microsecond=0)


def _event(event_id: str, symbol: str, offset: int) -> RawEvent:
    payload = f'{{"event":"{event_id}","symbol":"{symbol}"}}'.encode()
    received = NOW - timedelta(seconds=10 - offset)
    return RawEvent(
        event_id=event_id,
        source="fixture",
        source_event_id=f"source-{event_id}",
        event_type="price",
        symbol=symbol,
        event_time=received - timedelta(milliseconds=10),
        receive_time=received,
        ingest_time=received + timedelta(milliseconds=10),
        payload_bytes=payload,
        payload_hash=hashlib.sha256(payload).hexdigest(),
        provenance={
            "writer": "test",
            "source_event_id_scope": "fixture:price:symbol-time:v1",
        },
    )


def _collection(tmp_path: Path) -> Path:
    root = tmp_path / "collect"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store = ImmutableRawStore(root / "raw")
    ledger = RawEventLedger(root / "raw-ledger", blob_store=store)
    ledger.append(_event("one", "USDJPY", 1))
    ledger.append(_event("two", "EURUSD", 2))
    return root


def test_backup_is_create_only_and_restore_probe_verifies_all_blobs(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "backup" / "snapshot"

    created = create_backup(
        collection_root=collection,
        destination=destination,
        now=NOW,
    )
    verified = verify_backup(destination, now=NOW)

    assert created["manifest"]["event_count"] == 2
    assert verified["ok"] is True
    assert verified["event_count"] == 2
    assert verified["blob_count"] == 2
    assert verified["restore_health"]["symbol_counts"] == {"EURUSD": 1, "USDJPY": 1}
    assert verified["same_device"] is True
    with pytest.raises(RawLedgerBackupError, match="already exists"):
        create_backup(
            collection_root=collection,
            destination=destination,
            now=NOW,
        )


def test_backup_verification_rejects_blob_tampering(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "snapshot"
    create_backup(collection_root=collection, destination=destination, now=NOW)
    blob = next(path for path in (destination / "raw").rglob("*") if path.is_file())
    blob.write_bytes(b"tampered")

    with pytest.raises(RawLedgerBackupError, match="restore probe"):
        verify_backup(destination, now=NOW)


def test_backup_verification_rejects_missing_completion(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "snapshot"
    create_backup(collection_root=collection, destination=destination, now=NOW)
    (destination / "completion.json").unlink()

    with pytest.raises(RawLedgerBackupError, match="completion evidence"):
        verify_backup(destination, now=NOW)


def test_backup_fsync_failure_cannot_leave_completed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "backup" / "snapshot"
    real_fsync = backup_module._fsync_directory

    def fail_parent_fsync(path: Path) -> None:
        if path == destination.parent:
            raise RawLedgerBackupError("injected parent fsync failure")
        real_fsync(path)

    monkeypatch.setattr(backup_module, "_fsync_directory", fail_parent_fsync)
    with pytest.raises(RawLedgerBackupError, match="injected parent fsync failure"):
        create_backup(collection_root=collection, destination=destination, now=NOW)

    assert not (destination / "completion.json").exists()
    with pytest.raises(RawLedgerBackupError, match="completion evidence"):
        verify_backup(destination, now=NOW)


def test_backup_target_fsync_is_completion_commit_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "backup" / "snapshot"
    real_fsync = backup_module._fsync_directory
    calls: list[Path] = []
    target_fsyncs = 0

    def fail_completion_commit(path: Path) -> None:
        nonlocal target_fsyncs
        calls.append(path)
        if path == destination:
            target_fsyncs += 1
            if target_fsyncs == 2:
                raise RawLedgerBackupError("injected completion commit failure")
        real_fsync(path)

    monkeypatch.setattr(backup_module, "_fsync_directory", fail_completion_commit)
    with pytest.raises(RawLedgerBackupError, match="injected completion commit failure"):
        create_backup(collection_root=collection, destination=destination, now=NOW)

    first_target_fsync = calls.index(destination)
    second_target_fsync = calls.index(destination, first_target_fsync + 1)
    assert calls.index(destination.parent) < second_target_fsync
    assert not (destination / "completion.json").exists()


def test_backup_completion_write_failure_is_directory_fsynced_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = _collection(tmp_path)
    destination = tmp_path / "backup" / "snapshot"
    real_write = backup_module._write_create_only
    real_fsync = backup_module._fsync_directory
    target_fsyncs = 0

    def fail_after_completion_write(path: Path, payload: dict[str, object]) -> str:
        result = real_write(path, payload)
        if path.name == "completion.json":
            raise RawLedgerBackupError("injected completion write failure")
        return result

    def record_target_fsync(path: Path) -> None:
        nonlocal target_fsyncs
        if path == destination:
            target_fsyncs += 1
        real_fsync(path)

    monkeypatch.setattr(backup_module, "_write_create_only", fail_after_completion_write)
    monkeypatch.setattr(backup_module, "_fsync_directory", record_target_fsync)
    with pytest.raises(RawLedgerBackupError, match="injected completion write failure"):
        create_backup(collection_root=collection, destination=destination, now=NOW)

    assert target_fsyncs == 2
    assert not (destination / "completion.json").exists()


def test_backup_rejects_symlink_parent_before_creating_destination(tmp_path: Path) -> None:
    collection = _collection(tmp_path)
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    destination = linked_parent / "snapshot"

    with pytest.raises(RawLedgerBackupError, match="symlink ancestor"):
        create_backup(collection_root=collection, destination=destination, now=NOW)

    assert not (actual_parent / "snapshot").exists()
