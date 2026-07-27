"""Daily full-replay audit for the raw-evidence to SQLite projection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, IO

from . import decision_commit, decision_log, journal
from .operational_migration import (
    OperationalMigrationError,
    decision_records_from_event,
    price_record_from_row,
)
from .operational_shadow_sync import DECISION_SUPPORT_CURSOR_PREFIX, runtime_provenance
from .operational_store import (
    InvalidOperationalRecord,
    OperationalStore,
    SourceCursor,
    file_sha256,
    open_operational_writer,
)


class FullReplayError(RuntimeError):
    """A stable, identity-pinned full replay cannot be completed."""


@dataclass
class SourceReplayMetrics:
    source_name: str
    source_kind: str
    path: str
    device_id: int
    inode: int
    cursor_offset: int
    source_size: int
    source_mtime_ns: int = 0
    source_ctime_ns: int = 0
    replay_completed_at: str = ""
    chunk_count: int = 0
    chunk_bytes: int = 0
    chunk_rows: int = 0
    replay_lines: int = 0
    valid_rows: int = 0
    expected_primary_rows: int = 0
    expected_predictions: int = 0
    pit_ineligible: int = 0
    bootstrap_exclusions: int = 0
    incremental_rejections: int = 0
    unconsumed_complete_lines: int = 0
    incomplete_tail_bytes: int = 0
    violations: Counter[str] = field(default_factory=Counter)

    @property
    def violation_count(self) -> int:
        return sum(self.violations.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "source_kind": self.source_kind,
            "path": self.path,
            "device_id": self.device_id,
            "inode": self.inode,
            "cursor_offset": self.cursor_offset,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "source_ctime_ns": self.source_ctime_ns,
            "replay_completed_at": self.replay_completed_at,
            "chunk_count": self.chunk_count,
            "chunk_bytes": self.chunk_bytes,
            "chunk_rows": self.chunk_rows,
            "replay_lines": self.replay_lines,
            "valid_rows": self.valid_rows,
            "expected_primary_rows": self.expected_primary_rows,
            "expected_predictions": self.expected_predictions,
            "pit_ineligible": self.pit_ineligible,
            "bootstrap_exclusions": self.bootstrap_exclusions,
            "incremental_rejections": self.incremental_rejections,
            "unconsumed_complete_lines": self.unconsumed_complete_lines,
            "incomplete_tail_bytes": self.incomplete_tail_bytes,
            "violation_count": self.violation_count,
            "violations": dict(sorted(self.violations.items())),
        }


def run_full_replay(
    *,
    database_path: str | Path,
    decision_path: str | Path,
    price_path: str | Path,
    writer_id: str,
) -> dict[str, object]:
    """Hold the single-writer lease while replaying both authoritative sources."""

    started_at = datetime.now(UTC)
    database = Path(database_path).resolve()
    with open_operational_writer(database, writer_id=writer_id) as store:
        return full_replay_with_store(
            store,
            database_path=database,
            decision_path=decision_path,
            price_path=price_path,
            started_at=started_at,
        )


def full_replay_with_store(
    store: OperationalStore,
    *,
    database_path: str | Path,
    decision_path: str | Path,
    price_path: str | Path,
    started_at: datetime | None = None,
) -> dict[str, object]:
    """Replay both sources through an already leased writer and checkpoint WAL."""

    if store.writer_id is None:
        raise FullReplayError("full replay requires a writer-capable store")
    began = started_at or datetime.now(UTC)
    database = Path(database_path).resolve()
    if store.path.resolve() != database:
        raise FullReplayError(
            f"leased database differs from replay target: {store.path} != {database}"
        )
    decision_support_cursors = _decision_support_cursors(
        store,
        decision_path=Path(decision_path).resolve(),
    )
    with lock_replay_sources(
        decision_path,
        price_path,
        *decision_support_cursors,
    ):
        decisions = replay_source(
            store,
            source_name="decisions",
            source_kind="decisions",
            source_path=decision_path,
        )
        prices = replay_source(
            store,
            source_name="prices",
            source_kind="prices",
            source_path=price_path,
        )
        decision_support = [
            replay_decision_support_source(
                store,
                cursor=cursor,
                source_path=path,
            )
            for path, cursor in decision_support_cursors.items()
        ]
        checkpoint = store.checkpoint("TRUNCATE")
        audit = store.audit()
    total_violations = (
        decisions.violation_count
        + prices.violation_count
        + sum(metrics.violation_count for metrics in decision_support)
        + audit.foreign_key_violations
        + audit.source_manifest_violations
        + (0 if audit.integrity_check == "ok" else 1)
        + (0 if audit.wal_bytes == 0 else 1)
    )
    data_quality_exclusions = (
        decisions.bootstrap_exclusions
        + decisions.incremental_rejections
        + decisions.pit_ineligible
        + prices.bootstrap_exclusions
        + prices.incremental_rejections
    )
    return {
        "schema_version": 1,
        "operation": "operational_full_replay",
        "started_at": began.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "writer_id": store.writer_id,
        "runtime": _full_replay_runtime_provenance(),
        "verdict": "full_replay_verified" if total_violations == 0 else "full_replay_failed",
        "violation_count": total_violations,
        "data_quality_status": ("usable_with_exclusions" if data_quality_exclusions else "clean"),
        "data_quality_exclusions": data_quality_exclusions,
        "database": {
            **audit.to_dict(),
            "checkpoint": checkpoint,
            "sha256": file_sha256(database),
            "sha256_scope": (
                "checkpointed_main_database_file"
                if audit.wal_bytes == 0
                else "main_file_only_uncheckpointed_wal"
            ),
            "uncheckpointed_wal": audit.wal_bytes != 0,
        },
        "sources": {
            "decisions": decisions.to_dict(),
            "decision_support": [metrics.to_dict() for metrics in decision_support],
            "prices": prices.to_dict(),
        },
    }


@contextmanager
def lock_replay_sources(*paths: str | Path) -> Iterator[None]:
    """Hold both canonical raw snapshots stable for a complete replay."""

    handles: list[IO[bytes]] = []
    try:
        for path in sorted({Path(value).resolve() for value in paths}):
            source_handle = path.open("rb")
            try:
                fcntl.flock(source_handle.fileno(), fcntl.LOCK_SH)
            except BaseException:
                source_handle.close()
                raise
            handles.append(source_handle)
        yield
    finally:
        for locked_handle in reversed(handles):
            try:
                fcntl.flock(locked_handle.fileno(), fcntl.LOCK_UN)
            finally:
                locked_handle.close()


def replay_source(
    store: OperationalStore,
    *,
    source_name: str,
    source_kind: str,
    source_path: str | Path,
) -> SourceReplayMetrics:
    """Recompute every projection and byte-range proof through one durable cursor."""

    path = Path(source_path).resolve()
    cursor = store.source_cursor(source_name)
    if cursor is None:
        raise FullReplayError(f"source cursor is not bootstrapped: {source_name}")
    if cursor.source_kind != source_kind:
        raise FullReplayError(
            f"source kind differs for {source_name}: {cursor.source_kind} != {source_kind}"
        )
    if Path(cursor.source_path).resolve() != path:
        raise FullReplayError(
            f"source path differs for {source_name}: {cursor.source_path} != {path}"
        )
    metrics = SourceReplayMetrics(
        source_name=source_name,
        source_kind=source_kind,
        path=str(path),
        device_id=cursor.device_id,
        inode=cursor.inode,
        cursor_offset=cursor.byte_offset,
        source_size=0,
    )
    chunks = store.connection.execute(
        """
        SELECT start_offset, end_offset, chunk_sha256, last_line_start_offset,
               last_line_sha256, row_count, capture_kind
        FROM source_chunks
        WHERE source_name = ?
        ORDER BY start_offset, end_offset
        """,
        (source_name,),
    ).fetchall()
    if not chunks:
        raise FullReplayError(f"source has no chunk manifest: {source_name}")
    bootstrap_end = int(chunks[0]["end_offset"])
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        _verify_identity(cursor, before)
        metrics.source_size = before.st_size
        metrics.source_mtime_ns = before.st_mtime_ns
        metrics.source_ctime_ns = before.st_ctime_ns
        _verify_cursor_metadata(cursor, before, metrics)
        _verify_chunks(handle, chunks, metrics)
        _replay_consumed_prefix(
            store,
            handle,
            cursor=cursor,
            bootstrap_end=bootstrap_end,
            metrics=metrics,
        )
        complete_suffix, incomplete_tail = _suffix_state(
            handle,
            start=cursor.byte_offset,
            end=before.st_size,
        )
        metrics.unconsumed_complete_lines = complete_suffix
        metrics.incomplete_tail_bytes = incomplete_tail
        if complete_suffix:
            metrics.violations["unconsumed_complete_lines"] += complete_suffix
        if incomplete_tail:
            metrics.violations["incomplete_source_tail"] += 1
        after = os.fstat(handle.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise FullReplayError(f"source changed during full replay: {path}")
        metrics.replay_completed_at = datetime.now(UTC).isoformat()
    _verify_population_counts(store, metrics)
    return metrics


def replay_decision_support_source(
    store: OperationalStore,
    *,
    cursor: SourceCursor,
    source_path: str | Path,
) -> SourceReplayMetrics:
    """Re-hash every compact manifest chunk and reject any unconsumed tail."""

    path = Path(source_path).resolve()
    if cursor.source_kind != "decisions":
        raise FullReplayError(
            f"source kind differs for {cursor.source_name}: " f"{cursor.source_kind} != decisions"
        )
    if Path(cursor.source_path).resolve() != path:
        raise FullReplayError(
            f"source path differs for {cursor.source_name}: " f"{cursor.source_path} != {path}"
        )
    metrics = SourceReplayMetrics(
        source_name=cursor.source_name,
        source_kind="decision_support",
        path=str(path),
        device_id=cursor.device_id,
        inode=cursor.inode,
        cursor_offset=cursor.byte_offset,
        source_size=0,
    )
    chunks = store.connection.execute(
        """
        SELECT start_offset, end_offset, chunk_sha256, last_line_start_offset,
               last_line_sha256, row_count, capture_kind
        FROM source_chunks
        WHERE source_name = ?
        ORDER BY start_offset, end_offset
        """,
        (cursor.source_name,),
    ).fetchall()
    if not chunks:
        raise FullReplayError(f"source has no chunk manifest: {cursor.source_name}")
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        _verify_identity(cursor, before)
        metrics.source_size = before.st_size
        metrics.source_mtime_ns = before.st_mtime_ns
        metrics.source_ctime_ns = before.st_ctime_ns
        _verify_cursor_metadata(cursor, before, metrics)
        _verify_chunks(handle, chunks, metrics)
        metrics.replay_lines = metrics.chunk_rows
        complete_suffix, incomplete_tail = _suffix_state(
            handle,
            start=cursor.byte_offset,
            end=before.st_size,
        )
        metrics.unconsumed_complete_lines = complete_suffix
        metrics.incomplete_tail_bytes = incomplete_tail
        if complete_suffix:
            metrics.violations["unconsumed_complete_lines"] += complete_suffix
        if incomplete_tail:
            metrics.violations["incomplete_source_tail"] += 1
        after = os.fstat(handle.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise FullReplayError(f"source changed during full replay: {path}")
        metrics.replay_completed_at = datetime.now(UTC).isoformat()
    return metrics


def _decision_support_cursors(
    store: OperationalStore,
    *,
    decision_path: Path,
) -> dict[Path, SourceCursor]:
    rows = store.connection.execute(
        """
        SELECT source_name
        FROM source_cursors
        WHERE source_name LIKE ?
        ORDER BY source_name
        """,
        (f"{DECISION_SUPPORT_CURSOR_PREFIX}%",),
    ).fetchall()
    cursors: dict[Path, SourceCursor] = {}
    for row in rows:
        source_name = str(row["source_name"])
        cursor = store.source_cursor(source_name)
        if cursor is None:
            raise FullReplayError(f"decision support cursor disappeared: {source_name}")
        support_path = Path(cursor.source_path).resolve()
        expected_name = f"{DECISION_SUPPORT_CURSOR_PREFIX}{support_path.name}"
        if source_name != expected_name:
            raise FullReplayError(
                f"decision support cursor name differs: {source_name} != {expected_name}"
            )
        if support_path.parent != decision_path.parent:
            raise FullReplayError(
                f"decision support source is outside decision directory: {support_path}"
            )
        if support_path.name not in decision_commit.COMPACT_FILENAMES.values():
            raise FullReplayError(f"unsupported decision support source: {support_path}")
        if support_path in cursors:
            raise FullReplayError(f"duplicate decision support source cursor: {support_path}")
        cursors[support_path] = cursor

    existing = {
        (decision_path.parent / filename).resolve()
        for filename in decision_commit.COMPACT_FILENAMES.values()
        if (decision_path.parent / filename).is_file()
    }
    if set(cursors) != existing:
        raise FullReplayError(
            "decision support cursors differ from canonical compact sources: "
            f"{sorted(str(value) for value in cursors)} != "
            f"{sorted(str(value) for value in existing)}"
        )
    return dict(sorted(cursors.items(), key=lambda item: str(item[0])))


def _verify_chunks(
    handle: Any,
    chunks: list[Any],
    metrics: SourceReplayMetrics,
) -> None:
    expected_start = 0
    for index, chunk in enumerate(chunks):
        start = int(chunk["start_offset"])
        end = int(chunk["end_offset"])
        metrics.chunk_count += 1
        metrics.chunk_bytes += end - start
        metrics.chunk_rows += int(chunk["row_count"])
        expected_kind = "bootstrap" if index == 0 else "incremental"
        if start != expected_start:
            metrics.violations["chunk_gap_or_overlap"] += 1
        if str(chunk["capture_kind"]) != expected_kind:
            metrics.violations["chunk_capture_kind"] += 1
        digest, rows, last_start, last_hash, ends_newline = _fingerprint_range(
            handle,
            start=start,
            end=end,
        )
        if digest != str(chunk["chunk_sha256"]):
            metrics.violations["chunk_sha256_mismatch"] += 1
        if rows != int(chunk["row_count"]):
            metrics.violations["chunk_row_count_mismatch"] += 1
        if end > start and not ends_newline:
            metrics.violations["chunk_incomplete_final_line"] += 1
        if last_start != int(chunk["last_line_start_offset"]):
            metrics.violations["chunk_last_line_start_mismatch"] += 1
        if last_hash != str(chunk["last_line_sha256"]):
            metrics.violations["chunk_last_line_sha256_mismatch"] += 1
        expected_start = end
    if expected_start != metrics.cursor_offset:
        metrics.violations["chunk_cursor_end_mismatch"] += 1


def _replay_consumed_prefix(
    store: OperationalStore,
    handle: Any,
    *,
    cursor: SourceCursor,
    bootstrap_end: int,
    metrics: SourceReplayMetrics,
) -> None:
    expected_primary_ids: set[str] = set()
    expected_prediction_ids: set[str] = set()
    expected_rejection_offsets: set[int] = set()
    imported_at = datetime.now(UTC)
    commits: dict[str, dict[str, object]] = {}
    if metrics.source_kind == "decisions":
        handle.seek(0)
        prefix = handle.read(cursor.byte_offset)
        commit_values: list[object] = []
        for raw_line in prefix.splitlines():
            try:
                commit_values.append(json.loads(raw_line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        prefix_commits = decision_commit.commits_from_values(commit_values)
        verified_commits = decision_commit.load_commits(
            Path(metrics.path),
            verify_compact=True,
        )
        commits = {
            transaction_id: verified_commits[transaction_id]
            for transaction_id, record in prefix_commits.items()
            if decision_commit.commit_records_equal(
                verified_commits.get(transaction_id),
                record,
            )
        }
    handle.seek(0)
    while handle.tell() < cursor.byte_offset:
        line_start = handle.tell()
        line = handle.readline(cursor.byte_offset - line_start)
        line_end = handle.tell()
        metrics.replay_lines += 1
        if not line.endswith(b"\n"):
            metrics.violations["replay_incomplete_line"] += 1
            break
        parsed, reason = _parse_object(line)
        if parsed is None:
            _account_exclusion(
                store,
                line=line,
                line_start=line_start,
                line_end=line_end,
                reason=reason,
                bootstrap_end=bootstrap_end,
                expected_rejection_offsets=expected_rejection_offsets,
                metrics=metrics,
            )
            continue
        try:
            if metrics.source_kind == "decisions":
                if parsed.get("event_type") == decision_commit.COMMIT_EVENT_TYPE:
                    continue
                if parsed.get("event_type") == decision_log.DECISION_BATCH_EVENT_TYPE:
                    events: Sequence[Mapping[str, object]] = (
                        decision_log.decision_events_from_batch(
                            parsed,
                            commits=commits,
                            line_start_offset=line_start,
                            line_sha256=hashlib.sha256(line).hexdigest(),
                        )
                    )
                    if not events:
                        logical_events = decision_log.decision_events_from_batch(
                            parsed,
                            commits=commits,
                        )
                        raise OperationalMigrationError(
                            "superseded_decision_batch"
                            if logical_events
                            else "uncommitted_or_invalid_decision_batch"
                        )
                elif journal.is_pit_eligible_entry(parsed):
                    raise OperationalMigrationError("uncommitted_pit_event")
                else:
                    events = [parsed]
                for event in events:
                    audit, prediction, _failure = decision_records_from_event(
                        event,
                        imported_at=imported_at,
                    )
                    metrics.valid_rows += 1
                    _record_unique(
                        expected_primary_ids,
                        audit.event_id,
                        metrics,
                        "duplicate_decision_id",
                    )
                    _verify_audit_row(store, audit, metrics)
                    if prediction is None:
                        metrics.pit_ineligible += 1
                        unexpected = store.connection.execute(
                            "SELECT 1 FROM predictions WHERE prediction_id = ?",
                            (audit.event_id,),
                        ).fetchone()
                        if unexpected is not None:
                            metrics.violations["unexpected_prediction_for_pit_failure"] += 1
                    else:
                        _record_unique(
                            expected_prediction_ids,
                            prediction.prediction_id,
                            metrics,
                            "duplicate_prediction_id",
                        )
                        _verify_prediction_row(store, prediction, metrics)
            elif metrics.source_kind == "prices":
                price = price_record_from_row(parsed)
                metrics.valid_rows += 1
                _record_unique(
                    expected_primary_ids,
                    price.source_record_id,
                    metrics,
                    "duplicate_price_id",
                )
                _verify_price_row(store, price, metrics)
            else:
                raise FullReplayError(f"unsupported source kind: {metrics.source_kind}")
        except (OperationalMigrationError, InvalidOperationalRecord, ValueError) as error:
            _account_exclusion(
                store,
                line=line,
                line_start=line_start,
                line_end=line_end,
                reason=_error_reason(error),
                bootstrap_end=bootstrap_end,
                expected_rejection_offsets=expected_rejection_offsets,
                metrics=metrics,
            )
    metrics.expected_primary_rows = len(expected_primary_ids)
    metrics.expected_predictions = len(expected_prediction_ids)
    actual_rejections = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM ingest_rejections WHERE source_name = ?",
            (metrics.source_name,),
        ).fetchone()[0]
    )
    if actual_rejections != len(expected_rejection_offsets):
        metrics.violations["rejection_population_mismatch"] += abs(
            actual_rejections - len(expected_rejection_offsets)
        )


def _account_exclusion(
    store: OperationalStore,
    *,
    line: bytes,
    line_start: int,
    line_end: int,
    reason: str,
    bootstrap_end: int,
    expected_rejection_offsets: set[int],
    metrics: SourceReplayMetrics,
) -> None:
    if line_end <= bootstrap_end:
        metrics.bootstrap_exclusions += 1
        return
    metrics.incremental_rejections += 1
    expected_rejection_offsets.add(line_start)
    row = store.connection.execute(
        """
        SELECT line_end_offset, raw_line_sha256, reason
        FROM ingest_rejections
        WHERE source_name = ? AND line_start_offset = ?
        """,
        (metrics.source_name, line_start),
    ).fetchone()
    expected = (line_end, hashlib.sha256(line).hexdigest(), reason)
    if row is None:
        metrics.violations["missing_rejection"] += 1
    elif (
        int(row["line_end_offset"]),
        str(row["raw_line_sha256"]),
        str(row["reason"]),
    ) != expected:
        metrics.violations["rejection_payload_mismatch"] += 1


def _verify_audit_row(
    store: OperationalStore,
    record: Any,
    metrics: SourceReplayMetrics,
) -> None:
    row = store.connection.execute(
        """
        SELECT event_type, symbol, timeframe, event_time_ns,
               prediction_time_ns, available_time_ns, pit_eligible,
               pit_failure_reason, source, schema_version, payload_json,
               record_sha256
        FROM audit_events WHERE event_id = ?
        """,
        (record.event_id,),
    ).fetchone()
    if row is None:
        metrics.violations["missing_audit_event"] += 1
        return
    expected = (
        record.event_type,
        record.symbol,
        record.timeframe,
        _utc_ns(record.event_time),
        _optional_utc_ns(record.prediction_time),
        _optional_utc_ns(record.available_time),
        int(record.pit_eligible),
        record.pit_failure_reason,
        record.source,
        record.schema_version,
        _canonical_payload(record.payload),
    )
    if tuple(row[:-1]) != expected:
        metrics.violations["audit_payload_or_projection_mismatch"] += 1
    expected_hash = _record_sha256(
        {
            "event_id": record.event_id,
            "event_type": record.event_type,
            "symbol": record.symbol,
            "timeframe": record.timeframe,
            "event_time_ns": _utc_ns(record.event_time),
            "prediction_time_ns": _optional_utc_ns(record.prediction_time),
            "available_time_ns": _optional_utc_ns(record.available_time),
            "pit_eligible": record.pit_eligible,
            "pit_failure_reason": record.pit_failure_reason,
            "source": record.source,
            "schema_version": record.schema_version,
            "payload": record.payload,
        }
    )
    if str(row["record_sha256"]) != expected_hash:
        metrics.violations["audit_record_sha256_mismatch"] += 1


def _verify_prediction_row(
    store: OperationalStore,
    record: Any,
    metrics: SourceReplayMetrics,
) -> None:
    row = store.connection.execute(
        """
        SELECT decision_id, symbol, timeframe, prediction_time_ns,
               available_time_ns, due_time_ns, source, schema_version,
               payload_json, record_sha256
        FROM predictions WHERE prediction_id = ?
        """,
        (record.prediction_id,),
    ).fetchone()
    if row is None:
        metrics.violations["missing_prediction"] += 1
        return
    expected = (
        record.decision_id,
        record.symbol,
        record.timeframe,
        _utc_ns(record.prediction_time),
        _utc_ns(record.available_time),
        _utc_ns(record.due_time),
        record.source,
        record.schema_version,
        _canonical_payload(record.payload),
    )
    if tuple(row[:-1]) != expected:
        metrics.violations["prediction_payload_or_projection_mismatch"] += 1
    expected_hash = _record_sha256(
        {
            "prediction_id": record.prediction_id,
            "decision_id": record.decision_id,
            "symbol": record.symbol,
            "timeframe": record.timeframe,
            "prediction_time_ns": _utc_ns(record.prediction_time),
            "available_time_ns": _utc_ns(record.available_time),
            "due_time_ns": _utc_ns(record.due_time),
            "source": record.source,
            "schema_version": record.schema_version,
            "payload": record.payload,
        }
    )
    if str(row["record_sha256"]) != expected_hash:
        metrics.violations["prediction_record_sha256_mismatch"] += 1


def _verify_price_row(
    store: OperationalStore,
    record: Any,
    metrics: SourceReplayMetrics,
) -> None:
    row = store.connection.execute(
        """
        SELECT symbol, timeframe, event_time_ns, available_time_ns,
               ingested_time_ns, source, revision_id, schema_version,
               ohlc_scope, open, high, low, close, bid, ask, payload_json,
               record_sha256
        FROM price_points WHERE source_record_id = ?
        """,
        (record.source_record_id,),
    ).fetchone()
    if row is None:
        metrics.violations["missing_price_point"] += 1
        return
    expected = (
        record.symbol,
        record.timeframe,
        _utc_ns(record.event_time),
        _utc_ns(record.available_time),
        _utc_ns(record.ingested_time),
        record.source,
        record.revision_id,
        record.schema_version,
        record.ohlc_scope,
        record.open,
        record.high,
        record.low,
        record.close,
        record.bid,
        record.ask,
        _canonical_payload(record.payload),
    )
    if tuple(row[:-1]) != expected:
        metrics.violations["price_payload_or_projection_mismatch"] += 1
    expected_hash = _record_sha256(
        {
            "source_record_id": record.source_record_id,
            "symbol": record.symbol,
            "timeframe": record.timeframe,
            "event_time_ns": _utc_ns(record.event_time),
            "available_time_ns": _utc_ns(record.available_time),
            "ingested_time_ns": _utc_ns(record.ingested_time),
            "source": record.source,
            "revision_id": record.revision_id,
            "schema_version": record.schema_version,
            "ohlc_scope": record.ohlc_scope,
            "open": record.open,
            "high": record.high,
            "low": record.low,
            "close": record.close,
            "bid": record.bid,
            "ask": record.ask,
            "payload": record.payload,
        }
    )
    if str(row["record_sha256"]) != expected_hash:
        metrics.violations["price_record_sha256_mismatch"] += 1


def _verify_population_counts(
    store: OperationalStore,
    metrics: SourceReplayMetrics,
) -> None:
    if metrics.source_kind == "decisions":
        actual_primary = int(
            store.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        )
        actual_predictions = int(
            store.connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        )
        if actual_predictions != metrics.expected_predictions:
            metrics.violations["prediction_population_mismatch"] += abs(
                actual_predictions - metrics.expected_predictions
            )
    else:
        actual_primary = int(
            store.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0]
        )
    if actual_primary != metrics.expected_primary_rows:
        metrics.violations["primary_population_mismatch"] += abs(
            actual_primary - metrics.expected_primary_rows
        )


def _fingerprint_range(
    handle: Any,
    *,
    start: int,
    end: int,
) -> tuple[str, int, int, str, bool]:
    handle.seek(start)
    digest = hashlib.sha256()
    remaining = end - start
    rows = 0
    line_start = start
    last_line_start = start
    final_byte = b""
    position = start
    while remaining:
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        digest.update(chunk)
        final_byte = chunk[-1:]
        search = 0
        while True:
            newline = chunk.find(b"\n", search)
            if newline < 0:
                break
            last_line_start = line_start
            line_start = position + newline + 1
            rows += 1
            search = newline + 1
        position += len(chunk)
        remaining -= len(chunk)
    if remaining:
        return digest.hexdigest(), rows, last_line_start, "", False
    if end == start:
        return digest.hexdigest(), 0, start, hashlib.sha256(b"").hexdigest(), True
    handle.seek(last_line_start)
    last_line = handle.read(end - last_line_start)
    return (
        digest.hexdigest(),
        rows,
        last_line_start,
        hashlib.sha256(last_line).hexdigest(),
        final_byte == b"\n",
    )


def _suffix_state(handle: Any, *, start: int, end: int) -> tuple[int, int]:
    handle.seek(start)
    remaining = end - start
    complete = 0
    trailing = 0
    while remaining:
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        complete += chunk.count(b"\n")
        final_newline = chunk.rfind(b"\n")
        if final_newline < 0:
            trailing += len(chunk)
        else:
            trailing = len(chunk) - final_newline - 1
        remaining -= len(chunk)
    return complete, trailing


def _parse_object(line: bytes) -> tuple[Mapping[str, object] | None, str]:
    if not line.strip():
        return None, "blank_line"
    try:
        payload: Any = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "malformed_json"
    if not isinstance(payload, Mapping):
        return None, "non_object_json"
    return payload, ""


def _record_unique(
    values: set[str],
    value: str,
    metrics: SourceReplayMetrics,
    violation: str,
) -> None:
    if value in values:
        metrics.violations[violation] += 1
    values.add(value)


def _verify_identity(cursor: SourceCursor, stat: os.stat_result) -> None:
    if (stat.st_dev, stat.st_ino) != (cursor.device_id, cursor.inode):
        raise FullReplayError(f"source identity changed: {cursor.source_name}")
    if stat.st_size < cursor.byte_offset:
        raise FullReplayError(
            f"source truncated: {cursor.source_name} " f"{stat.st_size} < {cursor.byte_offset}"
        )


def _verify_cursor_metadata(
    cursor: SourceCursor,
    stat: os.stat_result,
    metrics: SourceReplayMetrics,
) -> None:
    if stat.st_size != cursor.source_size_at_sync:
        metrics.violations["source_size_at_sync_mismatch"] += 1
    if stat.st_mtime_ns != cursor.source_mtime_ns:
        metrics.violations["source_mtime_mismatch"] += 1
    if stat.st_ctime_ns != cursor.source_ctime_ns:
        metrics.violations["source_ctime_mismatch"] += 1


def _canonical_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _record_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _utc_ns(value: datetime) -> int:
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return (
        delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000
    )


def _optional_utc_ns(value: datetime | None) -> int | None:
    return _utc_ns(value) if value is not None else None


def _error_reason(error: BaseException) -> str:
    value = str(error).strip() or type(error).__name__
    return value.replace("\n", " ")[:256]


def _full_replay_runtime_provenance() -> dict[str, object]:
    provenance = runtime_provenance()
    digest = hashlib.sha256()
    digest.update(str(provenance["implementation_sha256"]).encode("ascii"))
    digest.update(b"\0")
    digest.update(Path(__file__).read_bytes())
    provenance["implementation_sha256"] = digest.hexdigest()
    return provenance


__all__ = [
    "FullReplayError",
    "SourceReplayMetrics",
    "full_replay_with_store",
    "lock_replay_sources",
    "replay_source",
    "run_full_replay",
]
