#!/usr/bin/env python3
"""Evaluate and govern the analysis-only direction threshold policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fx_intel import direction_threshold as threshold  # noqa: E402

DEFAULT_OUTCOMES = PROJECT_ROOT / "logs" / "briefing_decision_outcomes.json"
DEFAULT_POLICY = PROJECT_ROOT / "logs" / "direction_threshold_policy.json"


def _outcomes(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"outcome reportを読めません: {error}") from error
    raw = payload.get("outcomes") if isinstance(payload, dict) else None
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def _required_policy(path: Path) -> threshold.ThresholdPolicy:
    policy = threshold.load_policy(path)
    if policy is None:
        raise SystemExit(f"有効なpolicyを読めません: {path}")
    return policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    evaluate.add_argument("--candidate", type=float, action="append")
    approve = subparsers.add_parser("approve")
    approve.add_argument("--by", required=True)
    subparsers.add_parser("activate")
    subparsers.add_parser("status")
    args = parser.parse_args(argv)

    if args.command == "evaluate":
        candidates = args.candidate or list(threshold.DEFAULT_CANDIDATES)
        policy = threshold.evaluate_threshold_candidates(
            _outcomes(args.outcomes),
            candidates=candidates,
        )
        threshold.save_policy(policy, args.policy)
    elif args.command == "approve":
        policy = threshold.approve_policy(_required_policy(args.policy), args.by)
        threshold.save_policy(policy, args.policy)
    elif args.command == "activate":
        policy = threshold.activate_policy(_required_policy(args.policy))
        threshold.save_policy(policy, args.policy)
    else:
        policy = _required_policy(args.policy)
    print(json.dumps(policy.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
