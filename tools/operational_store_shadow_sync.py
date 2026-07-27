#!/usr/bin/env python3
"""Bootstrap and incrementally shadow-sync the SQLite operational candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_shadow_sync import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    ShadowSyncError,
    bootstrap_candidate,
    sync_sources,
)
from fx_intel.operational_store import OperationalStoreError  # noqa: E402
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="parity確認済みSQLite候補をbyte-offset方式でshadow同期する"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="parity reportが固定したJSONL末尾へ初期cursorを設定する",
    )
    _add_common_paths(bootstrap)
    bootstrap.add_argument("--parity-report", type=Path, required=True)
    bootstrap.add_argument("--report", type=Path, required=True)

    sync = subparsers.add_parser(
        "sync",
        help="両JSONLの完全な追記行だけを単一writerで同期する",
    )
    _add_common_paths(sync)
    sync.add_argument("--report", type=Path)
    sync.add_argument("--max-bytes-per-source", type=int, default=DEFAULT_MAX_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report is not None and args.report.exists():
        return _error(f"report already exists: {args.report}")
    try:
        if args.command == "bootstrap":
            payload = bootstrap_candidate(
                database_path=args.db,
                parity_report_path=args.parity_report,
                decision_path=args.decisions,
                price_path=args.prices,
                writer_id=args.writer_id,
            )
        else:
            payload = sync_sources(
                database_path=args.db,
                decision_path=args.decisions,
                price_path=args.prices,
                writer_id=args.writer_id,
                max_bytes_per_source=args.max_bytes_per_source,
            )
        if args.report is not None:
            atomic_write_json_create_only(args.report, payload)
    except (ShadowSyncError, OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
    return 1 if bool(payload.get("quality_failure")) else 0


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--writer-id", required=True)


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
