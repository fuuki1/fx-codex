"""Crash-safe append-only metadata ledger for immutable raw payload blobs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Literal

from data_platform.contracts.raw_event import RawEvent
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError


class RawEventLedgerError(RuntimeError):
    """Raised when durability, integrity, or idempotency cannot be proven."""


AppendStatus = Literal["stored", "idempotent"]


@dataclass(frozen=True)
class RawEventAppendResult:
    status: AppendStatus
    event: RawEvent


@dataclass(frozen=True)
class RawLedgerStats:
    total_events: int
    unique_payloads: int
    duplicate_events: int
    quarantined_events: int
    provenance_events: int
    missing_event_time: int
    last_ingest_time: datetime | None

    def to_dict(self) -> dict[str, Any]:
        total = self.total_events
        return {
            "total_events": total,
            "unique_payloads": self.unique_payloads,
            "duplicate_events": self.duplicate_events,
            "duplicate_ratio": self.duplicate_events / total if total else None,
            "quarantined_events": self.quarantined_events,
            "quarantine_ratio": self.quarantined_events / total if total else None,
            "provenance_events": self.provenance_events,
            "provenance_coverage": self.provenance_events / total if total else None,
            "missing_event_time": self.missing_event_time,
            "last_ingest_time": (
                None if self.last_ingest_time is None else self.last_ingest_time.isoformat()
            ),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_event_id TEXT,
    source_event_id_scope TEXT,
    payload_hash TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    duplicate_of TEXT REFERENCES raw_events(event_id),
    supersedes_event_id TEXT REFERENCES raw_events(event_id),
    event_time TEXT,
    ingest_time TEXT NOT NULL,
    ledger_recorded_at TEXT NOT NULL,
    provenance_present INTEGER NOT NULL CHECK (provenance_present = 1),
    input_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_events_source_identity
    ON raw_events(source, source_event_id_scope, source_event_id, sequence DESC);
CREATE TABLE IF NOT EXISTS event_annotations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL REFERENCES raw_events(event_id),
    quality_status TEXT NOT NULL CHECK (quality_status = 'quarantined'),
    reason TEXT NOT NULL,
    annotated_at TEXT NOT NULL,
    ledger_recorded_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_annotations_event
    ON event_annotations(event_id, sequence DESC);
CREATE TRIGGER IF NOT EXISTS raw_events_no_update
BEFORE UPDATE ON raw_events BEGIN SELECT RAISE(ABORT, 'raw_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS raw_events_no_delete
BEFORE DELETE ON raw_events BEGIN SELECT RAISE(ABORT, 'raw_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS raw_events_no_replace
BEFORE INSERT ON raw_events
WHEN EXISTS (
    SELECT 1 FROM raw_events
    WHERE sequence = NEW.sequence OR event_id = NEW.event_id
)
BEGIN SELECT RAISE(ABORT, 'raw_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_annotations_no_update
BEFORE UPDATE ON event_annotations BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_annotations_no_delete
BEFORE DELETE ON event_annotations BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_annotations_no_replace
BEFORE INSERT ON event_annotations
WHEN EXISTS (
    SELECT 1 FROM event_annotations
    WHERE sequence = NEW.sequence OR annotation_id = NEW.annotation_id
)
BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
"""


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RawEventLedgerError(f"{field_name} must be a non-empty string")
    return value.strip()


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RawEventLedgerError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class RawEventLedger:
    """SQLite-backed logical append log with create-only raw blobs.

    SQLite transactions make each metadata append crash-atomic.  Update/delete
    triggers protect normal application access; this is local integrity, not an
    external signature or protection from a privileged filesystem attacker.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        blob_store: ImmutableRawStore | None = None,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.root = Path(root)
        self.database_path = self.root / "events.sqlite3"
        self.blobs = blob_store or ImmutableRawStore(self.root / "blobs")
        if busy_timeout_seconds < 0:
            raise RawEventLedgerError("busy_timeout_seconds must be non-negative")
        self.busy_timeout_seconds = busy_timeout_seconds

    def append(self, event: RawEvent) -> RawEventAppendResult:
        """Store bytes first, then atomically append one metadata record."""

        if (
            event.quality_status != "received"
            or event.duplicate_of is not None
            or event.supersedes_event_id is not None
            or event.quarantine_reason is not None
            or event.canonical_event_id is not None
            or event.source_trust_score is not None
        ):
            raise RawEventLedgerError(
                "raw append accepts unannotated received events; use annotate_quarantine later"
            )
        self._prepare_writable_storage()
        ref = self.blobs.put(event.payload_bytes)
        if ref.sha256 != event.payload_hash:
            raise RawEventLedgerError("immutable blob address differs from RawEvent payload_hash")

        input_json = _canonical_json(event.metadata_dict())
        input_sha = _sha256_text(input_json)
        identity_scope = event.provenance.get("source_event_id_scope")
        if event.source_event_id is not None and identity_scope is None:
            raise RawEventLedgerError("source_event_id requires provenance source_event_id_scope")
        connection = self._connect(read_only=False)
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT input_sha256, metadata_json FROM raw_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if existing["input_sha256"] != input_sha:
                    raise RawEventLedgerError(
                        f"event_id {event.event_id!r} already exists with different evidence"
                    )
                annotated = self._apply_stored_annotations(event, existing["metadata_json"])
                self._verify_blob(event.payload_hash)
                connection.commit()
                return RawEventAppendResult(status="idempotent", event=annotated)

            annotated = self._annotate_against_head(connection, event)
            recorded_at = datetime.now(UTC).isoformat()
            metadata = annotated.metadata_dict()
            stored_record = {"ledger_recorded_at": recorded_at, **metadata}
            metadata_json = _canonical_json(metadata)
            record_sha = _sha256_text(_canonical_json(stored_record))
            connection.execute(
                """
                INSERT INTO raw_events (
                    event_id, source, source_event_id, source_event_id_scope,
                    payload_hash, quality_status,
                    duplicate_of, supersedes_event_id, event_time, ingest_time,
                    ledger_recorded_at, provenance_present, input_sha256,
                    record_sha256, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    annotated.event_id,
                    annotated.source,
                    annotated.source_event_id,
                    identity_scope,
                    annotated.payload_hash,
                    annotated.quality_status,
                    annotated.duplicate_of,
                    annotated.supersedes_event_id,
                    None if annotated.event_time is None else annotated.event_time.isoformat(),
                    annotated.ingest_time.isoformat(),
                    recorded_at,
                    input_sha,
                    record_sha,
                    metadata_json,
                ),
            )
            connection.commit()
            return RawEventAppendResult(status="stored", event=annotated)
        except sqlite3.Error as error:
            connection.rollback()
            raise RawEventLedgerError(f"raw event metadata transaction failed: {error}") from error
        finally:
            connection.close()

    def annotate_quarantine(
        self,
        event_id: str,
        *,
        annotation_id: str,
        reason: str,
        annotated_at: datetime,
        provenance: Mapping[str, str],
    ) -> AppendStatus:
        """Append a post-ingest quality result without mutating the raw event."""

        event_id = _required_text(event_id, field_name="event_id")
        annotation_id = _required_text(annotation_id, field_name="annotation_id")
        reason = _required_text(reason, field_name="reason")
        stamp = _aware_utc(annotated_at, field_name="annotated_at").isoformat()
        if not provenance:
            raise RawEventLedgerError("annotation provenance must not be empty")
        normalized = {
            _required_text(key, field_name="provenance key"): _required_text(
                value, field_name="provenance value"
            )
            for key, value in provenance.items()
        }
        record = {
            "annotation_id": annotation_id,
            "event_id": event_id,
            "quality_status": "quarantined",
            "reason": reason,
            "annotated_at": stamp,
            "provenance": normalized,
        }
        input_sha = _sha256_text(_canonical_json(record))
        self._prepare_writable_storage()
        connection = self._connect(read_only=False)
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            raw_event = connection.execute(
                "SELECT ledger_recorded_at FROM raw_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if raw_event is None:
                raise RawEventLedgerError(f"cannot annotate unknown event_id {event_id!r}")
            recorded_at = datetime.now(UTC)
            available_at = max(
                datetime.fromisoformat(str(raw_event["ledger_recorded_at"])),
                datetime.fromisoformat(stamp),
                recorded_at,
            )
            record["ledger_recorded_at"] = recorded_at.isoformat()
            record["available_at"] = available_at.isoformat()
            record_sha = _sha256_text(_canonical_json(record))
            existing = connection.execute(
                "SELECT input_sha256 FROM event_annotations WHERE annotation_id = ?",
                (annotation_id,),
            ).fetchone()
            if existing is not None:
                if existing["input_sha256"] != input_sha:
                    raise RawEventLedgerError(
                        f"annotation_id {annotation_id!r} conflicts with existing evidence"
                    )
                connection.commit()
                return "idempotent"
            connection.execute(
                """
                INSERT INTO event_annotations (
                    annotation_id, event_id, quality_status, reason, annotated_at,
                    ledger_recorded_at, available_at, provenance_json, input_sha256,
                    record_sha256
                ) VALUES (?, ?, 'quarantined', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation_id,
                    event_id,
                    reason,
                    stamp,
                    recorded_at.isoformat(),
                    available_at.isoformat(),
                    _canonical_json(normalized),
                    input_sha,
                    record_sha,
                ),
            )
            connection.commit()
            return "stored"
        except sqlite3.Error as error:
            connection.rollback()
            raise RawEventLedgerError(
                f"raw event annotation transaction failed: {error}"
            ) from error
        finally:
            connection.close()

    def _annotate_against_head(self, connection: sqlite3.Connection, event: RawEvent) -> RawEvent:
        if event.source_event_id is None:
            return event
        head = connection.execute(
            """
            SELECT event_id, payload_hash, duplicate_of
            FROM raw_events
            WHERE source = ? AND source_event_id = ? AND source_event_id_scope = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (
                event.source,
                event.source_event_id,
                event.provenance["source_event_id_scope"],
            ),
        ).fetchone()
        if head is None:
            return event
        head_id = str(head["duplicate_of"] or head["event_id"])
        if head["payload_hash"] == event.payload_hash:
            return replace(event, quality_status="duplicate", duplicate_of=head_id)
        return replace(event, supersedes_event_id=head_id)

    @staticmethod
    def _apply_stored_annotations(event: RawEvent, metadata_json: str) -> RawEvent:
        metadata = json.loads(metadata_json)
        return replace(
            event,
            quality_status=str(metadata["quality_status"]),
            duplicate_of=metadata.get("duplicate_of"),
            supersedes_event_id=metadata.get("supersedes_event_id"),
        )

    def stats(self) -> RawLedgerStats:
        if not self.database_path.is_file():
            return RawLedgerStats(0, 0, 0, 0, 0, 0, None)
        self._validate_existing_storage()
        connection = self._connect(read_only=True)
        try:
            connection.execute("BEGIN")
            stats = self._stats_from_connection(connection)
        except sqlite3.Error as error:
            raise RawEventLedgerError(f"cannot read raw event stats: {error}") from error
        finally:
            connection.close()
        return stats

    @staticmethod
    def _stats_from_connection(connection: sqlite3.Connection) -> RawLedgerStats:
        row = connection.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT payload_hash) AS unique_payloads,
                   SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
                   SUM(provenance_present) AS provenance_events,
                   SUM(CASE WHEN event_time IS NULL THEN 1 ELSE 0 END) AS missing_event_time,
                   MAX(ingest_time) AS last_ingest_time
            FROM raw_events
            """).fetchone()
        quarantined = connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM event_annotations"
        ).fetchone()[0]
        last = row["last_ingest_time"]
        return RawLedgerStats(
            total_events=int(row["total"]),
            unique_payloads=int(row["unique_payloads"]),
            duplicate_events=int(row["duplicates"] or 0),
            quarantined_events=int(quarantined),
            provenance_events=int(row["provenance_events"] or 0),
            missing_event_time=int(row["missing_event_time"] or 0),
            last_ingest_time=None if last is None else datetime.fromisoformat(str(last)),
        )

    def health_snapshot(
        self,
        *,
        now: datetime | None = None,
        stale_after_seconds: float | None = None,
        max_duplicate_ratio: float | None = None,
        max_quarantine_ratio: float | None = None,
        verify_blobs: bool = True,
        required_symbols: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Return fail-closed health; missing thresholds are ``unknown``, never ``ok``."""

        observed_at = _aware_utc(now or datetime.now(UTC), field_name="now")
        for name, threshold in (
            ("max_duplicate_ratio", max_duplicate_ratio),
            ("max_quarantine_ratio", max_quarantine_ratio),
        ):
            if threshold is not None and not 0.0 <= threshold <= 1.0:
                raise RawEventLedgerError(f"{name} must be between 0 and 1")
        if stale_after_seconds is not None and stale_after_seconds < 0:
            raise RawEventLedgerError("stale_after_seconds must be non-negative")

        stats_error: str | None = None
        connection: sqlite3.Connection | None = None
        database_present = self.database_path.is_file()
        stats = RawLedgerStats(0, 0, 0, 0, 0, 0, None)
        if database_present:
            try:
                self._validate_existing_storage()
                connection = self._connect(read_only=True)
                connection.execute("BEGIN")
                stats = self._stats_from_connection(connection)
            except (RawEventLedgerError, sqlite3.Error) as error:
                if connection is not None:
                    connection.close()
                    connection = None
                stats_error = type(error).__name__

        failures: list[str] = []
        unknowns: list[str] = []
        if stats_error is not None:
            failures.append("metadata_stats_unavailable")
        age_seconds: float | None = None
        if stats.last_ingest_time is None:
            failures.append("no_events")
        else:
            age_seconds = (observed_at - stats.last_ingest_time).total_seconds()
            if age_seconds < 0:
                failures.append("future_ingest_time")
            if stale_after_seconds is None:
                unknowns.append("staleness_threshold_unset")
            elif age_seconds > stale_after_seconds:
                failures.append("stale")
        if max_duplicate_ratio is None:
            unknowns.append("duplicate_threshold_unset")
        elif (
            stats.total_events and stats.duplicate_events / stats.total_events > max_duplicate_ratio
        ):
            failures.append("duplicate_ratio_exceeded")
        if max_quarantine_ratio is None:
            unknowns.append("quarantine_threshold_unset")
        elif (
            stats.total_events
            and stats.quarantined_events / stats.total_events > max_quarantine_ratio
        ):
            failures.append("quarantine_ratio_exceeded")

        blob_errors = 0
        metadata_errors = 0
        symbol_counts: dict[str, int] = {}
        symbol_latest_ingest: dict[str, datetime] = {}
        normalized_required_symbols = tuple(
            sorted(
                {
                    _required_text(symbol, field_name="required symbol")
                    .upper()
                    .replace("_", "")
                    .replace("/", "")
                    for symbol in required_symbols
                }
            )
        )
        sqlite_integrity = "not_checked"
        if database_present:
            if connection is None:
                return {
                    "status": "degraded",
                    "observed_at": observed_at.isoformat(),
                    "age_seconds": age_seconds,
                    "reasons": [*failures, "storage_integrity_failed", *unknowns],
                    "blob_integrity_errors": 0,
                    "metadata_integrity_errors": 0,
                    "sqlite_integrity": "not_checked",
                    "stats_error_type": stats_error,
                    "symbol_counts": symbol_counts,
                    "symbol_last_ingest_times": {},
                    "symbol_age_seconds": {},
                    "missing_required_symbols": list(normalized_required_symbols),
                    "stale_required_symbols": [],
                    **stats.to_dict(),
                }
            try:
                sqlite_integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                expected_triggers = {
                    "raw_events_no_update",
                    "raw_events_no_delete",
                    "raw_events_no_replace",
                    "event_annotations_no_update",
                    "event_annotations_no_delete",
                    "event_annotations_no_replace",
                }
                observed_triggers = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }
                if not expected_triggers <= observed_triggers:
                    failures.append("append_only_guards_missing")
                if list(connection.execute("PRAGMA foreign_key_check")):
                    failures.append("foreign_key_integrity_failed")
                hashes = [
                    str(row[0])
                    for row in connection.execute("SELECT DISTINCT payload_hash FROM raw_events")
                ]
                for row in connection.execute("""
                    SELECT event_id, source, source_event_id, source_event_id_scope,
                           payload_hash, quality_status,
                           duplicate_of, supersedes_event_id, event_time, ingest_time,
                           metadata_json, ledger_recorded_at, input_sha256, record_sha256
                    FROM raw_events
                    """):
                    try:
                        metadata = json.loads(str(row["metadata_json"]))
                        symbol = metadata.get("symbol")
                        if isinstance(symbol, str) and symbol:
                            normalized_symbol = symbol.upper().replace("_", "").replace("/", "")
                            symbol_counts[normalized_symbol] = (
                                symbol_counts.get(normalized_symbol, 0) + 1
                            )
                            symbol_ingest = datetime.fromisoformat(str(metadata.get("ingest_time")))
                            if symbol_ingest.tzinfo is None or symbol_ingest.utcoffset() is None:
                                raise ValueError("symbol ingest_time must be timezone-aware")
                            symbol_ingest = symbol_ingest.astimezone(UTC)
                            current_latest = symbol_latest_ingest.get(normalized_symbol)
                            if current_latest is None or symbol_ingest > current_latest:
                                symbol_latest_ingest[normalized_symbol] = symbol_ingest
                        stored = {"ledger_recorded_at": row["ledger_recorded_at"], **metadata}
                        original_input = {
                            **metadata,
                            "canonical_event_id": None,
                            "duplicate_of": None,
                            "quality_status": "received",
                            "quarantine_reason": None,
                            "source_trust_score": None,
                            "supersedes_event_id": None,
                        }
                        indexed_fields = {
                            "event_id": row["event_id"],
                            "source": row["source"],
                            "source_event_id": row["source_event_id"],
                            "payload_hash": row["payload_hash"],
                            "quality_status": row["quality_status"],
                            "duplicate_of": row["duplicate_of"],
                            "supersedes_event_id": row["supersedes_event_id"],
                            "event_time": row["event_time"],
                            "ingest_time": row["ingest_time"],
                        }
                        index_matches = all(
                            metadata.get(key) == value for key, value in indexed_fields.items()
                        )
                        index_matches = index_matches and (
                            metadata.get("provenance", {}).get("source_event_id_scope")
                            == row["source_event_id_scope"]
                        )
                        if (
                            not index_matches
                            or _sha256_text(_canonical_json(original_input)) != row["input_sha256"]
                            or _sha256_text(_canonical_json(stored)) != row["record_sha256"]
                        ):
                            metadata_errors += 1
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata_errors += 1
                for row in connection.execute("""
                    SELECT annotation_id, event_id, quality_status, reason, annotated_at,
                           ledger_recorded_at, available_at, provenance_json,
                           input_sha256, record_sha256
                    FROM event_annotations
                    """):
                    try:
                        annotation = {
                            "annotation_id": row["annotation_id"],
                            "event_id": row["event_id"],
                            "quality_status": row["quality_status"],
                            "reason": row["reason"],
                            "annotated_at": row["annotated_at"],
                            "ledger_recorded_at": row["ledger_recorded_at"],
                            "available_at": row["available_at"],
                            "provenance": json.loads(str(row["provenance_json"])),
                        }
                        annotation_input = {
                            key: value
                            for key, value in annotation.items()
                            if key not in {"ledger_recorded_at", "available_at"}
                        }
                        if (
                            _sha256_text(_canonical_json(annotation_input)) != row["input_sha256"]
                            or _sha256_text(_canonical_json(annotation)) != row["record_sha256"]
                        ):
                            metadata_errors += 1
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata_errors += 1
            except sqlite3.Error as error:
                failures.append("sqlite_integrity_unavailable")
                hashes = []
                sqlite_integrity = f"error:{type(error).__name__}"
            finally:
                connection.close()
            if sqlite_integrity != "ok":
                failures.append("sqlite_integrity_failed")
            if metadata_errors:
                failures.append("metadata_record_integrity_failed")
            if verify_blobs:
                for payload_hash in hashes:
                    try:
                        self.blobs.get(payload_hash)
                    except (OSError, RawStoreError, ValueError):
                        blob_errors += 1
                if blob_errors:
                    failures.append("raw_blob_integrity_failed")
            else:
                unknowns.append("raw_blob_integrity_not_checked")

        missing_required_symbols = [
            symbol for symbol in normalized_required_symbols if symbol_counts.get(symbol, 0) == 0
        ]
        if missing_required_symbols:
            failures.append("required_symbol_coverage_missing")
        symbol_age_seconds = {
            symbol: (observed_at - ingested_at).total_seconds()
            for symbol, ingested_at in sorted(symbol_latest_ingest.items())
        }
        future_required_symbols = [
            symbol
            for symbol in normalized_required_symbols
            if symbol_age_seconds.get(symbol, 0) < 0
        ]
        stale_required_symbols = (
            []
            if stale_after_seconds is None
            else [
                symbol
                for symbol in normalized_required_symbols
                if symbol in symbol_age_seconds and symbol_age_seconds[symbol] > stale_after_seconds
            ]
        )
        if future_required_symbols:
            failures.append("future_required_symbol_ingest_time")
        if stale_required_symbols:
            failures.append("required_symbol_stale")

        status = "degraded" if failures else "unknown" if unknowns else "ok"
        return {
            "status": status,
            "observed_at": observed_at.isoformat(),
            "age_seconds": age_seconds,
            "reasons": [*failures, *unknowns],
            "blob_integrity_errors": blob_errors,
            "metadata_integrity_errors": metadata_errors,
            "sqlite_integrity": sqlite_integrity,
            "stats_error_type": stats_error,
            "symbol_counts": symbol_counts,
            "symbol_last_ingest_times": {
                symbol: ingested_at.isoformat()
                for symbol, ingested_at in sorted(symbol_latest_ingest.items())
            },
            "symbol_age_seconds": symbol_age_seconds,
            "missing_required_symbols": missing_required_symbols,
            "stale_required_symbols": stale_required_symbols,
            **stats.to_dict(),
        }

    def _verify_blob(self, payload_hash: str) -> None:
        try:
            self.blobs.get(payload_hash)
        except (OSError, RawStoreError, ValueError) as error:
            raise RawEventLedgerError(
                f"event metadata exists but payload {payload_hash} is unavailable"
            ) from error

    def _prepare_writable_storage(self) -> None:
        self._reject_symlink_ancestors()
        if self.root.exists():
            root_stat = self.root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise RawEventLedgerError("raw ledger root must be a real directory")
            if stat.S_IMODE(root_stat.st_mode) & 0o077:
                raise RawEventLedgerError("raw ledger root permissions must not allow group/other")
        else:
            self.root.mkdir(mode=0o700, parents=True)
        if self.database_path.exists() or self.database_path.is_symlink():
            db_stat = self.database_path.lstat()
            if stat.S_ISLNK(db_stat.st_mode) or not stat.S_ISREG(db_stat.st_mode):
                raise RawEventLedgerError("raw ledger database must be a regular file")
            if stat.S_IMODE(db_stat.st_mode) & 0o077:
                raise RawEventLedgerError("raw ledger database permissions are too broad")
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.database_path, flags, 0o600)
            os.close(descriptor)

    def _validate_existing_storage(self) -> None:
        self._reject_symlink_ancestors()
        root_stat = self.root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise RawEventLedgerError("raw ledger root must be a real directory")
        if stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise RawEventLedgerError("raw ledger root permissions must not allow group/other")
        db_stat = self.database_path.lstat()
        if stat.S_ISLNK(db_stat.st_mode) or not stat.S_ISREG(db_stat.st_mode):
            raise RawEventLedgerError("raw ledger database must be a regular file")
        if stat.S_IMODE(db_stat.st_mode) & 0o077:
            raise RawEventLedgerError("raw ledger database permissions are too broad")

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        try:
            if read_only:
                connection = sqlite3.connect(
                    f"file:{self.database_path}?mode=ro",
                    uri=True,
                    timeout=self.busy_timeout_seconds,
                )
            else:
                connection = sqlite3.connect(
                    self.database_path,
                    timeout=self.busy_timeout_seconds,
                    isolation_level=None,
                )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
            if not read_only:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as error:
            raise RawEventLedgerError(f"cannot open raw event ledger: {error}") from error

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA)

    def _reject_symlink_ancestors(self) -> None:
        candidate = self.root.absolute()
        nearest_existing: Path | None = None
        for path in (candidate, *candidate.parents):
            if path.is_symlink():
                raise RawEventLedgerError(f"raw ledger path contains symlink ancestor: {path}")
            if path.exists() and nearest_existing is None:
                nearest_existing = path
        if nearest_existing is None:
            raise RawEventLedgerError("raw ledger path has no existing directory ancestor")
        if not nearest_existing.is_dir():
            raise RawEventLedgerError(
                f"raw ledger nearest existing ancestor is not a directory: {nearest_existing}"
            )
        path_stat = nearest_existing.stat()
        if path_stat.st_uid != os.getuid() or stat.S_IMODE(path_stat.st_mode) & 0o077:
            raise RawEventLedgerError(
                "raw ledger nearest existing ancestor must be owner-only and user-owned"
            )
