#!/usr/bin/env python3
"""Score matured operational predictions once and append immutable outcomes.

This closes the operational learning loop: the sync path writes predictions and
prices, and this entry point turns matured predictions into scored outcomes.
It is read-mostly and idempotent — a prediction is scored exactly once, and a
prediction whose price path has not arrived yet is deferred, never frozen.
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

from fx_intel.operational_scoring import score_due_predictions  # noqa: E402
from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    open_operational_writer,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="満期済み予測を正準scorerで採点し、immutable outcomeを追記する"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--limit",
        type=int,
        default=1_000,
        help="1回の掃引で採点する上限件数(既定1000)",
    )
    parser.add_argument(
        "--writer-id",
        default="operational-score",
        help="排他writer識別子。同時実行はWriterBusyで拒否される",
    )
    parser.add_argument(
        "--as-of",
        help="採点基準時刻(aware ISO-8601)。省略時は現在UTC",
    )
    return parser


def _as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of must be timezone aware")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = _as_of(args.as_of)
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2

    # The checkpoint records which observation boundary produced these labels,
    # so a later audit can tell replays apart from the original scoring run.
    checkpoint = f"operational-score:{as_of.isoformat()}"
    try:
        with open_operational_writer(args.db, writer_id=args.writer_id) as store:
            report = score_due_predictions(
                store,
                as_of=as_of,
                source_checkpoint=checkpoint,
                limit=args.limit,
            )
    except OperationalStoreError as error:
        print(
            json.dumps(
                {"ok": False, "error": str(error), "kind": type(error).__name__},
                ensure_ascii=False,
            )
        )
        return 1

    payload: dict[str, object] = {"ok": True, "as_of": as_of.isoformat()}
    payload.update(report.to_dict())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
