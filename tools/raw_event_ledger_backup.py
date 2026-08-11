#!/usr/bin/env python3
"""Create or verify a create-only raw-event-ledger backup snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.raw.backup import (  # noqa: E402
    RawLedgerBackupError,
    create_backup,
    verify_backup,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a completed snapshot")
    create.add_argument("--collection-root", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify restore integrity")
    verify.add_argument("--backup", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(
                collection_root=args.collection_root,
                destination=args.destination,
            )
        else:
            result = verify_backup(args.backup)
    except (OSError, RawLedgerBackupError) as error:
        print(
            json.dumps(
                {"ok": False, "error_type": type(error).__name__, "detail": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
