#!/usr/bin/env python3
"""Run one scheduled operational-store sync or full replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_full_replay import FullReplayError  # noqa: E402
from fx_intel.operational_scheduler import (  # noqa: E402
    OperationalScheduleError,
    ScheduledRunFailure,
    run_scheduled,
)
from fx_intel.operational_shadow_sync import ShadowSyncError  # noqa: E402
from fx_intel.operational_store import OperationalStoreError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="定期shadow同期または日次full replayをcreate-only証跡付きで1回実行する"
    )
    parser.add_argument("action", choices=("sync", "full-replay"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument(
        "--run-id",
        help="再現テスト・手動実行用。省略時はUTC時刻とPIDから生成する",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_scheduled(
            action=args.action,
            database_path=args.db,
            decision_path=args.decisions,
            price_path=args.prices,
            evidence_dir=args.evidence_dir,
            run_id=args.run_id,
        )
    except ScheduledRunFailure as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(error),
                    "intent": str(error.intent_path),
                    "failure_report": str(error.failure_path),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (
        FullReplayError,
        OperationalScheduleError,
        OperationalStoreError,
        ShadowSyncError,
        OSError,
        ValueError,
    ) as error:
        print(
            json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": result.exit_code == 0,
                "intent": str(result.intent_path),
                "report": str(result.report_path),
                **result.payload,
            },
            ensure_ascii=False,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
