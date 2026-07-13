# Model governance and promotion policy

## Principles

- A model file is not an approved decision component.
- Missing/skipped evidence fails closed.
- Thresholds are configurable mandate choices with a written rationale, not universal constants.
- Prediction, outcome, evaluation and transition events are append-only and linked; original predictions are never edited.
- Automatic retraining may create a research challenger only. It cannot promote itself or enable live.
- The policy defines potential adjacent stages `research → validated → shadow → paper`; `limited_live` and `live` are forbidden. The current registry cannot enact any promotion without a durable external approval seal, so every stored model remains `research`.

These are policy and library contracts, not a statement that the current model has reached any stage. There is no authoritative end-to-end promotion service yet. Calibration evidence schema v4 binds logistic weights, target/direction/barriers/cost assumptions, dataset/feature/label versions, selected trial and complete trial ledger. The sole supported method must be predeclared before calibration, and its parameters are deterministically refitted from calibration-window raw observations. Prediction input is bound to hashed dataset, feature-registry and point-in-time store artifacts; source records carry event, publication, availability, ingestion and revision times. An unsealed research probability is explanatory only and forces `no_trade`.

## Registry record

Each `ModelRecord` includes model ID, stage, training time, data cutoff, performance and calibration metrics, limitations, approver, promotion/demotion reasons and artifact SHA-256. Experiment manifests additionally bind commit/dirty state, data/outputs, windows, seed, costs, dependencies and source ledger. Promotion provenance requires structured model metadata, a timestamp-aligned return matrix whose trial columns exactly match the complete trial ledger, immutable raw test observations and a separately hashed lockbox observation artifact. DSR/PBO, calibration, net expected R, drawdown, 2× cost expected R and PIT status are recomputed. Test and lockbox equity must both reconcile row by row from initial equity, raw R and recorded risk fractions.

Every calibration, independent-test and lockbox label observation records prediction time, label end time, label availability time, declared horizon seconds and the SHA-256 of its barrier-path artifact. Closed-schema verification requires the complete horizon and the observed label to finish before the next evaluation window, and requires the label to be available by the applicable evaluation cutoff. Event evidence independently rejects `recorded_at`, `effective_from` or `effective_to` clocks beyond the artifact creation boundary; a scheduled event timestamp may legitimately be later because it describes the event rather than when its vintage became knowable. A file hash proves binding and tamper evidence; it does not prove that an untrusted producer generated economically correct labels.

Promotion reports embed the complete policy, evidence and aware evaluation timestamp; semantic re-evaluation must reproduce the report byte-for-byte. `evidence_gates_passed` records internal research checks, while `passed` remains false until authoritative approval and durable external one-time lockbox-consumption seals exist. The current build implements neither seal, so `ModelRegistry.promote()` and registry loading reject every promoted state and remain research-only. A hash is an integrity check, not an external authorization signature.

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
- Shadow predictions are logged but cannot influence orders. Paper influence requires governance promotion and a paper-safe config.
- Compare both on aligned timestamps, costs and coverage; report disagreement and abstention.
- Keep the prior champion artifact/config available for atomic rollback.

## Demotion and stop conditions

Demote or abstain on negative rolling net R, calibration/log-loss breakdown, drawdown/tail breach, PSI/KS/prediction/concept drift, data-source or writer incident, spread/slippage error, broker disconnect, reconciliation mismatch, unexpected position, lockbox/provenance violation, regime concentration or operational instability.

Unmatured labels produce `human_review`, not a clean performance bill. Data drift can independently trigger warning, size reduction or abstention before labels mature. `drift.py` never enables automatic retraining/live promotion.

## Approval record and rollback

Every transition event records UTC timestamp, model, from/to stage, actor, reason and the full gate report. Demotion records the trigger and target. Rollback restores the previous hashed artifact and paper config, then re-runs data freshness, registry integrity, dry-run prediction and reconciliation checks before resuming.

No operator should turn warnings, skipped checks or incomplete evidence into a promotion approval by overriding a gate. Committee stage evaluation (`fx_intel/promotion.py`) is a legacy decision component, not an authoritative deployment approval service. There is no candidate-promotion `--force` shortcut; the former deployment path and broker execution stack were removed on 2026-07-10. The system is analysis-only, and any future rollback override must be separately scoped to restoring a previously approved paper artifact—not approving a new candidate.
