#!/usr/bin/env python3
"""Create or verify an externally retainable virtual-portfolio V2 checkpoint."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio_checkpoint import (  # noqa: E402
    VirtualPortfolioCheckpointError,
    checkpoint_file_sha256,
    create_v2_checkpoint,
    verify_v2_checkpoint,
)


def _created_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("created-at must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("created-at must be timezone-aware")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify a V2 event-chain checkpoint")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--created-at", type=_created_at)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--checkpoint", type=Path, required=True)
    verify.add_argument("--expected-checkpoint-file-sha256", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "create":
            payload = create_v2_checkpoint(
                args.database,
                args.output,
                created_at=args.created_at,
            )
            result: dict[str, object] = {
                "ok": True,
                "status": "created",
                "output": str(args.output.expanduser().resolve()),
                "checkpoint_file_sha256": checkpoint_file_sha256(args.output),
                "checkpoint": payload,
            }
        else:
            verification = verify_v2_checkpoint(
                args.database,
                args.checkpoint,
                expected_checkpoint_file_sha256=args.expected_checkpoint_file_sha256,
            )
            ok = verification.get("passed") is True
            result = {
                "ok": ok,
                "status": "verified" if ok else "failed_closed",
                "verification": verification,
            }
    except (OSError, sqlite3.Error, ValueError, VirtualPortfolioCheckpointError) as error:
        result = {
            "ok": False,
            "status": "failed_closed",
            "error": str(error),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
