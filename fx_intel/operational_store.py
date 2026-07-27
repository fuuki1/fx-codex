"""Fail-closed SQLite WAL operational store for FX decision-support state.

The append-only raw evidence remains authoritative.  This database is the
transactional operational index used to find due predictions and bounded price
windows without rescanning the full JSONL history.

Mutation is deliberately available only through :func:`open_operational_writer`.
The context manager owns a process-wide advisory lock for the database path, so
different launchd jobs cannot silently become concurrent writers.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 5
APPLICATION_ID = 0x46584344  # "FXCD"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1_000


class OperationalStoreError(RuntimeError):
    """Base error for operational-store failures."""


class UnsafeSQLiteRuntime(OperationalStoreError):
    """The linked SQLite runtime contains the known WAL-reset corruption bug."""


class WriterBusy(OperationalStoreError):
    """Another process already owns the operational-store writer lock."""


class ImmutableRecordConflict(OperationalStoreError):
    """A natural key was reused with different immutable content."""


class InvalidOperationalRecord(OperationalStoreError):
    """A record does not satisfy the operational point-in-time contract."""


class UnsupportedSchemaVersion(OperationalStoreError):
    """The database schema is newer than this code understands."""


@dataclass(frozen=True)
class AuditEventRecord:
    event_id: str
    event_type: str
    symbol: str
    timeframe: str
    event_time: datetime
    source: str
    payload: Mapping[str, object]
    pit_eligible: bool
    pit_failure_reason: str
    schema_version: int = 1
    prediction_time: datetime | None = None
    available_time: datetime | None = None
    ingested_time: datetime | None = None


@dataclass(frozen=True)
class PredictionRecord:
    prediction_id: str
    decision_id: str
    symbol: str
    timeframe: str
    prediction_time: datetime
    available_time: datetime
    due_time: datetime
    source: str
    payload: Mapping[str, object]
    schema_version: int = 1
    ingested_time: datetime | None = None


@dataclass(frozen=True)
class PricePointRecord:
    source_record_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    available_time: datetime
    ingested_time: datetime
    source: str
    revision_id: str
    close: float
    payload: Mapping[str, object]
    ohlc_scope: str
    schema_version: int = 1
    open: float | None = None
    high: float | None = None
    low: float | None = None
    bid: float | None = None
    ask: float | None = None


@dataclass(frozen=True)
class OutcomeRecord:
    prediction_id: str
    label_version: str
    holding_end_time: datetime
    scored_time: datetime
    status: str
    source_checkpoint: str
    payload: Mapping[str, object]
    realized_net_r: float | None = None


@dataclass(frozen=True)
class SourceCursor:
    source_name: str
    source_kind: str
    source_path: str
    device_id: int
    inode: int
    byte_offset: int
    last_line_start_offset: int
    last_line_sha256: str
    source_size_at_sync: int
    source_mtime_ns: int
    source_ctime_ns: int
    updated_at_ns: int
    writer_id: str


@dataclass(frozen=True)
class BatchWriteResult:
    inserted: int
    identical_retries: int

    def __add__(self, other: BatchWriteResult) -> BatchWriteResult:
        return BatchWriteResult(
            inserted=self.inserted + other.inserted,
            identical_retries=self.identical_retries + other.identical_retries,
        )


@dataclass(frozen=True)
class StoreAudit:
    path: str
    sqlite_version: str
    schema_version: int
    journal_mode: str
    synchronous: int
    integrity_check: str
    foreign_key_violations: int
    source_manifest_violations: int
    audit_events: int
    predictions: int
    pending_predictions: int
    price_points: int
    outcomes: int
    checkpoints: int
    source_cursors: int
    source_chunks: int
    ingest_rejections: int
    database_bytes: int
    wal_bytes: int
    shm_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sqlite_version": self.sqlite_version,
            "schema_version": self.schema_version,
            "journal_mode": self.journal_mode,
            "synchronous": self.synchronous,
            "integrity_check": self.integrity_check,
            "foreign_key_violations": self.foreign_key_violations,
            "source_manifest_violations": self.source_manifest_violations,
            "rows": {
                "audit_events": self.audit_events,
                "predictions": self.predictions,
                "pending_predictions": self.pending_predictions,
                "price_points": self.price_points,
                "outcomes": self.outcomes,
                "checkpoints": self.checkpoints,
                "source_cursors": self.source_cursors,
                "source_chunks": self.source_chunks,
                "ingest_rejections": self.ingest_rejections,
            },
            "bytes": {
                "database": self.database_bytes,
                "wal": self.wal_bytes,
                "shm": self.shm_bytes,
            },
        }


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    event_time_ns INTEGER NOT NULL,
    prediction_time_ns INTEGER,
    available_time_ns INTEGER,
    pit_eligible INTEGER NOT NULL CHECK (pit_eligible IN (0, 1)),
    pit_failure_reason TEXT NOT NULL,
    source TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    payload_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    ingested_time_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (
        pit_eligible = 0
        OR (
            prediction_time_ns IS NOT NULL
            AND available_time_ns IS NOT NULL
            AND available_time_ns <= prediction_time_ns
        )
    )
) STRICT;

CREATE INDEX IF NOT EXISTS audit_events_timeline_idx
ON audit_events(symbol, timeframe, event_time_ns, event_id);

CREATE INDEX IF NOT EXISTS audit_events_pit_idx
ON audit_events(pit_eligible, event_time_ns, event_id);

CREATE INDEX IF NOT EXISTS audit_events_page_idx
ON audit_events(event_time_ns DESC, event_id DESC);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    prediction_time_ns INTEGER NOT NULL,
    available_time_ns INTEGER NOT NULL,
    due_time_ns INTEGER NOT NULL,
    source TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'scored', 'evaluation_unavailable')),
    payload_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    ingested_time_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (available_time_ns <= prediction_time_ns),
    CHECK (prediction_time_ns <= due_time_ns),
    CHECK (available_time_ns <= ingested_time_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS predictions_due_idx
ON predictions(status, due_time_ns, prediction_id);

CREATE INDEX IF NOT EXISTS predictions_pit_idx
ON predictions(symbol, timeframe, prediction_time_ns, prediction_id);

CREATE TABLE IF NOT EXISTS price_points (
    source_record_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    event_time_ns INTEGER NOT NULL,
    available_time_ns INTEGER NOT NULL,
    ingested_time_ns INTEGER NOT NULL,
    source TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    ohlc_scope TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL CHECK (close > 0),
    bid REAL,
    ask REAL,
    payload_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (event_time_ns <= available_time_ns),
    CHECK (available_time_ns <= ingested_time_ns),
    CHECK (open IS NULL OR open > 0),
    CHECK (high IS NULL OR high > 0),
    CHECK (low IS NULL OR low > 0),
    CHECK (bid IS NULL OR bid > 0),
    CHECK (ask IS NULL OR ask > 0),
    CHECK (high IS NULL OR low IS NULL OR high >= low),
    CHECK (high IS NULL OR close <= high),
    CHECK (low IS NULL OR close >= low),
    CHECK (bid IS NULL OR ask IS NULL OR bid <= ask),
    UNIQUE(symbol, timeframe, event_time_ns, source, revision_id)
) STRICT;

CREATE INDEX IF NOT EXISTS price_points_window_idx
ON price_points(symbol, timeframe, event_time_ns, available_time_ns);

CREATE INDEX IF NOT EXISTS price_points_page_idx
ON price_points(event_time_ns DESC, source_record_id DESC);

CREATE INDEX IF NOT EXISTS price_points_symbol_page_idx
ON price_points(symbol, timeframe, event_time_ns DESC, source_record_id DESC);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id TEXT NOT NULL,
    label_version TEXT NOT NULL,
    holding_end_time_ns INTEGER NOT NULL,
    scored_time_ns INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scored', 'evaluation_unavailable')),
    realized_net_r REAL,
    source_checkpoint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    PRIMARY KEY(prediction_id, label_version),
    FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id),
    CHECK (holding_end_time_ns <= scored_time_ns),
    CHECK (status != 'scored' OR realized_net_r IS NOT NULL)
) STRICT;

CREATE INDEX IF NOT EXISTS outcomes_scored_idx
ON outcomes(scored_time_ns, prediction_id, label_version);

CREATE TABLE IF NOT EXISTS work_checkpoints (
    job_name TEXT PRIMARY KEY,
    high_watermark_ns INTEGER NOT NULL,
    high_watermark_key TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_cursors (
    source_name TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('decisions', 'prices')),
    source_path TEXT NOT NULL,
    device_id INTEGER NOT NULL CHECK (device_id >= 0),
    inode INTEGER NOT NULL CHECK (inode >= 0),
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    last_line_start_offset INTEGER NOT NULL CHECK (last_line_start_offset >= 0),
    last_line_sha256 TEXT NOT NULL,
    source_size_at_sync INTEGER NOT NULL CHECK (source_size_at_sync >= byte_offset),
    source_mtime_ns INTEGER NOT NULL CHECK (source_mtime_ns >= 0),
    source_ctime_ns INTEGER NOT NULL CHECK (source_ctime_ns >= 0),
    updated_at_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (last_line_start_offset <= byte_offset)
) STRICT;

CREATE TABLE IF NOT EXISTS source_chunks (
    source_name TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset >= start_offset),
    chunk_sha256 TEXT NOT NULL,
    last_line_start_offset INTEGER NOT NULL CHECK (last_line_start_offset >= 0),
    last_line_sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    capture_kind TEXT NOT NULL CHECK (capture_kind IN ('bootstrap', 'incremental')),
    captured_at_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL,
    PRIMARY KEY(source_name, start_offset, end_offset),
    CHECK (last_line_start_offset <= end_offset)
) STRICT;

CREATE INDEX IF NOT EXISTS source_chunks_timeline_idx
ON source_chunks(source_name, end_offset);

CREATE TABLE IF NOT EXISTS ingest_rejections (
    source_name TEXT NOT NULL,
    line_start_offset INTEGER NOT NULL CHECK (line_start_offset >= 0),
    line_end_offset INTEGER NOT NULL CHECK (line_end_offset > line_start_offset),
    raw_line_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    writer_id TEXT NOT NULL,
    PRIMARY KEY(source_name, line_start_offset)
) STRICT;

CREATE INDEX IF NOT EXISTS ingest_rejections_timeline_idx
ON ingest_rejections(source_name, line_start_offset);
"""

_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit events are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit events are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS predictions_immutable_fields
    BEFORE UPDATE ON predictions
    WHEN OLD.prediction_id IS NOT NEW.prediction_id
      OR OLD.decision_id IS NOT NEW.decision_id
      OR OLD.symbol IS NOT NEW.symbol
      OR OLD.timeframe IS NOT NEW.timeframe
      OR OLD.prediction_time_ns IS NOT NEW.prediction_time_ns
      OR OLD.available_time_ns IS NOT NEW.available_time_ns
      OR OLD.due_time_ns IS NOT NEW.due_time_ns
      OR OLD.source IS NOT NEW.source
      OR OLD.schema_version IS NOT NEW.schema_version
      OR OLD.payload_json IS NOT NEW.payload_json
      OR OLD.record_sha256 IS NOT NEW.record_sha256
      OR OLD.ingested_time_ns IS NOT NEW.ingested_time_ns
      OR OLD.writer_id IS NOT NEW.writer_id
    BEGIN
        SELECT RAISE(ABORT, 'immutable prediction fields');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS predictions_status_forward_only
    BEFORE UPDATE OF status ON predictions
    WHEN OLD.status IS NOT NEW.status
      AND NOT (
          OLD.status = 'pending'
          AND NEW.status IN ('scored', 'evaluation_unavailable')
      )
    BEGIN
        SELECT RAISE(ABORT, 'prediction status cannot move backward');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS predictions_no_delete
    BEFORE DELETE ON predictions
    BEGIN
        SELECT RAISE(ABORT, 'predictions are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS price_points_no_update
    BEFORE UPDATE ON price_points
    BEGIN
        SELECT RAISE(ABORT, 'price points are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS price_points_no_delete
    BEFORE DELETE ON price_points
    BEGIN
        SELECT RAISE(ABORT, 'price points are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS outcomes_no_update
    BEFORE UPDATE ON outcomes
    BEGIN
        SELECT RAISE(ABORT, 'outcomes are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
    BEFORE DELETE ON outcomes
    BEGIN
        SELECT RAISE(ABORT, 'outcomes are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_cursors_no_delete
    BEFORE DELETE ON source_cursors
    BEGIN
        SELECT RAISE(ABORT, 'source cursors cannot be deleted');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_cursors_forward_only
    BEFORE UPDATE ON source_cursors
    WHEN OLD.source_name IS NOT NEW.source_name
      OR OLD.source_kind IS NOT NEW.source_kind
      OR OLD.source_path IS NOT NEW.source_path
      OR OLD.device_id IS NOT NEW.device_id
      OR OLD.inode IS NOT NEW.inode
      OR NEW.byte_offset < OLD.byte_offset
    BEGIN
        SELECT RAISE(ABORT, 'source cursor identity changed or moved backward');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_cursors_chunk_required
    BEFORE UPDATE OF byte_offset, last_line_start_offset, last_line_sha256
    ON source_cursors
    WHEN OLD.byte_offset IS NOT NEW.byte_offset
      AND NOT EXISTS (
          SELECT 1
          FROM source_chunks
          WHERE source_name = NEW.source_name
            AND start_offset = OLD.byte_offset
            AND end_offset = NEW.byte_offset
            AND last_line_start_offset = NEW.last_line_start_offset
            AND last_line_sha256 = NEW.last_line_sha256
            AND capture_kind = 'incremental'
      )
    BEGIN
        SELECT RAISE(ABORT, 'source cursor advance requires a matching chunk');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_contiguous
    BEFORE INSERT ON source_chunks
    WHEN NOT EXISTS (
        SELECT 1
        FROM source_cursors
        WHERE source_name = NEW.source_name
          AND (
              (NEW.capture_kind = 'bootstrap' AND byte_offset = NEW.end_offset)
              OR
              (NEW.capture_kind = 'incremental' AND byte_offset = NEW.start_offset)
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'source chunk is not contiguous with its cursor');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_no_update
    BEFORE UPDATE ON source_chunks
    BEGIN
        SELECT RAISE(ABORT, 'source chunks are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_no_delete
    BEFORE DELETE ON source_chunks
    BEGIN
        SELECT RAISE(ABORT, 'source chunks are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ingest_rejections_no_update
    BEFORE UPDATE ON ingest_rejections
    BEGIN
        SELECT RAISE(ABORT, 'ingest rejections are append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ingest_rejections_no_delete
    BEFORE DELETE ON ingest_rejections
    BEGIN
        SELECT RAISE(ABORT, 'ingest rejections are append-only');
    END;
    """,
)


def sqlite_wal_runtime_is_safe(version: tuple[int, int, int]) -> bool:
    """Return whether *version* contains the March 2026 WAL-reset fix."""

    major, minor, patch = version
    if major != 3:
        return major > 3
    if minor > 51:
        return True
    if minor == 51:
        return patch >= 3
    if minor == 50:
        return patch >= 7
    if minor == 44:
        return patch >= 6
    return False


def validate_sqlite_runtime() -> None:
    """Reject SQLite builds affected by the multi-connection WAL-reset bug."""

    version = tuple(sqlite3.sqlite_version_info[:3])
    if len(version) != 3 or not sqlite_wal_runtime_is_safe(version):
        raise UnsafeSQLiteRuntime(
            "SQLite WAL runtime is not approved: "
            f"{sqlite3.sqlite_version}. Require >=3.51.3, 3.50.7+, or 3.44.6 "
            "within the supported backport branches."
        )


class OperationalStore:
    """A connection with either writer or enforced query-only capability."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        writer_id: str | None,
    ) -> None:
        self.path = path
        self.connection = connection
        self.writer_id = writer_id

    @contextmanager
    def atomic_batch(self) -> Iterator[OperationalStore]:
        """Group bulk immutable inserts into one FULL-synchronous transaction."""

        self._require_writer()
        with _immediate_transaction(self.connection):
            yield self

    def record_audit_event(self, record: AuditEventRecord) -> bool:
        """Insert every structurally valid decision, including PIT-ineligible history."""

        writer_id = self._require_writer()
        event_id = _required(record.event_id, "event_id")
        event_type = _required(record.event_type, "event_type")
        symbol = _required(record.symbol, "symbol")
        timeframe = _required(record.timeframe, "timeframe")
        source = _required(record.source, "source")
        event_ns = _utc_ns(record.event_time, "event_time")
        prediction_ns = (
            _utc_ns(record.prediction_time, "prediction_time")
            if record.prediction_time is not None
            else None
        )
        available_ns = (
            _utc_ns(record.available_time, "available_time")
            if record.available_time is not None
            else None
        )
        ingested_ns = _utc_ns(
            record.ingested_time or datetime.now(UTC),
            "ingested_time",
        )
        if record.schema_version <= 0:
            raise InvalidOperationalRecord("schema_version must be positive")
        if record.pit_eligible:
            if prediction_ns is None or available_ns is None:
                raise InvalidOperationalRecord(
                    "PIT-eligible audit events require prediction_time and available_time"
                )
            if available_ns > prediction_ns:
                raise InvalidOperationalRecord("available_time must not exceed prediction_time")
            if record.pit_failure_reason:
                raise InvalidOperationalRecord(
                    "PIT-eligible audit events cannot carry a failure reason"
                )
        failure_reason = (
            ""
            if record.pit_eligible
            else _required(record.pit_failure_reason, "pit_failure_reason")
        )
        payload_json = _canonical_json(record.payload)
        record_hash = _record_hash(
            {
                "event_id": event_id,
                "event_type": event_type,
                "symbol": symbol,
                "timeframe": timeframe,
                "event_time_ns": event_ns,
                "prediction_time_ns": prediction_ns,
                "available_time_ns": available_ns,
                "pit_eligible": record.pit_eligible,
                "pit_failure_reason": failure_reason,
                "source": source,
                "schema_version": record.schema_version,
                "payload": json.loads(payload_json),
            }
        )
        with _immediate_transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, event_type, symbol, timeframe, event_time_ns,
                    prediction_time_ns, available_time_ns, pit_eligible,
                    pit_failure_reason, source, schema_version, payload_json,
                    record_sha256, ingested_time_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event_id,
                    event_type,
                    symbol,
                    timeframe,
                    event_ns,
                    prediction_ns,
                    available_ns,
                    int(record.pit_eligible),
                    failure_reason,
                    source,
                    record.schema_version,
                    payload_json,
                    record_hash,
                    ingested_ns,
                    writer_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            self._assert_same_hash(
                "audit_events",
                "event_id = ?",
                (event_id,),
                record_hash,
                event_id,
            )
            return False

    def record_audit_events(
        self,
        records: Iterator[AuditEventRecord] | list[AuditEventRecord],
    ) -> BatchWriteResult:
        """Insert a batch atomically while preserving per-event idempotency."""

        inserted = 0
        identical = 0
        with self.atomic_batch():
            for record in records:
                if self.record_audit_event(record):
                    inserted += 1
                else:
                    identical += 1
        return BatchWriteResult(inserted, identical)

    def record_prediction(self, record: PredictionRecord) -> bool:
        """Insert an immutable prediction; return False for an identical retry."""

        writer_id = self._require_writer()
        prediction_id = _required(record.prediction_id, "prediction_id")
        decision_id = _required(record.decision_id, "decision_id")
        symbol = _required(record.symbol, "symbol")
        timeframe = _required(record.timeframe, "timeframe")
        source = _required(record.source, "source")
        prediction_ns = _utc_ns(record.prediction_time, "prediction_time")
        available_ns = _utc_ns(record.available_time, "available_time")
        due_ns = _utc_ns(record.due_time, "due_time")
        ingested_ns = _utc_ns(
            record.ingested_time or datetime.now(UTC),
            "ingested_time",
        )
        if available_ns > prediction_ns:
            raise InvalidOperationalRecord("available_time must not exceed prediction_time")
        if prediction_ns > due_ns:
            raise InvalidOperationalRecord("due_time must not precede prediction_time")
        if available_ns > ingested_ns:
            raise InvalidOperationalRecord("available_time must not exceed ingested_time")
        if record.schema_version <= 0:
            raise InvalidOperationalRecord("schema_version must be positive")

        payload_json = _canonical_json(record.payload)
        record_hash = _record_hash(
            {
                "prediction_id": prediction_id,
                "decision_id": decision_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "prediction_time_ns": prediction_ns,
                "available_time_ns": available_ns,
                "due_time_ns": due_ns,
                "source": source,
                "schema_version": record.schema_version,
                "payload": json.loads(payload_json),
            }
        )
        with _immediate_transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO predictions(
                    prediction_id, decision_id, symbol, timeframe,
                    prediction_time_ns, available_time_ns, due_time_ns,
                    source, schema_version, payload_json, record_sha256,
                    ingested_time_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prediction_id) DO NOTHING
                """,
                (
                    prediction_id,
                    decision_id,
                    symbol,
                    timeframe,
                    prediction_ns,
                    available_ns,
                    due_ns,
                    source,
                    record.schema_version,
                    payload_json,
                    record_hash,
                    ingested_ns,
                    writer_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            self._assert_same_hash(
                "predictions",
                "prediction_id = ?",
                (prediction_id,),
                record_hash,
                prediction_id,
            )
            return False

    def record_predictions(
        self,
        records: Iterator[PredictionRecord] | list[PredictionRecord],
    ) -> BatchWriteResult:
        """Insert a prediction batch in one transaction."""

        inserted = 0
        identical = 0
        with self.atomic_batch():
            for record in records:
                if self.record_prediction(record):
                    inserted += 1
                else:
                    identical += 1
        return BatchWriteResult(inserted, identical)

    def record_price_point(self, record: PricePointRecord) -> bool:
        """Insert an immutable PIT price observation with OHLC/quote checks."""

        writer_id = self._require_writer()
        source_record_id = _required(record.source_record_id, "source_record_id")
        symbol = _required(record.symbol, "symbol")
        timeframe = _required(record.timeframe, "timeframe")
        source = _required(record.source, "source")
        revision_id = _required(record.revision_id, "revision_id")
        ohlc_scope = _required(record.ohlc_scope, "ohlc_scope")
        event_ns = _utc_ns(record.event_time, "event_time")
        available_ns = _utc_ns(record.available_time, "available_time")
        ingested_ns = _utc_ns(record.ingested_time, "ingested_time")
        if event_ns > available_ns or available_ns > ingested_ns:
            raise InvalidOperationalRecord(
                "price times must satisfy event_time <= available_time <= ingested_time"
            )
        _validate_price_values(record)
        if record.schema_version <= 0:
            raise InvalidOperationalRecord("schema_version must be positive")

        payload_json = _canonical_json(record.payload)
        envelope = {
            "source_record_id": source_record_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "event_time_ns": event_ns,
            "available_time_ns": available_ns,
            "ingested_time_ns": ingested_ns,
            "source": source,
            "revision_id": revision_id,
            "schema_version": record.schema_version,
            "ohlc_scope": ohlc_scope,
            "open": record.open,
            "high": record.high,
            "low": record.low,
            "close": record.close,
            "bid": record.bid,
            "ask": record.ask,
            "payload": json.loads(payload_json),
        }
        record_hash = _record_hash(envelope)
        try:
            with _immediate_transaction(self.connection):
                cursor = self.connection.execute(
                    """
                    INSERT INTO price_points(
                        source_record_id, symbol, timeframe, event_time_ns,
                        available_time_ns, ingested_time_ns, source, revision_id,
                        schema_version, ohlc_scope, open, high, low, close, bid, ask,
                        payload_json, record_sha256, writer_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_record_id) DO NOTHING
                    """,
                    (
                        source_record_id,
                        symbol,
                        timeframe,
                        event_ns,
                        available_ns,
                        ingested_ns,
                        source,
                        revision_id,
                        record.schema_version,
                        ohlc_scope,
                        record.open,
                        record.high,
                        record.low,
                        record.close,
                        record.bid,
                        record.ask,
                        payload_json,
                        record_hash,
                        writer_id,
                    ),
                )
                if cursor.rowcount == 1:
                    return True
                self._assert_same_hash(
                    "price_points",
                    "source_record_id = ?",
                    (source_record_id,),
                    record_hash,
                    source_record_id,
                )
                return False
        except sqlite3.IntegrityError as error:
            raise ImmutableRecordConflict(
                "price natural key already has another source record: "
                f"{symbol}/{timeframe}/{record.event_time.isoformat()}/{source}/{revision_id}"
            ) from error

    def record_price_points(
        self,
        records: Iterator[PricePointRecord] | list[PricePointRecord],
    ) -> BatchWriteResult:
        """Insert a price batch in one transaction."""

        inserted = 0
        identical = 0
        with self.atomic_batch():
            for record in records:
                if self.record_price_point(record):
                    inserted += 1
                else:
                    identical += 1
        return BatchWriteResult(inserted, identical)

    def record_outcome(self, record: OutcomeRecord) -> bool:
        """Insert an immutable outcome and close its prediction atomically."""

        writer_id = self._require_writer()
        prediction_id = _required(record.prediction_id, "prediction_id")
        label_version = _required(record.label_version, "label_version")
        source_checkpoint = _required(record.source_checkpoint, "source_checkpoint")
        if record.status not in {"scored", "evaluation_unavailable"}:
            raise InvalidOperationalRecord(f"unsupported outcome status: {record.status}")
        if record.status == "scored" and record.realized_net_r is None:
            raise InvalidOperationalRecord("scored outcomes require realized_net_r")
        if record.realized_net_r is not None and not math.isfinite(record.realized_net_r):
            raise InvalidOperationalRecord("realized_net_r must be finite")
        holding_end_ns = _utc_ns(record.holding_end_time, "holding_end_time")
        scored_ns = _utc_ns(record.scored_time, "scored_time")
        if holding_end_ns > scored_ns:
            raise InvalidOperationalRecord("scored_time must not precede holding_end_time")

        payload_json = _canonical_json(record.payload)
        record_hash = _record_hash(
            {
                "prediction_id": prediction_id,
                "label_version": label_version,
                "holding_end_time_ns": holding_end_ns,
                "scored_time_ns": scored_ns,
                "status": record.status,
                "realized_net_r": record.realized_net_r,
                "source_checkpoint": source_checkpoint,
                "payload": json.loads(payload_json),
            }
        )
        with _immediate_transaction(self.connection):
            prediction = self.connection.execute(
                "SELECT status FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
            if prediction is None:
                raise InvalidOperationalRecord(
                    f"outcome references unknown prediction: {prediction_id}"
                )
            cursor = self.connection.execute(
                """
                INSERT INTO outcomes(
                    prediction_id, label_version, holding_end_time_ns,
                    scored_time_ns, status, realized_net_r, source_checkpoint,
                    payload_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prediction_id, label_version) DO NOTHING
                """,
                (
                    prediction_id,
                    label_version,
                    holding_end_ns,
                    scored_ns,
                    record.status,
                    record.realized_net_r,
                    source_checkpoint,
                    payload_json,
                    record_hash,
                    writer_id,
                ),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                self._assert_same_hash(
                    "outcomes",
                    "prediction_id = ? AND label_version = ?",
                    (prediction_id, label_version),
                    record_hash,
                    f"{prediction_id}/{label_version}",
                )
            updated = self.connection.execute(
                """
                UPDATE predictions
                SET status = ?
                WHERE prediction_id = ? AND status = 'pending'
                """,
                (record.status, prediction_id),
            )
            if updated.rowcount == 0:
                current_prediction = self.connection.execute(
                    "SELECT status FROM predictions WHERE prediction_id = ?",
                    (prediction_id,),
                ).fetchone()
                if current_prediction is None:
                    raise InvalidOperationalRecord(f"prediction disappeared: {prediction_id}")
                if str(current_prediction["status"]) != record.status:
                    raise ImmutableRecordConflict(
                        f"prediction {prediction_id} is already {current_prediction['status']}"
                    )
            return inserted

    def due_predictions(
        self,
        *,
        as_of: datetime,
        limit: int = 1_000,
    ) -> list[dict[str, object]]:
        """Read only the pending predictions whose horizons have matured."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        as_of_ns = _utc_ns(as_of, "as_of")
        rows = self.connection.execute(
            """
            SELECT prediction_id, decision_id, symbol, timeframe,
                   prediction_time_ns, available_time_ns, due_time_ns,
                   source, schema_version, payload_json, record_sha256
            FROM predictions
            WHERE status = 'pending' AND due_time_ns <= ?
            ORDER BY due_time_ns, prediction_id
            LIMIT ?
            """,
            (as_of_ns, limit),
        ).fetchall()
        return [_decoded_row(row) for row in rows]

    def price_window(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        """Return a bounded, sorted price path for NumPy ``searchsorted``."""

        start_ns = _utc_ns(start, "start")
        end_ns = _utc_ns(end, "end")
        if end_ns < start_ns:
            raise ValueError("end must not precede start")
        rows = self.connection.execute(
            """
            SELECT source_record_id, event_time_ns, available_time_ns,
                   close, high, low, bid, ask, ohlc_scope, payload_json
            FROM price_points
            WHERE symbol = ? AND timeframe = ?
              AND event_time_ns BETWEEN ? AND ?
              AND available_time_ns <= ?
            ORDER BY event_time_ns, source_record_id
            """,
            (
                _required(symbol, "symbol"),
                _required(timeframe, "timeframe"),
                start_ns,
                end_ns,
                end_ns,
            ),
        ).fetchall()
        return [_decoded_row(row) for row in rows]

    def source_cursor(self, source_name: str) -> SourceCursor | None:
        """Return the durable byte cursor for one append-only raw source."""

        row = self.connection.execute(
            """
            SELECT source_name, source_kind, source_path, device_id, inode,
                   byte_offset, last_line_start_offset, last_line_sha256,
                   source_size_at_sync, source_mtime_ns, source_ctime_ns,
                   updated_at_ns, writer_id
            FROM source_cursors
            WHERE source_name = ?
            """,
            (_required(source_name, "source_name"),),
        ).fetchone()
        if row is None:
            return None
        return SourceCursor(
            source_name=str(row["source_name"]),
            source_kind=str(row["source_kind"]),
            source_path=str(row["source_path"]),
            device_id=int(row["device_id"]),
            inode=int(row["inode"]),
            byte_offset=int(row["byte_offset"]),
            last_line_start_offset=int(row["last_line_start_offset"]),
            last_line_sha256=str(row["last_line_sha256"]),
            source_size_at_sync=int(row["source_size_at_sync"]),
            source_mtime_ns=int(row["source_mtime_ns"]),
            source_ctime_ns=int(row["source_ctime_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            writer_id=str(row["writer_id"]),
        )

    def bootstrap_source_cursor(
        self,
        *,
        source_name: str,
        source_kind: str,
        source_path: str,
        device_id: int,
        inode: int,
        byte_offset: int,
        last_line_start_offset: int,
        last_line_sha256: str,
        source_mtime_ns: int,
        source_ctime_ns: int,
        source_sha256: str,
        row_count: int,
        captured_at: datetime | None = None,
    ) -> None:
        """Pin a parity-verified raw snapshot as the cursor's immutable origin."""

        writer_id = self._require_writer()
        name = _required(source_name, "source_name")
        kind = _source_kind(source_kind)
        path = _required(source_path, "source_path")
        _nonnegative(device_id, "device_id")
        _nonnegative(inode, "inode")
        _nonnegative(byte_offset, "byte_offset")
        _nonnegative(last_line_start_offset, "last_line_start_offset")
        _nonnegative(source_mtime_ns, "source_mtime_ns")
        _nonnegative(source_ctime_ns, "source_ctime_ns")
        _nonnegative(row_count, "row_count")
        if last_line_start_offset > byte_offset:
            raise InvalidOperationalRecord("last_line_start_offset exceeds byte_offset")
        _validate_sha256(last_line_sha256, "last_line_sha256")
        _validate_sha256(source_sha256, "source_sha256")
        captured_ns = _utc_ns(captured_at or datetime.now(UTC), "captured_at")
        with _immediate_transaction(self.connection):
            if self.source_cursor(name) is not None:
                raise InvalidOperationalRecord(f"source cursor already exists: {name}")
            self.connection.execute(
                """
                INSERT INTO source_cursors(
                    source_name, source_kind, source_path, device_id, inode,
                    byte_offset, last_line_start_offset, last_line_sha256,
                    source_size_at_sync, source_mtime_ns, source_ctime_ns,
                    updated_at_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    kind,
                    path,
                    device_id,
                    inode,
                    byte_offset,
                    last_line_start_offset,
                    last_line_sha256,
                    byte_offset,
                    source_mtime_ns,
                    source_ctime_ns,
                    captured_ns,
                    writer_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO source_chunks(
                    source_name, start_offset, end_offset, chunk_sha256,
                    last_line_start_offset, last_line_sha256, row_count,
                    capture_kind, captured_at_ns, writer_id
                ) VALUES (?, 0, ?, ?, ?, ?, ?, 'bootstrap', ?, ?)
                """,
                (
                    name,
                    byte_offset,
                    source_sha256,
                    last_line_start_offset,
                    last_line_sha256,
                    row_count,
                    captured_ns,
                    writer_id,
                ),
            )

    def advance_source_cursor(
        self,
        *,
        source_name: str,
        expected_offset: int,
        end_offset: int,
        chunk_sha256: str,
        last_line_start_offset: int,
        last_line_sha256: str,
        row_count: int,
        source_size_at_sync: int,
        source_mtime_ns: int,
        source_ctime_ns: int,
        captured_at: datetime | None = None,
    ) -> None:
        """Atomically append a chunk proof and move its cursor forward."""

        writer_id = self._require_writer()
        name = _required(source_name, "source_name")
        _nonnegative(expected_offset, "expected_offset")
        _nonnegative(end_offset, "end_offset")
        _nonnegative(last_line_start_offset, "last_line_start_offset")
        _nonnegative(row_count, "row_count")
        _nonnegative(source_size_at_sync, "source_size_at_sync")
        _nonnegative(source_mtime_ns, "source_mtime_ns")
        _nonnegative(source_ctime_ns, "source_ctime_ns")
        if end_offset <= expected_offset:
            raise InvalidOperationalRecord("end_offset must advance the source cursor")
        if last_line_start_offset < expected_offset or last_line_start_offset >= end_offset:
            raise InvalidOperationalRecord("last line must start inside the consumed chunk")
        if source_size_at_sync < end_offset:
            raise InvalidOperationalRecord("source_size_at_sync precedes end_offset")
        _validate_sha256(chunk_sha256, "chunk_sha256")
        _validate_sha256(last_line_sha256, "last_line_sha256")
        captured_ns = _utc_ns(captured_at or datetime.now(UTC), "captured_at")
        with _immediate_transaction(self.connection):
            prior = self.source_cursor(name)
            if prior is None:
                raise InvalidOperationalRecord(f"source cursor is not bootstrapped: {name}")
            if prior.byte_offset != expected_offset:
                raise InvalidOperationalRecord(
                    f"source cursor changed concurrently: {name} "
                    f"{prior.byte_offset} != {expected_offset}"
                )
            self.connection.execute(
                """
                INSERT INTO source_chunks(
                    source_name, start_offset, end_offset, chunk_sha256,
                    last_line_start_offset, last_line_sha256, row_count,
                    capture_kind, captured_at_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'incremental', ?, ?)
                """,
                (
                    name,
                    expected_offset,
                    end_offset,
                    chunk_sha256,
                    last_line_start_offset,
                    last_line_sha256,
                    row_count,
                    captured_ns,
                    writer_id,
                ),
            )
            updated = self.connection.execute(
                """
                UPDATE source_cursors
                SET byte_offset = ?,
                    last_line_start_offset = ?,
                    last_line_sha256 = ?,
                    source_size_at_sync = ?,
                    source_mtime_ns = ?,
                    source_ctime_ns = ?,
                    updated_at_ns = ?,
                    writer_id = ?
                WHERE source_name = ? AND byte_offset = ?
                """,
                (
                    end_offset,
                    last_line_start_offset,
                    last_line_sha256,
                    source_size_at_sync,
                    source_mtime_ns,
                    source_ctime_ns,
                    captured_ns,
                    writer_id,
                    name,
                    expected_offset,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidOperationalRecord(f"failed to advance source cursor: {name}")

    def record_ingest_rejection(
        self,
        *,
        source_name: str,
        line_start_offset: int,
        line_end_offset: int,
        raw_line_sha256: str,
        reason: str,
        observed_at: datetime | None = None,
    ) -> bool:
        """Record a rejected complete source line without copying its raw payload."""

        writer_id = self._require_writer()
        name = _required(source_name, "source_name")
        _nonnegative(line_start_offset, "line_start_offset")
        _nonnegative(line_end_offset, "line_end_offset")
        if line_end_offset <= line_start_offset:
            raise InvalidOperationalRecord("line_end_offset must follow line_start_offset")
        line_hash = _required(raw_line_sha256, "raw_line_sha256")
        _validate_sha256(line_hash, "raw_line_sha256")
        rejection_reason = _required(reason, "reason")
        observed_ns = _utc_ns(observed_at or datetime.now(UTC), "observed_at")
        with _immediate_transaction(self.connection):
            inserted = self.connection.execute(
                """
                INSERT INTO ingest_rejections(
                    source_name, line_start_offset, line_end_offset,
                    raw_line_sha256, reason, observed_at_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_name, line_start_offset) DO NOTHING
                """,
                (
                    name,
                    line_start_offset,
                    line_end_offset,
                    line_hash,
                    rejection_reason,
                    observed_ns,
                    writer_id,
                ),
            )
            if inserted.rowcount == 1:
                return True
            prior = self.connection.execute(
                """
                SELECT line_end_offset, raw_line_sha256, reason
                FROM ingest_rejections
                WHERE source_name = ? AND line_start_offset = ?
                """,
                (name, line_start_offset),
            ).fetchone()
            if prior is None or (
                int(prior["line_end_offset"]),
                str(prior["raw_line_sha256"]),
                str(prior["reason"]),
            ) != (line_end_offset, line_hash, rejection_reason):
                raise ImmutableRecordConflict(
                    f"ingest_rejections immutable key conflict: {name}/{line_start_offset}"
                )
            return False

    def advance_checkpoint(
        self,
        *,
        job_name: str,
        high_watermark: datetime,
        high_watermark_key: str,
        source_manifest_sha256: str,
        updated_at: datetime | None = None,
    ) -> None:
        """Advance an idempotent job checkpoint; backward movement is rejected."""

        writer_id = self._require_writer()
        name = _required(job_name, "job_name")
        key = _required(high_watermark_key, "high_watermark_key")
        manifest = _required(source_manifest_sha256, "source_manifest_sha256")
        _validate_sha256(manifest, "source_manifest_sha256")
        watermark_ns = _utc_ns(high_watermark, "high_watermark")
        updated_ns = _utc_ns(updated_at or datetime.now(UTC), "updated_at")
        with _immediate_transaction(self.connection):
            prior = self.connection.execute(
                """
                SELECT high_watermark_ns, high_watermark_key
                FROM work_checkpoints
                WHERE job_name = ?
                """,
                (name,),
            ).fetchone()
            if prior is not None:
                prior_position = (int(prior["high_watermark_ns"]), str(prior["high_watermark_key"]))
                if (watermark_ns, key) < prior_position:
                    raise InvalidOperationalRecord(
                        f"checkpoint {name} cannot move backward: "
                        f"{(watermark_ns, key)} < {prior_position}"
                    )
            self.connection.execute(
                """
                INSERT INTO work_checkpoints(
                    job_name, high_watermark_ns, high_watermark_key,
                    source_manifest_sha256, updated_at_ns, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    high_watermark_ns = excluded.high_watermark_ns,
                    high_watermark_key = excluded.high_watermark_key,
                    source_manifest_sha256 = excluded.source_manifest_sha256,
                    updated_at_ns = excluded.updated_at_ns,
                    writer_id = excluded.writer_id
                """,
                (name, watermark_ns, key, manifest, updated_ns, writer_id),
            )

    def checkpoint(self, mode: str = "PASSIVE") -> dict[str, int]:
        """Run an explicit WAL checkpoint while holding the single-writer lock."""

        self._require_writer()
        normalized = mode.upper()
        if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"unsupported checkpoint mode: {mode}")
        row = self.connection.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
        if row is None:
            raise OperationalStoreError("SQLite did not return checkpoint metrics")
        return {
            "busy": int(row[0]),
            "wal_pages": int(row[1]),
            "checkpointed_pages": int(row[2]),
        }

    def optimize(self) -> None:
        """Run SQLite's bounded statistics maintenance under writer ownership."""

        self._require_writer()
        self.connection.execute("PRAGMA optimize")

    def audit(self) -> StoreAudit:
        """Run read-only structural and integrity checks."""

        integrity_rows = self.connection.execute("PRAGMA quick_check").fetchall()
        integrity = ",".join(str(row[0]) for row in integrity_rows)
        foreign_keys = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        source_manifest_violations = _source_manifest_violations(self.connection)
        journal_mode = str(self.connection.execute("PRAGMA journal_mode").fetchone()[0])
        synchronous = int(self.connection.execute("PRAGMA synchronous").fetchone()[0])
        counts = {
            "audit_events": _count(self.connection, "audit_events"),
            "predictions": _count(self.connection, "predictions"),
            "pending_predictions": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM predictions WHERE status = 'pending'"
                ).fetchone()[0]
            ),
            "price_points": _count(self.connection, "price_points"),
            "outcomes": _count(self.connection, "outcomes"),
            "checkpoints": _count(self.connection, "work_checkpoints"),
            "source_cursors": _count(self.connection, "source_cursors"),
            "source_chunks": _count(self.connection, "source_chunks"),
            "ingest_rejections": _count(self.connection, "ingest_rejections"),
        }
        return StoreAudit(
            path=str(self.path),
            sqlite_version=sqlite3.sqlite_version,
            schema_version=int(self.connection.execute("PRAGMA user_version").fetchone()[0]),
            journal_mode=journal_mode,
            synchronous=synchronous,
            integrity_check=integrity,
            foreign_key_violations=len(foreign_keys),
            source_manifest_violations=source_manifest_violations,
            audit_events=counts["audit_events"],
            predictions=counts["predictions"],
            pending_predictions=counts["pending_predictions"],
            price_points=counts["price_points"],
            outcomes=counts["outcomes"],
            checkpoints=counts["checkpoints"],
            source_cursors=counts["source_cursors"],
            source_chunks=counts["source_chunks"],
            ingest_rejections=counts["ingest_rejections"],
            database_bytes=_size(self.path),
            wal_bytes=_size(_wal_path(self.path)),
            shm_bytes=_size(_shm_path(self.path)),
        )

    def _require_writer(self) -> str:
        if self.writer_id is None:
            raise OperationalStoreError("mutation requires open_operational_writer")
        return self.writer_id

    def _assert_same_hash(
        self,
        table: str,
        where_sql: str,
        values: tuple[object, ...],
        expected_hash: str,
        key: str,
    ) -> None:
        row = self.connection.execute(
            f"SELECT record_sha256 FROM {table} WHERE {where_sql}",
            values,
        ).fetchone()
        if row is None or str(row["record_sha256"]) != expected_hash:
            raise ImmutableRecordConflict(f"{table} immutable key conflict: {key}")


@contextmanager
def open_operational_writer(
    path: str | Path,
    *,
    writer_id: str,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[OperationalStore]:
    """Open the only mutation-capable connection for *path*."""

    validate_sqlite_runtime()
    normalized_writer_id = _required(writer_id, "writer_id")
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _writer_lock_path(target)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WriterBusy(f"operational writer is already active: {target}") from error
        connection = _connect(target, busy_timeout_ms=busy_timeout_ms)
        try:
            _initialize(connection)
            yield OperationalStore(target, connection, writer_id=normalized_writer_id)
        finally:
            connection.close()
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def open_operational_reader(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[OperationalStore]:
    """Open an existing store with SQLite query-only enforcement."""

    validate_sqlite_runtime()
    target = Path(path).resolve()
    if not target.is_file():
        raise OperationalStoreError(f"operational store does not exist: {target}")
    connection = _connect(
        target,
        busy_timeout_ms=busy_timeout_ms,
        read_only=True,
    )
    try:
        _verify_schema(connection)
        connection.execute("PRAGMA query_only=ON")
        yield OperationalStore(target, connection, writer_id=None)
    finally:
        connection.close()


def online_backup(
    source: OperationalStore,
    destination: str | Path,
) -> Path:
    """Create a consistent, create-only SQLite Online Backup API snapshot."""

    target = Path(destination).resolve()
    if target.exists():
        raise OperationalStoreError(f"backup destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise OperationalStoreError(f"backup temporary path already exists: {temporary}")
    destination_connection: sqlite3.Connection | None = None
    try:
        destination_connection = sqlite3.connect(temporary)
        source.connection.backup(destination_connection)
        check = destination_connection.execute("PRAGMA quick_check").fetchall()
        if [str(row[0]) for row in check] != ["ok"]:
            raise OperationalStoreError(f"backup quick_check failed: {check}")
        destination_connection.close()
        destination_connection = None
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return target
    except BaseException:
        if destination_connection is not None:
            destination_connection.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def file_sha256(path: str | Path) -> str:
    """Hash a completed artifact without loading it all into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(
    path: Path,
    *,
    busy_timeout_ms: int,
    read_only: bool = False,
) -> sqlite3.Connection:
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")
    database: str | Path = path
    uri = False
    if read_only:
        database = f"{path.as_uri()}?mode=ro"
        uri = True
    connection = sqlite3.connect(
        database,
        timeout=busy_timeout_ms / 1_000,
        isolation_level=None,
        uri=uri,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _initialize(connection: sqlite3.Connection) -> None:
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"database schema {current} is newer than supported schema {SCHEMA_VERSION}"
        )
    if current > 0 and application_id != APPLICATION_ID:
        raise OperationalStoreError(f"unexpected SQLite application_id: {application_id:#x}")
    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise OperationalStoreError(f"failed to enable WAL mode: {journal_mode}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(f"PRAGMA wal_autocheckpoint={DEFAULT_WAL_AUTOCHECKPOINT_PAGES}")
    with _immediate_transaction(connection):
        if 0 < current < 5 and _table_exists(connection, "source_cursors"):
            cursor_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(source_cursors)").fetchall()
            }
            if "source_ctime_ns" not in cursor_columns:
                connection.execute("""
                    ALTER TABLE source_cursors
                    ADD COLUMN source_ctime_ns INTEGER NOT NULL DEFAULT 0
                    CHECK (source_ctime_ns >= 0)
                    """)
        for statement in _SCHEMA_SQL.split(";"):
            normalized = statement.strip()
            if normalized:
                connection.execute(normalized)
        for trigger in _TRIGGER_SQL:
            connection.execute(trigger)
        if current == 0:
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        if current < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        now_ns = _utc_ns(datetime.now(UTC), "initialized_at")
        connection.execute(
            """
            INSERT INTO store_metadata(key, value, updated_at_ns)
            VALUES ('schema_contract', 'fx-operational-store-v5', ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at_ns = excluded.updated_at_ns
            WHERE store_metadata.value IS NOT excluded.value
            """,
            (now_ns,),
        )
    _verify_schema(connection)


def _verify_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"database schema {version} does not match supported schema {SCHEMA_VERSION}"
        )
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if application_id != APPLICATION_ID:
        raise OperationalStoreError(f"unexpected SQLite application_id: {application_id:#x}")
    required = {
        "store_metadata",
        "audit_events",
        "predictions",
        "price_points",
        "outcomes",
        "work_checkpoints",
        "source_cursors",
        "source_chunks",
        "ingest_rejections",
    }
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    missing = required - present
    if missing:
        raise OperationalStoreError(f"operational schema is incomplete: {sorted(missing)}")
    required_indexes = {
        "audit_events_page_idx",
        "price_points_page_idx",
        "price_points_symbol_page_idx",
    }
    present_indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'index'"
        ).fetchall()
    }
    missing_indexes = required_indexes - present_indexes
    if missing_indexes:
        raise OperationalStoreError(
            f"operational paging indexes are incomplete: {sorted(missing_indexes)}"
        )
    required_triggers = {
        "audit_events_no_update",
        "audit_events_no_delete",
        "predictions_immutable_fields",
        "predictions_status_forward_only",
        "predictions_no_delete",
        "price_points_no_update",
        "price_points_no_delete",
        "outcomes_no_update",
        "outcomes_no_delete",
        "source_cursors_no_delete",
        "source_cursors_forward_only",
        "source_cursors_chunk_required",
        "source_chunks_contiguous",
        "source_chunks_no_update",
        "source_chunks_no_delete",
        "ingest_rejections_no_update",
        "ingest_rejections_no_delete",
    }
    present_triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall()
    }
    missing_triggers = required_triggers - present_triggers
    if missing_triggers:
        raise OperationalStoreError(
            f"operational append-only triggers are incomplete: {sorted(missing_triggers)}"
        )


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _validate_price_values(record: PricePointRecord) -> None:
    values = {
        "open": record.open,
        "high": record.high,
        "low": record.low,
        "close": record.close,
        "bid": record.bid,
        "ask": record.ask,
    }
    for name, value in values.items():
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise InvalidOperationalRecord(f"{name} must be a positive number")
    if record.high is not None and record.low is not None and record.high < record.low:
        raise InvalidOperationalRecord("high must not be below low")
    if record.high is not None and record.close > record.high:
        raise InvalidOperationalRecord("close must not exceed high")
    if record.low is not None and record.close < record.low:
        raise InvalidOperationalRecord("close must not be below low")
    if record.bid is not None and record.ask is not None and record.bid > record.ask:
        raise InvalidOperationalRecord("crossed bid/ask quotes are inadmissible")


def _canonical_json(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise InvalidOperationalRecord(f"payload is not canonical JSON: {error}") from error


def _record_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _utc_ns(value: datetime, field_name: str) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidOperationalRecord(f"{field_name} must be timezone-aware")
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta: timedelta = utc_value - epoch
    return (
        delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000
    )


def _required(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise InvalidOperationalRecord(f"{field_name} is required")
    return normalized


def _source_kind(value: str) -> str:
    normalized = _required(value, "source_kind")
    if normalized not in {"decisions", "prices"}:
        raise InvalidOperationalRecord(f"unsupported source_kind: {normalized}")
    return normalized


def _nonnegative(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidOperationalRecord(f"{field_name} must be a non-negative integer")


def _validate_sha256(value: str, field_name: str) -> None:
    if len(value) != 64:
        raise InvalidOperationalRecord(f"{field_name} must be a 64-character SHA-256")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise InvalidOperationalRecord(f"{field_name} must be hexadecimal SHA-256") from error


def _decoded_row(row: sqlite3.Row) -> dict[str, object]:
    result: dict[str, object] = dict(row)
    payload = result.pop("payload_json", None)
    if isinstance(payload, str):
        result["payload"] = json.loads(payload)
    return result


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _source_manifest_violations(connection: sqlite3.Connection) -> int:
    violations = 0
    cursors = connection.execute("""
        SELECT source_name, byte_offset, last_line_start_offset, last_line_sha256
        FROM source_cursors
        ORDER BY source_name
        """).fetchall()
    cursor_names = {str(row["source_name"]) for row in cursors}
    chunk_names = {
        str(row[0])
        for row in connection.execute("SELECT DISTINCT source_name FROM source_chunks").fetchall()
    }
    violations += len(chunk_names - cursor_names)
    for cursor in cursors:
        source_name = str(cursor["source_name"])
        chunks = connection.execute(
            """
            SELECT start_offset, end_offset, last_line_start_offset,
                   last_line_sha256, capture_kind
            FROM source_chunks
            WHERE source_name = ?
            ORDER BY start_offset, end_offset
            """,
            (source_name,),
        ).fetchall()
        if not chunks:
            violations += 1
            continue
        expected_start = 0
        for index, chunk in enumerate(chunks):
            start = int(chunk["start_offset"])
            end = int(chunk["end_offset"])
            expected_kind = "bootstrap" if index == 0 else "incremental"
            if start != expected_start:
                violations += 1
            if str(chunk["capture_kind"]) != expected_kind:
                violations += 1
            if index > 0 and end <= start:
                violations += 1
            expected_start = end
        final = chunks[-1]
        if int(cursor["byte_offset"]) != int(final["end_offset"]):
            violations += 1
        if int(cursor["last_line_start_offset"]) != int(final["last_line_start_offset"]):
            violations += 1
        if str(cursor["last_line_sha256"]) != str(final["last_line_sha256"]):
            violations += 1
    violations += int(connection.execute("""
            SELECT COUNT(*)
            FROM ingest_rejections AS rejection
            WHERE NOT EXISTS (
                SELECT 1
                FROM source_chunks AS chunk
                WHERE chunk.source_name = rejection.source_name
                  AND chunk.start_offset <= rejection.line_start_offset
                  AND chunk.end_offset >= rejection.line_end_offset
            )
            """).fetchone()[0])
    return violations


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _writer_lock_path(path: Path) -> str:
    return f"{path}.writer.lock"


def _wal_path(path: Path) -> Path:
    return Path(f"{path}-wal")


def _shm_path(path: Path) -> Path:
    return Path(f"{path}-shm")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
