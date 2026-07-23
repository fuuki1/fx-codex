"""Fixtures for integration tests that start from a reviewed policy."""

from __future__ import annotations

from datetime import datetime

from fx_intel import trade_outcome
from fx_intel.evaluation_labels import (
    DEFAULT_COST_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)


def mark_candidate_ready_for_review(
    registry: dict,
    candidate_id: str,
    *,
    evaluated_at: datetime,
) -> dict:
    """Populate an already unit-tested prospective proof for integration setup."""

    record = registry["candidates"][candidate_id]
    sample_count = trade_outcome.PROSPECTIVE_MIN_EFFECTIVE_SAMPLES
    record["stage"] = "ready_for_review"
    record["ready_for_review_at"] = evaluated_at.isoformat()
    record["prospective_last_evaluated_at"] = evaluated_at.isoformat()
    record["prospective_dataset_hash"] = "f" * 64
    record["prospective_effective_dataset_hash"] = "e" * 64
    record["prospective_outcome_keys"] = [
        f"prospective-decision-{index}+{NET_LABEL_VERSION}" for index in range(sample_count)
    ]
    record["prospective_metrics"] = {
        "schema": trade_outcome.PROSPECTIVE_EVIDENCE_SCHEMA,
        "scoped_samples": sample_count,
        "quality_excluded_samples": 0,
        "raw_samples": sample_count,
        "net_label_samples": sample_count,
        "effective_samples": sample_count,
        "net_label_coverage": 1.0,
        "overlap_ratio": 0.0,
        "cluster_count": sample_count,
        "market_days": sample_count,
        "net_expectancy_r": 0.4,
        "net_expectancy_lcb": 0.2,
        "dsr": 0.99,
        "dsr_trials": 1,
        "max_drawdown_r": -0.2,
        "cost_stressed_expectancy_r": 0.35,
        "max_symbol_concentration": 0.5,
        "label_versions": [NET_LABEL_VERSION],
        "label_provenances": [NET_LABEL_PROVENANCE],
        "cost_model_ids": [DEFAULT_COST_MODEL_ID],
        "contract_consistent": True,
        "blocking_reasons": [],
    }
    registry["ready_for_review_count"] = 1
    return registry
