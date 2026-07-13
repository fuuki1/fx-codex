#!/usr/bin/env python3
"""Operational runner for the expectancy-maximization layer.

This command is intended for cron/CI/dashboard refresh jobs. It reads the
per-timeframe decision journal plus dense price snapshots, derives the
maximization profile, writes logs/briefing_maximization.json, and writes an
operations monitor JSON with findings and improvement candidates.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import journal, maximization, price_history, trade_outcome  # noqa: E402

DEFAULT_TF_JOURNAL_PATH = REPO_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_TF_PRICES_PATH = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_PROFILE_PATH = REPO_ROOT / "logs" / "briefing_maximization.json"
DEFAULT_MONITOR_PATH = REPO_ROOT / "logs" / "maximization_monitor.json"


def run_maximization_monitor(
    *,
    journal_path: Path = DEFAULT_TF_JOURNAL_PATH,
    prices_path: Path = DEFAULT_TF_PRICES_PATH,
    profile_json_path: Path = DEFAULT_PROFILE_PATH,
    monitor_json_path: Path = DEFAULT_MONITOR_PATH,
    symbols: Sequence[str] = maximization.MVP_SYMBOLS,
    timeframes: Sequence[str] = maximization.MVP_TIMEFRAMES,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _utc(now or datetime.now(UTC))
    entries = list(journal.read_entries(journal_path, as_of=generated_at))
    price_rows = list(price_history.read_snapshot_entries(prices_path, as_of=generated_at))
    scoring_entries = entries + price_rows

    profile = maximization.derive_timeframe_maximization(
        scoring_entries,
        now=generated_at,
        symbols=symbols,
        timeframes=timeframes,
    )
    monitor = maximization.build_monitoring_snapshot(profile, now=generated_at)
    monitor["runner"] = {
        "schema": 1,
        "journal_path": str(journal_path),
        "prices_path": str(prices_path),
        "profile_json_path": str(profile_json_path),
        "monitor_json_path": str(monitor_json_path),
        "decision_rows": len(entries),
        "price_rows": len(price_rows),
        "symbols": list(symbols),
        "timeframes": list(timeframes),
    }

    _write_json_atomic(profile_json_path, profile.to_dict())
    _write_json_atomic(monitor_json_path, monitor)
    return {
        "exit_code": int(monitor.get("exit_code", 0) or 0),
        "monitor": monitor,
        "profile": profile,
        "profile_json_path": profile_json_path,
        "monitor_json_path": monitor_json_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="時間足別AI判断の期待値最大化プロファイルを運用更新する"
    )
    parser.add_argument("--journal", type=Path, default=DEFAULT_TF_JOURNAL_PATH)
    parser.add_argument("--prices", type=Path, default=DEFAULT_TF_PRICES_PATH)
    parser.add_argument("--profile-json", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR_PATH)
    parser.add_argument("--symbols", nargs="+", default=list(maximization.MVP_SYMBOLS))
    parser.add_argument("--timeframes", nargs="+", default=list(maximization.MVP_TIMEFRAMES))
    parser.add_argument("--quiet", action="store_true", help="標準出力の要約を抑制する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_maximization_monitor(
        journal_path=args.journal,
        prices_path=args.prices,
        profile_json_path=args.profile_json,
        monitor_json_path=args.monitor_json,
        symbols=args.symbols,
        timeframes=args.timeframes,
    )
    if not args.quiet:
        _print_summary(result)
    return int(result["exit_code"])


def _print_summary(result: Mapping[str, Any]) -> None:
    monitor = result.get("monitor")
    if not isinstance(monitor, Mapping):
        print("maximization monitor: no monitor payload")
        return
    summary = monitor.get("summary") if isinstance(monitor.get("summary"), Mapping) else {}
    counts = summary.get("action_counts", {}) if isinstance(summary, Mapping) else {}
    print(
        "maximization monitor: "
        f"status={monitor.get('status')} exit={monitor.get('exit_code')} "
        f"cells={summary.get('cell_count', 0) if isinstance(summary, Mapping) else 0} "
        f"mature={summary.get('mature_cell_count', 0) if isinstance(summary, Mapping) else 0} "
        f"avoid={counts.get('avoid', 0) if isinstance(counts, Mapping) else 0} "
        f"dampen={counts.get('dampen', 0) if isinstance(counts, Mapping) else 0} "
        f"boost={counts.get('boost', 0) if isinstance(counts, Mapping) else 0}"
    )
    print(f"profile_json={result.get('profile_json_path')}")
    print(f"monitor_json={result.get('monitor_json_path')}")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(
                trade_outcome.json_safe(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
        tmp_path.replace(target)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
