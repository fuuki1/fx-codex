"""Sampling uncertainty and multiple-testing diagnostics for FX research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, isfinite, log
from typing import Literal

import numpy as np
import pandas as pd

from fx_backtester.overfitting import norm_cdf, norm_ppf, per_period_sharpe


@dataclass(frozen=True)
class BootstrapConfidenceInterval:
    estimate: float
    lower: float
    upper: float
    confidence: float
    block_size: int
    resamples: int
    seed: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def probabilistic_sharpe_ratio(
    returns: pd.Series | np.ndarray | list[float], benchmark_sharpe: float = 0.0
) -> dict[str, float | int]:
    """Probability that observed per-period Sharpe exceeds a benchmark."""

    series = _returns(returns, minimum=3)
    sharpe = per_period_sharpe(series)
    skewness = float(series.skew())
    kurtosis = float(series.kurt()) + 3.0
    denominator_term = 1.0 - skewness * sharpe + (kurtosis - 1.0) * sharpe**2 / 4.0
    if denominator_term <= 0 or not isfinite(denominator_term):
        raise ValueError("Sharpe sampling correction is non-positive")
    standard_error = (denominator_term / (len(series) - 1.0)) ** 0.5
    statistic = (sharpe - benchmark_sharpe) / standard_error
    return {
        "probabilistic_sharpe_ratio": norm_cdf(statistic),
        "sharpe_per_period": sharpe,
        "benchmark_sharpe": benchmark_sharpe,
        "standard_error": standard_error,
        "n_observations": len(series),
        "skewness": skewness,
        "kurtosis": kurtosis,
    }


def minimum_track_record_length(
    returns: pd.Series | np.ndarray | list[float],
    *,
    benchmark_sharpe: float = 0.0,
    confidence: float = 0.95,
) -> int:
    """Minimum observations needed for Sharpe to exceed benchmark at confidence."""

    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between 0.5 and 1")
    series = _returns(returns, minimum=3)
    sharpe = per_period_sharpe(series)
    edge = sharpe - benchmark_sharpe
    if edge <= 0:
        return 2**31 - 1
    skewness = float(series.skew())
    kurtosis = float(series.kurt()) + 3.0
    correction = 1.0 - skewness * sharpe + (kurtosis - 1.0) * sharpe**2 / 4.0
    if correction <= 0 or not isfinite(correction):
        raise ValueError("Sharpe sampling correction is non-positive")
    required = 1.0 + correction * (norm_ppf(confidence) / edge) ** 2
    return int(ceil(required))


def circular_block_bootstrap_mean_ci(
    values: pd.Series | np.ndarray | list[float],
    *,
    block_size: int,
    resamples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 42,
) -> BootstrapConfidenceInterval:
    """Circular block-bootstrap CI that preserves short-range serial dependence."""

    series = _returns(values, minimum=2).to_numpy(dtype=float)
    if not 1 <= block_size <= len(series):
        raise ValueError("block_size must be between 1 and the number of observations")
    if resamples < 100:
        raise ValueError("at least 100 bootstrap resamples are required")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    rng = np.random.default_rng(seed)
    block_count = int(ceil(len(series) / block_size))
    offsets = np.arange(block_size)
    estimates = np.empty(resamples, dtype=float)
    for iteration in range(resamples):
        starts = rng.integers(0, len(series), size=block_count)
        indices = ((starts[:, None] + offsets) % len(series)).ravel()[: len(series)]
        estimates[iteration] = float(series[indices].mean())
    alpha = (1.0 - confidence) / 2.0
    lower, upper = np.quantile(estimates, [alpha, 1.0 - alpha])
    return BootstrapConfidenceInterval(
        estimate=float(series.mean()),
        lower=float(lower),
        upper=float(upper),
        confidence=confidence,
        block_size=block_size,
        resamples=resamples,
        seed=seed,
    )


def block_sign_permutation_test(
    values: pd.Series | np.ndarray | list[float],
    *,
    block_size: int,
    permutations: int = 5_000,
    alternative: Literal["greater", "two-sided"] = "greater",
    seed: int = 42,
) -> dict[str, float | int | str]:
    """Block sign-flip test for a non-zero mean under serial dependence."""

    series = _returns(values, minimum=2).to_numpy(dtype=float)
    if not 1 <= block_size <= len(series):
        raise ValueError("invalid block_size")
    if permutations < 100:
        raise ValueError("at least 100 permutations are required")
    if alternative not in {"greater", "two-sided"}:
        raise ValueError("unsupported alternative")
    block_ids = np.arange(len(series)) // block_size
    block_count = int(block_ids.max()) + 1
    observed = float(series.mean())
    rng = np.random.default_rng(seed)
    exceedances = 0
    for _ in range(permutations):
        signs = rng.choice(np.array([-1.0, 1.0]), size=block_count)
        permuted = float((series * signs[block_ids]).mean())
        if alternative == "greater":
            exceedances += permuted >= observed
        else:
            exceedances += abs(permuted) >= abs(observed)
    return {
        "observed_mean": observed,
        "p_value": (exceedances + 1.0) / (permutations + 1.0),
        "permutations": permutations,
        "block_size": block_size,
        "alternative": alternative,
        "seed": seed,
    }


def adjust_p_values(
    p_values: list[float] | np.ndarray,
    *,
    method: Literal["bonferroni", "holm"] = "holm",
) -> np.ndarray:
    """Family-wise error control for the complete disclosed trial family."""

    values = np.asarray(p_values, dtype=float)
    if values.ndim != 1 or not len(values):
        raise ValueError("p_values must be a non-empty vector")
    if not bool(np.isfinite(values).all()) or bool(((values < 0) | (values > 1)).any()):
        raise ValueError("p_values must be finite and in [0, 1]")
    if method == "bonferroni":
        return np.minimum(values * len(values), 1.0)
    if method != "holm":
        raise ValueError("unsupported adjustment method")
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    adjusted_ordered = np.maximum.accumulate(
        [(len(values) - rank) * value for rank, value in enumerate(ordered)]
    )
    output = np.empty_like(values)
    output[order] = np.minimum(adjusted_ordered, 1.0)
    return output


def fold_dispersion(values: list[float] | np.ndarray) -> dict[str, float | int]:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if not len(array):
        raise ValueError("fold metrics are empty")
    return {
        "folds": len(array),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "std": float(array.std(ddof=1)) if len(array) > 1 else 0.0,
        "minimum": float(array.min()),
        "maximum": float(array.max()),
        "positive_fold_rate": float((array > 0).mean()),
    }


def rank_stability(fold_by_trial_scores: pd.DataFrame) -> dict[str, float | int]:
    """Mean pairwise Spearman rank correlation across validation folds."""

    if fold_by_trial_scores.shape[0] < 2 or fold_by_trial_scores.shape[1] < 2:
        raise ValueError("rank stability requires at least two folds and two trials")
    if bool(fold_by_trial_scores.isna().any().any()):
        raise ValueError("rank stability does not accept missing trial scores")
    ranks = fold_by_trial_scores.rank(axis=1, method="average", ascending=False)
    correlations: list[float] = []
    for left in range(len(ranks)):
        for right in range(left + 1, len(ranks)):
            value = float(ranks.iloc[left].corr(ranks.iloc[right], method="pearson"))
            if isfinite(value):
                correlations.append(value)
    if not correlations:
        raise ValueError("rank correlations are undefined")
    return {
        "folds": len(ranks),
        "trials": len(ranks.columns),
        "pairwise_comparisons": len(correlations),
        "mean_rank_correlation": float(np.mean(correlations)),
        "minimum_rank_correlation": float(np.min(correlations)),
    }


def selection_stability(selected_parameters: list[str]) -> dict[str, float | int]:
    if not selected_parameters:
        raise ValueError("selected_parameters cannot be empty")
    counts = pd.Series(selected_parameters, dtype=str).value_counts()
    probabilities = counts.to_numpy(dtype=float) / len(selected_parameters)
    entropy = -float(np.sum(probabilities * np.log(probabilities)))
    normalized_entropy = entropy / log(len(counts)) if len(counts) > 1 else 0.0
    return {
        "folds": len(selected_parameters),
        "unique_selections": len(counts),
        "top_selection_share": float(probabilities.max()),
        "normalized_selection_entropy": normalized_entropy,
    }


def _returns(values: pd.Series | np.ndarray | list[float], *, minimum: int) -> pd.Series:
    series = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(series) < minimum:
        raise ValueError(f"at least {minimum} finite observations are required")
    return series
