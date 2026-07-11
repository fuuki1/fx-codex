# Institutional FX research target

## Status and definition

This project targets an **institutional-grade research process (A)**: traceable point-in-time data, leakage-resistant validation, realistic execution assumptions, calibrated uncertainty, independent risk vetoes, reproducible artifacts, and governed paper operation.

It does **not** possess an **institutional information advantage (B)** such as bank customer flow, prime-broker positioning, consolidated ECN depth, dealer axes, option-flow intelligence, or guaranteed low-latency venue data. Public COT, candles, scanner snapshots, news and volume fields are public observations or proxies. They must never be described as proprietary order flow.

“Institutional-grade” is a stage earned by evidence, not a description of the current system or a model family. As of 2026-07-11, the code has stronger research controls but the available data and lockbox evidence cannot support a profitability or readiness claim.

## Target properties

A candidate may advance only when all applicable properties are evidenced:

1. Every feature is provably available at prediction time; releases, revisions, first-seen times and transformations are versioned.
2. Train, tune, calibration, test and one-time lockbox periods are chronological, purged and embargoed.
3. Labels match the strategy horizon, enter no earlier than the next executable reference, use volatility-adjusted barriers, and record first touch, MFE/MAE, costs and label end.
4. All trials are disclosed; PBO, DSR/PSR, block uncertainty, multiple testing, fold/rank/parameter stability and coverage are evaluated.
5. Cost-adjusted expected R—not hit rate—is the primary target; full-engine 1.5×/2×/3× cost stress, gaps and stop-first ambiguity are tested.
6. Probabilities are calibrated on a separate period and evaluated by Brier, log loss, ECE and calibration slope. The system can abstain when uncertainty, disagreement, costs, risk or data quality are unacceptable.
7. Signal, sizing, execution and portfolio risk are separate. Gross leverage and shared-currency exposure are portfolio constraints.
8. Data-quality and risk vetoes cannot be outvoted. Staleness, duplicate writer, source disagreement, drift, reconciliation, loss and drawdown failures stop new decisions.
9. Dataset, code, dirty state, config, costs, seed, windows, trials and artifacts have hashes and can be regenerated.
10. Champion/challenger transitions are adjacent, evidence-backed, append-audited and human-approved. Automatic live promotion is impossible.

## Maturity stages

| Stage | Meaning | Minimum evidence |
|---|---|---|
| Research | Code/idea can be reproduced; no deployment claim | PIT schema, tests, manifest, disclosed limitations |
| Validated | Frozen candidate passes a configured validation protocol | Separate splits/lockbox, all trials, OOS net-R uncertainty, calibration, PBO/DSR, stresses, coverage |
| Shadow | Produces append-only predictions but cannot affect orders | Validated artifact, fresh data, drift/incident monitoring, reconciliation |
| Paper | Can affect the isolated paper stack | Adequate shadow duration, live-like execution/TCA, no unresolved major incidents, human approval |
| Limited live | Small bounded live mandate | Outside this implementation; separately approved policy and operational authority required |
| Live | Full approved mandate | Outside this implementation |

The governance library deliberately rejects the final two stages. Although the policy type can represent `paper`, the local broker execution stack was removed on 2026-07-10, so paper operation is also currently unavailable; a stage name is not an execution capability.

## Primary objective and reporting hierarchy

Optimize and report in this order:

1. Point-in-time and operational validity.
2. Net expected R and its block-bootstrap confidence interval.
3. Drawdown, tail loss, expected shortfall and loss-streak risk.
4. Calibration and abstention coverage.
5. Regime, pair, session and cost contribution/stability.
6. PBO/DSR/PSR and trial-family disclosure.
7. Hit rate only as a secondary diagnostic.

If evidence is missing, the outcome is `evaluation unavailable` or `promotion denied`; it is never imputed to pass.

## Explicit non-goals

- Guaranteeing profit, a win rate, or monthly return.
- Generating a trade at every observation.
- Replacing contracted market data, broker reconciliation, risk oversight or human approval with an LLM.
- Inferring causality from technical correlations.
- Calling a synthetic, short, unprovenanced or tuned-on-test result an improvement.
