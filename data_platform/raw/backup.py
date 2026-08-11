"""Create-only, integrity-verifiable backups for the raw event shadow ledger."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
from typing import Any

from data_platform.raw.content_addressed import address_path
from data_platform.raw.event_ledger import RawEventLedger
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError

BACKUP_CONTRACT = "raw-event-ledger-backup-v1"
COMPLETION_CONTRACT = "raw-event-ledger-backup-completion-v1"


class RawLedgerBackupError(RuntimeError):
    """Raised when backup completeness or restore integrity cannot be proven."""


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RawLedgerBackupError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_create_only(path: Path, payload: dict[str, Any]) -> str:
    encoded = _canonical_json(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:
        raise RawLedgerBackupError(f"cannot open directory for fsync: {path}") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise RawLedgerBackupError(f"cannot fsync directory: {path}") from error
    finally:
        os.close(descriptor)


def _reject_symlink_ancestors(path: Path) -> None:
    candidate = path.absolute()
    for current in (candidate, *candidate.parents):
        if current.is_symlink():
            raise RawLedgerBackupError(f"backup path contains symlink ancestor: {current}")


def _require_owner_only_regular(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RawLedgerBackupError(f"backup file unavailable: {path}") from error
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or stat.S_IMODE(path_stat.st_mode) & 0o077
    ):
        raise RawLedgerBackupError(f"backup file must be user-owned owner-only regular: {path}")


def _require_owner_only_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RawLedgerBackupError(f"backup directory unavailable: {path}") from error
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or stat.S_IMODE(path_stat.st_mode) & 0o077
    ):
        raise RawLedgerBackupError(
            f"backup directory must be user-owned owner-only real directory: {path}"
        )


def _stable_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    _require_owner_only_regular(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(encoded) > max_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RawLedgerBackupError(f"backup file changed while reading: {path}")
        return encoded
    finally:
        os.close(descriptor)


def _copy_stable_file(source: Path, destination: Path) -> str:
    _require_owner_only_regular(source)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    try:
        before = os.fstat(source_fd)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RawLedgerBackupError(f"backup file changed while copying: {source}")
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    return digest.hexdigest()


def _database_snapshot(source: Path, destination: Path) -> None:
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"{source.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
        quick_check = [
            str(row[0]) for row in destination_connection.execute("PRAGMA quick_check").fetchall()
        ]
        if quick_check != ["ok"]:
            raise RawLedgerBackupError(f"backup quick_check failed: {quick_check}")
        if list(destination_connection.execute("PRAGMA foreign_key_check")):
            raise RawLedgerBackupError("backup foreign-key check failed")
        destination_connection.commit()
    except sqlite3.Error as error:
        raise RawLedgerBackupError(f"cannot snapshot raw ledger database: {error}") from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    destination.chmod(0o600)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def _snapshot_rows(database: Path) -> tuple[list[str], int, int]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        hashes = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT payload_hash FROM raw_events ORDER BY payload_hash"
            )
        ]
        event_count = int(connection.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])
        annotation_count = int(
            connection.execute("SELECT COUNT(*) FROM event_annotations").fetchone()[0]
        )
    except sqlite3.Error as error:
        raise RawLedgerBackupError(f"cannot inspect backup database: {error}") from error
    finally:
        connection.close()
    return hashes, event_count, annotation_count


def create_backup(
    *,
    collection_root: Path,
    destination: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one completed snapshot without overwriting an existing destination."""

    observed_at = _aware_utc(now or datetime.now(UTC), field_name="now")
    source_root = collection_root.absolute()
    target = destination.absolute()
    _reject_symlink_ancestors(source_root)
    if not source_root.is_dir():
        raise RawLedgerBackupError(f"collection root not found: {source_root}")
    source_stat = source_root.lstat()
    if source_stat.st_uid != os.getuid() or stat.S_IMODE(source_stat.st_mode) & 0o077:
        raise RawLedgerBackupError("collection root must be user-owned and owner-only")
    if target.exists() or target.is_symlink():
        raise RawLedgerBackupError(f"backup destination already exists: {target}")

    ledger = RawEventLedger(
        source_root / "raw-ledger",
        blob_store=ImmutableRawStore(source_root / "raw"),
    )
    source_health = ledger.health_snapshot(
        now=observed_at,
        stale_after_seconds=10**12,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    if source_health["status"] != "ok":
        raise RawLedgerBackupError(
            f"source raw ledger integrity is not healthy: {source_health['reasons']}"
        )

    _reject_symlink_ancestors(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(target)
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    ledger_root = target / "raw-ledger"
    ledger_root.mkdir(mode=0o700)
    database = ledger_root / "events.sqlite3"
    _database_snapshot(ledger.database_path, database)
    payload_hashes, event_count, annotation_count = _snapshot_rows(database)
    if event_count < 1 or not payload_hashes:
        raise RawLedgerBackupError("backup source contains no raw events")

    source_store = ImmutableRawStore(source_root / "raw")
    backup_store = ImmutableRawStore(target / "raw")
    payload_bytes = 0
    try:
        for payload_hash in payload_hashes:
            payload = source_store.get(payload_hash)
            copied = backup_store.put(payload)
            if copied.sha256 != payload_hash:
                raise RawLedgerBackupError(f"backup blob address mismatch for {payload_hash}")
            payload_bytes += copied.size
    except (OSError, RawStoreError, ValueError) as error:
        raise RawLedgerBackupError(f"cannot copy immutable raw blobs: {error}") from error

    raw_root = target / "raw"
    raw_root.chmod(0o700)
    for directory in raw_root.iterdir():
        if directory.is_dir():
            directory.chmod(0o700)

    payload_hashes_sha256 = hashlib.sha256(
        ("\n".join(payload_hashes) + "\n").encode("ascii")
    ).hexdigest()
    manifest: dict[str, Any] = {
        "contract": BACKUP_CONTRACT,
        "created_at": observed_at.isoformat(),
        "source_hostname": socket.gethostname(),
        "source_collection_root_sha256": hashlib.sha256(
            str(source_root).encode("utf-8")
        ).hexdigest(),
        "source_device_id": source_root.stat().st_dev,
        "backup_device_id_at_creation": target.stat().st_dev,
        "same_device_at_creation": source_root.stat().st_dev == target.stat().st_dev,
        "database_sha256": _file_sha256(database),
        "payload_hashes_sha256": payload_hashes_sha256,
        "event_count": event_count,
        "annotation_count": annotation_count,
        "blob_count": len(payload_hashes),
        "payload_bytes": payload_bytes,
        "source_health": source_health,
    }
    manifest_path = target / "manifest.json"
    manifest_sha256 = _write_create_only(manifest_path, manifest)
    completion = {
        "contract": COMPLETION_CONTRACT,
        "created_at": observed_at.isoformat(),
        "manifest_sha256": manifest_sha256,
        "database_sha256": manifest["database_sha256"],
    }
    completion_path = target / "completion.json"
    try:
        _fsync_directory(ledger_root)
        for directory in raw_root.iterdir():
            if directory.is_dir():
                _fsync_directory(directory)
        _fsync_directory(raw_root)
        _fsync_directory(target)
        # Persist the snapshot directory entry before publishing completion.
        # The target-directory fsync after completion is the commit point.
        _fsync_directory(target.parent)
        _write_create_only(completion_path, completion)
        _fsync_directory(target)
    except BaseException:
        completion_path.unlink(missing_ok=True)
        try:
            _fsync_directory(target)
        except RawLedgerBackupError:
            pass
        raise
    return {"backup": str(target), "manifest": manifest, "completion": completion}


def verify_backup(
    backup_root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a completed snapshot and prove it can be opened as a read-only restore."""

    observed_at = _aware_utc(now or datetime.now(UTC), field_name="now")
    root = backup_root.absolute()
    _reject_symlink_ancestors(root)
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise RawLedgerBackupError(f"backup root unavailable: {root}") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RawLedgerBackupError("backup root must be a real directory")
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise RawLedgerBackupError("backup root must be user-owned and owner-only")

    manifest_path = root / "manifest.json"
    completion_path = root / "completion.json"
    database = root / "raw-ledger" / "events.sqlite3"
    try:
        manifest_bytes = _stable_file_bytes(manifest_path, max_bytes=1024 * 1024)
        completion_bytes = _stable_file_bytes(completion_path, max_bytes=1024 * 1024)
        completion = json.loads(completion_bytes)
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RawLedgerBackupError) as error:
        raise RawLedgerBackupError(f"backup completion evidence is unreadable: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("contract") != BACKUP_CONTRACT:
        raise RawLedgerBackupError("backup manifest contract mismatch")
    if not isinstance(completion, dict) or completion.get("contract") != COMPLETION_CONTRACT:
        raise RawLedgerBackupError("backup completion contract mismatch")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if completion.get("manifest_sha256") != manifest_sha256:
        raise RawLedgerBackupError("backup manifest hash mismatch")

    temporary_parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(
        prefix="fx-codex-backup-verify-", dir=temporary_parent
    ) as temporary_name:
        fixed_root = Path(temporary_name)
        fixed_root.chmod(0o700)
        fixed_ledger_root = fixed_root / "raw-ledger"
        fixed_ledger_root.mkdir(mode=0o700)
        fixed_database = fixed_ledger_root / "events.sqlite3"
        database_sha256 = _copy_stable_file(database, fixed_database)
        if (
            manifest.get("database_sha256") != database_sha256
            or completion.get("database_sha256") != database_sha256
        ):
            raise RawLedgerBackupError("backup database hash mismatch")
        payload_hashes, event_count, annotation_count = _snapshot_rows(fixed_database)
        expected_payload_digest = hashlib.sha256(
            ("\n".join(payload_hashes) + "\n").encode("ascii")
        ).hexdigest()
        if expected_payload_digest != manifest.get("payload_hashes_sha256"):
            raise RawLedgerBackupError("backup payload manifest mismatch")
        if event_count != manifest.get("event_count"):
            raise RawLedgerBackupError("backup event count mismatch")
        if annotation_count != manifest.get("annotation_count"):
            raise RawLedgerBackupError("backup annotation count mismatch")
        if len(payload_hashes) != manifest.get("blob_count"):
            raise RawLedgerBackupError("backup blob count mismatch")

        raw_root = root / "raw"
        _require_owner_only_directory(root / "raw-ledger")
        _require_owner_only_directory(raw_root)
        expected_blob_paths = {address_path(raw_root, digest) for digest in payload_hashes}
        actual_blob_paths: set[Path] = set()
        for item in raw_root.rglob("*"):
            item_stat = item.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise RawLedgerBackupError(f"backup raw tree contains symlink: {item}")
            if stat.S_ISDIR(item_stat.st_mode):
                _require_owner_only_directory(item)
            if stat.S_ISREG(item_stat.st_mode):
                _require_owner_only_regular(item)
                actual_blob_paths.add(item)
        if actual_blob_paths != expected_blob_paths:
            raise RawLedgerBackupError("backup raw tree does not exactly match manifest blobs")

        fixed_store = ImmutableRawStore(fixed_root / "raw")
        maximum_payload_bytes = max(1, int(manifest.get("payload_bytes", 0)))
        for digest in payload_hashes:
            payload = _stable_file_bytes(
                address_path(raw_root, digest), max_bytes=maximum_payload_bytes
            )
            if hashlib.sha256(payload).hexdigest() != digest:
                raise RawLedgerBackupError(f"backup restore probe blob hash mismatch for {digest}")
            fixed_store.put(payload)

        ledger = RawEventLedger(fixed_ledger_root, blob_store=fixed_store)
        restore_health = ledger.health_snapshot(
            now=observed_at,
            stale_after_seconds=10**12,
            max_duplicate_ratio=1,
            max_quarantine_ratio=1,
            verify_blobs=True,
        )
        if restore_health["status"] != "ok":
            raise RawLedgerBackupError(
                f"backup restore probe is not healthy: {restore_health['reasons']}"
            )
    created_at = _aware_utc(
        datetime.fromisoformat(str(manifest.get("created_at"))),
        field_name="backup created_at",
    )
    age_seconds = (observed_at - created_at).total_seconds()
    if age_seconds < 0:
        raise RawLedgerBackupError("backup creation time is in the future")
    current_device = root.stat().st_dev
    return {
        "ok": True,
        "backup": str(root),
        "created_at": created_at.isoformat(),
        "age_seconds": age_seconds,
        "source_hostname": manifest.get("source_hostname"),
        "source_collection_root_sha256": manifest.get("source_collection_root_sha256"),
        "source_device_id": manifest.get("source_device_id"),
        "backup_device_id": current_device,
        "same_device": manifest.get("source_device_id") == current_device,
        "event_count": event_count,
        "annotation_count": annotation_count,
        "blob_count": len(payload_hashes),
        "payload_bytes": manifest.get("payload_bytes"),
        "database_sha256": database_sha256,
        "manifest_sha256": manifest_sha256,
        "restore_health": restore_health,
    }
