#!/usr/bin/env python3
"""Benchmark compact operational pages and prove full keyset traversal."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_read_model import (  # noqa: E402
    canonical_response_bytes,
    decision_page,
    price_page,
)
from fx_intel.operational_shadow_sync import runtime_provenance  # noqa: E402
from fx_intel.operational_store import (  # noqa: E402
    OperationalStore,
    OperationalStoreError,
    open_operational_reader,
)
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="compact read modelのkeyset完全走査・latency・payload量を測る"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--page-size", type=int, default=100)
    return parser


def run_benchmark(
    *,
    database_path: str | Path,
    iterations: int,
    page_size: int,
) -> dict[str, object]:
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    database = Path(database_path).resolve()
    with open_operational_reader(database) as store:
        decision_sample = decision_page(store, limit=page_size)
        price_sample = price_page(store, limit=page_size)
        decision_times = _measure(
            lambda: decision_page(store, limit=page_size),
            iterations,
        )
        price_times = _measure(
            lambda: price_page(store, limit=page_size),
            iterations,
        )
        decision_walk = _walk(store, kind="decisions", page_size=page_size)
        price_walk = _walk(store, kind="prices", page_size=page_size)
        decision_raw_bytes = _raw_page_bytes(
            store,
            table="audit_events",
            time_field="event_time_ns",
            key_field="event_id",
            page_size=page_size,
        )
        price_raw_bytes = _raw_page_bytes(
            store,
            table="price_points",
            time_field="event_time_ns",
            key_field="source_record_id",
            page_size=page_size,
        )
        plans = {
            "decisions_global": _plan(
                store,
                """
                SELECT event_id FROM audit_events
                WHERE rowid <= ?
                ORDER BY event_time_ns DESC, event_id DESC LIMIT ?
                """,
                (2**63 - 1, page_size),
            ),
            "prices_global": _plan(
                store,
                """
                SELECT source_record_id FROM price_points
                WHERE rowid <= ?
                ORDER BY event_time_ns DESC, source_record_id DESC LIMIT ?
                """,
                (2**63 - 1, page_size),
            ),
        }
    return {
        "schema_version": 1,
        "operation": "operational_read_benchmark",
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": _runtime(),
        "database": str(database),
        "iterations": iterations,
        "page_size": page_size,
        "decisions": {
            "latency_ms": _latency(decision_times),
            "response_bytes": len(canonical_response_bytes(decision_sample)),
            "raw_payload_bytes_for_same_rows": decision_raw_bytes,
            "payload_reduction_ratio": _reduction(
                len(canonical_response_bytes(decision_sample)),
                decision_raw_bytes,
            ),
            "full_traversal": decision_walk,
        },
        "prices": {
            "latency_ms": _latency(price_times),
            "response_bytes": len(canonical_response_bytes(price_sample)),
            "raw_payload_bytes_for_same_rows": price_raw_bytes,
            "payload_reduction_ratio": _reduction(
                len(canonical_response_bytes(price_sample)),
                price_raw_bytes,
            ),
            "full_traversal": price_walk,
        },
        "query_plans": plans,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists():
        return _error(f"output already exists: {args.output}")
    try:
        report = run_benchmark(
            database_path=args.db,
            iterations=args.iterations,
            page_size=args.page_size,
        )
        atomic_write_json_create_only(args.output, report)
    except (OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": True, "output": str(args.output), **report}, ensure_ascii=False))
    return 0


def _measure(function: Callable[[], object], iterations: int) -> list[float]:
    values: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        function()
        values.append((time.perf_counter() - started) * 1_000)
    return values


def _walk(store: OperationalStore, *, kind: str, page_size: int) -> dict[str, object]:
    cursor = None
    identifiers: set[str] = set()
    returned = 0
    pages = 0
    while True:
        if kind == "decisions":
            page: Any = decision_page(store, limit=page_size, cursor=cursor)
            key = "event_id"
        else:
            page = price_page(store, limit=page_size, cursor=cursor)
            key = "source_record_id"
        pages += 1
        for item in page["items"]:
            returned += 1
            identifiers.add(str(item[key]))
        cursor = page["page"]["next_cursor"]
        if cursor is None:
            break
    return {
        "pages": pages,
        "returned": returned,
        "unique_ids": len(identifiers),
        "duplicates": returned - len(identifiers),
    }


def _latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return {
        "median": statistics.median(values),
        "p95": ordered[p95_index],
        "minimum": min(values),
        "maximum": max(values),
    }


def _plan(
    store: OperationalStore,
    sql: str,
    values: tuple[object, ...],
) -> list[str]:
    return [
        str(row[3])
        for row in store.connection.execute(
            f"EXPLAIN QUERY PLAN {sql}",
            values,
        ).fetchall()
    ]


def _raw_page_bytes(
    store: OperationalStore,
    *,
    table: str,
    time_field: str,
    key_field: str,
    page_size: int,
) -> int:
    return int(
        store.connection.execute(
            f"""
            SELECT COALESCE(SUM(LENGTH(payload_json)), 0)
            FROM (
                SELECT payload_json
                FROM {table}
                ORDER BY {time_field} DESC, {key_field} DESC
                LIMIT ?
            )
            """,
            (page_size,),
        ).fetchone()[0]
    )


def _reduction(response_bytes: int, raw_bytes: int) -> float | None:
    if raw_bytes <= 0:
        return None
    return 1 - response_bytes / raw_bytes


def _runtime() -> dict[str, object]:
    payload = runtime_provenance()
    digest = hashlib.sha256()
    digest.update(str(payload["implementation_sha256"]).encode("ascii"))
    for path in (
        REPO_ROOT / "fx_intel" / "operational_read_model.py",
        REPO_ROOT / "fx_intel" / "operational_read_api.py",
        Path(__file__),
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    payload["implementation_sha256"] = digest.hexdigest()
    return payload


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
