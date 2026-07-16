# Model governance and promotion policy

## Principles

- A model file is not an approved decision component.
- Missing/skipped evidence fails closed.
- Thresholds are configurable mandate choices with a written rationale, not universal constants.
- Prediction, outcome, evaluation and transition events are append-only and linked; original predictions are never edited.
- Automatic retraining may create a research challenger only. It cannot promote itself or enable live.
- The governance library permits adjacent records `research → validated → shadow → paper`; `limited_live` and `live` are rejected. The current repository has no broker paper stack, so this is a policy state model, not an available execution path.

These are policy and library contracts, not a statement that the current model has reached any stage. There is no authoritative end-to-end promotion service yet; caller-supplied evidence must be bound to independently verified artifacts before it can support a transition.

## Registry record

Each `ModelRecord` includes model ID, stage, training time, data cutoff, performance and calibration metrics, limitations, approver, promotion/demotion reasons and artifact SHA-256. Experiment manifests additionally bind commit/dirty state, data/outputs, windows, seed, costs, dependencies and source ledger.

## Initial candidate policy

`PromotionPolicy` defaults are conservative starting points: sample ≥200, DSR probability ≥0.95, PBO ≤0.20, net expected R ≥0, 95% lower bound >0, max drawdown ≤15%, Brier improvement >0, 2× cost expected R ≥0, ≥3 regimes and ≥3 pairs, plus zero PIT/future-feature and unresolved major data/operational incidents. Shadow-to-paper also requires at least 30 shadow days and live-like execution evidence.

These values must be reviewed for horizon, serial dependence, capital mandate, turnover, broker and loss tolerance. A lower statistical threshold cannot be justified merely because current data fails. Policy changes are versioned before results are viewed.

## Required gates

1. Immutable dataset/artifact hashes and source commit; clean build state.
2. Non-synthetic, licensed/authorized data with zero known PIT/future-feature violations.
3. Complete trial ledger and sufficient effective sample.
4. Separate calibration and test, one-time lockbox not reused for selection.
5. Positive cost-adjusted OOS expectancy and policy confidence lower bound.
6. DSR/PBO and multiple-testing/fold stability evidence.
7. Calibration improvement and acceptable coverage/abstention.
8. Full-engine 2× cost robustness and bounded drawdown/tails.
9. Pair/regime/session diversity without one cell explaining most profits.
10. No unresolved major data, execution, reconciliation or operational incident.
11. Adjacent stage, named human approver, reason and tested rollback.

## Champion/challenger behavior

- Champion remains unchanged while challengers run research/shadow.
- A challenger cannot consume champion outputs as labels or tune on champion failures without a registered new experiment.
- Shadow predictions are logged but cannot influence any broker action. Any `paper` stage name is evidence metadata only; broker paper execution is unavailable.
- Compare both on aligned timestamps, costs and coverage; report disagreement and abstention.
- Keep the prior champion artifact/config available for atomic rollback.

## Demotion and stop conditions

Demote or abstain on negative rolling net R, calibration/log-loss breakdown, drawdown/tail breach, PSI/KS/prediction/concept drift, data-source or writer incident, spread/slippage error, read-only source disconnect, coverage mismatch, lockbox/provenance violation, regime concentration or operational instability.

Unmatured labels produce `human_review`, not a clean performance bill. Data drift can independently trigger warning, size reduction or abstention before labels mature. `drift.py` never enables automatic retraining/live promotion.

## Approval record and rollback

Every transition event records UTC timestamp, model, from/to stage, actor, reason and the full gate report. Demotion records the trigger and target. Rollback restores the previous hashed artifact and paper config, then re-runs data freshness, registry integrity, dry-run prediction and reconciliation checks before resuming.

No operator should turn warnings, skipped checks or incomplete evidence into a promotion approval by overriding a gate. Committee stage evaluation (`fx_intel/promotion.py`) is a legacy decision component, not an authoritative deployment approval service. There is no candidate-promotion `--force` shortcut; the former deployment path and broker execution stack were removed on 2026-07-10. The system is analysis-only, and any future rollback override must be separately scoped to restoring a previously approved paper artifact—not approving a new candidate.
