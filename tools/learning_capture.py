#!/usr/bin/env python3
"""Capture local learning logs without posting to Discord.

This runner is a safe one-shot alternative to fx_briefing_loop.sh while
bootstrapping the dashboard. It writes journals, learning profiles, timeframe
price snapshots, and monitoring JSON, but all briefing runs use --no-discord.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CaptureStep:
    name: str
    command: list[str]
    allowed_exit_codes: tuple[int, ...] = (0,)


def build_steps(args: argparse.Namespace) -> list[CaptureStep]:
    python = str(args.python)
    symbols = [symbol.upper().replace("/", "") for symbol in (args.symbols or [])]
    symbol_args = ["--symbols", *symbols] if symbols else []
    llm_args = [] if args.use_llm else ["--no-llm"]
    common_briefing_args = [
        *llm_args,
        "--no-discord",
        "--no-export-events",
        "--no-event-archive",
        *symbol_args,
    ]
    steps: list[CaptureStep] = []
    if not args.skip_snapshot:
        steps.append(
            CaptureStep("timeframe-price-snapshot", [python, "fx_tf_snapshot.py", *symbol_args])
        )
    if not args.skip_fusion:
        steps.append(
            CaptureStep(
                "fusion-briefing-capture", [python, "fx_briefing.py", *common_briefing_args]
            )
        )
    if not args.skip_timeframe:
        steps.append(
            CaptureStep(
                "timeframe-briefing-capture",
                [python, "fx_briefing.py", "--per-timeframe", *common_briefing_args],
            )
        )
    if not args.skip_trade_monitor:
        steps.append(
            CaptureStep(
                "trade-outcome-monitor",
                [python, "tools/trade_outcome_monitor.py", "--quiet"],
                # 初期状態では成熟したTP/SL採点対象がなく exit=1 になり得るが、
                # 監視JSONは書き出されるため学習ログ収集自体の失敗とは扱わない。
                (0, 1),
            )
        )
    if not args.skip_decision_monitor:
        steps.append(
            CaptureStep(
                "decision-expectancy-monitor",
                [python, "tools/decision_expectancy_monitor.py", "--quiet"],
                # 完全判断ログも初期状態ではサンプル不足や期待R悪化で exit=1 になり得る。
                # 監視JSONを書けていればログ収集自体は続行できる。
                (0, 1),
            )
        )
    return steps


def run_steps(steps: Sequence[CaptureStep], *, keep_going: bool = False) -> int:
    failures: list[tuple[str, int]] = []
    for step in steps:
        print(f"[learning-capture] {step.name}: {' '.join(step.command)}")
        completed = subprocess.run(step.command, cwd=REPO_ROOT, check=False)
        if completed.returncode not in step.allowed_exit_codes:
            failures.append((step.name, completed.returncode))
            print(f"[learning-capture] {step.name} failed: exit={completed.returncode}")
            if not keep_going:
                return completed.returncode
        elif completed.returncode != 0:
            print(
                f"[learning-capture] {step.name} completed with health exit={completed.returncode}"
            )
    if failures:
        return failures[-1][1]
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discord送信なしでFX分析AIの学習ログを1回分収集する"
    )
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Claude等のLLM分析を使う。既定はAPI消費を避けるため自前分析のみ",
    )
    parser.add_argument("--skip-snapshot", action="store_true")
    parser.add_argument("--skip-fusion", action="store_true")
    parser.add_argument("--skip-timeframe", action="store_true")
    parser.add_argument("--skip-trade-monitor", action="store_true")
    parser.add_argument("--skip-decision-monitor", action="store_true")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="途中のステップが失敗しても後続ステップを試す",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    steps = build_steps(args)
    if not steps:
        print("[learning-capture] 実行対象がありません")
        return 0
    return run_steps(steps, keep_going=args.keep_going)


if __name__ == "__main__":
    raise SystemExit(main())
