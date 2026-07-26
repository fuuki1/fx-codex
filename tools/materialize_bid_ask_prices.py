#!/usr/bin/env python3
"""Materialize completed 1-minute OANDA bid/ask bars for outcome scoring."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.runtime.bid_ask_bridge import (  # noqa: E402
    DEFAULT_OUTPUT_TIMEFRAMES,
    BidAskBridgeError,
    materialize_increment,
)
from fx_intel.universe import MVP_SYMBOLS  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingest-log-dir", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=list(MVP_SYMBOLS),
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=list(DEFAULT_OUTPUT_TIMEFRAMES),
    )
    parser.add_argument("--close-delay-seconds", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = materialize_increment(
            ingest_log_dir=args.ingest_log_dir,
            state_path=args.state,
            output_path=args.output,
            instruments=args.instruments,
            timeframes=args.timeframes,
            close_delay=timedelta(seconds=args.close_delay_seconds),
            write=not args.dry_run,
        )
    except (BidAskBridgeError, OSError, ValueError) as error:
        print(f"[bid-ask-materializer] {error}", file=sys.stderr)
        return 74
    print(json.dumps({"dry_run": args.dry_run, **result.to_dict()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
