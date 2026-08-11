#!/usr/bin/env python3
"""Capture registered ALFRED vintages into raw-first, PIT-safe research evidence.

This operator tool is analysis-only. It uses the empirically guarded keyless
ALFRED graph endpoint, records availability as local capture completion, never
fabricates provider release time, and never makes the result promotion-eligible.
Canonical briefing remains disconnected until prospective evidence and source
governance are separately accepted.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import socket
import sys
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.fred_macro import (  # noqa: E402
    ALFRED_BASE,
    Fetcher,
    MacroPITCaptureError,
    MacroPITLog,
    SERIES_REGISTRY,
    capture_vintage_result,
    requests_fetcher,
)
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from fx_intel.state_io import atomic_write_json  # noqa: E402

CAPTURE_REPORT_SCHEMA_VERSION = 1
DEFAULT_LOOKBACK_DAYS = 5 * 366
Clock = Callable[[], datetime]


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _code_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(root: Path, path: Path | None, default: Path) -> Path:
    selected = default if path is None else path
    return selected if selected.is_absolute() else root / selected


def run_capture(
    *,
    root: Path,
    output_root: Path,
    report_path: Path,
    series_ids: Sequence[str],
    vintage: date,
    observation_start: date,
    observation_end: date,
    fetcher: Fetcher = requests_fetcher,
    clock: Clock | None = None,
) -> dict[str, object]:
    """Run one bounded multi-series capture and write immutable/latest reports."""

    now = clock or (lambda: datetime.now(UTC))
    started_at = _aware_utc(now(), "capture start")
    normalized_series = tuple(str(series).strip().upper() for series in series_ids)
    if not normalized_series or len(set(normalized_series)) != len(normalized_series):
        raise ValueError("series must be a non-empty unique list")
    unknown = sorted(set(normalized_series) - set(SERIES_REGISTRY))
    if unknown:
        raise ValueError(f"series are not registered: {unknown}")
    if observation_end < observation_start:
        raise ValueError("observation_end must not precede observation_start")
    if observation_end > vintage:
        raise ValueError("observation_end must not be later than vintage_date")
    if vintage > started_at.date():
        raise ValueError("vintage_date must not be in the future")

    raw_store = ImmutableRawStore(output_root / "raw")
    log = MacroPITLog(output_root / "observations.jsonl")
    results: list[dict[str, object]] = []
    for series_id in normalized_series:
        try:
            captured = capture_vintage_result(
                series_id,
                vintage,
                observation_start,
                observation_end,
                fetcher=fetcher,
                store=raw_store,
                log=log,
                now=now,
            )
        except Exception as error:  # report every failed series; never substitute data
            result: dict[str, object] = {
                "series_id": series_id,
                "status": "error",
                "error_type": type(error).__name__,
                "detail": str(error)[:500],
                "raw_payload_sha256": None,
                "raw_retained": False,
            }
            if isinstance(error, MacroPITCaptureError):
                result["raw_payload_sha256"] = error.raw_payload_sha256
                result["source_uri"] = error.source_uri
                result["raw_retained"] = error.raw_payload_sha256 is not None
            results.append(result)
            continue
        values = captured.observations
        results.append(
            {
                "series_id": series_id,
                "status": "ok",
                "source_uri": captured.source_uri,
                "raw_payload_sha256": captured.raw_payload_sha256,
                "raw_retained": True,
                "observation_count": len(values),
                "appended_count": captured.appended_count,
                "missing_value_count": sum(value.value is None for value in values),
                "first_period": values[0].period.isoformat() if values else None,
                "last_period": values[-1].period.isoformat() if values else None,
                "available_at": values[0].available_at.isoformat() if values else None,
            }
        )

    completed_at = _aware_utc(now(), "capture completion")
    run_id = f"macro-{uuid.uuid4().hex}"
    complete = all(result["status"] == "ok" for result in results)
    manifest_path = output_root / "manifests" / f"{run_id}.json"
    report: dict[str, object] = {
        "schema_version": CAPTURE_REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "complete" if complete else "partial_failure",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "host": socket.gethostname(),
        "source_contract": "alfredgraph_keyless_empirically_guarded_v1",
        "source_base": ALFRED_BASE,
        "vintage_date": vintage.isoformat(),
        "observation_start": observation_start.isoformat(),
        "observation_end": observation_end.isoformat(),
        "requested_series": list(normalized_series),
        "results": results,
        "observation_log": str(log.path),
        "raw_store": str(raw_store.root),
        "collector_code_sha256": {
            "data_platform/collect/fred_macro.py": _code_sha256(
                root / "data_platform" / "collect" / "fred_macro.py"
            ),
            "tools/fred_macro_capture.py": _code_sha256(Path(__file__).resolve()),
        },
        "manifest_path": str(manifest_path),
        "promotion_eligible": False,
        "canonical_briefing_connected": False,
        "limitations": [
            "keyless alfredgraph behavior is empirically guarded, not an accepted API SLA",
            "provider release and schedule times are unavailable and remain null",
            "availability begins at local capture; historical backfill is not contemporaneous evidence",
            "local manifests are not external custody or trusted timestamps",
        ],
    }
    atomic_write_json(manifest_path, report)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    latest = {**report, "manifest_sha256": manifest_sha256}
    atomic_write_json(report_path, latest)
    return latest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--series", nargs="+", default=sorted(SERIES_REGISTRY))
    parser.add_argument("--vintage-date", type=date.fromisoformat)
    parser.add_argument("--observation-start", type=date.fromisoformat)
    parser.add_argument("--observation-end", type=date.fromisoformat)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    started = datetime.now(UTC)
    vintage = args.vintage_date or started.date()
    observation_end = args.observation_end or vintage
    if args.lookback_days < 1:
        print("fred macro capture error: lookback-days must be positive", file=sys.stderr)
        return 1
    observation_start = args.observation_start or (
        observation_end - timedelta(days=args.lookback_days)
    )
    output_root = _resolve(root, args.output_root, Path("collect/macro"))
    report_path = _resolve(root, args.report, Path("logs/macro_pit_capture_latest.json"))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "series": args.series,
                    "vintage_date": vintage.isoformat(),
                    "observation_start": observation_start.isoformat(),
                    "observation_end": observation_end.isoformat(),
                    "output_root": str(output_root),
                    "report": str(report_path),
                },
                indent=2,
            )
        )
        return 0
    try:
        report = run_capture(
            root=root,
            output_root=output_root,
            report_path=report_path,
            series_ids=args.series,
            vintage=vintage,
            observation_start=observation_start,
            observation_end=observation_end,
        )
    except (OSError, ValueError) as error:
        print(f"fred macro capture error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
