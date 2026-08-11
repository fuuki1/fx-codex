#!/usr/bin/env python3
"""Compare full JSONL scans with indexed SQLite pages on identical records."""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import heapq
import json
import math
from pathlib import Path
import statistics
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_migration import (  # noqa: E402
    OperationalMigrationError,
    decision_records_from_event,
    price_record_from_row,
    runtime_provenance,
)
from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    open_operational_reader,
)
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="JSONL全走査とSQLite indexed pageの同一record性能比較"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output", type=Path)
    return parser


def run_benchmark(
    *,
    db_path: str | Path,
    decision_path: str | Path,
    price_path: str | Path,
    iterations: int,
    page_size: int,
) -> dict[str, object]:
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    decision_target = Path(decision_path)
    price_target = Path(price_path)
    imported_at = datetime.now(UTC)

    with open_operational_reader(db_path) as store:
        # Warm caches and assert that both paths page over the same admissible population.
        json_decisions = _jsonl_decision_page(decision_target, page_size, imported_at)
        sql_decisions = _sqlite_decision_page(store.connection, page_size)
        json_prices = _jsonl_price_page(price_target, page_size)
        sql_prices = _sqlite_price_page(store.connection, page_size)
        if json_decisions != sql_decisions:
            raise OperationalMigrationError("decision benchmark page parity failed")
        if json_prices != sql_prices:
            raise OperationalMigrationError("price benchmark page parity failed")

        decision_json_times = _measure(
            lambda: _jsonl_decision_page(decision_target, page_size, imported_at),
            iterations,
        )
        decision_sql_times = _measure(
            lambda: _sqlite_decision_page(store.connection, page_size),
            iterations,
        )
        price_json_times = _measure(
            lambda: _jsonl_price_page(price_target, page_size),
            iterations,
        )
        price_sql_times = _measure(
            lambda: _sqlite_price_page(store.connection, page_size),
            iterations,
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": runtime_provenance(),
        "iterations": iterations,
        "page_size": page_size,
        "sources": {
            "decisions": {
                "path": str(decision_target.resolve()),
                "bytes": decision_target.stat().st_size,
            },
            "prices": {
                "path": str(price_target.resolve()),
                "bytes": price_target.stat().st_size,
            },
        },
        "decision_page": _comparison(decision_json_times, decision_sql_times),
        "price_page": _comparison(price_json_times, price_sql_times),
    }


def _jsonl_decision_page(
    path: Path,
    limit: int,
    imported_at: datetime,
) -> list[str]:
    latest: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            try:
                audit, _prediction, _failure = decision_records_from_event(
                    parsed,
                    imported_at=imported_at,
                )
            except (OperationalMigrationError, ValueError):
                continue
            event_ns = _datetime_ns(audit.event_time)
            _keep_latest(latest, (event_ns, audit.event_id, _canonical(parsed)), limit)
    return [payload for _event_ns, _event_id, payload in sorted(latest, reverse=True)]


def _sqlite_decision_page(connection, limit: int) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT payload_json
            FROM audit_events
            ORDER BY event_time_ns DESC, event_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def _jsonl_price_page(path: Path, limit: int) -> list[str]:
    latest: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            try:
                price = price_record_from_row(parsed)
            except (OperationalMigrationError, ValueError):
                continue
            event_ns = _datetime_ns(price.event_time)
            _keep_latest(
                latest,
                (event_ns, price.source_record_id, _canonical(parsed)),
                limit,
            )
    return [payload for _event_ns, _source_record_id, payload in sorted(latest, reverse=True)]


def _sqlite_price_page(connection, limit: int) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT payload_json
            FROM price_points
            ORDER BY event_time_ns DESC, source_record_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def _measure(operation, iterations: int) -> list[float]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return samples


def _keep_latest(
    heap: list[tuple[int, str, str]],
    item: tuple[int, str, str],
    limit: int,
) -> None:
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def _comparison(jsonl: list[float], sqlite: list[float]) -> dict[str, object]:
    jsonl_summary = _summary(jsonl)
    sqlite_summary = _summary(sqlite)
    sqlite_median = float(sqlite_summary["median_seconds"])
    speedup = (
        float(jsonl_summary["median_seconds"]) / sqlite_median if sqlite_median > 0 else math.inf
    )
    return {
        "jsonl_full_scan": jsonl_summary,
        "sqlite_indexed_page": sqlite_summary,
        "median_speedup_x": round(speedup, 3),
    }


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "median_seconds": round(statistics.median(ordered), 9),
        "p95_seconds": round(ordered[p95_index], 9),
        "min_seconds": round(ordered[0], 9),
        "max_seconds": round(ordered[-1], 9),
    }


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _datetime_ns(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value.astimezone(UTC) - epoch
    return (
        delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output is not None and args.output.exists():
        print(
            json.dumps(
                {"ok": False, "error": f"benchmark output already exists: {args.output}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        report = run_benchmark(
            db_path=args.db,
            decision_path=args.decisions,
            price_path=args.prices,
            iterations=args.iterations,
            page_size=args.page_size,
        )
        if args.output is not None:
            atomic_write_json_create_only(args.output, report)
    except (OperationalMigrationError, OperationalStoreError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
