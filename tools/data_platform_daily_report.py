#!/usr/bin/env python3
"""Build one fail-closed prospective data-platform daily report.

The report reads the canonical capture journal (or legacy batch logs), verifies raw bytes, and
binds same-day primary-health, independent-secondary and deterministic-replay
evidence by SHA-256. Missing, stale or contradictory evidence is recorded as a
failure; it is never inferred. Reports outside the prospective generation
window are non-qualifying and cannot be used to backfill historical days.

Exit codes:
- 0: report written and the day qualifies
- 2: report written but one or more qualifying conditions failed
- 1: report could not be computed because an input was malformed
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import sys
from typing import Any

from data_platform.collect.capture_journal import (
    CaptureJournal,
    CaptureJournalError,
    JournalState,
)
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError

REQUIRED_PAIRS = frozenset({"USDJPY", "EURUSD", "GBPUSD", "AUDUSD"})
MAX_REPORT_AGE_DAYS = 4
FRESHNESS_SAMPLE_MAX = 100_000
MAX_FUTURE_SKEW_SECONDS = 5.0


class DailyReportError(RuntimeError):
    """The daily-report inputs are malformed or internally inconsistent."""


def _load_json_object(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise DailyReportError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DailyReportError(f"JSON evidence must be an object: {path}")
    return payload, hashlib.sha256(raw).hexdigest()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise DailyReportError(
                    f"malformed JSONL at {path}:{line_number}: {error.msg}"
                ) from error
            if not isinstance(payload, dict):
                raise DailyReportError(f"JSONL row must be an object at {path}:{line_number}")
            yield payload


def _parse_timestamp(value: object, field_name: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise DailyReportError(f"invalid {field_name}: {value!r}") from error
    if stamp.tzinfo is None:
        raise DailyReportError(f"{field_name} must be timezone-aware")
    return stamp.astimezone(UTC)


def _matches_day(row: dict[str, Any], day: date) -> bool:
    value = row.get("received_at") or row.get("occurred_at")
    if value is None:
        return False
    return _parse_timestamp(value, "row timestamp").date() == day


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _same_day_evidence(
    payload: dict[str, Any] | None,
    *,
    day: date,
    boolean_field: str,
    evidence_role: str,
    generated_at: datetime,
) -> tuple[bool, str | None]:
    if (
        payload is None
        or payload.get("report_date") != day.isoformat()
        or payload.get("evidence_role") != evidence_role
        or payload.get(boolean_field) is not True
    ):
        return False, None
    source_id = payload.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        return False, None
    observed = payload.get("observed_at")
    if observed is None:
        return False, None
    observed_at = _parse_timestamp(observed, f"{evidence_role}.observed_at")
    if observed_at.date() != day:
        return False, None
    if observed_at > generated_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        return False, None
    return True, source_id.strip()


def _evidence_is_distinct(
    *,
    paths: tuple[Path | None, ...],
    digests: tuple[str | None, ...],
    source_ids: tuple[str | None, ...],
) -> bool:
    if any(path is None for path in paths):
        return False
    resolved_paths = {str(path.resolve()) for path in paths if path is not None}
    present_digests = {digest for digest in digests if digest is not None}
    present_source_ids = {source_id for source_id in source_ids if source_id is not None}
    return (
        len(resolved_paths) == len(paths)
        and len(present_digests) == len(digests)
        and len(present_source_ids) == len(source_ids)
    )


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def build_daily_report(
    *,
    collection_root: Path,
    day: date,
    primary_evidence: Path | None,
    secondary_evidence: Path | None,
    replay_evidence: Path | None,
) -> dict[str, Any]:
    if not collection_root.is_dir():
        raise DailyReportError(f"collection root not found: {collection_root}")

    generated_at = datetime.now(UTC)
    age_days = (generated_at.date() - day).days
    prospective_window_ok = 0 <= age_days <= MAX_REPORT_AGE_DAYS
    checked_hashes: set[str] = set()
    raw_errors: list[str] = []
    pair_counts: Counter[str] = Counter()
    observed_primary_pairs: set[str] = set()
    freshness_sample: list[float] = []
    freshness_seen = 0
    freshness_max: float | None = None
    freshness_rng = random.Random(0)
    flag_counts: Counter[str] = Counter()
    quote_count = 0
    journal_raw_count = 0

    journal_root = collection_root / "log" / "capture_journal"
    journal_present = journal_root.is_dir() and any(journal_root.glob("capture-????-??-??.sqlite3"))
    journal = CaptureJournal(journal_root, verify=False) if journal_present else None
    journal_verified = False

    def consume_quote_rows(
        rows: Iterable[dict[str, Any]],
        *,
        legacy_raw_store: ImmutableRawStore | None,
    ) -> None:
        nonlocal freshness_max, freshness_seen, quote_count
        for row in rows:
            if not _matches_day(row, day):
                continue
            quote_count += 1
            instrument = str(row.get("instrument", ""))
            pair_counts[instrument] += 1
            is_primary = (
                row.get("provider") == "oanda"
                and row.get("collection_mode") == "live_stream"
                and row.get("account_environment") == "live"
                and row.get("source_endpoint_class") == "streaming_pricing"
                and row.get("quality_state") == "usable"
            )

            raw_hash = str(row.get("raw_payload_sha256", ""))
            if legacy_raw_store is not None and raw_hash not in checked_hashes:
                checked_hashes.add(raw_hash)
                try:
                    legacy_raw_store.get(raw_hash)
                except (OSError, RawStoreError, ValueError) as error:
                    raw_errors.append(f"{raw_hash[:12]}: {type(error).__name__}")

            event = row.get("provider_event_time")
            received = row.get("received_at")
            timestamps_valid = True
            received_at: datetime | None = None
            event_at: datetime | None = None
            if received is None:
                if "received_timestamp_missing" not in raw_errors:
                    raw_errors.append("received_timestamp_missing")
                timestamps_valid = False
            else:
                received_at = _parse_timestamp(received, "received_at")
                future_limit = generated_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
                if received_at > future_limit:
                    if "future_quote_timestamp" not in raw_errors:
                        raw_errors.append("future_quote_timestamp")
                    timestamps_valid = False
            if event is None:
                if is_primary and "primary_event_timestamp_missing" not in raw_errors:
                    raw_errors.append("primary_event_timestamp_missing")
                timestamps_valid = False
            else:
                event_at = _parse_timestamp(event, "provider_event_time")
                future_limit = generated_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
                if event_at > future_limit:
                    if "future_quote_timestamp" not in raw_errors:
                        raw_errors.append("future_quote_timestamp")
                    timestamps_valid = False
            if received_at is not None and event_at is not None:
                lag = (received_at - event_at).total_seconds()
                if lag < -MAX_FUTURE_SKEW_SECONDS:
                    if "provider_event_after_receipt" not in raw_errors:
                        raw_errors.append("provider_event_after_receipt")
                    timestamps_valid = False
                if timestamps_valid:
                    observed_lag = max(0.0, lag)
                    freshness_seen += 1
                    freshness_max = (
                        observed_lag if freshness_max is None else max(freshness_max, observed_lag)
                    )
                    if len(freshness_sample) < FRESHNESS_SAMPLE_MAX:
                        freshness_sample.append(observed_lag)
                    else:
                        replacement = freshness_rng.randrange(freshness_seen)
                        if replacement < FRESHNESS_SAMPLE_MAX:
                            freshness_sample[replacement] = observed_lag
            if is_primary and timestamps_valid:
                observed_primary_pairs.add(instrument)
            flags = row.get("quality_flags", [])
            if isinstance(flags, list):
                flag_counts.update(str(flag) for flag in flags)

    if journal_present:
        assert journal is not None
        try:
            consume_quote_rows(
                (record.quote.to_dict() for record in journal.iter_verified_quotes()),
                legacy_raw_store=None,
            )
            journal_verified = True
        except CaptureJournalError as error:
            raw_errors.append(f"capture_journal: {type(error).__name__}: {error}")
    else:
        consume_quote_rows(
            _iter_jsonl(collection_root / "log" / "quotes.jsonl"),
            legacy_raw_store=ImmutableRawStore(collection_root / "raw"),
        )

    quarantine_count = 0
    if journal_verified:
        assert journal is not None
        for entry in journal.iter_entries():
            if entry.occurred_at.date() != day:
                continue
            if entry.state is JournalState.RAW_DURABLE:
                journal_raw_count += 1
                continue
            quarantine_count += int(entry.metadata.get("quarantined_count", 0))
            journal_flags = entry.metadata.get("quality_flag_counts", {})
            if isinstance(journal_flags, dict):
                for flag, count in journal_flags.items():
                    if isinstance(count, int) and count > 0:
                        flag_counts[str(flag)] += count
            reason = entry.metadata.get("reason")
            if reason:
                flag_counts.update(str(reason).split(","))
    elif not journal_present:
        for row in _iter_jsonl(collection_root / "log" / "quarantine.jsonl"):
            if not _matches_day(row, day):
                continue
            quarantine_count += 1
            flags = row.get("quality_flags", [])
            if isinstance(flags, list):
                flag_counts.update(str(flag) for flag in flags)
            reason = row.get("reason")
            if reason:
                flag_counts.update(str(reason).split(","))

    incident_rows: list[dict[str, Any]] = []
    incidents_dir = collection_root / "state" / "incidents"
    if incidents_dir.is_dir():
        for path in sorted(incidents_dir.glob("*.json")):
            payload, _digest = _load_json_object(path)
            if payload is not None and _matches_day(payload, day):
                incident_rows.append(payload)
    critical_incidents = sum(
        1 for incident in incident_rows if incident.get("severity") == "critical"
    )

    primary_payload, primary_sha = _load_json_object(primary_evidence)
    secondary_payload, secondary_sha = _load_json_object(secondary_evidence)
    replay_payload, replay_sha = _load_json_object(replay_evidence)
    primary_health_ok, primary_source_id = _same_day_evidence(
        primary_payload,
        day=day,
        boolean_field="primary_up",
        evidence_role="primary_health",
        generated_at=generated_at,
    )
    primary_pair_coverage_ok = REQUIRED_PAIRS.issubset(observed_primary_pairs)
    primary_up = primary_health_ok and primary_pair_coverage_ok
    secondary_up, secondary_source_id = _same_day_evidence(
        secondary_payload,
        day=day,
        boolean_field="secondary_up",
        evidence_role="independent_secondary",
        generated_at=generated_at,
    )
    replay_ok, replay_source_id = _same_day_evidence(
        replay_payload,
        day=day,
        boolean_field="replay_ok",
        evidence_role="deterministic_replay",
        generated_at=generated_at,
    )
    supporting_evidence_contract_ok = primary_health_ok and secondary_up and replay_ok
    supporting_evidence_distinct = _evidence_is_distinct(
        paths=(primary_evidence, secondary_evidence, replay_evidence),
        digests=(primary_sha, secondary_sha, replay_sha),
        source_ids=(primary_source_id, secondary_source_id, replay_source_id),
    )
    raw_hash_verified = quote_count > 0 and not raw_errors
    qualifying_day = (
        prospective_window_ok
        and raw_hash_verified
        and replay_ok
        and critical_incidents == 0
        and primary_up
        and secondary_up
        and supporting_evidence_contract_ok
        and supporting_evidence_distinct
    )

    disk = shutil.disk_usage(collection_root)
    return {
        "schema_version": 1,
        "report_date": day.isoformat(),
        "generated_at": generated_at.isoformat(),
        "prospective_window_ok": prospective_window_ok,
        "report_age_days": age_days,
        "qualifying_day": qualifying_day,
        "raw_hash_verified": raw_hash_verified,
        "replay_ok": replay_ok,
        "critical_incidents": critical_incidents,
        "primary_up": primary_up,
        "primary_health_ok": primary_health_ok,
        "primary_pair_coverage_ok": primary_pair_coverage_ok,
        "secondary_up": secondary_up,
        "supporting_evidence_contract_ok": supporting_evidence_contract_ok,
        "supporting_evidence_distinct": supporting_evidence_distinct,
        "required_pairs": sorted(REQUIRED_PAIRS),
        "observed_primary_pairs": sorted(observed_primary_pairs),
        "quote_count": quote_count,
        "quote_count_by_pair": dict(sorted(pair_counts.items())),
        "quarantine_count": quarantine_count,
        "quality_flag_counts": dict(sorted(flag_counts.items())),
        "freshness_seconds": {
            "p50": _percentile(freshness_sample, 0.50),
            "p95": _percentile(freshness_sample, 0.95),
            "p99": _percentile(freshness_sample, 0.99),
            "max": freshness_max,
            "population_count": freshness_seen,
            "sample_count": len(freshness_sample),
            "sample_method": "deterministic_reservoir_v1",
        },
        "raw_blob_count_verified": (
            journal_raw_count if journal_verified else max(0, len(checked_hashes) - len(raw_errors))
        ),
        "raw_verification_errors": raw_errors,
        "incident_types": [str(incident.get("type", "unknown")) for incident in incident_rows],
        "disk_free_bytes": disk.free,
        "supporting_evidence": {
            "primary": {
                "path": str(primary_evidence) if primary_evidence else None,
                "sha256": primary_sha,
                "source_id": primary_source_id,
            },
            "secondary": {
                "path": str(secondary_evidence) if secondary_evidence else None,
                "sha256": secondary_sha,
                "source_id": secondary_source_id,
            },
            "replay": {
                "path": str(replay_evidence) if replay_evidence else None,
                "sha256": replay_sha,
                "source_id": replay_source_id,
            },
        },
        "unmet_conditions": [
            name
            for name, passed in (
                ("prospective_window_ok", prospective_window_ok),
                ("raw_hash_verified", raw_hash_verified),
                ("replay_ok", replay_ok),
                ("critical_incidents_zero", critical_incidents == 0),
                ("primary_health_ok", primary_health_ok),
                ("primary_pair_coverage_ok", primary_pair_coverage_ok),
                ("secondary_up", secondary_up),
                ("supporting_evidence_contract_ok", supporting_evidence_contract_ok),
                ("supporting_evidence_distinct", supporting_evidence_distinct),
            )
            if not passed
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--primary-evidence", type=Path)
    parser.add_argument("--secondary-evidence", type=Path)
    parser.add_argument("--replay-evidence", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_daily_report(
            collection_root=args.collection_root,
            day=args.date,
            primary_evidence=args.primary_evidence,
            secondary_evidence=args.secondary_evidence,
            replay_evidence=args.replay_evidence,
        )
        output = args.output_dir / f"daily_report_{args.date.isoformat()}.json"
        _atomic_write_json(output, report)
    except DailyReportError as error:
        print(f"daily report error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "qualifying_day": report["qualifying_day"]}))
    return 0 if report["qualifying_day"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
