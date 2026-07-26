#!/usr/bin/env python3
"""Seal, verify, and restore analysis-only capture-journal archives."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.journal_archive import (  # noqa: E402
    JournalArchiveError,
    restore_archive_set,
    seal_historical_shard,
    verify_archive,
)
from data_platform.collect.capture_journal import CaptureJournalError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    seal = commands.add_parser("seal", help="seal one non-active UTC-day shard")
    seal.add_argument("--journal-root", type=Path, required=True)
    seal.add_argument("--archive-root", type=Path, required=True)
    seal.add_argument("--shard-date", required=True)

    verify = commands.add_parser("verify", help="verify one sealed archive")
    verify.add_argument("--manifest", type=Path, required=True)

    restore = commands.add_parser(
        "restore",
        help="restore an explicit, complete genesis-anchored manifest prefix",
    )
    restore.add_argument("--manifest", type=Path, action="append", required=True)
    restore.add_argument("--destination-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "seal":
            result = seal_historical_shard(
                args.journal_root,
                args.archive_root,
                shard_date=args.shard_date,
                sealed_at=datetime.now(UTC),
            )
            payload: object = {
                "manifest_path": str(result.manifest_path.resolve()),
                "archive_path": str(result.archive_path.resolve()),
                "manifest": result.manifest,
            }
        elif args.command == "verify":
            payload = verify_archive(args.manifest)
        else:
            payload = restore_archive_set(
                args.manifest,
                args.destination_root,
            ).to_dict()
    except (CaptureJournalError, JournalArchiveError) as error:
        print(f"capture-journal archive error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
