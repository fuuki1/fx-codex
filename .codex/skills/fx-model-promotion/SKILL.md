---
name: fx-model-promotion
description: Evaluate and record fail-closed model transitions from research through validated, shadow, and paper using provenance, lockbox, expectancy, calibration, PBO/DSR, coverage, cost stress, drift, and incident evidence. Use for promotion, demotion, or rollback decisions.
---

# FX model promotion

## Purpose and inputs

Make promotion a governed evidence decision rather than a win-rate threshold. Inputs are a candidate artifact/hash, experiment manifest, full trial ledger, validation/lockbox report, operational history, target stage, policy, human approver, and rollback target.

## Procedure

1. Verify clean commit, dataset/artifact hashes, seed, windows, costs, limitations, and full trials. Reject synthetic data and missing provenance.
2. Confirm zero PIT/future-feature violations; separate calibration/test; one-time lockbox not reused for selection.
3. Evaluate policy-configured sample, net expectancy and 95% lower bound, DSR, PBO, max drawdown, Brier/log-loss improvement, 2× cost expectancy, regime/pair coverage, execution quality, and unresolved incidents.
4. Move exactly one stage: `research → validated → shadow → paper`. This repository workflow must reject `limited_live` and `live`.
5. Require a named human and reason. Append the transition report to the registry; do not rewrite old prediction or transition events.
6. Demote on negative net expectancy, calibration breakdown, drift, drawdown, data/execution incident, or regime concentration. Preserve the previous artifact and tested rollback instructions.

## Commands

```bash
.venv/bin/pytest -q tests/test_governance.py tests/test_calibration.py tests/test_drift.py
.venv/bin/python promote_params.py --check
```

Use `fx_backtester/governance.py`; policy thresholds must be supplied/documented for the mandate rather than described as universal truths.

## Pass and fail conditions

Pass only when every critical gate is present and true, the target is adjacent, human approval is recorded, paper/live guards remain off, and rollback is verified. Unknown, skipped, stale, reused lockbox, dirty build, synthetic data, or unresolved incident means no promotion.

## Output format

Return candidate/stage; hashes and cutoffs; gate table with observed/required; decision; approver/reason; limitations; demotion triggers; registry event; rollback command. Never emit a live-enable command.

## Prohibited actions

No `--force` to bypass statistical warnings for deployment, no automatic live promotion/retraining, no stage skipping, no retroactive metric edits, and no promotion from current test results after tuning to them.

## Example

“Use `$fx-model-promotion` to evaluate model-20260710 for research→validated; missing evidence must block rather than become a warning.”
