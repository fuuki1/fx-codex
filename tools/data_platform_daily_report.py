#!/usr/bin/env python3
"""Daily continuous-operation report for the data platform (evidence writer).

Writes one ``daily_report_YYYY-MM-DD.json`` per UTC day into an operations
directory. The scorecard's continuous-operation section counts a day ONLY when
every one of these holds, all recomputed from durable artifacts (never from
memory of the process that collected them):

- ``primary_up``          the live poller (default: truefx) accepted >= 1 real
                          quote whose ``received_at`` falls on the day
- ``secondary_up``        the independent backfill mirror (default: dukascopy)
                          successfully fetched/verified >= 1 file that day
- ``raw_hash_verified``   every raw payload cited by the day's accepted quotes
                          re-reads intact from the content-addressed store
- ``replay_ok``           re-parsing those raw payloads reproduces every
                          accepted quote's data fields (deterministic replay)
- ``critical_incidents``  count of incident JSONs + raw-hash-mismatch
                          quarantine rows for the day (must be 0 to qualify)

The report never repairs or hides a bad state: a down collector yields an
honest ``primary_up: false`` report, and a hash/replay failure is written as
such (the scorecard will refuse the day).

Usage:
    python3 -m tools.data_platform_daily_report \
        --live-root logs/data_platform/collect/truefx \
        --mirror-root logs/data_platform/mirror \
        --ops-dir logs/data_platform/ops [--date 2026-07-14]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.truefx import TruefxContext, parse_rates_payload  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _day_of(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(UTC).date().isoformat()


def _verify_raw_and_replay(
    day_rows: list[dict[str, Any]],
    store: ImmutableRawStore,
    instruments: list[str],
) -> tuple[bool, bool, dict[str, Any]]:
    """Re-hash every cited raw blob and re-derive the accepted quotes from it."""

    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in day_rows:
        by_sha.setdefault(str(row["raw_payload_sha256"]), []).append(row)
    raw_ok = True
    replay_ok = True
    checked = 0
    for sha, rows in by_sha.items():
        try:
            payload = store.get(sha)  # get() re-hashes; corrupt blobs raise
        except RawStoreError:
            raw_ok = False
            replay_ok = False
            continue
        checked += 1
        context = TruefxContext(
            received_at=datetime.fromisoformat(str(rows[0]["received_at"])),
            connection_id=str(rows[0]["connection_id"]),
        )
        reparsed = {
            (quote.instrument, quote.provider_event_time.isoformat(), quote.bid, quote.ask)
            for quote in parse_rates_payload(payload, context, instruments)
            if quote.provider_event_time is not None
        }
        for row in rows:
            key = (
                str(row["instrument"]),
                str(row["provider_event_time"]),
                float(row["bid"]),
                float(row["ask"]),
            )
            if key not in reparsed:
                replay_ok = False
    detail = {"raw_blobs_checked": checked, "raw_blobs_cited": len(by_sha)}
    return raw_ok, replay_ok, detail


def build_report(
    *,
    day: str,
    live_root: Path,
    mirror_root: Path,
    primary_provider: str,
    secondary_source_prefix: str,
    instruments: list[str],
) -> dict[str, Any]:
    quotes = _read_jsonl(live_root / "log" / "quotes.jsonl")
    day_rows = [
        row
        for row in quotes
        if str(row.get("provider")) == primary_provider and _day_of(str(row["received_at"])) == day
    ]
    primary_up = bool(day_rows)

    if day_rows:
        raw_ok, replay_ok, verify_detail = _verify_raw_and_replay(
            day_rows, ImmutableRawStore(live_root / "raw"), instruments
        )
    else:
        # nothing collected -> nothing verifiable; a down day never qualifies,
        # and we do not claim verification we could not perform.
        raw_ok, replay_ok, verify_detail = False, False, {"raw_blobs_checked": 0}

    manifest = _read_jsonl(mirror_root / "manifest.jsonl")
    secondary_rows = [
        row
        for row in manifest
        if str(row.get("source", "")).startswith(secondary_source_prefix)
        and row.get("status") in ("fetched", "skipped")
        and _day_of(str(row["fetched_at"])) == day
    ]
    secondary_up = bool(secondary_rows)

    incidents = 0
    incidents_dir = live_root / "state" / "incidents"
    if incidents_dir.is_dir():
        for path in sorted(incidents_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if _day_of(str(payload.get("occurred_at"))) == day:
                incidents += 1
    quarantine = _read_jsonl(live_root / "log" / "quarantine.jsonl")
    hash_mismatches = [
        row
        for row in quarantine
        if row.get("reason") == "raw_hash_mismatch"
        and _day_of(
            str(row.get("occurred_at", row.get("received_at", "1970-01-01T00:00:00+00:00")))
        )
        == day
    ]
    incidents += len(hash_mismatches)

    return {
        "schema": "data_platform_daily_report_v1",
        "date": day,
        "primary_provider": primary_provider,
        "primary_provider_type": "aggregator" if primary_provider == "truefx" else "unknown",
        "secondary_source_prefix": secondary_source_prefix,
        "primary_up": primary_up,
        "secondary_up": secondary_up,
        "raw_hash_verified": raw_ok,
        "replay_ok": replay_ok,
        "critical_incidents": incidents,
        "accepted_quotes": len(day_rows),
        "secondary_files": len(secondary_rows),
        "verify_detail": verify_detail,
        "generated_at": datetime.now(UTC).isoformat(),
        "semantics": (
            "primary_up = live poller accepted >=1 quote this UTC day; secondary_up = "
            "independent mirror fetched/verified >=1 file this day; raw/replay recomputed "
            "from the content-addressed store, never trusted from memory"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--mirror-root", type=Path, required=True)
    parser.add_argument("--ops-dir", type=Path, required=True)
    parser.add_argument("--primary-provider", default="truefx")
    parser.add_argument("--secondary-source-prefix", default="dukascopy")
    parser.add_argument("--instruments", nargs="+", default=["USD_JPY", "EUR_USD", "GBP_USD"])
    args = parser.parse_args(argv)
    report = build_report(
        day=args.date,
        live_root=args.live_root,
        mirror_root=args.mirror_root,
        primary_provider=args.primary_provider,
        secondary_source_prefix=args.secondary_source_prefix,
        instruments=list(args.instruments),
    )
    args.ops_dir.mkdir(parents=True, exist_ok=True)
    out = args.ops_dir / f"daily_report_{args.date}.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
