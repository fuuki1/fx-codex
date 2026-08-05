#!/usr/bin/env python3
"""Inspect or explicitly append-migrate V1 virtual accounting into V2.

The command only operates on the offline simulation database.  It contains no
broker URL, credentials, account identifier, order API, or position mutation.
Status and audit are read-only.  Migration requires an exact confirmation
token because V1 original writer timestamps cannot be reconstructed.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio import (  # noqa: E402
    open_virtual_portfolio_reader,
    open_virtual_portfolio_writer,
)

DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio.sqlite3"
CONFIRMATION = "append-v2-accounting"


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("recorded-at must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("recorded-at must be timezone-aware")
    return parsed.astimezone(UTC)


def _status(database: Path) -> dict[str, object]:
    with open_virtual_portfolio_reader(database) as store:
        return store.virtual_portfolio_v2_snapshot()


def _audit(database: Path) -> dict[str, object]:
    with open_virtual_portfolio_reader(database) as store:
        return store.audit_virtual_portfolio_v2()


def _migrate(
    database: Path,
    *,
    writer_id: str,
    recorded_at: datetime,
    confirmation: str,
) -> dict[str, object]:
    if confirmation != CONFIRMATION:
        raise ValueError(f"migration requires --confirm {CONFIRMATION}; no rows were written")
    with open_virtual_portfolio_writer(database, writer_id=writer_id) as store:
        before = store.virtual_portfolio_v2_snapshot()
        migration = store.migrate_virtual_portfolio_v2_accounting(
            recorded_at=recorded_at,
        )
        after = store.virtual_portfolio_v2_snapshot()
        audit = store.audit_virtual_portfolio_v2()
    return {
        "contract": "virtual-portfolio-v2-migration-run",
        "database": str(database.resolve()),
        "before": before,
        "migration": migration,
        "after": after,
        "audit": audit,
        "broker_connected": False,
        "execution_mode": "offline_simulation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read or append-migrate the offline virtual portfolio V2 ledger"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="read-only V2 projection status")
    subparsers.add_parser("audit", help="read-only V2 integrity audit")
    migrate = subparsers.add_parser(
        "migrate",
        help="append missing V1 cash events to V2; never rewrites V1 rows",
    )
    migrate.add_argument("--writer-id", default="virtual-portfolio-v2-migration")
    migrate.add_argument("--recorded-at", type=_aware, default=None)
    migrate.add_argument("--confirm", default="")
    args = parser.parse_args()
    database = args.database.resolve()
    try:
        if args.command == "status":
            result = _status(database)
        elif args.command == "audit":
            result = _audit(database)
        else:
            result = _migrate(
                database,
                writer_id=str(args.writer_id),
                recorded_at=args.recorded_at or datetime.now(UTC),
                confirmation=str(args.confirm),
            )
    except (OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": args.command,
                    "database": str(database),
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
    if args.command == "audit":
        return 0 if result.get("passed") is True else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
