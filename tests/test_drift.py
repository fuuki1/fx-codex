from __future__ import annotations

import numpy as np
import pandas as pd

from fx_backtester.drift import (
    data_drift_report,
    ks_distance,
    monitor_drift,
    performance_drift_report,
    population_stability_index,
    prediction_drift_report,
    wasserstein_distance,
)


def _features(seed: int = 1, rows: int = 1000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    first = rng.normal(0.0, 1.0, rows)
    return pd.DataFrame({"carry": first, "momentum": 0.5 * first + rng.normal(0, 0.5, rows)})


def test_distribution_metrics_are_zero_for_identical_samples() -> None:
    values = np.linspace(-2.0, 2.0, 100)
    assert population_stability_index(values, values) == 0.0
    assert ks_distance(values, values) == 0.0
    assert wasserstein_distance(values, values) == 0.0


def test_data_drift_abstains_on_large_shift_and_reports_feature_reason() -> None:
    baseline = _features()
    current = baseline + 4.0

    report = data_drift_report(baseline, current)

    assert report.action == "abstain"
    assert any("carry" in reason for reason in report.reasons)
    assert any(feature.psi is not None and feature.psi > 0.25 for feature in report.features)


def test_missing_rate_and_correlation_shift_are_visible() -> None:
    baseline = _features()
    current = _features(seed=2)
    current.loc[:199, "carry"] = np.nan
    current["momentum"] = -current["momentum"]

    report = data_drift_report(baseline, current)

    carry = next(feature for feature in report.features if feature.feature == "carry")
    assert carry.missing_rate_delta > 0.1
    assert report.max_correlation_shift is not None
    assert report.action == "abstain"


def test_missing_or_unexpected_feature_schema_abstains_immediately() -> None:
    baseline = _features()
    current = baseline.drop(columns="momentum").assign(replacement=1.0)

    report = data_drift_report(baseline, current)

    assert report.action == "abstain"
    assert report.features == ()
    assert any("missing_features=momentum" in reason for reason in report.reasons)
    assert any("unexpected_features=replacement" in reason for reason in report.reasons)


def test_prediction_drift_tracks_abstention_and_probability_distribution() -> None:
    report = prediction_drift_report(
        np.full(100, 0.50),
        np.full(100, 0.75),
        baseline_abstained=np.zeros(100, dtype=bool),
        current_abstained=np.ones(100, dtype=bool),
    )

    assert report.action == "warn"
    assert report.probability_mean_delta == 0.25
    assert report.abstention_rate_delta == 1.0


def test_performance_drift_separates_unmatured_ground_truth() -> None:
    report = performance_drift_report(None, None, None, None)

    assert not report.labels_available
    assert report.action == "human_review"
    assert report.reasons == ("ground_truth_not_mature",)


def test_one_mature_label_requires_human_review_instead_of_allow() -> None:
    report = performance_drift_report([1], [0.8], [1], [0.8])

    assert not report.labels_available
    assert report.action == "human_review"
    assert report.reasons == ("insufficient_mature_labels:1<30",)


def test_performance_breakdown_demotes_model() -> None:
    labels = np.tile([0, 1], 200)
    good = np.where(labels == 1, 0.8, 0.2)
    bad = np.where(labels == 1, 0.2, 0.8)

    report = performance_drift_report(
        labels,
        good,
        labels,
        bad,
        baseline_returns_r=np.full(len(labels), 0.1),
        current_returns_r=np.full(len(labels), -0.1),
    )

    assert report.labels_available
    assert report.action == "demote"
    assert report.brier_delta is not None and report.brier_delta > 0
    assert any("current_expected_r" in reason for reason in report.reasons)


def test_combined_monitor_never_enables_auto_retraining_or_live_promotion() -> None:
    baseline = _features()
    current = baseline.copy()
    probabilities = np.linspace(0.2, 0.8, len(baseline))

    report = monitor_drift(baseline, current, probabilities, probabilities)

    assert report.final_action == "human_review"  # labels have not matured
    assert not report.automated_retraining_allowed
    assert not report.automated_live_promotion_allowed
