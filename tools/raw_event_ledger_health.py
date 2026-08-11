#!/usr/bin/env python3
"""Read-only health report for the default-off raw event shadow ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.raw.event_ledger import RawEventLedger  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--stale-after-seconds", type=float, required=True)
    parser.add_argument("--max-duplicate-ratio", type=float, required=True)
    parser.add_argument("--max-quarantine-ratio", type=float, required=True)
    parser.add_argument("--required-symbol", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = RawEventLedger(
        args.collection_root / "raw-ledger",
        blob_store=ImmutableRawStore(args.collection_root / "raw"),
    )
    health = ledger.health_snapshot(
        stale_after_seconds=args.stale_after_seconds,
        max_duplicate_ratio=args.max_duplicate_ratio,
        max_quarantine_ratio=args.max_quarantine_ratio,
        required_symbols=args.required_symbol,
    )
    print(json.dumps(health, indent=2, sort_keys=True))
    return 0 if health["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
