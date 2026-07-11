# FX research protocol

## Implementation status

This is the required protocol, not evidence that a candidate has completed it. `pit_dataset.py` now creates a content-addressed, raw-preserving research dataset artifact, and `research_experiment.py` binds precomputed development/test rows, five temporal partitions, an expected aligned tune trial list, fixed calibration, descriptive test diagnostics and declared cost reruns. Lockbox `label/net_r` values are forbidden in the prepared artifact and may be attached only after an experiment-ID-keyed shared local claim succeeds. This remains an evidence binder—not a promotion-grade trainer/orchestrator. It cannot attest how predictions/stress results were produced, independent trial pre-registration, audited feature joins, global one-time custody or non-inspection by the outcome provider. All affected promotion fields remain `None`; the manifest denies promotion.

## 1. Pre-registration

Before looking at test/lockbox results, record:

- economic hypothesis and whether it is causal, correlational or only a proxy;
- target pair(s), regime/session, prediction time, holding horizon, entry reference and exit policy;
- label/feature versions and exact availability rules;
- simple baselines, candidate family, feature set, grid/search budget and random seeds;
- primary metric (net expected R) and secondary risk/calibration metrics;
- observed costs and stress scenarios;
- temporal splits, label-aware purge and embargo;
- minimum sample/coverage and configurable promotion policy;
- known limitations and a one-time lockbox purpose.

Changing any selection choice after seeing test results converts that test into tune/validation. Allocate a new untouched test/lockbox and record the change.

## 2. Data acceptance

1. Hash and preserve raw data. `materialize_pit_dataset` is the research-only canonical artifact path; successful local audit is necessary but not sufficient for promotion.
2. Require aware UTC and source-specific event/publication/availability/ingestion/revision metadata.
3. Run `evaluate_price_quality` and source-specific release checks. No future as-of match is permitted.
4. Quantify duplicates, gaps, staleness, OHLC, quotes/spreads, volume, source disagreement and writer ownership.
5. Mark free/scanner/legacy data as research-only where historical availability cannot be proven.
6. Do not use files in `runs/data/` for promotion until a source, license, acquisition time, transformation lineage and hash are attached.
7. Keep envelope integrity distinct from feature-join integrity. A dataset with zero envelope violations must not be reported as zero point-in-time feature violations until the actual as-of feature graph is audited.

## 3. Label contract

- Use a horizon aligned to the strategy (supported design points: 15m, 1h, 4h, 24h, 72h).
- Default entry is next executable bar open. Record `prediction_time`, `entry_time/reference`, volatility, barriers, time barrier and `label_end_time`.
- Use high/low or lower bars for first touch; forming-bar ranges are not post-prediction paths. If TP and SL touch in one unresolved bar, use stop-first or discard with an ambiguity flag.
- Stop MFE/MAE at the first exit. Gap-through stops execute at the adverse open. Deduct spread, slippage, commission and financing when known.
- Direction label, “worth trading” meta-label, size and exit policy are separate targets.

## 4. Temporal development

Use `chronological_model_partitions`:

1. train: feature transforms and model parameters;
2. tune: early stopping, model/feature/hyperparameter and threshold choices;
3. calibration: fixed calibrator fit;
4. test: one ordinary evaluation after choices freeze;
5. lockbox: one final governance evaluation, then permanently marked opened.

`research_experiment.py` supplies only a local procedural approximation of the last boundary. The prepared artifact records prediction inputs and a position commitment but requires lockbox `label/net_r` to be null. Evaluation creates and fsyncs an exclusive marker in a configured shared local claim store before it reads supplied outcomes, persists those outcomes in that store only after the claim, and then calls the process-local open guard. A crash after the marker consumes that local claim, and an identical artifact in another directory cannot claim the same experiment ID in the same store. This still does not prove global one-time custody or provider non-inspection, so both governance fields remain unavailable.

For strategy stability, run anchored and rolling purged walk-forward plus CPCV-like robustness. Training labels must end before an evaluation window; embargo adds an extra gap. Test folds may not overlap for independent aggregate claims.

## 5. Trials and inference

- Store every valid and failed trial, parameters, window, score and aligned returns in a trial ledger.
- PBO input must contain the same timestamps for every trial; NaN is not silently “flat return.”
- Report PBO, DSR, PSR, minimum track record, circular block-bootstrap expected-R CI, block sign-permutation p-value, Holm-adjusted p-values, fold dispersion, rank stability and selection stability.
- State the effective sample problem for overlapping labels and serially correlated returns. A nominal trade count is not necessarily an independent sample count.
- White’s Reality Check, Hansen SPA and stationary bootstrap are optional comparisons only when actually implemented and logged.

## 6. Execution and portfolio replay

The backtest must run through next-open order scheduling, bid/ask/spread, adverse slippage, commission, gaps, stop-first same-bar logic, event blackout, daily/weekly/monthly/hard stops, currency aggregation and portfolio gross leverage.

Run observed, 1.5×, 2× and 3× cost scenarios with `rerun_cost_stress`; post-hoc cost attribution cannot be the promotion stress. Add delayed entry/exit, spread spike, missing quote, event-window deterioration and rejection/partial-fill scenarios when the data and engine contracts support them.

## 7. Calibration and abstention

Evaluate raw and calibrated probabilities with Brier, log loss, ECE/MCE, slope/intercept and reliability bins. If multiple calibrators are compared, fit on calibration and choose on a later selection window; never choose on test.

`no_trade` is the required output when probability is uncalibrated, uncertainty is too wide, model disagreement is high, net expected R is unavailable/nonpositive, or any data/risk/operational veto is active. Report coverage and performance conditional on coverage to avoid hiding bad cases through excessive abstention.

## 8. Baselines, ablation and reporting

Compare on identical immutable data and splits:

- deterministic incumbent;
- flat/no-trade and simple directional/statistical baseline;
- current ML and candidate;
- calibrated vs raw;
- no-trade gate on/off;
- regime gate on/off;
- costs on/off and cost reruns;
- feature groups removed one at a time.

Report period/pair/regime/session contributions and concentration. Improvement requires better unseen, cost-adjusted evidence—not more code, a higher in-sample Sharpe, or synthetic results.

## 9. Reproducibility record

Every experiment stores: experiment ID; commit/dirty state; dataset/source/version/hash; feature/label/model versions; hyperparameters; seed; train/tune/calibration/test/lockbox windows; costs; environment/dependency definition; all metrics/trials/artifacts; source-ledger version; creation time. The current research binder additionally records why `point_in_time_violations`, test isolation, lockbox non-reuse and operational incidents are unavailable instead of coercing them to zero/false.

## 10. Required review

An independent reviewer asks whether there is future data, test feedback, hidden trials, optimistic costs/fills, period/pair/regime concentration, miscalibration, weak baselines, stale-data trading, irreproducibility, proprietary-data overclaim, missing no-trade, live bypass, weakened controls, or result-driven conveniences. Resolve critical findings and re-run the full relevant check set.
