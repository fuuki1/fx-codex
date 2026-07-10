---
name: fx-backtest-validation
description: Validate FX strategies with purged walk-forward/CPCV, separate calibration/test/lockbox windows, realistic execution costs, all-trial PBO/DSR, bootstrap uncertainty, and baseline comparisons. Use for any performance or robustness claim.
---

# FX backtest validation

## Purpose and inputs

Determine whether an apparent edge survives unseen data and realistic costs. Inputs are immutable datasets with provenance, strategy factory/config, label horizon, planned search space, cost assumptions, and seed.

## Procedure

1. Run point-in-time/data QA first. Synthetic or unprovenanced data is functional-test-only.
2. Pre-register target, horizon, metric, parameter grid, trials, and promotion threshold. Include deterministic and simple baselines.
3. Partition chronologically: train, tune, calibration, test, untouched lockbox. Purge by `label_end_time`, embargo boundaries, and reject overlapping test folds or random splits.
4. Fit transforms/features only on train; tune model/threshold on tune; fit a predetermined calibrator on calibration (or select it on another later window); evaluate test once. Open lockbox once after selection is frozen.
5. Log every attempted trial and aligned return series. Compute PBO only on complete same-period trial matrices; calculate DSR/PSR, minimum track record, block-bootstrap CI, block permutation, multiple-testing correction, fold dispersion, rank and parameter stability.
6. Re-run the engine under observed, 1.5×, 2×, and 3× costs. Include gap-through stops, stop-first same-bar policy, spread/slippage/commission and portfolio gross leverage.
7. Compare expected R, distribution/tails, drawdown, calibration, abstention coverage, regime/pair/session contribution, and baselines. State “no improvement” when warranted.

## Commands

```bash
.venv/bin/pytest -q tests/test_time_series_validation.py tests/test_overfitting.py tests/test_statistical_validation.py tests/test_stress.py
.venv/bin/python -m fx_backtester.cli backtest --data <data.csv> --strategy <name> --output-dir <run-dir>
.venv/bin/python -m fx_backtester.cli walk-forward --data <data.csv> --strategy <name> --train-bars 500 --test-bars 100
```

## Pass and fail conditions

Pass requires valid PIT data, disclosed trials, no split leakage, untouched one-time lockbox, positive cost-adjusted OOS expectancy with acceptable uncertainty, calibrated improvement, robustness across folds/regimes/pairs, and policy-configured PBO/DSR/cost gates. Any skipped/unavailable gate is not pass.

## Output format

Write `reports/institutional_benchmark_<timestamp>.md` with data/hash/window, partitions, costs, trials, sample size, expected R+CI, DSR/PBO/PSR, Sharpe/DD/tails, calibration, abstention, stresses, ablations, baselines, and limitations. Link run artifacts.

## Prohibited actions

Do not select on test/lockbox, hide failed trials, treat NaN as flat returns, use post-hoc cost subtraction as the sole stress, claim improvement from synthetic data, or optimize the reporting period after seeing results.

## Example

“Use `$fx-backtest-validation` to compare RSI, MA, and the current ML strategy on one PIT dataset with a frozen lockbox and 2× cost gate.”
