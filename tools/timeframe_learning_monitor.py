#!/usr/bin/env python3
"""Refresh per-timeframe learning caches outside the decision publication lock."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import (  # noqa: E402
    journal,
    maximization,
    price_history,
    stage_timing,
    tf_learning,
    tp_sl_learning,
)
from fx_intel.state_io import atomic_write_json  # noqa: E402

DEFAULT_JOURNAL = REPO_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_PRICES = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_TF_LEARNING = REPO_ROOT / "logs" / "briefing_tf_learning.json"
DEFAULT_TP_SL_LEARNING = REPO_ROOT / "logs" / "briefing_tp_sl_learning.json"
DEFAULT_MAXIMIZATION = REPO_ROOT / "logs" / "briefing_maximization.json"
DEFAULT_MONITOR = REPO_ROOT / "logs" / "timeframe_learning_monitor.json"
MONITOR_CONTRACT = "fx-codex-timeframe-learning-monitor-v1"


def run(args: argparse.Namespace) -> dict[str, object]:
    now = datetime.now(UTC)
    timing = stage_timing.StageTimingRecorder(
        args.stage_timing_log,
        pipeline="timeframe_learning_monitor",
    )
    with timing.stage("timeframe_learning_journal_read"):
        decision_source_before = _source_identity(args.journal)
        decisions = list(journal.read_entries(args.journal))
        decision_source = _source_identity(args.journal)
        if decision_source != decision_source_before:
            raise RuntimeError("decision journal changed during timeframe learning read")
        pit_decisions = [
            row
            for row in decisions
            if journal.is_pit_eligible_entry(row) and _not_future(row.get("prediction_time"), now)
        ]
    with timing.stage("timeframe_learning_price_read"):
        price_source_before = _source_identity(args.prices)
        raw_prices = list(journal.read_entries(args.prices))
        price_source = _source_identity(args.prices)
        if price_source != price_source_before:
            raise RuntimeError("price journal changed during timeframe learning read")
        prices = [
            row
            for row in raw_prices
            if price_history.is_pit_eligible_snapshot_entry(row, as_of=now)
        ]
    scoring_entries = [*pit_decisions, *prices]

    with timing.stage("timeframe_direction_learning_refresh"):
        direction = tf_learning.derive_timeframe_learning(scoring_entries, now=now)
        tf_learning.save_timeframe_learning(direction, args.tf_learning)
    with timing.stage("timeframe_tp_sl_learning_refresh"):
        tp_sl = tp_sl_learning.derive_timeframe_tp_sl_learning(scoring_entries, now=now)
        tp_sl_learning.save_timeframe_tp_sl_learning(tp_sl, args.tp_sl_learning)
    with timing.stage("timeframe_maximization_learning_refresh"):
        maximum = maximization.derive_timeframe_maximization(scoring_entries, now=now)
        maximization.save_timeframe_maximization(maximum, args.maximization)

    payload: dict[str, object] = {
        "schema": 1,
        "monitor_contract": MONITOR_CONTRACT,
        "generated_at": now.isoformat(),
        "status": "ok",
        "decision_rows": len(decisions),
        "pit_eligible_decision_rows": len(pit_decisions),
        "price_rows": len(raw_prices),
        "pit_eligible_price_rows": len(prices),
        "excluded_future_or_invalid_price_rows": len(raw_prices) - len(prices),
        "direction_profiles": len(direction.profiles),
        "tp_sl_profiles": len(tp_sl.profiles),
        "maximization_cells": len(maximum.cells),
        "input_watermarks": {
            "decisions": {
                **decision_source,
                "sha256": _rows_sha256(pit_decisions),
                "raw_rows": len(decisions),
                "eligible_rows": len(pit_decisions),
                "max_prediction_time": _max_timestamp(pit_decisions, "prediction_time"),
            },
            "prices": {
                **price_source,
                "sha256": _rows_sha256(prices),
                "raw_rows": len(raw_prices),
                "eligible_rows": len(prices),
                "max_available_time": _max_timestamp(prices, "available_time"),
            },
        },
        "cache_artifacts": {
            "timeframe_direction": _artifact(args.tf_learning),
            "timeframe_tp_sl": _artifact(args.tp_sl_learning),
            "timeframe_maximization": _artifact(args.maximization),
        },
        "stage_timing": stage_timing.summarize(timing.records),
    }
    atomic_write_json(args.monitor_json, payload)
    return payload


def _not_future(value: object, now: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.astimezone(UTC) <= now
    )


def _rows_sha256(rows: Sequence[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _max_timestamp(rows: Sequence[dict[str, object]], key: str) -> str | None:
    values: list[datetime] = []
    for row in rows:
        try:
            parsed = datetime.fromisoformat(str(row.get(key)).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            values.append(parsed.astimezone(UTC))
    return max(values).isoformat() if values else None


def _artifact(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _source_identity(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "source_path": str(path.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "read_offset_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="時間足別の全履歴学習cacheをdecision critical path外で更新する"
    )
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    parser.add_argument("--tf-learning", type=Path, default=DEFAULT_TF_LEARNING)
    parser.add_argument("--tp-sl-learning", type=Path, default=DEFAULT_TP_SL_LEARNING)
    parser.add_argument("--maximization", type=Path, default=DEFAULT_MAXIMIZATION)
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR)
    parser.add_argument("--stage-timing-log", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    if not args.quiet:
        print(
            "timeframe learning monitor: "
            f"decisions={result['decision_rows']} prices={result['price_rows']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
