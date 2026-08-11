#!/usr/bin/env python3
"""Create a daily full-replay evidence report for the operational candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_full_replay import (  # noqa: E402
    FullReplayError,
    run_full_replay,
)
from fx_intel.operational_store import OperationalStoreError  # noqa: E402
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="raw全履歴とSQLite projection/chunk/rejectionを日次再照合する"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--writer-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report.exists():
        return _error(f"report already exists: {args.report}")
    try:
        payload = run_full_replay(
            database_path=args.db,
            decision_path=args.decisions,
            price_path=args.prices,
            writer_id=args.writer_id,
        )
        atomic_write_json_create_only(args.report, payload)
    except (FullReplayError, OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": True, "report": str(args.report), **payload}, ensure_ascii=False))
    return 0 if payload["verdict"] == "full_replay_verified" else 1


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
