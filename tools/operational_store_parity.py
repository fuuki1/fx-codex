#!/usr/bin/env python3
"""Verify exact JSONL payload/classification parity against a candidate store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_migration import (  # noqa: E402
    OperationalMigrationError,
    verify_candidate_parity,
)
from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    file_sha256,
    open_operational_reader,
)
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="固定JSONLとSQLite候補のpayload・PIT分類parityを再検証する"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--decisions-sha256", required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--prices-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report.exists():
        return _error(f"parity report already exists: {args.report}")
    try:
        with open_operational_reader(args.db) as store:
            report = verify_candidate_parity(
                store,
                decision_path=args.decisions,
                decision_sha256=args.decisions_sha256,
                price_path=args.prices,
                price_sha256=args.prices_sha256,
            )
        payload = report.to_dict()
        payload["database_sha256"] = file_sha256(args.db)
        atomic_write_json_create_only(args.report, payload)
    except (OperationalMigrationError, OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": True, "report": str(args.report), **payload}, ensure_ascii=False))
    return 0 if report.parity_verdict == "parity_verified" else 1


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
