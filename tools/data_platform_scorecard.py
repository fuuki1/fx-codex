#!/usr/bin/env python3
"""Machine-judged data-platform empirical scorecard.

The score is earned only from evidence artifacts and prospective operational
reports. Code existence, test count and copied historical reports earn no
points. Malformed or contradictory evidence fails closed.

Sections (100 total):
- trading market data: 30
- dual-source verification: 15
- macro/event PIT: 15
- continuous operation: 20
- reproducibility/audit: 10
- fault tolerance/secrets: 10
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

SCORECARD_VERSION = "1.1.0"
REQUIRED_PAIRS = 3
REQUIRED_TRADING_DAYS = 30


class ScorecardInputError(RuntimeError):
    """Evidence could not be evaluated safely."""


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScorecardInputError(f"unreadable evidence file {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ScorecardInputError(f"evidence file {path} must contain a JSON object")
    return payload


def _load_daily_reports(operations_dir: Path | None) -> list[dict[str, Any]]:
    if operations_dir is None:
        return []
    if not operations_dir.is_dir():
        raise ScorecardInputError(f"operations directory not found: {operations_dir}")

    reports: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for path in sorted(operations_dir.glob("daily_report_*.json")):
        payload = _load_json(path)
        assert payload is not None
        report_date = payload.get("report_date")
        if not isinstance(report_date, str):
            raise ScorecardInputError(f"daily report missing report_date: {path}")
        try:
            date.fromisoformat(report_date)
        except ValueError as error:
            raise ScorecardInputError(
                f"daily report has invalid report_date {report_date!r}: {path}"
            ) from error
        expected_name = f"daily_report_{report_date}.json"
        if path.name != expected_name:
            raise ScorecardInputError(
                f"daily report filename/date mismatch: {path.name} != {expected_name}"
            )
        if report_date in seen_dates:
            raise ScorecardInputError(f"duplicate daily report date: {report_date}")
        seen_dates.add(report_date)
        reports.append(payload)
    return reports


@dataclass
class Evidence:
    bundle_dir: Path
    collection: dict[str, Any] | None
    quality: dict[str, Any] | None
    divergence: dict[str, Any] | None
    macro: dict[str, Any] | None
    replay: dict[str, Any] | None
    incidents: dict[str, Any] | None
    secrets: dict[str, Any] | None
    fault_injection: dict[str, Any] | None
    reproduction: dict[str, Any] | None
    daily_reports: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, bundle_dir: Path, operations_dir: Path | None) -> Evidence:
        if not bundle_dir.is_dir():
            raise ScorecardInputError(f"evidence bundle directory not found: {bundle_dir}")
        return cls(
            bundle_dir=bundle_dir,
            collection=_load_json(bundle_dir / "collection_summary.json"),
            quality=_load_json(bundle_dir / "quality_report.json"),
            divergence=_load_json(bundle_dir / "divergence_report.json"),
            macro=_load_json(bundle_dir / "macro_pit_report.json"),
            replay=_load_json(bundle_dir / "replay_report.json"),
            incidents=_load_json(bundle_dir / "incident_report.json"),
            secrets=_load_json(bundle_dir / "secrets_scan.json"),
            fault_injection=_load_json(bundle_dir / "fault_injection_report.json"),
            reproduction=_load_json(bundle_dir / "independent_reproduction.json"),
            daily_reports=_load_daily_reports(operations_dir),
        )


@dataclass(frozen=True)
class Award:
    section: str
    points: float
    max_points: float
    reason: str
    evidence_path: str


@dataclass(frozen=True)
class Cap:
    limit: float
    reason: str


@dataclass(frozen=True)
class Fatal:
    reason: str
    evidence_path: str


class ScoreBuilder:
    def __init__(self) -> None:
        self.awards: list[Award] = []
        self.unmet: list[str] = []
        self.caps: list[Cap] = []
        self.fatals: list[Fatal] = []
        self.evidence_paths: set[str] = set()

    def award(self, section: str, points: float, maximum: float, reason: str, path: str) -> None:
        bounded = max(0.0, min(points, maximum))
        self.awards.append(Award(section, bounded, maximum, reason, path))
        if path:
            self.evidence_paths.add(path)

    def miss(self, section: str, maximum: float, reason: str) -> None:
        self.awards.append(Award(section, 0.0, maximum, reason, ""))
        self.unmet.append(f"[{section}] {reason}")

    def cap(self, limit: float, reason: str) -> None:
        self.caps.append(Cap(limit, reason))

    def fatal(self, reason: str, path: str) -> None:
        self.fatals.append(Fatal(reason, path))


def _check_fatals(ev: Evidence, sb: ScoreBuilder) -> None:
    quality = ev.quality or {}
    if int(quality.get("future_timestamp_accepted", 0)) > 0:
        sb.fatal("future-data violation: future timestamps accepted", "quality_report.json")
    if int(quality.get("raw_hash_mismatch_count", 0)) > 0:
        sb.fatal("raw hash mismatch detected", "quality_report.json")
    if int(quality.get("stale_used_as_tradable_count", 0)) > 0:
        sb.fatal("stale quote treated as tradable", "quality_report.json")
    collection = ev.collection or {}
    if collection.get("synthetic_or_replay_counted_as_real") is True:
        sb.fatal("synthetic/replay rows counted as real connection", "collection_summary.json")
    secrets = ev.secrets or {}
    if int(secrets.get("leak_count", 0)) > 0:
        sb.fatal("secret leakage detected", "secrets_scan.json")
    if ev.replay is not None and ev.replay.get("status") == "mismatch":
        sb.fatal("deterministic replay mismatch on real data", "replay_report.json")


def _real_sources(collection: dict[str, Any]) -> list[dict[str, Any]]:
    sources = collection.get("sources", [])
    if not isinstance(sources, list):
        return []
    return [
        source
        for source in sources
        if isinstance(source, dict)
        and source.get("synthetic") is not True
        and source.get("replay_fixture") is not True
    ]


def _score_market_data(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "trading_market_data"
    if ev.collection is None:
        sb.miss(section, 30, "collection_summary.json missing — no market-data evidence")
        sb.cap(65, "real broker bid/ask: 0 quotes (no collection evidence)")
        sb.cap(75, "no live market data evidence")
        return

    bidask = [
        source
        for source in _real_sources(ev.collection)
        if source.get("has_bid_ask") is True and int(source.get("quote_count", 0)) > 0
    ]
    live = [
        source
        for source in bidask
        if source.get("collection_mode") == "live_stream"
        and source.get("account_environment") not in ("practice", "demo")
    ]
    demo_live = [
        source
        for source in bidask
        if source.get("collection_mode") == "live_stream" and source not in live
    ]
    historical = [source for source in bidask if source.get("collection_mode") == "historical_download"]
    total_quotes = sum(int(source.get("quote_count", 0)) for source in bidask)
    pairs = {str(pair) for source in bidask for pair in source.get("instruments", [])}

    if total_quotes <= 0:
        sb.cap(65, "real broker bid/ask: 0 quotes")
    if not live:
        sb.cap(75, "no live market data (historical download and/or demo only)")
    if demo_live and not live:
        sb.cap(90, "practice/demo live stream only")

    if live:
        live_quotes = sum(int(source.get("quote_count", 0)) for source in live)
        providers = sorted(str(source.get("provider")) for source in live)
        sb.award(
            section,
            15,
            15,
            f"live non-demo broker stream: {live_quotes} quotes from {providers}",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 15, "no live non-demo broker bid/ask stream collected")

    if total_quotes > 0:
        modes = sorted({str(source.get("collection_mode")) for source in bidask})
        sb.award(
            section,
            7,
            7,
            f"real bid/ask quotes ingested: {total_quotes} (modes: {modes})",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 7, "no real bid/ask quotes ingested")

    if len(pairs) >= REQUIRED_PAIRS:
        sb.award(section, 4, 4, f"pair coverage: {sorted(pairs)}", "collection_summary.json")
    elif pairs:
        sb.award(
            section,
            4 * len(pairs) / REQUIRED_PAIRS,
            4,
            f"partial pair coverage: {sorted(pairs)}",
            "collection_summary.json",
        )
        sb.unmet.append(f"[{section}] fewer than {REQUIRED_PAIRS} pairs with real bid/ask")
    else:
        sb.miss(section, 4, "no pairs with real bid/ask")

    honest_sizes = bidask and all(
        source.get("sizes_present") is True or source.get("sizes_flagged_absent") is True
        for source in bidask
    )
    if honest_sizes:
        sb.award(
            section,
            2,
            2,
            "bid/ask sizes present or honestly flagged provider_does_not_supply_*",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 2, "size fields neither present nor flagged absent")

    if bidask and all(source.get("raw_first_verified") is True for source in bidask):
        sb.award(
            section,
            2,
            2,
            "raw-first storage verified before normalization",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 2, "raw-first storage not verified for all real sources")


def _score_dual_source(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "dual_source_verification"
    report = ev.divergence
    if report is None:
        sb.miss(section, 15, "divergence_report.json missing")
        sb.cap(90, "no independent second source comparison")
        return

    providers = report.get("providers", [])
    independent = (
        isinstance(providers, list)
        and len({str(provider) for provider in providers}) >= 2
        and report.get("providers_independent") is True
        and report.get("all_inputs_real") is True
    )
    if independent:
        sb.award(
            section,
            8,
            8,
            f"independent providers compared: {sorted(str(p) for p in providers)}",
            "divergence_report.json",
        )
    else:
        sb.miss(section, 8, "no two independent real-data providers compared")
        sb.cap(90, "no independent second source (same provider or non-real inputs)")

    pairs = {str(pair) for pair in report.get("instruments", [])}
    if len(pairs) >= REQUIRED_PAIRS:
        sb.award(section, 3, 3, f"compared pairs: {sorted(pairs)}", "divergence_report.json")
    elif pairs:
        sb.award(
            section,
            3 * len(pairs) / REQUIRED_PAIRS,
            3,
            f"partial pairs: {sorted(pairs)}",
            "divergence_report.json",
        )
    else:
        sb.miss(section, 3, "no instruments compared")

    metrics = report.get("metrics", {})
    wanted = {"mid_diff_pips", "spread_diff_pips", "receive_time_skew_ms"}
    measured = (
        isinstance(metrics, dict)
        and wanted.issubset(metrics)
        and all(isinstance(metrics[name], dict) and bool(metrics[name]) for name in wanted)
    )
    if measured:
        sb.award(section, 2, 2, "required divergence metrics measured", "divergence_report.json")
    else:
        sb.miss(section, 2, f"divergence metrics not all measured (need {sorted(wanted)})")

    if report.get("breach_policy_exercised") is True:
        sb.award(
            section,
            2,
            2,
            "breach policy exercised without averaging",
            "divergence_report.json",
        )
    else:
        sb.miss(section, 2, "divergence breach policy never exercised")


def _score_macro_pit(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "macro_event_pit"
    report = ev.macro
    if report is None:
        sb.miss(section, 15, "macro_pit_report.json missing")
        sb.cap(95, "no macro/event PIT evidence")
        return

    real_capture = report.get("real_data") is True and int(report.get("record_count", 0)) > 0
    if real_capture:
        sb.award(
            section,
            8,
            8,
            f"real macro records: {report.get('record_count')} from {report.get('provider')}",
            "macro_pit_report.json",
        )
    else:
        sb.miss(section, 8, "no real macro records captured")
        sb.cap(95, "no macro/event PIT evidence (no real records)")

    if report.get("as_of_query_verified") is True:
        sb.award(
            section,
            4,
            4,
            "as-of query verified on real data",
            "macro_pit_report.json",
        )
    else:
        sb.miss(section, 4, "as-of query not verified on real data")

    if report.get("revision_separation_verified") is True:
        sb.award(
            section,
            3,
            3,
            "initial and revised values stored separately",
            "macro_pit_report.json",
        )
    else:
        sb.miss(section, 3, "revision separation not verified")


def _score_operations(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "continuous_operation"
    valid_days = sum(
        1
        for report in ev.daily_reports
        if report.get("qualifying_day") is True
        and report.get("prospective_window_ok") is True
        and report.get("raw_hash_verified") is True
        and report.get("replay_ok") is True
        and int(report.get("critical_incidents", 0)) == 0
        and report.get("primary_up") is True
        and report.get("secondary_up") is True
    )

    if valid_days >= REQUIRED_TRADING_DAYS:
        sb.award(
            section,
            20,
            20,
            f"{valid_days} unique prospective qualifying trading days",
            "daily_report_*.json",
        )
    elif valid_days > 0:
        sb.award(
            section,
            20 * valid_days / REQUIRED_TRADING_DAYS,
            20,
            f"only {valid_days}/{REQUIRED_TRADING_DAYS} prospective qualifying days",
            "daily_report_*.json",
        )
        sb.cap(85, f"fewer than {REQUIRED_TRADING_DAYS} trading days ({valid_days})")
    else:
        sb.miss(section, 20, "no prospective qualifying days of continuous operation")
        sb.cap(85, f"fewer than {REQUIRED_TRADING_DAYS} trading days (0)")


def _score_reproducibility(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "reproducibility_audit"
    if ev.replay is not None and ev.replay.get("status") == "match" and ev.replay.get("real_data") is True:
        sb.award(
            section,
            5,
            5,
            f"deterministic replay on real data ({str(ev.replay.get('result_sha256'))[:12]}…)",
            "replay_report.json",
        )
    else:
        sb.miss(section, 5, "no deterministic replay match on real data")

    reproduction_ok = (
        ev.reproduction is not None
        and ev.reproduction.get("status") == "match"
        and ev.reproduction.get("real_data") is True
        and ev.reproduction.get("independent_environment") is True
    )
    if reproduction_ok:
        sb.award(
            section,
            5,
            5,
            "independent-environment reproduction matched on real data",
            "independent_reproduction.json",
        )
    else:
        sb.miss(section, 5, "no independent-environment reproduction on real data")


def _score_fault_and_secrets(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "fault_tolerance_secrets"
    fault = ev.fault_injection
    scenarios = fault.get("scenarios", []) if fault is not None else []
    if not isinstance(scenarios, list) or not scenarios:
        sb.miss(section, 6, "no fault-injection scenarios recorded")
    else:
        passed = [
            scenario
            for scenario in scenarios
            if isinstance(scenario, dict) and scenario.get("outcome") == "pass"
        ]
        fraction = len(passed) / len(scenarios)
        sb.award(
            section,
            6 * fraction,
            6,
            f"fault injection: {len(passed)}/{len(scenarios)} scenarios fail-closed",
            "fault_injection_report.json",
        )
        if fraction < 1.0:
            sb.unmet.append(f"[{section}] {len(scenarios) - len(passed)} scenarios failing")

    if ev.secrets is not None and int(ev.secrets.get("leak_count", -1)) == 0:
        sb.award(section, 2, 2, "secrets scan clean (0 leaks)", "secrets_scan.json")
    else:
        sb.miss(section, 2, "no clean secrets scan evidence")

    if ev.secrets is not None and ev.secrets.get("no_order_path_verified") is True:
        sb.award(
            section,
            2,
            2,
            "collector verified to import no order/executor path",
            "secrets_scan.json",
        )
    else:
        sb.miss(section, 2, "no-order-path isolation not verified")


def _section_totals(sb: ScoreBuilder) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {}
    for award in sb.awards:
        entry = totals.setdefault(award.section, {"points": 0.0, "max_points": 0.0})
        entry["points"] = round(entry["points"] + award.points, 2)
        entry["max_points"] = round(entry["max_points"] + award.max_points, 2)
    return totals


def compute_scorecard(evidence: Evidence) -> dict[str, Any]:
    sb = ScoreBuilder()
    _check_fatals(evidence, sb)
    _score_market_data(evidence, sb)
    _score_dual_source(evidence, sb)
    _score_macro_pit(evidence, sb)
    _score_operations(evidence, sb)
    _score_reproducibility(evidence, sb)
    _score_fault_and_secrets(evidence, sb)

    raw_score = sum(award.points for award in sb.awards)
    maximum = sum(award.max_points for award in sb.awards)
    cap = min((item.limit for item in sb.caps), default=100.0)
    if sb.fatals:
        status = "failed"
        score = 0.0
    else:
        score = min(raw_score, cap)
        status = "capped" if sb.caps or score < raw_score else "ok"

    return {
        "scorecard_version": SCORECARD_VERSION,
        "status": status,
        "score": round(score, 2),
        "raw_score": round(raw_score, 2),
        "max_score": round(maximum, 2),
        "hard_cap": cap,
        "hard_cap_reasons": [{"limit": item.limit, "reason": item.reason} for item in sb.caps],
        "fatal_reasons": [
            {"reason": item.reason, "evidence": item.evidence_path} for item in sb.fatals
        ],
        "sections": _section_totals(sb),
        "awards": [
            {
                "section": item.section,
                "points": round(item.points, 2),
                "max_points": item.max_points,
                "reason": item.reason,
                "evidence": item.evidence_path,
            }
            for item in sb.awards
        ],
        "unmet_conditions": sb.unmet,
        "evidence_paths": sorted(sb.evidence_paths),
        "evidence_bundle": str(evidence.bundle_dir),
        "daily_report_count": len(evidence.daily_reports),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# Data platform scorecard v{result['scorecard_version']}",
        "",
        f"**status: {result['status']} — score {result['score']} / 100**"
        f" (raw {result['raw_score']}, hard cap {result['hard_cap']})",
        "",
        "| section | points | max |",
        "|---|---:|---:|",
    ]
    for name, entry in result["sections"].items():
        lines.append(f"| {name} | {entry['points']} | {entry['max_points']} |")
    if result["hard_cap_reasons"]:
        lines.extend(["", "## Hard caps", ""])
        lines.extend(
            f"- **≤{item['limit']}**: {item['reason']}" for item in result["hard_cap_reasons"]
        )
    if result["fatal_reasons"]:
        lines.extend(["", "## FATAL", ""])
        lines.extend(
            f"- {item['reason']} ({item['evidence']})" for item in result["fatal_reasons"]
        )
    if result["unmet_conditions"]:
        lines.extend(["", "## Unmet conditions", ""])
        lines.extend(f"- {item}" for item in result["unmet_conditions"])
    lines.extend(["", "## Award basis", ""])
    for item in result["awards"]:
        if item["points"] > 0:
            lines.append(
                f"- [{item['section']}] +{item['points']}/{item['max_points']}: "
                f"{item['reason']} ({item['evidence']})"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--operations-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compute_scorecard(Evidence.load(args.evidence_dir, args.operations_dir))
    except ScorecardInputError as error:
        print(json.dumps({"status": "error", "detail": str(error)}))
        return 1
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.md_out:
        args.md_out.write_text(render_markdown(result), encoding="utf-8")
    print(payload)
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
