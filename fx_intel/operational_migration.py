"""Create-only JSONL to SQLite operational-store migration with PIT parity.

The migration never edits source JSONL.  Every input is pinned by an expected
SHA-256 before writing and checked again after streaming, so an active append
cannot produce a falsely verified candidate.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
from typing import Any

from . import decision_commit, decision_log, operational_contracts as contracts
from .operational_store import (
    AuditEventRecord,
    BatchWriteResult,
    InvalidOperationalRecord,
    OperationalStore,
    PredictionRecord,
    PricePointRecord,
    StoreAudit,
    file_sha256,
)


class OperationalMigrationError(RuntimeError):
    """The candidate cannot be claimed as a stable, parity-checked migration."""


class SourceFingerprintMismatch(OperationalMigrationError):
    """A source file does not match its pinned SHA-256."""


@dataclass(frozen=True)
class SourceFingerprint:
    path: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass
class ImportMetrics:
    source_lines: int = 0
    json_objects: int = 0
    malformed_json: int = 0
    non_object_json: int = 0
    structurally_invalid: int = 0
    inserted: int = 0
    identical_retries: int = 0
    excluded: int = 0
    exclusion_reasons: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_lines": self.source_lines,
            "json_objects": self.json_objects,
            "malformed_json": self.malformed_json,
            "non_object_json": self.non_object_json,
            "structurally_invalid": self.structurally_invalid,
            "inserted": self.inserted,
            "identical_retries": self.identical_retries,
            "excluded": self.excluded,
            "exclusion_reasons": dict(sorted(self.exclusion_reasons.items())),
        }


@dataclass
class DecisionImportMetrics(ImportMetrics):
    audit_events_inserted: int = 0
    audit_event_retries: int = 0
    pit_eligible: int = 0
    pit_ineligible: int = 0
    predictions_inserted: int = 0
    prediction_retries: int = 0

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload.update(
            {
                "audit_events_inserted": self.audit_events_inserted,
                "audit_event_retries": self.audit_event_retries,
                "pit_eligible": self.pit_eligible,
                "pit_ineligible": self.pit_ineligible,
                "predictions_inserted": self.predictions_inserted,
                "prediction_retries": self.prediction_retries,
            }
        )
        return payload


@dataclass(frozen=True)
class CandidateMigrationReport:
    schema_version: int
    run_id: str
    started_at: str
    completed_at: str
    runtime: Mapping[str, object]
    decision_source: SourceFingerprint
    decision_support_sources: tuple[SourceFingerprint, ...]
    price_source: SourceFingerprint
    decisions: DecisionImportMetrics
    prices: ImportMetrics
    database: StoreAudit
    verdict: str
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "runtime": dict(self.runtime),
            "sources": {
                "decisions": self.decision_source.to_dict(),
                "decision_support": [source.to_dict() for source in self.decision_support_sources],
                "prices": self.price_source.to_dict(),
            },
            "decisions": self.decisions.to_dict(),
            "prices": self.prices.to_dict(),
            "database": self.database.to_dict(),
            "verdict": self.verdict,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class CandidateParityReport:
    schema_version: int
    generated_at: str
    runtime: Mapping[str, object]
    decision_source: SourceFingerprint
    decision_support_sources: tuple[SourceFingerprint, ...]
    price_source: SourceFingerprint
    decision_objects: int
    decision_excluded: int
    decision_duplicate_ids: int
    audit_events_missing: int
    audit_events_unexpected: int
    audit_payload_mismatches: int
    audit_pit_mismatches: int
    predictions_expected: int
    predictions_missing: int
    predictions_unexpected: int
    prediction_payload_mismatches: int
    price_objects: int
    price_excluded: int
    price_duplicate_ids: int
    price_points_missing: int
    price_points_unexpected: int
    price_payload_mismatches: int
    exclusion_reasons: Mapping[str, int]
    database: StoreAudit
    parity_verdict: str
    data_usability: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "runtime": dict(self.runtime),
            "sources": {
                "decisions": self.decision_source.to_dict(),
                "decision_support": [source.to_dict() for source in self.decision_support_sources],
                "prices": self.price_source.to_dict(),
            },
            "decisions": {
                "source_objects": self.decision_objects,
                "excluded": self.decision_excluded,
                "duplicate_ids": self.decision_duplicate_ids,
                "audit_events_missing": self.audit_events_missing,
                "audit_events_unexpected": self.audit_events_unexpected,
                "audit_payload_mismatches": self.audit_payload_mismatches,
                "audit_pit_mismatches": self.audit_pit_mismatches,
                "predictions_expected": self.predictions_expected,
                "predictions_missing": self.predictions_missing,
                "predictions_unexpected": self.predictions_unexpected,
                "prediction_payload_mismatches": self.prediction_payload_mismatches,
            },
            "prices": {
                "source_objects": self.price_objects,
                "excluded": self.price_excluded,
                "duplicate_ids": self.price_duplicate_ids,
                "price_points_missing": self.price_points_missing,
                "price_points_unexpected": self.price_points_unexpected,
                "price_payload_mismatches": self.price_payload_mismatches,
            },
            "exclusion_reasons": dict(sorted(self.exclusion_reasons.items())),
            "database": self.database.to_dict(),
            "parity_verdict": self.parity_verdict,
            "data_usability": self.data_usability,
        }


def migrate_jsonl_candidate(
    store: OperationalStore,
    *,
    decision_path: str | Path,
    decision_sha256: str,
    price_path: str | Path,
    price_sha256: str,
    run_id: str,
    batch_size: int = 500,
    now: datetime | None = None,
) -> CandidateMigrationReport:
    """Stream pinned decision and price JSONL into a fresh candidate store."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    normalized_run_id = _required(run_id, "run_id")
    started = _aware_utc(now or datetime.now(UTC), "now")
    decision_source = fingerprint_source(decision_path, decision_sha256)
    decision_support_sources = fingerprint_decision_support_sources(decision_source.path)
    price_source = fingerprint_source(price_path, price_sha256)

    decisions = import_decision_jsonl(
        store,
        decision_source.path,
        batch_size=batch_size,
        imported_at=started,
    )
    prices = import_price_jsonl(
        store,
        price_source.path,
        batch_size=batch_size,
    )

    _assert_source_unchanged(decision_source)
    for support_source in decision_support_sources:
        _assert_source_unchanged(support_source)
    _assert_source_unchanged(price_source)
    database = store.audit()
    limitations: list[str] = []
    if decisions.pit_eligible == 0:
        limitations.append("no_pit_eligible_predictions")
    if decisions.pit_ineligible:
        limitations.append("pit_ineligible_decisions_retained_for_audit")
    if decisions.malformed_json or decisions.structurally_invalid:
        limitations.append("decision_source_contains_invalid_rows")
    if decisions.exclusion_reasons.get("superseded_decision_batch"):
        limitations.append("decision_source_contains_superseded_batches")
    if prices.malformed_json or prices.structurally_invalid or prices.excluded:
        limitations.append("price_source_contains_excluded_rows")
    if database.audit_events != decisions.audit_events_inserted:
        limitations.append("audit_event_count_drift")
    if database.predictions != decisions.predictions_inserted:
        limitations.append("prediction_count_drift")
    if database.price_points != prices.inserted:
        limitations.append("price_point_count_drift")

    if any(
        value in limitations
        for value in (
            "decision_source_contains_invalid_rows",
            "audit_event_count_drift",
            "prediction_count_drift",
            "price_point_count_drift",
        )
    ):
        verdict = "parity_failed"
    elif "no_pit_eligible_predictions" in limitations:
        verdict = "research_only"
    elif limitations:
        verdict = "parity_with_exclusions"
    else:
        verdict = "parity_verified"

    completed = datetime.now(UTC)
    return CandidateMigrationReport(
        schema_version=1,
        run_id=normalized_run_id,
        started_at=started.isoformat(),
        completed_at=completed.isoformat(),
        runtime=runtime_provenance(),
        decision_source=decision_source,
        decision_support_sources=decision_support_sources,
        price_source=price_source,
        decisions=decisions,
        prices=prices,
        database=database,
        verdict=verdict,
        limitations=tuple(limitations),
    )


def fingerprint_source(path: str | Path, expected_sha256: str) -> SourceFingerprint:
    """Verify a source's exact bytes before candidate mutation starts."""

    target = Path(path).resolve()
    if not target.is_file():
        raise OperationalMigrationError(f"migration source does not exist: {target}")
    expected = _sha256(expected_sha256, "expected_sha256")
    before = target.stat()
    actual = file_sha256(target)
    after = target.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SourceFingerprintMismatch(f"source changed while hashing: {target}")
    if actual != expected:
        raise SourceFingerprintMismatch(
            f"source SHA-256 mismatch for {target}: expected {expected}, got {actual}"
        )
    return SourceFingerprint(path=str(target), bytes=after.st_size, sha256=actual)


def fingerprint_decision_support_sources(
    decision_path: str | Path,
) -> tuple[SourceFingerprint, ...]:
    """Pin every existing canonical compact journal and every referenced one.

    Existing compact files are pinned even when the current full prefix has no
    cross-log commits.  Their verified EOFs become the incremental verifier's
    durable origins, so the first post-bootstrap commit never requires an
    unbounded scan through historical compact rows.
    """

    target = Path(decision_path).resolve()
    paths = set(decision_commit.referenced_compact_paths(target))
    for filename in decision_commit.COMPACT_FILENAMES.values():
        candidate = target.parent / filename
        if candidate.is_file():
            paths.add(candidate)
    return tuple(_pin_source(path) for path in sorted(paths))


def _pin_source(path: str | Path) -> SourceFingerprint:
    target = Path(path).resolve()
    if not target.is_file():
        raise OperationalMigrationError(
            f"referenced decision support source does not exist: {target}"
        )
    before = target.stat()
    actual = file_sha256(target)
    after = target.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SourceFingerprintMismatch(f"source changed while hashing: {target}")
    return SourceFingerprint(path=str(target), bytes=after.st_size, sha256=actual)


def runtime_provenance() -> dict[str, object]:
    """Capture exact runtime and dirty implementation identity for evidence."""

    implementation_files = (
        Path(__file__).resolve(),
        Path(__file__).with_name("operational_store.py").resolve(),
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


def verify_candidate_parity(
    store: OperationalStore,
    *,
    decision_path: str | Path,
    decision_sha256: str,
    price_path: str | Path,
    price_sha256: str,
) -> CandidateParityReport:
    """Re-read pinned JSONL and prove exact payload/classification parity."""

    decision_source = fingerprint_source(decision_path, decision_sha256)
    decision_support_sources = fingerprint_decision_support_sources(decision_source.path)
    price_source = fingerprint_source(price_path, price_sha256)
    exclusions: Counter[str] = Counter()

    decision_objects = 0
    decision_excluded = 0
    decision_duplicate_ids = 0
    audit_missing = 0
    audit_payload_mismatches = 0
    audit_pit_mismatches = 0
    predictions_expected = 0
    predictions_missing = 0
    prediction_payload_mismatches = 0
    decision_ids: set[str] = set()
    prediction_ids: set[str] = set()
    generated_at = datetime.now(UTC)

    for source_line in decision_log.iter_decision_source(decision_source.path):
        if source_line.error:
            decision_excluded += 1
            exclusions[f"decision:{source_line.error}"] += 1
            continue
        for parsed in source_line.events:
            decision_objects += 1
            try:
                audit, prediction, pit_failure = decision_records_from_event(
                    parsed,
                    imported_at=generated_at,
                )
            except (InvalidOperationalRecord, OperationalMigrationError, ValueError) as error:
                decision_excluded += 1
                exclusions[f"decision:{_error_key(error)}"] += 1
                continue
            if audit.event_id in decision_ids:
                decision_duplicate_ids += 1
            decision_ids.add(audit.event_id)
            if prediction is None:
                decision_excluded += 1
                exclusions[f"decision:{pit_failure}"] += 1
            else:
                predictions_expected += 1
                prediction_ids.add(prediction.prediction_id)

            stored_audit = store.connection.execute(
                """
                SELECT payload_json, pit_eligible
                FROM audit_events
                WHERE event_id = ?
                """,
                (audit.event_id,),
            ).fetchone()
            if stored_audit is None:
                audit_missing += 1
            else:
                if str(stored_audit["payload_json"]) != _canonical_payload(parsed):
                    audit_payload_mismatches += 1
                if bool(stored_audit["pit_eligible"]) is not audit.pit_eligible:
                    audit_pit_mismatches += 1

            if prediction is not None:
                stored_prediction = store.connection.execute(
                    "SELECT payload_json FROM predictions WHERE prediction_id = ?",
                    (prediction.prediction_id,),
                ).fetchone()
                if stored_prediction is None:
                    predictions_missing += 1
                elif str(stored_prediction["payload_json"]) != _canonical_payload(parsed):
                    prediction_payload_mismatches += 1

    price_objects = 0
    price_excluded = 0
    price_duplicate_ids = 0
    price_missing = 0
    price_payload_mismatches = 0
    price_ids: set[str] = set()
    for _line_number, parsed, parse_error in _iter_jsonl(price_source.path):
        if parse_error:
            price_excluded += 1
            exclusions[f"price:{parse_error}"] += 1
            continue
        assert parsed is not None
        price_objects += 1
        try:
            price = price_record_from_row(parsed)
        except (InvalidOperationalRecord, OperationalMigrationError, ValueError) as error:
            price_excluded += 1
            exclusions[f"price:{_error_key(error)}"] += 1
            continue
        if price.source_record_id in price_ids:
            price_duplicate_ids += 1
        price_ids.add(price.source_record_id)
        stored_price = store.connection.execute(
            "SELECT payload_json FROM price_points WHERE source_record_id = ?",
            (price.source_record_id,),
        ).fetchone()
        if stored_price is None:
            price_missing += 1
        elif str(stored_price["payload_json"]) != _canonical_payload(parsed):
            price_payload_mismatches += 1

    _assert_source_unchanged(decision_source)
    for support_source in decision_support_sources:
        _assert_source_unchanged(support_source)
    _assert_source_unchanged(price_source)
    database = store.audit()
    audit_unexpected = max(0, database.audit_events - len(decision_ids))
    predictions_unexpected = max(0, database.predictions - len(prediction_ids))
    price_unexpected = max(0, database.price_points - len(price_ids))
    drift = sum(
        (
            decision_duplicate_ids,
            audit_missing,
            audit_unexpected,
            audit_payload_mismatches,
            audit_pit_mismatches,
            predictions_missing,
            predictions_unexpected,
            prediction_payload_mismatches,
            price_duplicate_ids,
            price_missing,
            price_unexpected,
            price_payload_mismatches,
        )
    )
    parity_verdict = "parity_verified" if drift == 0 else "parity_failed"
    if predictions_expected == 0:
        data_usability = "research_only"
    elif price_excluded or decision_excluded:
        data_usability = "usable_with_exclusions"
    else:
        data_usability = "usable_candidate"
    return CandidateParityReport(
        schema_version=1,
        generated_at=generated_at.isoformat(),
        runtime=runtime_provenance(),
        decision_source=decision_source,
        decision_support_sources=decision_support_sources,
        price_source=price_source,
        decision_objects=decision_objects,
        decision_excluded=decision_excluded,
        decision_duplicate_ids=decision_duplicate_ids,
        audit_events_missing=audit_missing,
        audit_events_unexpected=audit_unexpected,
        audit_payload_mismatches=audit_payload_mismatches,
        audit_pit_mismatches=audit_pit_mismatches,
        predictions_expected=predictions_expected,
        predictions_missing=predictions_missing,
        predictions_unexpected=predictions_unexpected,
        prediction_payload_mismatches=prediction_payload_mismatches,
        price_objects=price_objects,
        price_excluded=price_excluded,
        price_duplicate_ids=price_duplicate_ids,
        price_points_missing=price_missing,
        price_points_unexpected=price_unexpected,
        price_payload_mismatches=price_payload_mismatches,
        exclusion_reasons=exclusions,
        database=database,
        parity_verdict=parity_verdict,
        data_usability=data_usability,
    )


def import_decision_jsonl(
    store: OperationalStore,
    path: str | Path,
    *,
    batch_size: int,
    imported_at: datetime,
) -> DecisionImportMetrics:
    """Retain all valid audit events and promote only PIT-eligible predictions."""

    metrics = DecisionImportMetrics()
    audit_batch: list[AuditEventRecord] = []
    prediction_batch: list[PredictionRecord] = []
    for source_line in decision_log.iter_decision_source(path):
        metrics.source_lines += 1
        if source_line.error == "malformed_json":
            metrics.malformed_json += 1
            metrics.exclusion_reasons[source_line.error] += 1
            continue
        if source_line.error == "non_object_json":
            metrics.non_object_json += 1
            metrics.exclusion_reasons[source_line.error] += 1
            continue
        if source_line.error == "superseded_decision_batch":
            metrics.excluded += 1
            metrics.exclusion_reasons[source_line.error] += 1
            continue
        if source_line.error:
            metrics.structurally_invalid += 1
            metrics.excluded += 1
            metrics.exclusion_reasons[source_line.error] += 1
            continue
        for parsed in source_line.events:
            metrics.json_objects += 1
            try:
                audit, prediction, pit_failure = decision_records_from_event(
                    parsed,
                    imported_at=imported_at,
                )
            except (InvalidOperationalRecord, OperationalMigrationError, ValueError) as error:
                metrics.structurally_invalid += 1
                metrics.exclusion_reasons[_error_key(error)] += 1
                continue
            audit_batch.append(audit)
            if prediction is None:
                metrics.pit_ineligible += 1
                metrics.excluded += 1
                metrics.exclusion_reasons[pit_failure] += 1
            else:
                metrics.pit_eligible += 1
                prediction_batch.append(prediction)
            if len(audit_batch) >= batch_size:
                audit_result, prediction_result = _flush_decision_batch(
                    store,
                    audit_batch,
                    prediction_batch,
                )
                _add_decision_results(metrics, audit_result, prediction_result)
                audit_batch.clear()
                prediction_batch.clear()
    if audit_batch:
        audit_result, prediction_result = _flush_decision_batch(
            store,
            audit_batch,
            prediction_batch,
        )
        _add_decision_results(metrics, audit_result, prediction_result)
    metrics.inserted = metrics.audit_events_inserted
    metrics.identical_retries = metrics.audit_event_retries
    return metrics


def import_price_jsonl(
    store: OperationalStore,
    path: str | Path,
    *,
    batch_size: int,
) -> ImportMetrics:
    """Import only price rows with explicit event and source identity."""

    metrics = ImportMetrics()
    batch: list[PricePointRecord] = []
    for line_number, parsed, parse_error in _iter_jsonl(path):
        metrics.source_lines += 1
        if parse_error == "malformed_json":
            metrics.malformed_json += 1
            metrics.exclusion_reasons[parse_error] += 1
            continue
        if parse_error == "non_object_json":
            metrics.non_object_json += 1
            metrics.exclusion_reasons[parse_error] += 1
            continue
        assert parsed is not None
        metrics.json_objects += 1
        try:
            batch.append(price_record_from_row(parsed))
        except (InvalidOperationalRecord, OperationalMigrationError, ValueError) as error:
            metrics.structurally_invalid += 1
            metrics.excluded += 1
            metrics.exclusion_reasons[_error_key(error)] += 1
            continue
        if len(batch) >= batch_size:
            result = store.record_price_points(batch)
            metrics.inserted += result.inserted
            metrics.identical_retries += result.identical_retries
            batch.clear()
    if batch:
        result = store.record_price_points(batch)
        metrics.inserted += result.inserted
        metrics.identical_retries += result.identical_retries
    return metrics


def decision_records_from_event(
    event: Mapping[str, object],
    *,
    imported_at: datetime,
) -> tuple[AuditEventRecord, PredictionRecord | None, str]:
    """Project one full decision event without upgrading unknown PIT metadata."""

    event_id = _required(event.get("decision_id"), "missing_decision_id")
    event_time = _parse_required_time(event.get("ts"), "invalid_event_time")
    symbol = _required(event.get("symbol"), "missing_symbol")
    timeframe = _required(event.get("timeframe"), "missing_timeframe")
    source = _required(event.get("producer") or event.get("source"), "missing_source")
    schema_version = _positive_integer(event.get("schema"), "invalid_schema_version")
    event_type = str(event.get("event_type") or "legacy_chart_decision")
    prediction_time = _parse_optional_time(event.get("prediction_time"))
    available_time = _parse_optional_time(event.get("max_feature_available_time"))
    pit_failure = decision_pit_failure_reason(event)
    pit_eligible = not pit_failure
    _ensure_canonical_payload(event)

    audit = AuditEventRecord(
        event_id=event_id,
        event_type=event_type,
        symbol=symbol,
        timeframe=timeframe,
        event_time=event_time,
        prediction_time=prediction_time,
        available_time=available_time,
        source=source,
        payload=event,
        pit_eligible=pit_eligible,
        pit_failure_reason=pit_failure,
        schema_version=schema_version,
        ingested_time=imported_at,
    )
    if not pit_eligible:
        return audit, None, pit_failure

    assert prediction_time is not None
    assert available_time is not None
    horizon = _positive_number(event.get("horizon_hours"), "invalid_horizon_hours")
    prediction = PredictionRecord(
        prediction_id=event_id,
        decision_id=event_id,
        symbol=symbol,
        timeframe=timeframe,
        prediction_time=prediction_time,
        available_time=available_time,
        due_time=add_market_open_hours(prediction_time, horizon),
        source=source,
        payload=event,
        schema_version=schema_version,
        ingested_time=event_time,
    )
    return audit, prediction, ""


def price_record_from_row(row: Mapping[str, object]) -> PricePointRecord:
    """Project one price row without substituting availability for event time."""

    source_record_id = _required(row.get("source_record_id"), "missing_source_record_id")
    event_time = _parse_required_time(row.get("event_time"), "missing_event_time")
    available_time = _parse_required_time(row.get("available_time"), "invalid_available_time")
    ingested_time = _parse_required_time(row.get("ingested_time"), "invalid_ingested_time")
    symbol = _required(row.get("symbol"), "missing_symbol")
    timeframe = _required(row.get("timeframe"), "missing_timeframe")
    source = _required(row.get("source"), "missing_source")
    ohlc_scope = _required(row.get("ohlc_scope"), "missing_ohlc_scope")
    schema_version = _positive_integer(row.get("schema_version"), "invalid_schema_version")
    _verify_row_content_hash(row)
    _ensure_canonical_payload(row)
    return PricePointRecord(
        source_record_id=source_record_id,
        symbol=symbol,
        timeframe=timeframe,
        event_time=event_time,
        available_time=available_time,
        ingested_time=ingested_time,
        source=source,
        revision_id=str(row.get("revision_id") or "initial"),
        schema_version=schema_version,
        ohlc_scope=ohlc_scope,
        open=_optional_number(row.get("open"), "invalid_open"),
        high=_optional_number(row.get("high"), "invalid_high"),
        low=_optional_number(row.get("low"), "invalid_low"),
        close=_positive_number(row.get("close"), "invalid_close"),
        bid=_optional_number(row.get("bid"), "invalid_bid"),
        ask=_optional_number(row.get("ask"), "invalid_ask"),
        payload=row,
    )


def decision_pit_failure_reason(event: Mapping[str, object]) -> str:
    """Explain the first failed clause of the journal PIT contract."""

    if event.get("pit_eligible") is not True:
        return "pit_flag_not_true"
    if event.get("pit_contract") != contracts.DECISION_JOURNAL_PIT_CONTRACT:
        return "pit_contract_mismatch"
    mode = str(event.get("mode") or "")
    expected = {
        "fusion": (contracts.FUSION_PRODUCER, contracts.FUSION_PRODUCER_VERSION),
        "per_timeframe": (
            contracts.TIMEFRAME_PRODUCER,
            contracts.TIMEFRAME_PRODUCER_VERSION,
        ),
    }.get(mode)
    if expected is None:
        return "unsupported_decision_mode"
    if (str(event.get("producer") or ""), str(event.get("producer_version") or "")) != expected:
        return "producer_identity_mismatch"
    if not str(event.get("decision_id") or "").strip():
        return "missing_decision_id"
    if not str(event.get("input_context_id") or "").strip():
        return "missing_input_context_id"
    source_ids = event.get("source_record_ids")
    if not isinstance(source_ids, list) or not any(str(value).strip() for value in source_ids):
        return "missing_source_record_ids"
    recorded = _parse_optional_time(event.get("ts"))
    prediction = _parse_optional_time(event.get("prediction_time"))
    source_cutoff = _parse_optional_time(event.get("source_cutoff"))
    available = _parse_optional_time(event.get("max_feature_available_time"))
    if None in (recorded, prediction, source_cutoff, available):
        return "invalid_pit_timestamps"
    assert recorded is not None
    assert prediction is not None
    assert source_cutoff is not None
    assert available is not None
    if recorded != prediction:
        return "recorded_prediction_time_mismatch"
    if source_cutoff > available:
        return "source_cutoff_after_availability"
    if available > prediction:
        return "future_feature_availability"
    if not contracts.is_pit_eligible_entry(event):
        return "journal_pit_contract_failed"
    return ""


def add_market_open_hours(start: datetime, hours: float) -> datetime:
    """Return the earliest wall-clock time with *hours* of FX market time elapsed."""

    normalized = _aware_utc(start, "start")
    if not math.isfinite(hours) or hours <= 0:
        raise OperationalMigrationError("invalid_horizon_hours")
    low = normalized
    high = normalized + timedelta(hours=hours + 72)
    while contracts.open_hours_between(normalized, high) < hours:
        high += timedelta(days=7)
    for _ in range(80):
        midpoint = low + (high - low) / 2
        if contracts.open_hours_between(normalized, midpoint) >= hours:
            high = midpoint
        else:
            low = midpoint
    return high


def _flush_decision_batch(
    store: OperationalStore,
    audits: list[AuditEventRecord],
    predictions: list[PredictionRecord],
) -> tuple[BatchWriteResult, BatchWriteResult]:
    with store.atomic_batch():
        audit_result = store.record_audit_events(audits)
        prediction_result = store.record_predictions(predictions)
    return audit_result, prediction_result


def _add_decision_results(
    metrics: DecisionImportMetrics,
    audits: BatchWriteResult,
    predictions: BatchWriteResult,
) -> None:
    metrics.audit_events_inserted += audits.inserted
    metrics.audit_event_retries += audits.identical_retries
    metrics.predictions_inserted += predictions.inserted
    metrics.prediction_retries += predictions.identical_retries


def _iter_jsonl(
    path: str | Path,
) -> Iterator[tuple[int, Mapping[str, object] | None, str]]:
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed: Any = json.loads(line)
            except json.JSONDecodeError:
                yield line_number, None, "malformed_json"
                continue
            if not isinstance(parsed, Mapping):
                yield line_number, None, "non_object_json"
                continue
            yield line_number, parsed, ""


def _assert_source_unchanged(source: SourceFingerprint) -> None:
    target = Path(source.path)
    current = fingerprint_source(target, source.sha256)
    if current.bytes != source.bytes:
        raise SourceFingerprintMismatch(f"source size changed during migration: {target}")


def _verify_row_content_hash(row: Mapping[str, object]) -> None:
    claimed = _sha256(_required(row.get("content_hash"), "missing_content_hash"), "content_hash")
    payload = {key: value for key, value in row.items() if key != "content_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    actual = hashlib.sha256(encoded).hexdigest()
    if claimed != actual:
        raise OperationalMigrationError("content_hash_mismatch")


def _ensure_canonical_payload(payload: Mapping[str, object]) -> None:
    try:
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise OperationalMigrationError("non_canonical_json_payload") from error


def _canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise OperationalMigrationError("non_canonical_json_payload") from error


def _parse_required_time(value: object, error_key: str) -> datetime:
    parsed = _parse_optional_time(value)
    if parsed is None:
        raise OperationalMigrationError(error_key)
    return parsed


def _parse_optional_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationalMigrationError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _positive_integer(value: object, error_key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperationalMigrationError(error_key)
    return value


def _positive_number(value: object, error_key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OperationalMigrationError(error_key)
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise OperationalMigrationError(error_key)
    return normalized


def _optional_number(value: object, error_key: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, error_key)


def _required(value: object, error_key: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OperationalMigrationError(error_key)
    return normalized


def _sha256(value: str, field_name: str) -> str:
    normalized = str(value).strip().lower()
    if len(normalized) != 64:
        raise OperationalMigrationError(f"{field_name}_must_be_sha256")
    try:
        bytes.fromhex(normalized)
    except ValueError as error:
        raise OperationalMigrationError(f"{field_name}_must_be_sha256") from error
    return normalized


def _error_key(error: BaseException) -> str:
    text = str(error).strip()
    if not text:
        return error.__class__.__name__
    return text.split(":", 1)[0].replace(" ", "_")


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()
