"""Create-only sealing archives and verified prefix restore for capture journals.

Archives are deliberately separate from hot-journal retention.  This module
never deletes a source shard.  An operator may consider pruning only after an
independent restore has verified a complete genesis-anchored archive prefix.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from data_platform.collect.capture_journal import (
    JOURNAL_SCHEMA_VERSION,
    ZERO_SHA256,
    CaptureJournal,
    CaptureJournalError,
)

ARCHIVE_SCHEMA_VERSION = 1
_COPY_CHUNK_BYTES = 1024 * 1024
_MANIFEST_SUFFIX = ".manifest.json"
_MIN_FREE_RESERVE_BYTES = 1024 * 1024 * 1024


class JournalArchiveError(RuntimeError):
    """A shard cannot be sealed, authenticated, or restored safely."""


@dataclass(frozen=True)
class SealedShard:
    manifest_path: Path
    archive_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class RestoreReport:
    destination_root: Path
    shard_dates: tuple[str, ...]
    restored_bytes: int
    genesis_sha256: str
    head_sha256: str
    head_sequence: int

    def to_dict(self) -> dict[str, object]:
        return {
            "destination_root": str(self.destination_root.resolve()),
            "shard_dates": list(self.shard_dates),
            "restored_bytes": self.restored_bytes,
            "genesis_sha256": self.genesis_sha256,
            "head_sha256": self.head_sha256,
            "head_sequence": self.head_sequence,
        }


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise JournalArchiveError(f"{field} must be a lowercase SHA-256")
    return value


def _require_integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise JournalArchiveError(f"{field} must be an integer >= {minimum}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_disk_headroom(path: Path, *, working_bytes: int, operation: str) -> None:
    available = shutil.disk_usage(path).free
    required = working_bytes + _MIN_FREE_RESERVE_BYTES
    if available < required:
        raise JournalArchiveError(
            f"insufficient disk headroom for {operation}: "
            f"available={available}, required={required}"
        )


def _create_only_bytes(path: Path, payload: bytes, *, mode: int = 0o440) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise JournalArchiveError(f"create-only artifact cannot be a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        created = False
        try:
            os.link(temporary, path)
            created = True
        except FileExistsError as error:
            if path.is_symlink():
                raise JournalArchiveError(
                    f"create-only artifact cannot be a symlink: {path}"
                ) from error
            if path.read_bytes() != payload:
                raise JournalArchiveError(f"create-only artifact collision: {path}") from error
        if created:
            os.chmod(path, mode)
        elif path.stat().st_mode & 0o777 != mode:
            raise JournalArchiveError(f"create-only artifact mode mismatch: {path}")
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _link_create_only(source: Path, destination: Path, *, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise JournalArchiveError(f"create-only artifact cannot be a symlink: {destination}")
    created = False
    try:
        os.link(source, destination)
        created = True
    except FileExistsError as error:
        if destination.is_symlink():
            raise JournalArchiveError(
                f"create-only artifact cannot be a symlink: {destination}"
            ) from error
        if _sha256_file(source) != _sha256_file(destination):
            raise JournalArchiveError(f"create-only artifact collision: {destination}") from error
    if created:
        os.chmod(destination, mode)
    elif destination.stat().st_mode & 0o777 != mode:
        raise JournalArchiveError(f"create-only artifact mode mismatch: {destination}")
    _fsync_directory(destination.parent)


def _inspect_database(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as error:
        raise JournalArchiveError(f"cannot open SQLite shard {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or quick[0] != "ok":
            raise JournalArchiveError(f"SQLite quick_check failed: {path}")
        meta = connection.execute("SELECT * FROM shard_meta WHERE singleton = 1").fetchone()
        first = connection.execute(
            "SELECT sequence, entry_sha256, occurred_at FROM events ORDER BY sequence LIMIT 1"
        ).fetchone()
        last = connection.execute(
            "SELECT sequence, entry_sha256, occurred_at FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if meta is None or first is None or last is None:
            raise JournalArchiveError(f"shard metadata or events are missing: {path}")
        counts = connection.execute("""
            SELECT
                COUNT(*) AS event_count,
                SUM(CASE WHEN state = 'raw_durable' THEN 1 ELSE 0 END) AS raw_count,
                SUM(CASE WHEN state = 'committed' THEN 1 ELSE 0 END) AS committed_count,
                SUM(CASE WHEN state = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_count,
                SUM(CASE WHEN state = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_count
            FROM events
            """).fetchone()
        quote_count = int(connection.execute("SELECT COUNT(*) FROM quote_rows").fetchone()[0])
        return {
            "journal_schema_version": int(meta["schema_version"]),
            "shard_date": str(meta["shard_date"]),
            "previous_sequence": int(meta["previous_sequence"]),
            "previous_entry_sha256": str(meta["previous_entry_sha256"]),
            "first_sequence": int(first["sequence"]),
            "first_entry_sha256": str(first["entry_sha256"]),
            "first_occurred_at": str(first["occurred_at"]),
            "last_sequence": int(last["sequence"]),
            "last_entry_sha256": str(last["entry_sha256"]),
            "last_occurred_at": str(last["occurred_at"]),
            "event_count": int(counts["event_count"]),
            "raw_count": int(counts["raw_count"] or 0),
            "committed_count": int(counts["committed_count"] or 0),
            "quarantined_count": int(counts["quarantined_count"] or 0),
            "unavailable_count": int(counts["unavailable_count"] or 0),
            "quote_row_count": quote_count,
        }
    except sqlite3.Error as error:
        raise JournalArchiveError(f"cannot inspect SQLite shard {path}: {error}") from error
    finally:
        connection.close()


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(
        f"file:{source}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    destination_connection = sqlite3.connect(destination, timeout=30.0)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        destination_connection.execute("VACUUM")
        destination_connection.commit()
    except sqlite3.Error as error:
        raise JournalArchiveError(f"cannot snapshot SQLite shard {source}: {error}") from error
    finally:
        destination_connection.close()
        source_connection.close()
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def _gzip_snapshot(source: Path, destination: Path) -> None:
    with source.open("rb") as input_handle, destination.open("wb") as output_handle:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=output_handle,
            compresslevel=9,
            mtime=0,
        ) as compressed:
            shutil.copyfileobj(input_handle, compressed, length=_COPY_CHUNK_BYTES)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _manifest_paths(archive_root: Path, shard_date: str) -> tuple[Path, Path]:
    base = f"capture-{shard_date}.sqlite3"
    return archive_root / f"{base}.gz", archive_root / f"{base}{_MANIFEST_SUFFIX}"


def seal_historical_shard(
    journal_root: str | Path,
    archive_root: str | Path,
    *,
    shard_date: str,
    sealed_at: datetime | None = None,
) -> SealedShard:
    """Seal one non-active shard into a create-only deterministic gzip archive."""

    journal_path = Path(journal_root)
    archive_path_root = Path(archive_root)
    journal = CaptureJournal(journal_path)
    shards = journal.shard_paths()
    source = journal_path / f"capture-{shard_date}.sqlite3"
    if source not in shards:
        raise JournalArchiveError(f"journal shard does not exist: {source}")
    if source == shards[-1]:
        raise JournalArchiveError("active/latest journal shard cannot be sealed")
    observed_sealed_at = sealed_at or datetime.now(UTC)
    if observed_sealed_at.tzinfo is None or observed_sealed_at.utcoffset() is None:
        raise JournalArchiveError("sealed_at must be timezone-aware")
    timestamp = observed_sealed_at.astimezone(UTC)
    archive_path, manifest_path = _manifest_paths(archive_path_root, shard_date)
    if archive_path.exists() != manifest_path.exists():
        raise JournalArchiveError(
            f"orphan archive artifact requires manual quarantine: {archive_path}, {manifest_path}"
        )
    if archive_path.exists():
        existing_manifest = load_manifest(manifest_path)
        verify_archive(manifest_path)
        return SealedShard(manifest_path, archive_path, existing_manifest)

    journal.checkpoint_shard(shard_date)
    journal.verify()
    source_inspection = _inspect_database(source)
    if source_inspection["shard_date"] != shard_date:
        raise JournalArchiveError("source shard date does not match requested shard_date")

    archive_path_root.mkdir(parents=True, exist_ok=True)
    _require_disk_headroom(
        archive_path_root,
        working_bytes=source.stat().st_size * 2,
        operation="archive sealing",
    )
    database_descriptor, database_name = tempfile.mkstemp(
        dir=archive_path_root,
        prefix=f".capture-{shard_date}.",
        suffix=".sqlite3",
    )
    os.close(database_descriptor)
    database_temporary = Path(database_name)
    gzip_descriptor, gzip_name = tempfile.mkstemp(
        dir=archive_path_root,
        prefix=f".capture-{shard_date}.",
        suffix=".sqlite3.gz",
    )
    os.close(gzip_descriptor)
    gzip_temporary = Path(gzip_name)
    try:
        _sqlite_snapshot(source, database_temporary)
        snapshot_inspection = _inspect_database(database_temporary)
        if snapshot_inspection != source_inspection:
            raise JournalArchiveError("SQLite snapshot differs from the verified source shard")
        sqlite_sha256 = _sha256_file(database_temporary)
        sqlite_bytes = database_temporary.stat().st_size
        _gzip_snapshot(database_temporary, gzip_temporary)
        archive_sha256 = _sha256_file(gzip_temporary)
        archive_bytes = gzip_temporary.stat().st_size
        _link_create_only(gzip_temporary, archive_path, mode=0o440)
        manifest: dict[str, Any] = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "artifact_class": "capture_journal_sealed_shard",
            "archive_file": archive_path.name,
            "archive_sha256": archive_sha256,
            "archive_bytes": archive_bytes,
            "compression": "gzip-level-9-mtime-0",
            "sqlite_sha256": sqlite_sha256,
            "sqlite_bytes": sqlite_bytes,
            "source_file": source.name,
            "source_sha256": _sha256_file(source),
            "sealed_at": timestamp.isoformat(),
            "shard": snapshot_inspection,
        }
        _create_only_bytes(manifest_path, _canonical_bytes(manifest), mode=0o440)
        verify_archive(manifest_path)
        journal.verify()
        return SealedShard(manifest_path, archive_path, manifest)
    finally:
        database_temporary.unlink(missing_ok=True)
        gzip_temporary.unlink(missing_ok=True)


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    if manifest_path.is_symlink():
        raise JournalArchiveError(f"archive manifest cannot be a symlink: {manifest_path}")
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise JournalArchiveError(
            f"cannot read archive manifest {manifest_path}: {error}"
        ) from error
    if not isinstance(payload, dict) or raw != _canonical_bytes(payload):
        raise JournalArchiveError(f"archive manifest is not canonical JSON: {manifest_path}")
    required = {
        "schema_version",
        "artifact_class",
        "archive_file",
        "archive_sha256",
        "archive_bytes",
        "compression",
        "sqlite_sha256",
        "sqlite_bytes",
        "source_file",
        "source_sha256",
        "sealed_at",
        "shard",
    }
    if set(payload) != required or payload["schema_version"] != ARCHIVE_SCHEMA_VERSION:
        raise JournalArchiveError(f"archive manifest schema is invalid: {manifest_path}")
    if payload["artifact_class"] != "capture_journal_sealed_shard":
        raise JournalArchiveError(f"archive manifest class is invalid: {manifest_path}")
    archive_file = payload["archive_file"]
    if (
        not isinstance(archive_file, str)
        or Path(archive_file).name != archive_file
        or not archive_file.endswith(".sqlite3.gz")
    ):
        raise JournalArchiveError(f"archive filename is unsafe: {manifest_path}")
    if payload["compression"] != "gzip-level-9-mtime-0":
        raise JournalArchiveError(f"archive compression is invalid: {manifest_path}")
    for field in ("archive_sha256", "sqlite_sha256", "source_sha256"):
        _require_sha256(payload[field], field=field)
    _require_integer(payload["archive_bytes"], field="archive_bytes", minimum=1)
    _require_integer(payload["sqlite_bytes"], field="sqlite_bytes", minimum=1)
    try:
        sealed_at = datetime.fromisoformat(str(payload["sealed_at"]))
    except ValueError as error:
        raise JournalArchiveError(f"archive sealed_at is invalid: {manifest_path}") from error
    if sealed_at.tzinfo is None or sealed_at.utcoffset() is None:
        raise JournalArchiveError(f"archive sealed_at is not timezone-aware: {manifest_path}")
    shard = payload["shard"]
    shard_fields = {
        "journal_schema_version",
        "shard_date",
        "previous_sequence",
        "previous_entry_sha256",
        "first_sequence",
        "first_entry_sha256",
        "first_occurred_at",
        "last_sequence",
        "last_entry_sha256",
        "last_occurred_at",
        "event_count",
        "raw_count",
        "committed_count",
        "quarantined_count",
        "unavailable_count",
        "quote_row_count",
    }
    if not isinstance(shard, dict) or set(shard) != shard_fields:
        raise JournalArchiveError(f"archive shard metadata is invalid: {manifest_path}")
    if (
        _require_integer(
            shard["journal_schema_version"],
            field="shard.journal_schema_version",
            minimum=1,
        )
        != JOURNAL_SCHEMA_VERSION
    ):
        raise JournalArchiveError(f"archive journal schema is invalid: {manifest_path}")
    shard_date = str(shard["shard_date"])
    try:
        if date.fromisoformat(shard_date).isoformat() != shard_date:
            raise ValueError
    except ValueError as error:
        raise JournalArchiveError(f"archive shard date is invalid: {manifest_path}") from error
    expected_source = f"capture-{shard_date}.sqlite3"
    if (
        payload["source_file"] != expected_source
        or archive_file != f"{expected_source}.gz"
        or manifest_path.name != f"{expected_source}{_MANIFEST_SUFFIX}"
    ):
        raise JournalArchiveError(f"archive filenames do not match shard date: {manifest_path}")
    previous_sequence = _require_integer(
        shard["previous_sequence"],
        field="shard.previous_sequence",
    )
    first_sequence = _require_integer(
        shard["first_sequence"],
        field="shard.first_sequence",
        minimum=1,
    )
    last_sequence = _require_integer(
        shard["last_sequence"],
        field="shard.last_sequence",
        minimum=1,
    )
    event_count = _require_integer(
        shard["event_count"],
        field="shard.event_count",
        minimum=1,
    )
    state_count = 0
    for field in (
        "raw_count",
        "committed_count",
        "quarantined_count",
        "unavailable_count",
    ):
        state_count += _require_integer(shard[field], field=f"shard.{field}")
    _require_integer(shard["quote_row_count"], field="shard.quote_row_count")
    if (
        first_sequence != previous_sequence + 1
        or last_sequence != previous_sequence + event_count
        or state_count != event_count
    ):
        raise JournalArchiveError(f"archive shard counts are inconsistent: {manifest_path}")
    for field in (
        "previous_entry_sha256",
        "first_entry_sha256",
        "last_entry_sha256",
    ):
        _require_sha256(shard[field], field=f"shard.{field}")
    for field in ("first_occurred_at", "last_occurred_at"):
        try:
            occurred_at = datetime.fromisoformat(str(shard[field]))
        except ValueError as error:
            raise JournalArchiveError(f"archive {field} is invalid: {manifest_path}") from error
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise JournalArchiveError(f"archive {field} is not timezone-aware: {manifest_path}")
    return payload


def _decompress_archive(archive_path: Path, destination: Path) -> None:
    try:
        with gzip.open(archive_path, "rb") as input_handle, destination.open("wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=_COPY_CHUNK_BYTES)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except (OSError, gzip.BadGzipFile) as error:
        raise JournalArchiveError(f"cannot decompress archive {archive_path}: {error}") from error


def verify_archive(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    manifest = load_manifest(path)
    if path.stat().st_mode & 0o777 != 0o440:
        raise JournalArchiveError(f"archive manifest mode mismatch: {path}")
    archive_path = path.parent / str(manifest["archive_file"])
    if archive_path.is_symlink():
        raise JournalArchiveError(f"archive payload cannot be a symlink: {archive_path}")
    if not archive_path.is_file():
        raise JournalArchiveError(f"archive payload is missing: {archive_path}")
    if archive_path.stat().st_mode & 0o777 != 0o440:
        raise JournalArchiveError(f"archive payload mode mismatch: {archive_path}")
    if _sha256_file(archive_path) != manifest["archive_sha256"]:
        raise JournalArchiveError(f"archive SHA-256 mismatch: {archive_path}")
    if archive_path.stat().st_size != manifest["archive_bytes"]:
        raise JournalArchiveError(f"archive size mismatch: {archive_path}")
    _require_disk_headroom(
        path.parent,
        working_bytes=int(manifest["sqlite_bytes"]),
        operation="archive verification",
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{archive_path.name}.verify.",
        suffix=".sqlite3",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _decompress_archive(archive_path, temporary)
        if temporary.stat().st_size != manifest["sqlite_bytes"]:
            raise JournalArchiveError(f"restored SQLite size mismatch: {archive_path}")
        if _sha256_file(temporary) != manifest["sqlite_sha256"]:
            raise JournalArchiveError(f"restored SQLite SHA-256 mismatch: {archive_path}")
        if _inspect_database(temporary) != manifest["shard"]:
            raise JournalArchiveError(f"restored SQLite metadata mismatch: {archive_path}")
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def _validate_manifest_chain(manifests: Sequence[Mapping[str, Any]]) -> None:
    if not manifests:
        raise JournalArchiveError("at least one archive manifest is required")
    first_shard = manifests[0]["shard"]
    if (
        first_shard["previous_sequence"] != 0
        or first_shard["previous_entry_sha256"] != ZERO_SHA256
        or first_shard["first_sequence"] != 1
    ):
        raise JournalArchiveError("restore set must begin at the journal genesis")
    previous = first_shard
    for manifest in manifests[1:]:
        current = manifest["shard"]
        if (
            current["previous_sequence"] != previous["last_sequence"]
            or current["previous_entry_sha256"] != previous["last_entry_sha256"]
            or current["first_sequence"] != previous["last_sequence"] + 1
        ):
            raise JournalArchiveError("archive manifests do not form a contiguous hash prefix")
        previous = current


def restore_archive_set(
    manifest_paths: Sequence[str | Path],
    destination_root: str | Path,
) -> RestoreReport:
    """Restore and fully verify an explicit genesis-anchored archive prefix."""

    loaded = [(Path(path), verify_archive(path)) for path in manifest_paths]
    loaded.sort(key=lambda item: str(item[1]["shard"]["shard_date"]))
    manifests = [item[1] for item in loaded]
    _validate_manifest_chain(manifests)
    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)
    expected_names = {
        f"capture-{manifest['shard']['shard_date']}.sqlite3" for manifest in manifests
    }
    unexpected = {
        path.name for path in destination.glob("capture-????-??-??.sqlite3")
    } - expected_names
    if unexpected:
        raise JournalArchiveError(
            "restore destination contains unexpected journal shards: "
            + ", ".join(sorted(unexpected))
        )
    missing_restore_bytes = sum(
        int(manifest["sqlite_bytes"])
        for manifest in manifests
        if not (destination / f"capture-{manifest['shard']['shard_date']}.sqlite3").exists()
    )
    _require_disk_headroom(
        destination,
        working_bytes=missing_restore_bytes,
        operation="archive restore",
    )

    restored_bytes = 0
    restored_dates: list[str] = []
    for manifest_path, manifest in loaded:
        archive_path = manifest_path.parent / str(manifest["archive_file"])
        shard_date = str(manifest["shard"]["shard_date"])
        target = destination / f"capture-{shard_date}.sqlite3"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination,
            prefix=f".capture-{shard_date}.restore.",
            suffix=".sqlite3",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            _decompress_archive(archive_path, temporary)
            if _sha256_file(temporary) != manifest["sqlite_sha256"]:
                raise JournalArchiveError(f"restored SQLite SHA-256 mismatch: {archive_path}")
            if _inspect_database(temporary) != manifest["shard"]:
                raise JournalArchiveError(f"restored SQLite metadata mismatch: {archive_path}")
            _link_create_only(temporary, target, mode=0o640)
            restored_bytes += target.stat().st_size
            restored_dates.append(shard_date)
        finally:
            temporary.unlink(missing_ok=True)

    try:
        journal = CaptureJournal(destination)
        identity = journal.identity()
    except CaptureJournalError as error:
        raise JournalArchiveError(f"restored journal verification failed: {error}") from error
    return RestoreReport(
        destination_root=destination,
        shard_dates=tuple(restored_dates),
        restored_bytes=restored_bytes,
        genesis_sha256=identity.genesis_sha256,
        head_sha256=identity.head_sha256,
        head_sequence=identity.head_sequence,
    )
