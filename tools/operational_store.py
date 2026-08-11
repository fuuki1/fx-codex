#!/usr/bin/env python3
"""Initialize, audit, checkpoint, and back up the SQLite operational store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    file_sha256,
    online_backup,
    open_operational_reader,
    open_operational_writer,
)

DEFAULT_STORE = Path("logs/fx_operational.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite WAL運用ストアの初期化・監査・checkpoint・backup"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_STORE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="安全版SQLiteでschemaをcreate/verify")
    initialize.add_argument("--writer-id", required=True)

    subparsers.add_parser("audit", help="quick_check、外部キー、件数、WAL容量を確認")

    checkpoint = subparsers.add_parser("checkpoint", help="writer lock内でWAL checkpoint")
    checkpoint.add_argument("--writer-id", required=True)
    checkpoint.add_argument(
        "--mode",
        choices=("PASSIVE", "FULL", "RESTART", "TRUNCATE"),
        default="PASSIVE",
    )

    backup = subparsers.add_parser("backup", help="Online Backup APIでcreate-only snapshot")
    backup.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            with open_operational_writer(args.db, writer_id=args.writer_id) as store:
                payload: object = store.audit().to_dict()
        elif args.command == "audit":
            with open_operational_reader(args.db) as store:
                payload = store.audit().to_dict()
        elif args.command == "checkpoint":
            with open_operational_writer(args.db, writer_id=args.writer_id) as store:
                payload = {
                    "checkpoint": store.checkpoint(args.mode),
                    "audit": store.audit().to_dict(),
                }
        elif args.command == "backup":
            with open_operational_reader(args.db) as store:
                output = online_backup(store, args.output)
            payload = {"backup": str(output), "sha256": file_sha256(output)}
        else:
            raise AssertionError(f"unhandled command: {args.command}")
    except (OperationalStoreError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
