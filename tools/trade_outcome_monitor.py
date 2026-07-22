#!/usr/bin/env python3
"""Operational runner for trade expectancy monitoring.

This command is intended for cron/CI/dashboard refresh jobs. It runs the
MFE/MAE/TP/SL outcome audit, updates the improvement registry, optionally
retests TP/SL variants, auto-pauses underperforming approved target policies,
and writes the dashboard monitor JSON in one deterministic pass.
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

from fx_intel import journal, trade_outcome  # noqa: E402

DEFAULT_JOURNAL_PATH = REPO_ROOT / "logs" / "briefing_journal.jsonl"
DEFAULT_REGISTRY_PATH = REPO_ROOT / "logs" / "trade_improvement_candidates.json"
DEFAULT_MONITOR_PATH = REPO_ROOT / "logs" / "trade_outcome_monitor.json"
DEFAULT_OUTCOME_REPORT_PATH = REPO_ROOT / "logs" / "trade_outcome_report.json"
DEFAULT_VARIANT_REPORT_PATH = REPO_ROOT / "logs" / "trade_variant_report.json"


def run_trade_outcome_monitor(
    *,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    monitor_json_path: Path = DEFAULT_MONITOR_PATH,
    outcome_json_path: Path | None = DEFAULT_OUTCOME_REPORT_PATH,
    variant_json_path: Path | None = DEFAULT_VARIANT_REPORT_PATH,
    update_registry: bool = True,
    retest_variants: bool = True,
    require_sample_ok: bool = False,
    target1_r_candidates: Sequence[float] | None = None,
    target2_r_candidates: Sequence[float] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full trade-outcome monitoring pass and write JSON artifacts."""
    generated_at = _utc(now or datetime.now(UTC))
    all_entries = list(journal.read_entries(journal_path))
    entries = [entry for entry in all_entries if journal.is_pit_eligible_entry(entry)]
    outcomes = trade_outcome.evaluate_trade_outcomes(entries)
    summary = trade_outcome.summarize_expectancy(outcomes)
    findings = trade_outcome.expectancy_findings(summary)
    expectancy_candidates = trade_outcome.improvement_candidates(summary)
    registry = trade_outcome.load_improvement_registry(registry_path) if update_registry else {}
    if registry.get("data_contract") != journal.FUSION_PIT_DATA_CONTRACT:
        registry = {}
    registry_updated = False
    variant_report: dict[str, Any] | None = None
    variant_candidates: list[trade_outcome.TradeImprovementCandidate] = []

    if update_registry:
        registry = trade_outcome.update_improvement_registry(
            registry,
            expectancy_candidates,
            managed_action_types=trade_outcome.EXPECTANCY_CANDIDATE_ACTION_TYPES,
            now=generated_at,
            data_contract=journal.FUSION_PIT_DATA_CONTRACT,
        )
        registry_updated = True

    if retest_variants:
        variant_report = trade_outcome.retest_tp_sl_variants(
            entries,
            target1_r_candidates=(target1_r_candidates or trade_outcome.DEFAULT_TP1_R_CANDIDATES),
            target2_r_candidates=(target2_r_candidates or trade_outcome.DEFAULT_TP2_R_CANDIDATES),
        )
        variant_candidates = trade_outcome.variant_improvement_candidates(variant_report)
        if update_registry and _variant_baseline_evaluated(variant_report) > 0:
            registry = trade_outcome.update_improvement_registry(
                registry,
                variant_candidates,
                managed_action_types=trade_outcome.VARIANT_CANDIDATE_ACTION_TYPES,
                now=generated_at,
                data_contract=journal.FUSION_PIT_DATA_CONTRACT,
            )
            registry_updated = True

    paused_policies: list[dict] = []
    if update_registry:
        registry, paused_policies = trade_outcome.auto_pause_underperforming_approved_policies(
            registry,
            summary,
            now=generated_at,
        )
        _write_json_atomic(registry_path, registry)

    health_report = trade_outcome.check_expectancy_health(
        summary,
        require_sample_ok=require_sample_ok,
    )
    monitor = trade_outcome.build_monitoring_snapshot(
        summary,
        registry=registry,
        health_report=health_report,
        now=generated_at,
    )
    monitor["runner"] = {
        "schema": 1,
        "journal_path": str(journal_path),
        "registry_path": str(registry_path),
        "monitor_json_path": str(monitor_json_path),
        "outcome_json_path": str(outcome_json_path) if outcome_json_path else None,
        "variant_json_path": str(variant_json_path) if variant_json_path else None,
        "registry_updated": registry_updated,
        "retest_variants": retest_variants,
        "outcome_count": len(outcomes),
        "pit_eligible_journal_rows": len(entries),
        "pit_ineligible_journal_rows": len(all_entries) - len(entries),
        "finding_count": len(findings),
        "expectancy_candidate_count": len(expectancy_candidates),
        "variant_candidate_count": len(variant_candidates),
        "auto_paused_policy_count": len(paused_policies),
        "require_sample_ok": require_sample_ok,
    }
    monitor["variant_retest"] = _variant_monitor_summary(variant_report, variant_candidates)
    monitor["auto_paused_policies"] = paused_policies

    if outcome_json_path is not None:
        _write_json_atomic(
            outcome_json_path,
            {
                "schema": 1,
                "generated_at": generated_at.isoformat(),
                "summary": summary,
                "findings": findings,
                "improvement_candidates": [
                    candidate.to_dict() for candidate in expectancy_candidates
                ],
                "improvement_registry": registry,
                "auto_paused_policies": paused_policies,
                "outcomes": [outcome.to_dict() for outcome in outcomes],
            },
        )
    if variant_json_path is not None and variant_report is not None:
        payload = dict(variant_report)
        payload["generated_at"] = generated_at.isoformat()
        payload["improvement_registry"] = registry
        _write_json_atomic(variant_json_path, payload)
    _write_json_atomic(monitor_json_path, monitor)

    return {
        "exit_code": int(monitor.get("exit_code", 0) or 0),
        "monitor": monitor,
        "registry": registry,
        "outcome_report_path": outcome_json_path,
        "variant_report_path": variant_json_path if variant_report is not None else None,
        "monitor_json_path": monitor_json_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MFE/MAE/TP/SL期待値監視をcron/CI向けに一括実行する"
    )
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR_PATH)
    parser.add_argument(
        "--outcome-json",
        type=Path,
        default=DEFAULT_OUTCOME_REPORT_PATH,
        help="詳細な期待値監査JSONの保存先。none で無効化",
    )
    parser.add_argument(
        "--variant-json",
        type=Path,
        default=DEFAULT_VARIANT_REPORT_PATH,
        help="TP/SL候補paper再採点JSONの保存先。none で無効化",
    )
    parser.add_argument(
        "--no-registry-update",
        action="store_true",
        help="改善候補レジストリを更新せず監視JSONだけ生成する",
    )
    parser.add_argument(
        "--no-variant-retest",
        action="store_true",
        help="TP/SL候補のpaper再採点をスキップする",
    )
    parser.add_argument(
        "--health-require-sample",
        action="store_true",
        help="最低サンプル数未満をヘルスチェック失敗扱いにする",
    )
    parser.add_argument(
        "--tp1-r-candidates",
        type=float,
        nargs="+",
        default=None,
        help="paper再採点するTP1のR倍率候補",
    )
    parser.add_argument(
        "--tp2-r-candidates",
        type=float,
        nargs="+",
        default=None,
        help="paper再採点するTP2のR倍率候補",
    )
    parser.add_argument("--quiet", action="store_true", help="標準出力の要約を抑制する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_trade_outcome_monitor(
        journal_path=args.journal,
        registry_path=args.registry,
        monitor_json_path=args.monitor_json,
        outcome_json_path=_optional_path(args.outcome_json),
        variant_json_path=_optional_path(args.variant_json),
        update_registry=not args.no_registry_update,
        retest_variants=not args.no_variant_retest,
        require_sample_ok=args.health_require_sample,
        target1_r_candidates=args.tp1_r_candidates,
        target2_r_candidates=args.tp2_r_candidates,
    )
    if not args.quiet:
        _print_summary(result)
    return int(result["exit_code"])


def _print_summary(result: Mapping[str, Any]) -> None:
    monitor = result.get("monitor")
    if not isinstance(monitor, Mapping):
        print("trade outcome monitor: no monitor payload")
        return
    registry = monitor.get("registry")
    runner = monitor.get("runner")
    counts = registry if isinstance(registry, Mapping) else {}
    run = runner if isinstance(runner, Mapping) else {}
    print(
        "trade outcome monitor: "
        f"status={monitor.get('status')} exit={monitor.get('exit_code')} "
        f"outcomes={run.get('outcome_count', 0)} "
        f"findings={run.get('finding_count', 0)} "
        f"paper_ready={counts.get('paper_ready_count', 0)} "
        f"approved={counts.get('approved_count', 0)} "
        f"auto_paused={counts.get('auto_paused_count', 0)}"
    )
    print(f"monitor_json={result.get('monitor_json_path')}")
    if result.get("outcome_report_path"):
        print(f"outcome_json={result.get('outcome_report_path')}")
    if result.get("variant_report_path"):
        print(f"variant_json={result.get('variant_report_path')}")


def _optional_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    if str(path).strip().lower() == "none":
        return None
    return Path(path)


def _variant_baseline_evaluated(report: Mapping[str, Any] | None) -> int:
    if not isinstance(report, Mapping):
        return 0
    baseline = report.get("baseline")
    overall = baseline.get("overall") if isinstance(baseline, Mapping) else None
    if not isinstance(overall, Mapping):
        return 0
    value = overall.get("evaluated", 0)
    return int(value) if isinstance(value, int | float) else 0


def _variant_monitor_summary(
    report: Mapping[str, Any] | None,
    candidates: Sequence[trade_outcome.TradeImprovementCandidate],
) -> dict[str, Any]:
    if report is None:
        return {"enabled": False}
    variants = report.get("variants")
    best = report.get("best")
    return {
        "enabled": True,
        "baseline_evaluated": _variant_baseline_evaluated(report),
        "variant_count": len(variants) if isinstance(variants, Sequence) else 0,
        "candidate_count": len(candidates),
        "best": dict(best) if isinstance(best, Mapping) else None,
        "cell_count": _variant_cell_count(report),
    }


def _variant_cell_count(report: Mapping[str, Any]) -> int:
    cells = report.get("cells")
    if not isinstance(cells, Mapping):
        return 0
    return sum(len(grouped) for grouped in cells.values() if isinstance(grouped, Mapping))


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
