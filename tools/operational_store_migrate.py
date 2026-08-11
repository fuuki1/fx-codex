#!/usr/bin/env python3
"""Create a parity-reported SQLite candidate from pinned JSONL sources."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, UTC
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_migration import (  # noqa: E402
    OperationalMigrationError,
    migrate_jsonl_candidate,
)
from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    file_sha256,
    open_operational_writer,
)
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SHA-256固定JSONLからcreate-only SQLite候補とparity reportを作る"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--decisions-sha256", required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--prices-sha256", required=True)
    parser.add_argument("--writer-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db.exists():
        return _error(f"candidate database already exists: {args.db}")
    if args.report.exists():
        return _error(f"migration report already exists: {args.report}")
    run_id = args.run_id or f"operational-migration-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    try:
        with open_operational_writer(args.db, writer_id=args.writer_id) as store:
            report = migrate_jsonl_candidate(
                store,
                decision_path=args.decisions,
                decision_sha256=args.decisions_sha256,
                price_path=args.prices,
                price_sha256=args.prices_sha256,
                run_id=run_id,
                batch_size=args.batch_size,
            )
            store.optimize()
            checkpoint = store.checkpoint("TRUNCATE")
            report = replace(report, database=store.audit())
        payload = report.to_dict()
        payload["checkpoint"] = checkpoint
        payload["database_sha256"] = file_sha256(args.db)
        atomic_write_json_create_only(args.report, payload)
    except (OperationalMigrationError, OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": True, "report": str(args.report), **payload}, ensure_ascii=False))
    return 0 if report.verdict in {"parity_verified", "parity_with_exclusions"} else 1


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
