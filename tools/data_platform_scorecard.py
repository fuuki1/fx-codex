#!/usr/bin/env python3
"""Machine-judged data-platform empirical scorecard.

Scores the data platform **only from evidence artifacts and operational
metrics** — never from code existence, line counts, or test counts. Every
awarded point cites the evidence file it was computed from; every unmet
condition and every hard cap is listed with its reason. The intent is that a
human cannot argue with the number without arguing with the evidence.

Sections (100 total)
    trading market data ............ 30
    dual-source verification ....... 15
    macro / event / news PIT ....... 15
    continuous operation ........... 20
    reproducibility / audit ........ 10
    fault tolerance / secrets ...... 10

Hard caps (the minimum applicable cap wins; fatal conditions zero the score):
    no real broker bid/ask quotes ............ cap 65
    no live market data (historical only) .... cap 75
    fewer than 30 trading days ............... cap 85
    practice/demo data only .................. cap 90
    no independent second source ............. cap 90
    no macro/event PIT evidence .............. cap 95
    future-data violation .................... score 0, status=failed
    raw hash mismatch ........................ score 0, status=failed
    stale quote used as tradable ............. score 0, status=failed
    synthetic/replay counted as real ......... score 0, status=failed
    secret leakage ........................... score 0, status=failed

Usage:
    python3 -m tools.data_platform_scorecard --evidence-dir <bundle> \
        [--operations-dir <daily-report-dir>] [--json-out X] [--md-out Y]

Exit codes: 0 = computed (status ok/capped), 2 = fatal (status failed),
1 = scorecard itself could not run (missing/invalid inputs).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from typing import Any

SCORECARD_VERSION = "1.0.0"
REQUIRED_PAIRS = 3
REQUIRED_TRADING_DAYS = 30

# ---------------------------------------------------------------------------
# evidence loading


class ScorecardInputError(RuntimeError):
    """Raised when the scorecard cannot even evaluate (not a low score)."""


def _load_json(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON or None when absent. Malformed files are an error:
    silently treating them as absent could hide tampering."""

    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ScorecardInputError(f"unreadable evidence file {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ScorecardInputError(f"evidence file {path} must contain a JSON object")
    return payload


@dataclass
class Evidence:
    """Typed view over one evidence bundle + optional operations directory."""

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
        daily: list[dict[str, Any]] = []
        if operations_dir is not None:
            if not operations_dir.is_dir():
                raise ScorecardInputError(f"operations directory not found: {operations_dir}")
            for report in sorted(operations_dir.glob("daily_report_*.json")):
                loaded = _load_json(report)
                if loaded is not None:
                    daily.append(loaded)
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
            daily_reports=daily,
        )


# ---------------------------------------------------------------------------
# scoring primitives


@dataclass
class Award:
    section: str
    points: float
    max_points: float
    reason: str
    evidence_path: str


@dataclass
class Cap:
    limit: float
    reason: str


@dataclass
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

    def award(self, section: str, points: float, max_points: float, reason: str, path: str) -> None:
        points = max(0.0, min(points, max_points))
        self.awards.append(Award(section, points, max_points, reason, path))
        if path:
            self.evidence_paths.add(path)

    def miss(self, section: str, max_points: float, reason: str) -> None:
        self.awards.append(Award(section, 0.0, max_points, reason, ""))
        self.unmet.append(f"[{section}] {reason}")

    def cap(self, limit: float, reason: str) -> None:
        self.caps.append(Cap(limit, reason))

    def fatal(self, reason: str, path: str) -> None:
        self.fatals.append(Fatal(reason, path))


# ---------------------------------------------------------------------------
# fatal checks (any true -> score 0 / status failed)


def _check_fatals(ev: Evidence, sb: ScoreBuilder) -> None:
    quality = ev.quality or {}
    if int(quality.get("future_timestamp_accepted", 0)) > 0:
        sb.fatal("future-data violation: future timestamps accepted", "quality_report.json")
    if int(quality.get("raw_hash_mismatch_count", 0)) > 0:
        sb.fatal("raw hash mismatch detected", "quality_report.json")
    if int(quality.get("stale_used_as_tradable_count", 0)) > 0:
        sb.fatal("stale quote treated as tradable", "quality_report.json")
    collection = ev.collection or {}
    if bool(collection.get("synthetic_or_replay_counted_as_real", False)):
        sb.fatal("synthetic/replay rows counted as real connection", "collection_summary.json")
    secrets = ev.secrets or {}
    if int(secrets.get("leak_count", 0)) > 0:
        sb.fatal("secret leakage detected", "secrets_scan.json")
    replay = ev.replay
    if replay is not None and replay.get("status") == "mismatch":
        sb.fatal("deterministic replay mismatch on real data", "replay_report.json")


# ---------------------------------------------------------------------------
# section: trading market data (30)


def _real_source_rows(collection: dict[str, Any]) -> list[dict[str, Any]]:
    sources = collection.get("sources", [])
    if not isinstance(sources, list):
        return []
    real: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if bool(source.get("synthetic", False)) or bool(source.get("replay_fixture", False)):
            continue  # never count synthetic/replay as real
        real.append(source)
    return real


def _score_market_data(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "trading_market_data"
    collection = ev.collection
    if collection is None:
        sb.miss(section, 30, "collection_summary.json missing — no market-data evidence")
        sb.cap(65, "real broker bid/ask: 0 quotes (no collection evidence)")
        sb.cap(75, "no live market data evidence")
        return
    real_sources = _real_source_rows(collection)
    # A source only counts in ANY category with actually-collected quotes:
    # an implemented-but-unconnected adapter (quote_count 0) earns nothing.
    bidask = [
        s
        for s in real_sources
        if bool(s.get("has_bid_ask", False)) and int(s.get("quote_count", 0)) > 0
    ]
    live = [
        s
        for s in bidask
        if s.get("collection_mode") == "live_stream"
        and s.get("account_environment") not in ("practice", "demo")
    ]
    demo_live = [s for s in bidask if s.get("collection_mode") == "live_stream" and s not in live]
    historical = [s for s in bidask if s.get("collection_mode") == "historical_download"]
    total_quotes = sum(int(s.get("quote_count", 0)) for s in bidask)
    pairs = {str(p) for s in bidask for p in s.get("instruments", [])}

    if total_quotes <= 0:
        sb.cap(65, "real broker bid/ask: 0 quotes")
    if not live:
        sb.cap(75, "no live market data (historical download and/or demo only)")
        if demo_live and not historical:
            sb.cap(90, "practice/demo live stream only")

    # live stream from a non-demo broker (15)
    if live:
        live_quotes = sum(int(s.get("quote_count", 0)) for s in live)
        sb.award(
            section,
            15,
            15,
            f"live non-demo broker stream: {live_quotes} quotes from "
            f"{sorted(str(s.get('provider')) for s in live)}",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 15, "no live non-demo broker bid/ask stream collected")

    # real (non-synthetic) bid/ask quotes of any collection mode (7)
    if total_quotes > 0:
        sb.award(
            section,
            7,
            7,
            f"real bid/ask quotes ingested: {total_quotes} "
            f"(modes: {sorted({str(s.get('collection_mode')) for s in bidask})})",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 7, "no real bid/ask quotes ingested")

    # pair coverage (4)
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

    # honest field provenance: sizes present or explicitly flagged absent (2)
    if bidask and all(
        bool(s.get("sizes_present", False)) or bool(s.get("sizes_flagged_absent", False))
        for s in bidask
    ):
        sb.award(
            section,
            2,
            2,
            "bid/ask sizes present or honestly flagged provider_does_not_supply_*",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 2, "size fields neither present nor flagged absent")

    # raw-first verified for every real source (2)
    if bidask and all(bool(s.get("raw_first_verified", False)) for s in bidask):
        sb.award(
            section,
            2,
            2,
            "raw-first storage verified (raw sha256 recorded before normalization)",
            "collection_summary.json",
        )
    else:
        sb.miss(section, 2, "raw-first storage not verified for all real sources")


# ---------------------------------------------------------------------------
# section: dual-source verification (15)


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
        and len({str(p) for p in providers}) >= 2
        and bool(report.get("providers_independent", False))
    )
    real_inputs = bool(report.get("all_inputs_real", False))
    if not (independent and real_inputs):
        sb.miss(section, 8, "no two independent real-data providers compared")
        sb.cap(90, "no independent second source (same provider or non-real inputs)")
    else:
        sb.award(
            section,
            8,
            8,
            f"independent providers compared: {sorted(str(p) for p in providers)}",
            "divergence_report.json",
        )
    pairs = {str(p) for p in report.get("instruments", [])}
    if len(pairs) >= REQUIRED_PAIRS:
        sb.award(section, 3, 3, f"compared pairs: {sorted(pairs)}", "divergence_report.json")
    elif pairs:
        sb.award(section, 3 * len(pairs) / REQUIRED_PAIRS, 3, f"partial pairs: {sorted(pairs)}", "")
    else:
        sb.miss(section, 3, "no instruments compared")
    metrics = report.get("metrics", {})
    wanted = {"mid_diff_pips", "spread_diff_pips", "receive_time_skew_ms"}
    populated = (
        isinstance(metrics, dict)
        and wanted.issubset(metrics.keys())
        # a null metric is an honest "could not measure" — it earns nothing
        and all(isinstance(metrics[name], dict) and metrics[name] for name in wanted)
    )
    if populated:
        sb.award(section, 2, 2, "required divergence metrics measured", "divergence_report.json")
    else:
        sb.miss(section, 2, f"divergence metrics not all measured (need {sorted(wanted)})")
    if bool(report.get("breach_policy_exercised", False)):
        sb.award(
            section,
            2,
            2,
            "breach policy exercised (no averaging; degraded/quarantined transition observed)",
            "divergence_report.json",
        )
    else:
        sb.miss(section, 2, "divergence breach policy never exercised")


# ---------------------------------------------------------------------------
# section: macro / event PIT (15)


def _score_macro_pit(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "macro_event_pit"
    report = ev.macro
    if report is None:
        sb.miss(section, 15, "macro_pit_report.json missing")
        sb.cap(95, "no macro/event PIT evidence")
        return
    real_capture = bool(report.get("real_data", False)) and int(report.get("record_count", 0)) > 0
    if not real_capture:
        sb.miss(section, 8, "no real macro records captured")
        sb.cap(95, "no macro/event PIT evidence (no real records)")
    else:
        sb.award(
            section,
            8,
            8,
            f"real macro records: {report.get('record_count')} from "
            f"{report.get('provider')} (vintage_correct={report.get('vintage_correct')})",
            "macro_pit_report.json",
        )
    if bool(report.get("as_of_query_verified", False)):
        sb.award(
            section,
            4,
            4,
            "as-of query verified on real data (pre-availability blocked)",
            "macro_pit_report.json",
        )
    else:
        sb.miss(section, 4, "as-of query not verified on real data")
    if bool(report.get("revision_separation_verified", False)):
        sb.award(
            section,
            3,
            3,
            "initial vs revised values stored separately (vintage evidence)",
            "macro_pit_report.json",
        )
    else:
        sb.miss(section, 3, "revision separation not verified")


# ---------------------------------------------------------------------------
# section: continuous operation (20)


def _score_operations(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "continuous_operation"
    valid_days = 0
    for report in ev.daily_reports:
        ok = (
            bool(report.get("raw_hash_verified", False))
            and bool(report.get("replay_ok", False))
            and int(report.get("critical_incidents", 0)) == 0
            and bool(report.get("primary_up", False))
            and bool(report.get("secondary_up", False))
        )
        if ok:
            valid_days += 1
    if valid_days >= REQUIRED_TRADING_DAYS:
        sb.award(
            section,
            20,
            20,
            f"{valid_days} qualifying trading days of continuous collection",
            "daily_report_*.json",
        )
    elif valid_days > 0:
        sb.award(
            section,
            20 * valid_days / REQUIRED_TRADING_DAYS,
            20,
            f"only {valid_days}/{REQUIRED_TRADING_DAYS} qualifying trading days",
            "daily_report_*.json",
        )
        sb.cap(85, f"fewer than {REQUIRED_TRADING_DAYS} trading days ({valid_days})")
    else:
        sb.miss(section, 20, "no qualifying trading days of continuous operation")
        sb.cap(85, f"fewer than {REQUIRED_TRADING_DAYS} trading days (0)")


# ---------------------------------------------------------------------------
# section: reproducibility / audit (10)


def _score_reproducibility(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "reproducibility_audit"
    replay = ev.replay
    if replay is not None and replay.get("status") == "match" and bool(replay.get("real_data")):
        sb.award(
            section,
            5,
            5,
            f"deterministic replay on real data (hash {str(replay.get('result_sha256'))[:12]}…)",
            "replay_report.json",
        )
    else:
        sb.miss(section, 5, "no deterministic replay match on real data")
    repro = ev.reproduction
    if (
        repro is not None
        and repro.get("status") == "match"
        and bool(repro.get("real_data"))
        and bool(repro.get("independent_environment"))
    ):
        sb.award(
            section,
            5,
            5,
            "independent-environment reproduction matched on real data",
            "independent_reproduction.json",
        )
    else:
        sb.miss(section, 5, "no independent-environment reproduction on real data")


# ---------------------------------------------------------------------------
# section: fault tolerance / secrets (10)


def _score_fault_and_secrets(ev: Evidence, sb: ScoreBuilder) -> None:
    section = "fault_tolerance_secrets"
    fault = ev.fault_injection
    if fault is None:
        sb.miss(section, 6, "fault_injection_report.json missing")
    else:
        scenarios = fault.get("scenarios", [])
        if not isinstance(scenarios, list) or not scenarios:
            sb.miss(section, 6, "no fault-injection scenarios recorded")
        else:
            passed = [s for s in scenarios if isinstance(s, dict) and s.get("outcome") == "pass"]
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
    secrets = ev.secrets
    if secrets is not None and int(secrets.get("leak_count", -1)) == 0:
        sb.award(section, 2, 2, "secrets scan clean (0 leaks)", "secrets_scan.json")
    else:
        sb.miss(section, 2, "no clean secrets scan evidence")
    if secrets is not None and bool(secrets.get("no_order_path_verified", False)):
        sb.award(
            section,
            2,
            2,
            "collector verified to import no order/executor path",
            "secrets_scan.json",
        )
    else:
        sb.miss(section, 2, "no-order-path isolation not verified")


# ---------------------------------------------------------------------------
# assembly


def compute_scorecard(evidence: Evidence) -> dict[str, Any]:
    sb = ScoreBuilder()
    _check_fatals(evidence, sb)
    _score_market_data(evidence, sb)
    _score_dual_source(evidence, sb)
    _score_macro_pit(evidence, sb)
    _score_operations(evidence, sb)
    _score_reproducibility(evidence, sb)
    _score_fault_and_secrets(evidence, sb)

    raw_score = sum(a.points for a in sb.awards)
    max_score = sum(a.max_points for a in sb.awards)
    applicable_cap = min((c.limit for c in sb.caps), default=100.0)
    if sb.fatals:
        status = "failed"
        final_score = 0.0
    else:
        final_score = min(raw_score, applicable_cap)
        status = "capped" if final_score < raw_score or sb.caps else "ok"

    return {
        "scorecard_version": SCORECARD_VERSION,
        "status": status,
        "score": round(final_score, 2),
        "raw_score": round(raw_score, 2),
        "max_score": round(max_score, 2),
        "hard_cap": applicable_cap,
        "hard_cap_reasons": [{"limit": c.limit, "reason": c.reason} for c in sb.caps],
        "fatal_reasons": [{"reason": f.reason, "evidence": f.evidence_path} for f in sb.fatals],
        "sections": _section_totals(sb),
        "awards": [
            {
                "section": a.section,
                "points": round(a.points, 2),
                "max_points": a.max_points,
                "reason": a.reason,
                "evidence": a.evidence_path,
            }
            for a in sb.awards
        ],
        "unmet_conditions": sb.unmet,
        "evidence_paths": sorted(sb.evidence_paths),
        "evidence_bundle": str(evidence.bundle_dir),
        "daily_report_count": len(evidence.daily_reports),
    }


def _section_totals(sb: ScoreBuilder) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {}
    for award in sb.awards:
        entry = totals.setdefault(award.section, {"points": 0.0, "max_points": 0.0})
        entry["points"] = round(entry["points"] + award.points, 2)
        entry["max_points"] = round(entry["max_points"] + award.max_points, 2)
    return totals


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
        lines += ["", "## Hard caps", ""]
        for cap in result["hard_cap_reasons"]:
            lines.append(f"- **≤{cap['limit']}**: {cap['reason']}")
    if result["fatal_reasons"]:
        lines += ["", "## FATAL", ""]
        for fatal in result["fatal_reasons"]:
            lines.append(f"- {fatal['reason']} ({fatal['evidence']})")
    if result["unmet_conditions"]:
        lines += ["", "## Unmet conditions", ""]
        for item in result["unmet_conditions"]:
            lines.append(f"- {item}")
    lines += ["", "## Award basis", ""]
    for award in result["awards"]:
        if award["points"] > 0:
            lines.append(
                f"- [{award['section']}] +{award['points']}/{award['max_points']}: "
                f"{award['reason']} ({award['evidence']})"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--operations-dir", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        evidence = Evidence.load(args.evidence_dir, args.operations_dir)
        result = compute_scorecard(evidence)
    except ScorecardInputError as error:
        print(json.dumps({"status": "error", "detail": str(error)}))
        return 1
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(payload + "\n")
    if args.md_out:
        args.md_out.write_text(render_markdown(result))
    print(payload)
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
