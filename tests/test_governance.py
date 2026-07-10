from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fx_backtester.governance import (
    GovernanceError,
    ModelRecord,
    ModelRegistry,
    PromotionEvidence,
    PromotionPolicy,
    apply_hard_veto,
    evaluate_promotion,
)

NOW = datetime(2026, 7, 10, tzinfo=UTC)
HASH = "a" * 64


def _passing_evidence() -> PromotionEvidence:
    return PromotionEvidence(
        dataset_hash=HASH,
        git_commit="1234567890abcdef",
        dirty_worktree=False,
        synthetic_data=False,
        point_in_time_violations=0,
        future_feature_violations=0,
        trial_count=40,
        sample_count=600,
        net_expectancy_r=0.15,
        expectancy_ci_lower_r=0.02,
        dsr_probability=0.98,
        pbo_probability=0.10,
        max_drawdown_pct=0.08,
        brier_improvement=0.02,
        cost_stress_2x_expectancy_r=0.03,
        regime_count=4,
        pair_count=3,
        lockbox_evaluated_once=True,
        lockbox_reused_for_selection=False,
        shadow_days=45,
        paper_days=90,
        major_operational_incidents=0,
        data_quality_incidents=0,
        calibration_window_separate=True,
        test_window_separate=True,
        live_like_execution_validated=True,
    )


def test_missing_or_synthetic_evidence_fails_closed() -> None:
    missing = evaluate_promotion(PromotionEvidence())
    synthetic = evaluate_promotion(PromotionEvidence(dataset_hash=HASH, synthetic_data=True))

    assert not missing.passed
    assert "dataset_hash" in missing.failures
    assert "git_commit" in missing.failures
    assert "non_synthetic_data" in synthetic.failures


def test_complete_evidence_passes_configurable_validated_gate() -> None:
    report = evaluate_promotion(
        _passing_evidence(),
        target_stage="validated",
        policy=PromotionPolicy(min_samples=500, max_pbo_probability=0.15),
    )

    assert report.passed
    assert report.failures == ()


def test_registry_requires_adjacent_human_approved_promotion_and_blocks_live(tmp_path) -> None:
    registry = ModelRegistry()
    record = ModelRecord(
        model_id="model-1",
        artifact_hash=HASH,
        trained_at=NOW.isoformat(),
        data_cutoff=NOW.isoformat(),
        metrics={"expectancy_r": 0.15},
        calibration_metrics={"brier": 0.20},
        limitations=["public data only"],
    )
    registry.register(record, now=NOW)
    validated = evaluate_promotion(_passing_evidence(), target_stage="validated")
    registry.promote(
        "model-1",
        validated,
        approved_by="risk-officer",
        reason="all validated gates passed",
        now=NOW,
    )

    assert registry.models["model-1"].stage == "validated"
    with pytest.raises(GovernanceError, match="exactly one stage"):
        registry.promote(
            "model-1",
            evaluate_promotion(_passing_evidence(), target_stage="paper"),
            approved_by="risk-officer",
            reason="skip shadow",
        )

    registry.models["model-1"].stage = "paper"
    live_report = evaluate_promotion(_passing_evidence(), target_stage="limited_live")
    with pytest.raises(GovernanceError, match="cannot enable live"):
        registry.promote(
            "model-1",
            live_report,
            approved_by="risk-officer",
            reason="must remain disabled",
        )

    path = tmp_path / "registry.json"
    registry.save(path)
    loaded = ModelRegistry.load(path)
    assert loaded.models["model-1"].artifact_hash == HASH
    assert loaded.events[-1]["event_type"] == "promoted"


def test_failed_evidence_cannot_be_promoted() -> None:
    registry = ModelRegistry()
    registry.register(ModelRecord("model-1", HASH, NOW.isoformat(), NOW.isoformat(), {}, {}, []))
    with pytest.raises(GovernanceError, match="evidence failed"):
        registry.promote(
            "model-1",
            evaluate_promotion(PromotionEvidence(), target_stage="validated"),
            approved_by="reviewer",
            reason="not enough evidence",
        )


def test_hard_veto_overrides_high_confidence_trade_request() -> None:
    vetoed = apply_hard_veto(
        "long",
        data_quality_reasons=("stale_price",),
        risk_reasons=("drawdown_breach",),
    )
    allowed = apply_hard_veto("short")

    assert vetoed.final_action == "no_trade"
    assert vetoed.vetoed
    assert vetoed.reasons == ("stale_price", "drawdown_breach")
    assert allowed.final_action == "short"
    assert not allowed.vetoed
