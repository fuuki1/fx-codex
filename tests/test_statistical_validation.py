from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fx_backtester.statistical_validation import (
    adjust_p_values,
    block_sign_permutation_test,
    circular_block_bootstrap_mean_ci,
    fold_dispersion,
    minimum_track_record_length,
    probabilistic_sharpe_ratio,
    rank_stability,
    selection_stability,
)


def _positive_returns(seed: int = 3, rows: int = 800) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0015, 0.01, rows)


def test_probabilistic_sharpe_and_track_record_are_finite_for_positive_edge() -> None:
    values = _positive_returns()
    psr = probabilistic_sharpe_ratio(values)
    minimum = minimum_track_record_length(values)

    assert psr["probabilistic_sharpe_ratio"] > 0.95
    assert 3 <= minimum < 2**31 - 1


def test_block_bootstrap_is_deterministic_and_contains_estimate() -> None:
    values = _positive_returns()
    first = circular_block_bootstrap_mean_ci(values, block_size=24, resamples=500, seed=11)
    second = circular_block_bootstrap_mean_ci(values, block_size=24, resamples=500, seed=11)

    assert first == second
    assert first.lower < first.estimate < first.upper


def test_block_permutation_detects_strong_positive_mean() -> None:
    result = block_sign_permutation_test(
        _positive_returns(rows=1200), block_size=12, permutations=1000, seed=9
    )
    assert result["p_value"] < 0.05


def test_multiple_testing_adjustments_are_never_smaller_than_raw() -> None:
    raw = np.array([0.001, 0.02, 0.04, 0.50])
    holm = adjust_p_values(raw, method="holm")
    bonferroni = adjust_p_values(raw, method="bonferroni")

    assert bool((holm >= raw).all())
    assert bool((bonferroni >= raw).all())
    assert bool((holm <= 1).all())


def test_fold_and_selection_stability_expose_concentration() -> None:
    dispersion = fold_dispersion([0.2, 0.1, -0.1, 0.3])
    selection = selection_stability(["a", "a", "a", "b"])

    assert dispersion["positive_fold_rate"] == pytest.approx(0.75)
    assert selection["top_selection_share"] == pytest.approx(0.75)
    assert selection["unique_selections"] == 2


def test_rank_stability_rewards_consistent_trial_ordering() -> None:
    scores = pd.DataFrame(
        {
            "trial_a": [3.0, 4.0, 5.0],
            "trial_b": [2.0, 3.0, 4.0],
            "trial_c": [1.0, 2.0, 3.0],
        },
        index=["fold1", "fold2", "fold3"],
    )
    report = rank_stability(scores)

    assert report["mean_rank_correlation"] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="missing"):
        rank_stability(scores.mask(scores == 3.0))
