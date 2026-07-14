"""Hard-cap and fatal-gate tests for the machine-judged data-platform scorecard.

The scorecard must award points only from evidence artifacts, apply every hard
cap the spec defines, and zero the score on fatal violations. These tests build
synthetic *evidence files* (JSON fixtures describing claimed evidence) — they
never claim the underlying data is real; the point is to verify the judge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.data_platform_scorecard import (
    Evidence,
    ScorecardInputError,
    compute_scorecard,
    render_markdown,
)


def _write(bundle: Path, name: str, payload: dict) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / name).write_text(json.dumps(payload))


def _full_green_bundle(bundle: Path, ops: Path) -> None:
    """Construct evidence that would legitimately score 100 (for gate tests)."""

    _write(
        bundle,
        "collection_summary.json",
        {
            "sources": [
                {
                    "provider": "broker-x",
                    "collection_mode": "live_stream",
                    "account_environment": "live",
                    "has_bid_ask": True,
                    "quote_count": 50_000,
                    "instruments": ["USDJPY", "EURUSD", "GBPUSD"],
                    "sizes_present": True,
                    "raw_first_verified": True,
                },
                {
                    "provider": "provider-y",
                    "collection_mode": "live_stream",
                    "account_environment": "live",
                    "has_bid_ask": True,
                    "quote_count": 48_000,
                    "instruments": ["USDJPY", "EURUSD", "GBPUSD"],
                    "sizes_flagged_absent": True,
                    "raw_first_verified": True,
                },
            ],
            "synthetic_or_replay_counted_as_real": False,
        },
    )
    _write(
        bundle,
        "quality_report.json",
        {
            "future_timestamp_accepted": 0,
            "raw_hash_mismatch_count": 0,
            "stale_used_as_tradable_count": 0,
        },
    )
    _write(
        bundle,
        "divergence_report.json",
        {
            "providers": ["broker-x", "provider-y"],
            "providers_independent": True,
            "all_inputs_real": True,
            "instruments": ["USDJPY", "EURUSD", "GBPUSD"],
            "metrics": {
                "mid_diff_pips": {"count": 9, "p50": 0.4, "max": 1.2},
                "spread_diff_pips": {"count": 9, "p50": 0.1, "max": 0.5},
                "receive_time_skew_ms": {"count": 9, "p50": 12.0, "max": 40.0},
            },
            "breach_policy_exercised": True,
        },
    )
    _write(
        bundle,
        "macro_pit_report.json",
        {
            "real_data": True,
            "record_count": 12,
            "provider": "alfred",
            "vintage_correct": True,
            "as_of_query_verified": True,
            "revision_separation_verified": True,
        },
    )
    _write(
        bundle,
        "replay_report.json",
        {"status": "match", "real_data": True, "result_sha256": "ab" * 32},
    )
    _write(
        bundle,
        "independent_reproduction.json",
        {"status": "match", "real_data": True, "independent_environment": True},
    )
    _write(
        bundle,
        "fault_injection_report.json",
        {"scenarios": [{"name": f"s{i}", "outcome": "pass"} for i in range(18)]},
    )
    _write(bundle, "secrets_scan.json", {"leak_count": 0, "no_order_path_verified": True})
    _write(bundle, "incident_report.json", {"critical_incidents": 0})
    ops.mkdir(parents=True, exist_ok=True)
    for day in range(1, 31):
        (ops / f"daily_report_2026-08-{day:02d}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "report_date": f"2026-08-{day:02d}",
                    "prospective_window_ok": True,
                    "qualifying_day": True,
                    "raw_hash_verified": True,
                    "replay_ok": True,
                    "critical_incidents": 0,
                    "primary_up": True,
                    "secondary_up": True,
                }
            )
        )


def test_empty_bundle_hits_every_absence_cap(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = compute_scorecard(Evidence.load(bundle, None))
    limits = {c["limit"] for c in result["hard_cap_reasons"]}
    assert {65, 75, 85, 90, 95}.issubset(limits)
    assert result["hard_cap"] == 65
    assert result["score"] <= 65
    assert result["status"] in ("capped", "ok")


def test_full_green_evidence_scores_100(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["status"] == "ok"
    assert result["hard_cap"] == 100.0
    assert result["score"] == 100.0
    markdown = render_markdown(result)
    assert "score 100.0 / 100" in markdown


def test_historical_only_capped_at_75(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    for source in summary["sources"]:
        source["collection_mode"] = "historical_download"
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] == 75
    assert result["score"] <= 75
    reasons = " ".join(c["reason"] for c in result["hard_cap_reasons"])
    assert "no live market data" in reasons


def test_demo_only_live_capped_at_90(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    for source in summary["sources"]:
        source["account_environment"] = "practice"
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] <= 90
    reasons = " ".join(c["reason"] for c in result["hard_cap_reasons"])
    assert "practice/demo" in reasons


def test_zero_quotes_capped_at_65(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    for source in summary["sources"]:
        source["quote_count"] = 0
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] == 65


def test_under_30_days_capped_at_85(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    for report in sorted(ops.glob("daily_report_*.json"))[10:]:
        report.unlink()
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] == 85
    assert result["score"] <= 85


def test_zero_quote_live_adapter_earns_nothing(tmp_path: Path) -> None:
    """An implemented-but-unconnected live adapter (quote_count=0) must not be
    treated as live market data — code existence never scores."""

    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    summary["sources"] = [
        {  # real historical quotes
            "provider": "dukascopy",
            "collection_mode": "historical_download",
            "account_environment": "datafeed",
            "has_bid_ask": True,
            "quote_count": 50_000,
            "instruments": ["USDJPY", "EURUSD", "GBPUSD"],
            "sizes_present": True,
            "raw_first_verified": True,
        },
        {  # implemented live adapter, ZERO quotes
            "provider": "oanda",
            "collection_mode": "live_stream",
            "account_environment": "live",
            "has_bid_ask": True,
            "quote_count": 0,
            "instruments": [],
            "sizes_flagged_absent": True,
            "raw_first_verified": True,
        },
    ]
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] <= 75  # no live market data
    live_awards = [a for a in result["awards"] if "live non-demo broker stream" in a["reason"]]
    assert live_awards == []  # zero-quote live earns nothing


def test_null_divergence_metrics_earn_nothing(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    report = json.loads((bundle / "divergence_report.json").read_text())
    report["metrics"]["spread_diff_pips"] = None  # honest could-not-measure
    _write(bundle, "divergence_report.json", report)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert any("not all measured" in u for u in result["unmet_conditions"])


def test_synthetic_sources_never_count(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    for source in summary["sources"]:
        source["synthetic"] = True
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["hard_cap"] == 65  # real quotes = 0 once synthetic excluded


@pytest.mark.parametrize(
    ("file_name", "payload", "expected"),
    [
        (
            "quality_report.json",
            {"future_timestamp_accepted": 1},
            "future-data violation",
        ),
        (
            "quality_report.json",
            {"raw_hash_mismatch_count": 2},
            "raw hash mismatch",
        ),
        (
            "quality_report.json",
            {"stale_used_as_tradable_count": 1},
            "stale quote treated as tradable",
        ),
        (
            "secrets_scan.json",
            {"leak_count": 3, "no_order_path_verified": True},
            "secret leakage",
        ),
    ],
)
def test_fatal_conditions_zero_the_score(
    tmp_path: Path, file_name: str, payload: dict, expected: str
) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    existing = json.loads((bundle / file_name).read_text())
    existing.update(payload)
    _write(bundle, file_name, existing)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["status"] == "failed"
    assert result["score"] == 0.0
    assert any(expected in f["reason"] for f in result["fatal_reasons"])


def test_synthetic_counted_as_real_is_fatal(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    summary = json.loads((bundle / "collection_summary.json").read_text())
    summary["synthetic_or_replay_counted_as_real"] = True
    _write(bundle, "collection_summary.json", summary)
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["status"] == "failed"
    assert result["score"] == 0.0


def test_replay_mismatch_is_fatal(tmp_path: Path) -> None:
    bundle, ops = tmp_path / "bundle", tmp_path / "ops"
    _full_green_bundle(bundle, ops)
    _write(bundle, "replay_report.json", {"status": "mismatch", "real_data": True})
    result = compute_scorecard(Evidence.load(bundle, ops))
    assert result["status"] == "failed"


def test_malformed_evidence_is_an_error_not_a_zero(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "collection_summary.json").write_text("{not json")
    with pytest.raises(ScorecardInputError):
        Evidence.load(bundle, None)


def test_points_come_only_from_evidence_files(tmp_path: Path) -> None:
    """No evidence files -> only misses; every award cites an evidence path."""

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = compute_scorecard(Evidence.load(bundle, None))
    assert result["raw_score"] == 0.0
    full = tmp_path / "full"
    ops = tmp_path / "ops"
    _full_green_bundle(full, ops)
    result_full = compute_scorecard(Evidence.load(full, ops))
    for award in result_full["awards"]:
        if award["points"] > 0:
            assert award["evidence"], f"award without evidence citation: {award}"
