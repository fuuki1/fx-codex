#!/usr/bin/env python3
"""Build one fail-closed prospective data-platform daily report.

The report is derived from the collector's append-only quote log and immutable
raw store. Secondary-source health and deterministic replay must be supplied as
separate same-day evidence files; missing or stale evidence is recorded as
``False`` rather than guessed. The output schema contains the exact fields used
by ``tools.data_platform_scorecard`` for the 30-trading-day gate.

Exit codes:
- 0: report written and the day qualifies
- 2: report written but one or more qualifying conditions failed
- 1: report could not be computed because an input was malformed
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError

REQUIRED_PAIRS = frozenset({"USDJPY", "EURUSD", "GBPUSD"})


class DailyReportError(RuntimeError):
    """The daily report inputs are malformed or internally inconsistent."""


def _load_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DailyReportError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DailyReportError(f"JSON evidence must be an object: {path}")
    return payload


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


def _same_day_boolean(
    payload: dict[str, Any] | None,
    *,
    day: date,
    boolean_field: str,
) -> bool:
    if payload is None or payload.get("report_date") != day.isoformat():
        return False
    return payload.get(boolean_field) is True


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
    secondary_evidence: Path | None,
    replay_evidence: Path | None,
) -> dict[str, Any]:
    if not collection_root.is_dir():
        raise DailyReportError(f"collection root not found: {collection_root}")
    log_root = collection_root / "log"
    accepted = [row for row in _iter_jsonl(log_root / "quotes.jsonl") if _matches_day(row, day)]
    quarantined = [
        row for row in _iter_jsonl(log_root / "quarantine.jsonl") if _matches_day(row, day)
    ]

    raw_store = ImmutableRawStore(collection_root / "raw")
    raw_errors: list[str] = []
    checked_hashes: set[str] = set()
    for row in accepted:
        raw_hash = str(row.get("raw_payload_sha256", ""))
        if raw_hash in checked_hashes:
            continue
        checked_hashes.add(raw_hash)
        try:
            raw_store.get(raw_hash)
        except (OSError, RawStoreError, ValueError) as error:
            raw_errors.append(f"{raw_hash[:12]}: {type(error).__name__}")

    pair_counts = Counter(str(row.get("instrument", "")) for row in accepted)
    primary_pairs = {
        str(row.get("instrument"))
        for row in accepted
        if row.get("provider") == "oanda"
        and row.get("collection_mode") == "live_stream"
        and row.get("account_environment") == "live"
        and row.get("quality_state") == "usable"
    }
    primary_up = REQUIRED_PAIRS.issubset(primary_pairs)

    freshness: list[float] = []
    for row in accepted:
        event = row.get("provider_event_time")
        received = row.get("received_at")
        if event is None or received is None:
            continue
        lag = (
            _parse_timestamp(received, "received_at")
            - _parse_timestamp(event, "provider_event_time")
        ).total_seconds()
        freshness.append(max(0.0, lag))

    flag_counts: Counter[str] = Counter()
    for row in (*accepted, *quarantined):
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
            payload = _load_json_object(path)
            if payload is not None and _matches_day(payload, day):
                incident_rows.append(payload)
    critical_incidents = sum(
        1 for incident in incident_rows if incident.get("severity") == "critical"
    )

    secondary_payload = _load_json_object(secondary_evidence)
    replay_payload = _load_json_object(replay_evidence)
    secondary_up = _same_day_boolean(
        secondary_payload,
        day=day,
        boolean_field="secondary_up",
    )
    replay_ok = _same_day_boolean(
        replay_payload,
        day=day,
        boolean_field="replay_ok",
    )
    raw_hash_verified = bool(accepted) and not raw_errors

    disk = shutil.disk_usage(collection_root)
    qualifying_day = (
        raw_hash_verified and replay_ok and critical_incidents == 0 and primary_up and secondary_up
    )

    return {
        "schema_version": 1,
        "report_date": day.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "qualifying_day": qualifying_day,
        "raw_hash_verified": raw_hash_verified,
        "replay_ok": replay_ok,
        "critical_incidents": critical_incidents,
        "primary_up": primary_up,
        "secondary_up": secondary_up,
        "required_pairs": sorted(REQUIRED_PAIRS),
        "observed_primary_pairs": sorted(primary_pairs),
        "quote_count": len(accepted),
        "quote_count_by_pair": dict(sorted(pair_counts.items())),
        "quarantine_count": len(quarantined),
        "quality_flag_counts": dict(sorted(flag_counts.items())),
        "freshness_seconds": {
            "p50": _percentile(freshness, 0.50),
            "p95": _percentile(freshness, 0.95),
            "p99": _percentile(freshness, 0.99),
            "max": max(freshness) if freshness else None,
        },
        "raw_blob_count_verified": len(checked_hashes) - len(raw_errors),
        "raw_verification_errors": raw_errors,
        "incident_types": [str(incident.get("type", "unknown")) for incident in incident_rows],
        "disk_free_bytes": disk.free,
        "secondary_evidence": str(secondary_evidence) if secondary_evidence else None,
        "replay_evidence": str(replay_evidence) if replay_evidence else None,
        "unmet_conditions": [
            name
            for name, passed in (
                ("raw_hash_verified", raw_hash_verified),
                ("replay_ok", replay_ok),
                ("critical_incidents_zero", critical_incidents == 0),
                ("primary_up", primary_up),
                ("secondary_up", secondary_up),
            )
            if not passed
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
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
