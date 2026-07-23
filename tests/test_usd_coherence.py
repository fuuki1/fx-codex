"""USDファクター整合監査(観測専用)のテスト。ネットワーク不要。

動機となった実測(2026-07-16): 3ペア同時longの提示は
USDJPY long=USD強 ∧ EURUSD/GBPUSD long=USD弱 の内部矛盾したUSD観で、
USD全面高の日にEURUSD/GBPUSDのlongが全敗した。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fx_intel import journal, usd_coherence
from fx_intel.journal import (
    COUNTERFACTUAL_ENTRY_KEY,
    blocked_gate_names,
    counterfactual_guard_entries,
)


def _entry(
    symbol: str,
    direction: str,
    conviction: int,
    analysis_direction: str | None = None,
    analysis_conviction: int | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "conviction": conviction,
        "analysis_direction": analysis_direction if analysis_direction is not None else direction,
        "analysis_conviction": (
            analysis_conviction if analysis_conviction is not None else conviction
        ),
    }


def test_usd_stance_mapping() -> None:
    assert usd_coherence.usd_stance("USDJPY", "long") == 1
    assert usd_coherence.usd_stance("USDJPY", "short") == -1
    assert usd_coherence.usd_stance("EURUSD", "long") == -1
    assert usd_coherence.usd_stance("GBPUSD", "short") == 1
    assert usd_coherence.usd_stance("USD/JPY", "long") == 1
    # USDを含まないクロスと無方向は対象外
    assert usd_coherence.usd_stance("EURJPY", "long") == 0
    assert usd_coherence.usd_stance("USDJPY", "neutral") == 0
    assert usd_coherence.usd_stance("USDJPY", "standby") == 0
    assert usd_coherence.usd_stance("USDJPY", None) == 0


def test_20260716_scenario_detects_contradiction_and_minority() -> None:
    """3ペア同時long: 確信度加重の多数派はUSD弱(-20-27+27)、少数派USDJPYが減衰候補。"""
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "long", 27),
            _entry("EURUSD", "long", 20),
            _entry("GBPUSD", "long", 27),
        ]
    )
    recommended = report["recommended"]
    assert recommended["contradiction"] is True
    assert recommended["aggregate_score"] == -20
    assert recommended["would_dampen"] == ["USDJPY"]
    assert recommended["stances"]["USDJPY"]["stance"] == 1
    assert recommended["stances"]["EURUSD"]["stance"] == -1
    warning = usd_coherence.format_warning_ja(report)
    assert "USD観の内部矛盾" in warning
    assert "USDJPY" in warning


def test_coherent_usd_view_has_no_contradiction() -> None:
    """USDJPY long + EURUSD/GBPUSD short = 全員USD強で整合。"""
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "long", 40),
            _entry("EURUSD", "short", 30),
            _entry("GBPUSD", "short", 20),
        ]
    )
    assert report["recommended"]["contradiction"] is False
    assert report["recommended"]["would_dampen"] == []
    assert usd_coherence.format_warning_ja(report) == ""


def test_gated_era_audits_analysis_track() -> None:
    """推奨が全てneutral化されても、ゲート前のanalysis側で矛盾を観測し続ける。"""
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "neutral", 0, analysis_direction="long", analysis_conviction=43),
            _entry("EURUSD", "neutral", 0, analysis_direction="long", analysis_conviction=25),
            _entry("GBPUSD", "neutral", 0, analysis_direction="neutral", analysis_conviction=0),
        ]
    )
    assert report["recommended"]["stances"] == {}
    assert report["recommended"]["contradiction"] is False
    analysis = report["analysis"]
    assert analysis["contradiction"] is True
    assert analysis["aggregate_score"] == 43 - 25
    assert analysis["would_dampen"] == ["EURUSD"]


def test_tie_reports_contradiction_without_dampen_proposal() -> None:
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "long", 20),
            _entry("EURUSD", "long", 20),
        ]
    )
    assert report["recommended"]["contradiction"] is True
    assert report["recommended"]["aggregate_score"] == 0
    assert report["recommended"]["would_dampen"] == []


def test_zero_conviction_direction_still_counts_with_floor_weight() -> None:
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "long", 0),
            _entry("EURUSD", "long", 5),
        ]
    )
    recommended = report["recommended"]
    assert recommended["contradiction"] is True
    # USDJPYは重み1に底上げされ、多数派はUSD弱(+1-5=-4)
    assert recommended["aggregate_score"] == -4
    assert recommended["would_dampen"] == ["USDJPY"]


def test_plan_trace_is_observed_only_and_none_without_stance() -> None:
    report = usd_coherence.audit_usd_coherence(
        [
            _entry("USDJPY", "long", 30),
            _entry("EURUSD", "neutral", 0, analysis_direction="neutral", analysis_conviction=0),
        ]
    )
    trace = usd_coherence.plan_trace(report, "USDJPY")
    assert trace is not None
    assert trace["gate"] == usd_coherence.GATE_NAME
    assert trace["status"] == "observed"
    assert trace["applied"] is False
    assert trace["recommended"]["stance"] == 1
    # スタンスを持たないペアにはtraceを付けない
    assert usd_coherence.plan_trace(report, "EURUSD") is None


def test_observed_trace_does_not_break_guard_counterfactual_eligibility() -> None:
    """観測traceはblocked扱いされず、期待値ガード反実仮想の適格性に影響しない。"""
    report = usd_coherence.audit_usd_coherence([_entry("GBPUSD", "neutral", 0, "long", 24)])
    usd_trace = usd_coherence.plan_trace(report, "GBPUSD")
    assert usd_trace is not None
    ts = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    entry = {
        "ts": ts.isoformat(),
        "prediction_time": ts.isoformat(),
        "source_cutoff": (ts - timedelta(minutes=2)).isoformat(),
        "max_feature_available_time": (ts - timedelta(seconds=1)).isoformat(),
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": "decision-gbpusd",
        "mode": "fusion",
        "producer": journal.FUSION_PRODUCER,
        "producer_version": journal.FUSION_PRODUCER_VERSION,
        "input_context_id": "context-gbpusd",
        "source_record_ids": ["source-gbpusd"],
        "symbol": "GBPUSD",
        "direction": "neutral",
        "conviction": 0,
        "analysis_direction": "long",
        "analysis_conviction": 24,
        "pre_guard_direction": "long",
        "pre_guard_conviction": 24,
        "pre_guard_stop": 1.335,
        "pre_guard_target1": 1.355,
        "pre_guard_target2": 1.365,
        "pre_guard_target_policy": {
            "policy_id": "default-atr-v1",
            "target1_r": 1.0,
            "target2_r": 2.0,
        },
        "pre_guard_cost_model_id": "scanner-proxy-mid-diagnostic-v1",
        "pre_guard_execution_snapshot": {"cost_model_id": "scanner-proxy-mid-diagnostic-v1"},
        "close": 1.345,
        "atr": 0.004,
        "gate_trace": [
            {"gate": "expectancy_guard", "status": "blocked"},
            usd_trace,
        ],
        "shadow_predictions": [
            {
                "producer": "fusion_raw",
                "direction": "long",
                "eligible_for_scoring": True,
                "stop": 1.335,
                "target1": 1.355,
                "target2": 1.365,
                "target_policy": {"policy_id": "shadow-default-atr-v1"},
            }
        ],
    }
    assert blocked_gate_names(entry) == {"expectancy_guard"}
    synthesized = counterfactual_guard_entries([entry])
    assert len(synthesized) == 1
    assert synthesized[0][COUNTERFACTUAL_ENTRY_KEY] is True
