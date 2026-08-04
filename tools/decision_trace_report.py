#!/usr/bin/env python3
"""判断トレース解析レポートを stdout へ出力する読み取り専用CLI。

このツールは入力を**読むだけ**で、ログ・SQLite・JSONLへ一切書き込まない。
出力先は stdout のみで、ファイル書き込み経路を持たない(設計上の保証)。

使い方:
    python tools/decision_trace_report.py --input logs/decisions.jsonl
    python tools/decision_trace_report.py --input - --format json
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.decision_trace import (  # noqa: E402
    FunnelReconciliationError,
    FunnelSummary,
    TraceReport,
    build_report,
    report_to_dict,
)


def _iter_json_lines(handle: Any) -> Iterator[object]:
    """JSONL を1行ずつ読む。壊れた行は捨てずにマーカーとして流す。

    解析層が indeterminate として件数と理由を残すため、ここで除外しない。
    """
    for raw in handle:
        text = raw.strip()
        if not text:
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            # 解析層で "event_not_mapping" として indeterminate 計上される。
            yield None


def _format_summary(title: str, summary: FunnelSummary) -> list[str]:
    lines = [
        f"{title}",
        f"  total_decisions   : {summary.total_decisions}",
        f"  passed_all_gates  : {summary.passed_all_gates}",
        f"  indeterminate     : {summary.indeterminate}",
    ]
    if summary.blocked_at:
        lines.append("  blocked_at (first blocker のみ計上):")
        for name, count in summary.blocked_at.items():
            share = summary.dominance.get(name, 0.0)
            lines.append(f"    {name:<28} {count:>6}  dominance={share:.3f}")
    else:
        lines.append("  blocked_at        : (なし)")
    if summary.invalid_transitions:
        lines.append(f"  invalid_transitions: {len(summary.invalid_transitions)} 件")
    if summary.missing_transitions:
        lines.append(f"  missing_transitions: {len(summary.missing_transitions)} 件")
    return lines


def _format_text(report: TraceReport) -> str:
    lines: list[str] = [
        "=== Decision Trace Analytics MVP (読み取り専用) ===",
        "",
    ]
    lines.extend(_format_summary("[全体]", report.overall))

    per_gate = report.latency.get("per_gate", {})
    if isinstance(per_gate, dict) and not per_gate.get("measurable", False):
        lines.append("")
        lines.append("[工程別遅延]")
        lines.append(f"  measurable=false  reason={per_gate.get('reason')}")
        lines.append("  ※ gate単位のtimestampが存在しないため推定値は算出しない")

    for label, section in (
        ("pair", report.by_pair),
        ("timeframe", report.by_timeframe),
        ("regime", report.by_regime),
    ):
        if not section:
            continue
        lines.append("")
        lines.append(f"[{label} 別]")
        for name, summary in section.items():
            lines.extend(_format_summary(f"  <{name}>", summary))

    if report.parse_reason_counts:
        lines.append("")
        lines.append("[indeterminate 理由]")
        for reason, count in report.parse_reason_counts.items():
            lines.append(f"  {reason:<34} {count:>6}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="判断トレース解析レポート(読み取り専用・stdout出力のみ)",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="判断イベントのJSONLパス。'-' で標準入力",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="出力形式(既定: text)",
    )
    args = parser.parse_args(argv)

    if args.input == "-":
        events = list(_iter_json_lines(sys.stdin))
    else:
        source = Path(args.input)
        if not source.is_file():
            print(f"入力が見つかりません: {source}", file=sys.stderr)
            return 2
        # 読み取り専用オープン。書き込みモードは使わない。
        with source.open("r", encoding="utf-8") as handle:
            events = list(_iter_json_lines(handle))

    try:
        report = build_report(events)
    except FunnelReconciliationError as error:
        print(f"ファネル恒等式が破れました: {error}", file=sys.stderr)
        return 3

    if args.format == "json":
        print(json.dumps(report_to_dict(report), ensure_ascii=False, sort_keys=True))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
