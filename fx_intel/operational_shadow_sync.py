"""Evidence-pinned, byte-offset shadow synchronization for operational SQLite.

The JSONL files remain the authoritative append-only evidence.  A parity-verified
candidate may be bootstrapped exactly once, after which only complete appended
lines are projected into SQLite.  Every consumed byte range receives a SHA-256
chunk record in the same transaction as its rows and cursor movement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
from typing import Any

from .operational_migration import (
    OperationalMigrationError,
    decision_records_from_event,
    price_record_from_row,
)
from .operational_store import (
    InvalidOperationalRecord,
    OperationalStore,
    SourceCursor,
    file_sha256,
    open_operational_writer,
)

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class ShadowSyncError(RuntimeError):
    """The shadow projection cannot advance without weakening its evidence."""


class BootstrapEvidenceMismatch(ShadowSyncError):
    """The candidate, parity report, or raw snapshot no longer matches."""


class SourceRotationDetected(ShadowSyncError):
    """A source was replaced or truncated instead of append-only extended."""


class SourceBoundaryMismatch(ShadowSyncError):
    """Previously consumed source bytes no longer match the durable boundary."""


@dataclass(frozen=True)
class CompleteSourceSnapshot:
    path: str
    device_id: int
    inode: int
    bytes: int
    mtime_ns: int
    sha256: str
    row_count: int
    last_line_start_offset: int
    last_line_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device_id": self.device_id,
            "inode": self.inode,
            "bytes": self.bytes,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "row_count": self.row_count,
            "last_line_start_offset": self.last_line_start_offset,
            "last_line_sha256": self.last_line_sha256,
        }


@dataclass(frozen=True)
class SourceSyncResult:
    source_name: str
    source_kind: str
    start_offset: int
    end_offset: int
    source_size_observed: int
    complete_lines: int
    partial_tail_bytes: int
    inserted: int
    identical_retries: int
    audit_events_inserted: int
    predictions_inserted: int
    pit_ineligible: int
    rejections: int
    chunk_sha256: str
    status: str

    @property
    def has_quality_failure(self) -> bool:
        return self.rejections > 0 or self.pit_ineligible > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "source_kind": self.source_kind,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "source_size_observed": self.source_size_observed,
            "complete_lines": self.complete_lines,
            "partial_tail_bytes": self.partial_tail_bytes,
            "inserted": self.inserted,
            "identical_retries": self.identical_retries,
            "audit_events_inserted": self.audit_events_inserted,
            "predictions_inserted": self.predictions_inserted,
            "pit_ineligible": self.pit_ineligible,
            "rejections": self.rejections,
            "chunk_sha256": self.chunk_sha256,
            "status": self.status,
            "quality_failure": self.has_quality_failure,
        }


def bootstrap_candidate(
    *,
    database_path: str | Path,
    parity_report_path: str | Path,
    decision_path: str | Path,
    price_path: str | Path,
    writer_id: str,
    allow_source_prefix: bool = False,
) -> dict[str, object]:
    """Attach durable EOF cursors only to an unchanged parity-verified candidate."""

    database = Path(database_path).resolve()
    parity_path = Path(parity_report_path).resolve()
    decisions = Path(decision_path).resolve()
    prices = Path(price_path).resolve()
    report = _load_json_object(parity_path)
    _verify_parity_verdict(report)
    expected_database_hash = _required_sha256(report.get("database_sha256"), "database_sha256")
    if not database.is_file():
        raise BootstrapEvidenceMismatch(f"candidate database does not exist: {database}")
    wal_path = Path(f"{database}-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise BootstrapEvidenceMismatch(
            f"candidate has an uncheckpointed WAL and cannot be hash-pinned: {wal_path}"
        )
    database_hash_before = file_sha256(database)
    if database_hash_before != expected_database_hash:
        raise BootstrapEvidenceMismatch(
            "candidate database SHA-256 differs from parity report: "
            f"expected {expected_database_hash}, got {database_hash_before}"
        )
    reported_database = _mapping(report.get("database"), "database")
    reported_database_path = Path(_required_text(reported_database.get("path"), "database.path"))
    if reported_database_path.resolve() != database:
        raise BootstrapEvidenceMismatch(
            f"parity report database path differs: {reported_database_path} != {database}"
        )

    sources = _mapping(report.get("sources"), "sources")
    decision_snapshot = _verified_snapshot(
        decisions,
        _mapping(sources.get("decisions"), "sources.decisions"),
        allow_prefix=allow_source_prefix,
    )
    price_snapshot = _verified_snapshot(
        prices,
        _mapping(sources.get("prices"), "sources.prices"),
        allow_prefix=allow_source_prefix,
    )
    captured_at = datetime.now(UTC)
    with open_operational_writer(database, writer_id=writer_id) as store:
        _verify_candidate_counts(store, report)
        with store.atomic_batch():
            _bootstrap_snapshot(
                store,
                source_name="decisions",
                source_kind="decisions",
                snapshot=decision_snapshot,
                captured_at=captured_at,
            )
            _bootstrap_snapshot(
                store,
                source_name="prices",
                source_kind="prices",
                snapshot=price_snapshot,
                captured_at=captured_at,
            )
        checkpoint = store.checkpoint("TRUNCATE")
        audit = store.audit()

    return {
        "schema_version": 1,
        "operation": "bootstrap",
        "status": "shadow_ready",
        "generated_at": datetime.now(UTC).isoformat(),
        "writer_id": writer_id,
        "runtime": runtime_provenance(),
        "parity_report": {
            "path": str(parity_path),
            "sha256": file_sha256(parity_path),
            "parity_verdict": "parity_verified",
        },
        "database": {
            "path": str(database),
            "sha256_before": database_hash_before,
            "sha256_after": file_sha256(database),
            "audit": audit.to_dict(),
            "checkpoint": checkpoint,
        },
        "sources": {
            "decisions": decision_snapshot.to_dict(),
            "prices": price_snapshot.to_dict(),
        },
        "limitations": [
            "shadow_projection_only",
            "append_only_raw_remains_authoritative",
            "no_production_read_cutover",
            *(["bootstrapped_from_verified_live_prefix"] if allow_source_prefix else []),
        ],
    }


def sync_sources(
    *,
    database_path: str | Path,
    decision_path: str | Path,
    price_path: str | Path,
    writer_id: str,
    max_bytes_per_source: int = DEFAULT_MAX_BYTES,
) -> dict[str, object]:
    """Synchronize both raw streams while one process owns the SQLite writer."""

    if max_bytes_per_source <= 0:
        raise ValueError("max_bytes_per_source must be positive")
    started_at = datetime.now(UTC)
    with open_operational_writer(database_path, writer_id=writer_id) as store:
        return sync_sources_with_store(
            store,
            decision_path=decision_path,
            price_path=price_path,
            max_bytes_per_source=max_bytes_per_source,
            started_at=started_at,
        )


def sync_sources_with_store(
    store: OperationalStore,
    *,
    decision_path: str | Path,
    price_path: str | Path,
    max_bytes_per_source: int = DEFAULT_MAX_BYTES,
    started_at: datetime | None = None,
) -> dict[str, object]:
    """Synchronize both sources atomically through an already leased writer."""

    if max_bytes_per_source <= 0:
        raise ValueError("max_bytes_per_source must be positive")
    if store.writer_id is None:
        raise ShadowSyncError("shadow sync requires a writer-capable store")
    began = started_at or datetime.now(UTC)
    with store.atomic_batch():
        decisions = sync_one_source(
            store,
            source_name="decisions",
            source_kind="decisions",
            source_path=decision_path,
            max_bytes=max_bytes_per_source,
        )
        prices = sync_one_source(
            store,
            source_name="prices",
            source_kind="prices",
            source_path=price_path,
            max_bytes=max_bytes_per_source,
        )
    audit = store.audit()
    quality_failure = decisions.has_quality_failure or prices.has_quality_failure
    return {
        "schema_version": 1,
        "operation": "incremental_shadow_sync",
        "started_at": began.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "writer_id": store.writer_id,
        "runtime": runtime_provenance(),
        "quality_failure": quality_failure,
        "sources": {
            "decisions": decisions.to_dict(),
            "prices": prices.to_dict(),
        },
        "database": audit.to_dict(),
    }


def sync_one_source(
    store: OperationalStore,
    *,
    source_name: str,
    source_kind: str,
    source_path: str | Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SourceSyncResult:
    """Consume only complete appended lines from one identity-pinned source."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    path = Path(source_path).resolve()
    cursor = store.source_cursor(source_name)
    if cursor is None:
        raise ShadowSyncError(f"source cursor is not bootstrapped: {source_name}")
    _verify_cursor_contract(cursor, source_kind=source_kind, source_path=path)

    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        _verify_source_identity(cursor, before)
        _verify_previous_boundary(handle, cursor)
        available = before.st_size - cursor.byte_offset
        if available == 0:
            return _no_change_result(cursor, before.st_size)
        handle.seek(cursor.byte_offset)
        requested = min(available, max_bytes)
        raw = handle.read(requested)
        if len(raw) != requested:
            raise SourceRotationDetected(f"source shortened while reading: {path}")
        newline = raw.rfind(b"\n")
        if newline < 0:
            if requested == max_bytes and available >= max_bytes:
                raise ShadowSyncError(f"complete JSONL line exceeds max_bytes={max_bytes}: {path}")
            return _partial_only_result(cursor, before.st_size, len(raw))
        consumed = raw[: newline + 1]
        partial_tail = len(raw) - len(consumed)
        after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise SourceRotationDetected(f"source identity changed while reading: {path}")
        if after.st_size < cursor.byte_offset + len(consumed):
            raise SourceRotationDetected(f"source truncated while reading: {path}")

    captured_at = datetime.now(UTC)
    parsed, rejections, pit_ineligible = _project_lines(
        consumed,
        source_kind=source_kind,
        start_offset=cursor.byte_offset,
        imported_at=captured_at,
    )
    inserted = 0
    identical = 0
    audit_inserted = 0
    predictions_inserted = 0
    with store.atomic_batch():
        if source_kind == "decisions":
            for audit_record, prediction_record in parsed:
                if store.record_audit_event(audit_record):
                    inserted += 1
                    audit_inserted += 1
                else:
                    identical += 1
                if prediction_record is not None:
                    if store.record_prediction(prediction_record):
                        inserted += 1
                        predictions_inserted += 1
                    else:
                        identical += 1
        else:
            for price_record in parsed:
                if store.record_price_point(price_record):
                    inserted += 1
                else:
                    identical += 1
        for rejection in rejections:
            store.record_ingest_rejection(
                source_name=source_name,
                line_start_offset=rejection[0],
                line_end_offset=rejection[1],
                raw_line_sha256=rejection[2],
                reason=rejection[3],
                observed_at=captured_at,
            )
        end_offset = cursor.byte_offset + len(consumed)
        final_line_relative_start = consumed.rfind(b"\n", 0, len(consumed) - 1) + 1
        final_line_start = cursor.byte_offset + final_line_relative_start
        final_line = consumed[final_line_relative_start:]
        store.advance_source_cursor(
            source_name=source_name,
            expected_offset=cursor.byte_offset,
            end_offset=end_offset,
            chunk_sha256=hashlib.sha256(consumed).hexdigest(),
            last_line_start_offset=final_line_start,
            last_line_sha256=hashlib.sha256(final_line).hexdigest(),
            row_count=consumed.count(b"\n"),
            source_size_at_sync=after.st_size,
            source_mtime_ns=after.st_mtime_ns,
            captured_at=captured_at,
        )
    status = "quality_failure" if rejections or pit_ineligible else "advanced"
    return SourceSyncResult(
        source_name=source_name,
        source_kind=source_kind,
        start_offset=cursor.byte_offset,
        end_offset=cursor.byte_offset + len(consumed),
        source_size_observed=after.st_size,
        complete_lines=consumed.count(b"\n"),
        partial_tail_bytes=partial_tail,
        inserted=inserted,
        identical_retries=identical,
        audit_events_inserted=audit_inserted,
        predictions_inserted=predictions_inserted,
        pit_ineligible=pit_ineligible,
        rejections=len(rejections),
        chunk_sha256=hashlib.sha256(consumed).hexdigest(),
        status=status,
    )


def fingerprint_complete_source(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> CompleteSourceSnapshot:
    """Hash a stable, newline-complete JSONL snapshot and capture its EOF boundary."""

    target = Path(path).resolve()
    if not target.is_file():
        raise BootstrapEvidenceMismatch(f"raw source does not exist: {target}")
    digest = hashlib.sha256()
    row_count = 0
    line_start = 0
    last_line_start = 0
    total = 0
    final_byte = b""
    with target.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            final_byte = chunk[-1:]
            position = 0
            while True:
                newline = chunk.find(b"\n", position)
                if newline < 0:
                    break
                last_line_start = line_start
                line_start = total + newline + 1
                row_count += 1
                position = newline + 1
            total += len(chunk)
        after = os.fstat(handle.fileno())
        stable_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if stable_before != stable_after or total != after.st_size:
            raise BootstrapEvidenceMismatch(f"raw source changed while hashing: {target}")
        if total and final_byte != b"\n":
            raise BootstrapEvidenceMismatch(
                f"bootstrap source has an incomplete final JSONL line: {target}"
            )
        if total:
            handle.seek(last_line_start)
            last_line = handle.read(total - last_line_start)
            last_line_hash = hashlib.sha256(last_line).hexdigest()
        else:
            last_line_hash = EMPTY_SHA256
    actual_hash = digest.hexdigest()
    if actual_hash != expected_sha256:
        raise BootstrapEvidenceMismatch(
            f"raw source SHA-256 mismatch for {target}: "
            f"expected {expected_sha256}, got {actual_hash}"
        )
    if total != expected_bytes:
        raise BootstrapEvidenceMismatch(
            f"raw source size mismatch for {target}: expected {expected_bytes}, got {total}"
        )
    return CompleteSourceSnapshot(
        path=str(target),
        device_id=after.st_dev,
        inode=after.st_ino,
        bytes=total,
        mtime_ns=after.st_mtime_ns,
        sha256=actual_hash,
        row_count=row_count,
        last_line_start_offset=last_line_start,
        last_line_sha256=last_line_hash,
    )


def fingerprint_complete_prefix(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> CompleteSourceSnapshot:
    """Verify one immutable complete prefix of a longer append-only JSONL."""

    target = Path(path).resolve()
    if not target.is_file():
        raise BootstrapEvidenceMismatch(f"raw source does not exist: {target}")
    if expected_bytes < 0:
        raise BootstrapEvidenceMismatch("expected_bytes must be non-negative")
    digest = hashlib.sha256()
    row_count = 0
    line_start = 0
    last_line_start = 0
    total = 0
    final_byte = b""
    with target.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        if before.st_size < expected_bytes:
            raise BootstrapEvidenceMismatch(f"raw source is shorter than verified prefix: {target}")
        remaining = expected_bytes
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise BootstrapEvidenceMismatch(
                    f"raw source ended inside verified prefix: {target}"
                )
            digest.update(chunk)
            final_byte = chunk[-1:]
            position = 0
            while True:
                newline = chunk.find(b"\n", position)
                if newline < 0:
                    break
                last_line_start = line_start
                line_start = total + newline + 1
                row_count += 1
                position = newline + 1
            total += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BootstrapEvidenceMismatch(
                f"raw source identity changed while hashing prefix: {target}"
            )
        if after.st_size < expected_bytes:
            raise BootstrapEvidenceMismatch(f"raw source truncated below verified prefix: {target}")
        if total and final_byte != b"\n":
            raise BootstrapEvidenceMismatch(
                f"verified prefix has an incomplete final JSONL line: {target}"
            )
        if total:
            handle.seek(last_line_start)
            last_line = handle.read(total - last_line_start)
            last_line_hash = hashlib.sha256(last_line).hexdigest()
        else:
            last_line_hash = EMPTY_SHA256
    actual_hash = digest.hexdigest()
    if actual_hash != expected_sha256:
        raise BootstrapEvidenceMismatch(
            "raw source prefix SHA-256 mismatch for "
            f"{target}: expected {expected_sha256}, got {actual_hash}"
        )
    return CompleteSourceSnapshot(
        path=str(target),
        device_id=after.st_dev,
        inode=after.st_ino,
        bytes=expected_bytes,
        mtime_ns=after.st_mtime_ns,
        sha256=actual_hash,
        row_count=row_count,
        last_line_start_offset=last_line_start,
        last_line_sha256=last_line_hash,
    )


def runtime_provenance() -> dict[str, object]:
    """Capture the exact shadow-sync implementation and runtime identity."""

    implementation_files = (
        Path(__file__).resolve(),
        Path(__file__).with_name("operational_store.py").resolve(),
        Path(__file__).with_name("operational_migration.py").resolve(),
        Path(__file__).with_name("state_io.py").resolve(),
    )
    digest = hashlib.sha256()
    for path in implementation_files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    repository = Path(__file__).resolve().parents[1]
    return {
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "sqlite_version": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "implementation_sha256": digest.hexdigest(),
        "git_commit": _git_output(repository, "rev-parse", "HEAD"),
        "git_dirty": bool(_git_output(repository, "status", "--porcelain")),
    }


def _verified_snapshot(
    actual_path: Path,
    reported: Mapping[str, object],
    *,
    allow_prefix: bool,
) -> CompleteSourceSnapshot:
    reported_path = Path(_required_text(reported.get("path"), "source.path")).resolve()
    if reported_path != actual_path and not allow_prefix:
        raise BootstrapEvidenceMismatch(
            f"raw source path differs from parity report: {actual_path} != {reported_path}"
        )
    expected_bytes = reported.get("bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
        raise BootstrapEvidenceMismatch("source.bytes must be an integer")
    if reported_path != actual_path:
        return fingerprint_complete_prefix(
            actual_path,
            expected_sha256=_required_sha256(
                reported.get("sha256"),
                "source.sha256",
            ),
            expected_bytes=expected_bytes,
        )
    return fingerprint_complete_source(
        actual_path,
        expected_sha256=_required_sha256(reported.get("sha256"), "source.sha256"),
        expected_bytes=expected_bytes,
    )


def _bootstrap_snapshot(
    store: OperationalStore,
    *,
    source_name: str,
    source_kind: str,
    snapshot: CompleteSourceSnapshot,
    captured_at: datetime,
) -> None:
    store.bootstrap_source_cursor(
        source_name=source_name,
        source_kind=source_kind,
        source_path=snapshot.path,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
        byte_offset=snapshot.bytes,
        last_line_start_offset=snapshot.last_line_start_offset,
        last_line_sha256=snapshot.last_line_sha256,
        source_mtime_ns=snapshot.mtime_ns,
        source_sha256=snapshot.sha256,
        row_count=snapshot.row_count,
        captured_at=captured_at,
    )


def _project_lines(
    raw: bytes,
    *,
    source_kind: str,
    start_offset: int,
    imported_at: datetime,
) -> tuple[list[Any], list[tuple[int, int, str, str]], int]:
    parsed_records: list[Any] = []
    rejections: list[tuple[int, int, str, str]] = []
    pit_ineligible = 0
    relative_offset = 0
    for line in raw.splitlines(keepends=True):
        line_start = start_offset + relative_offset
        line_end = line_start + len(line)
        relative_offset += len(line)
        line_hash = hashlib.sha256(line).hexdigest()
        if not line.strip():
            rejections.append((line_start, line_end, line_hash, "blank_line"))
            continue
        try:
            payload: Any = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            rejections.append((line_start, line_end, line_hash, "malformed_json"))
            continue
        if not isinstance(payload, Mapping):
            rejections.append((line_start, line_end, line_hash, "non_object_json"))
            continue
        try:
            if source_kind == "decisions":
                audit, prediction, failure = decision_records_from_event(
                    payload,
                    imported_at=imported_at,
                )
                parsed_records.append((audit, prediction))
                if failure:
                    pit_ineligible += 1
            elif source_kind == "prices":
                parsed_records.append(price_record_from_row(payload))
            else:
                raise ShadowSyncError(f"unsupported source kind: {source_kind}")
        except (OperationalMigrationError, InvalidOperationalRecord, ValueError) as error:
            rejections.append((line_start, line_end, line_hash, _error_reason(error)))
    return parsed_records, rejections, pit_ineligible


def _verify_cursor_contract(
    cursor: SourceCursor,
    *,
    source_kind: str,
    source_path: Path,
) -> None:
    if cursor.source_kind != source_kind:
        raise ShadowSyncError(
            f"source kind differs for {cursor.source_name}: "
            f"{cursor.source_kind} != {source_kind}"
        )
    if Path(cursor.source_path).resolve() != source_path:
        raise ShadowSyncError(
            f"source path differs for {cursor.source_name}: "
            f"{cursor.source_path} != {source_path}"
        )


def _verify_source_identity(cursor: SourceCursor, stat: os.stat_result) -> None:
    if (stat.st_dev, stat.st_ino) != (cursor.device_id, cursor.inode):
        raise SourceRotationDetected(
            f"source identity changed for {cursor.source_name}: "
            f"{(cursor.device_id, cursor.inode)} != {(stat.st_dev, stat.st_ino)}"
        )
    if stat.st_size < cursor.byte_offset:
        raise SourceRotationDetected(
            f"source truncated for {cursor.source_name}: " f"{stat.st_size} < {cursor.byte_offset}"
        )


def _verify_previous_boundary(handle: Any, cursor: SourceCursor) -> None:
    if cursor.byte_offset == 0:
        if cursor.last_line_start_offset != 0 or cursor.last_line_sha256 != EMPTY_SHA256:
            raise SourceBoundaryMismatch(
                f"empty source boundary is inconsistent: {cursor.source_name}"
            )
        return
    length = cursor.byte_offset - cursor.last_line_start_offset
    if length <= 0:
        raise SourceBoundaryMismatch(f"invalid previous line boundary: {cursor.source_name}")
    handle.seek(cursor.last_line_start_offset)
    previous_line = handle.read(length)
    if (
        len(previous_line) != length
        or not previous_line.endswith(b"\n")
        or hashlib.sha256(previous_line).hexdigest() != cursor.last_line_sha256
    ):
        raise SourceBoundaryMismatch(f"previous complete line changed: {cursor.source_name}")


def _no_change_result(cursor: SourceCursor, source_size: int) -> SourceSyncResult:
    return SourceSyncResult(
        source_name=cursor.source_name,
        source_kind=cursor.source_kind,
        start_offset=cursor.byte_offset,
        end_offset=cursor.byte_offset,
        source_size_observed=source_size,
        complete_lines=0,
        partial_tail_bytes=0,
        inserted=0,
        identical_retries=0,
        audit_events_inserted=0,
        predictions_inserted=0,
        pit_ineligible=0,
        rejections=0,
        chunk_sha256=EMPTY_SHA256,
        status="no_change",
    )


def _partial_only_result(
    cursor: SourceCursor,
    source_size: int,
    partial_bytes: int,
) -> SourceSyncResult:
    result = _no_change_result(cursor, source_size)
    return SourceSyncResult(
        **{
            **result.__dict__,
            "partial_tail_bytes": partial_bytes,
            "status": "waiting_for_complete_line",
        }
    )


def _verify_parity_verdict(report: Mapping[str, object]) -> None:
    if report.get("parity_verdict") != "parity_verified":
        raise BootstrapEvidenceMismatch("bootstrap requires parity_verdict=parity_verified")


def _verify_candidate_counts(
    store: OperationalStore,
    report: Mapping[str, object],
) -> None:
    reported_database = _mapping(report.get("database"), "database")
    rows = _mapping(reported_database.get("rows"), "database.rows")
    audit = store.audit()
    expected = {
        "audit_events": audit.audit_events,
        "predictions": audit.predictions,
        "pending_predictions": audit.pending_predictions,
        "price_points": audit.price_points,
        "outcomes": audit.outcomes,
        "checkpoints": audit.checkpoints,
    }
    for name, actual in expected.items():
        reported = rows.get(name)
        if isinstance(reported, bool) or not isinstance(reported, int) or reported != actual:
            raise BootstrapEvidenceMismatch(
                f"candidate row count differs for {name}: expected {reported}, got {actual}"
            )
    if audit.source_cursors or audit.source_chunks or audit.ingest_rejections:
        raise BootstrapEvidenceMismatch("candidate already has shadow-sync state")


def _load_json_object(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise BootstrapEvidenceMismatch(f"parity report does not exist: {path}")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapEvidenceMismatch(f"invalid parity report: {path}") from error
    return _mapping(payload, "parity_report")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BootstrapEvidenceMismatch(f"{field_name} must be an object")
    return value


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise BootstrapEvidenceMismatch(f"{field_name} is required")
    return normalized


def _required_sha256(value: object, field_name: str) -> str:
    normalized = _required_text(value, field_name).lower()
    if len(normalized) != 64:
        raise BootstrapEvidenceMismatch(f"{field_name} is not SHA-256")
    try:
        bytes.fromhex(normalized)
    except ValueError as error:
        raise BootstrapEvidenceMismatch(f"{field_name} is not SHA-256") from error
    return normalized


def _error_reason(error: BaseException) -> str:
    value = str(error).strip() or type(error).__name__
    return value.replace("\n", " ")[:256]


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


__all__ = [
    "BootstrapEvidenceMismatch",
    "CompleteSourceSnapshot",
    "DEFAULT_MAX_BYTES",
    "ShadowSyncError",
    "SourceBoundaryMismatch",
    "SourceRotationDetected",
    "SourceSyncResult",
    "bootstrap_candidate",
    "fingerprint_complete_source",
    "fingerprint_complete_prefix",
    "runtime_provenance",
    "sync_one_source",
    "sync_sources",
    "sync_sources_with_store",
]
