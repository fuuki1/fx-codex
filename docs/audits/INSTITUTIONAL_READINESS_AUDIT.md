# Institutional readiness audit

## Decision

**Status: research only.** Non-influential shadow observation/logging may continue, but no candidate has earned `validated` or `shadow` governance status. Paper and live are unavailable. The repository has materially stronger safety and research primitives, but the data, end-to-end evidence chain, operating state and performance evidence do not support promotion.

This is a process/readiness assessment, not investment advice or a profitability certification.

## Audit identity and scope

| Field | Evidence |
|---|---|
| Initial evidence capture | 2026-07-10 JST; branch `feat/decision-pipeline-checklist`, commit `5698409`, dirty; at that capture the branch was 1 commit ahead and 8 behind `origin/main` |
| Evidence freeze | 2026-07-11 10:41 JST |
| Final inspected code HEAD | `c84bd7629505eb1848dc77c99a7ecf2a7bdccdb6` on `feat/decision-pipeline-checklist`; the audit/report-only commit follows this code freeze |
| Final divergence at code freeze | `origin/main` at `35955829e76495cd2a6356323999b9c1e880b360`; 0 main-only commits and 7 branch-only commits |
| GitHub | PR [#26](https://github.com/fuuki1/fx-codex/pull/26) is `OPEN / MERGEABLE / CLEAN` at code HEAD `c84bd76`. Actions run `29135088717` passed on Python 3.11 and 3.12. |
| Worktree at code freeze | Only this audit and the benchmark report were untracked; the retained safety stash was not dropped. No merge conflict remained. |
| Local workspace | `/Users/takahashifuuki/Desktop/fx-codex` |
| Observed Mac mini checkout | `/Users/fuuki/srv/fx-codex`; separate Docker checkout observed under `/Users/fuuki/fx-codex/trader` |
| Mutation boundary | No Mac mini process, launchd job, cron, Docker service, paper/live setting, PR metadata or broker state was changed. The existing PR branch was merged with `origin/main` and updated with local fixes; no live or deployment operation was performed. |

The branch is a review candidate, not a release or trading artifact. Reconciliation removed the behind-main condition, but GitHub review/CI, a reproducible environment, deployment evidence and performance evidence remain separate gates. User-owned work was preserved; no hard reset, destructive checkout or stash drop was used.

## Observed operating and data state

### Local evidence snapshot

The local journal audit around 2026-07-10 07:11 UTC found:

| Stream | Rows | Duplicate rate | Detected gaps | Last observation |
|---|---:|---:|---:|---|
| Fusion/briefing journal | 6 | 33.3% | 2 | 2026-07-08 15:21:03 UTC |
| Timeframe decision journal | 48 | 16.7% | 28 | approximately 2026-07-10 03:39 UTC |
| Timeframe price snapshots | 40 | 0.0% | 28 | approximately 2026-07-10 03:39 UTC |

The outcome store had 20 observations, zero tradable/scored outcomes and only `no_future_prices`/unscored states. Files under `runs/data/` lack sufficient acquisition, license, first-seen and transformation lineage and are inadmissible for promotion.

At initial inspection the only relevant local long-running process was `com.fx-codex.tradingview-webhook`; no local Docker workload was observed. This process snapshot was diagnostic only.

### Mac mini evidence snapshot

The read-only remote inspection on 2026-07-10 found a dirty checkout approximately 18 commits behind then-current `main`, a cron execution failing with `ModuleNotFoundError`, stale snapshots, competing/restarting collectors and substantial duplicate contamination:

| Stream | Rows | Duplicate rate |
|---|---:|---:|
| Briefing journal | 475 | 25.26% |
| Timeframe decision journal | 3,544 | 53.50% |
| Timeframe price snapshots | 21,876 | 26.98% |

Those price records did not provide verified historical bid/ask or trustworthy source OHLC suitable for execution validation. The 18-commit figure is an observation, not a current guarantee; it must be rechecked immediately before any migration.

## Findings by severity

### Critical — unresolved

1. **No promotion-admissible dataset.** Primary price/macro/COT/news/scanner ingestion is not fully materialized through immutable point-in-time records with legal availability, first ingestion, revision lineage and source contracts.
2. **No end-to-end validation run.** The new label, temporal split, calibration, statistical, stress and governance components are not bound by one authoritative orchestrator and independently verified manifest.
3. **No valid performance evidence.** Synthetic diagnostics do not establish edge; real journals are stale/duplicated/incomplete, outcomes are unscored, PBO is unavailable and the only positive synthetic baseline has 11 trades and fails cost/statistical gates.
4. **Observed operating state is not migration-ready.** The Mac mini snapshot showed stale/duplicate data, a broken scheduled command and ambiguous process ownership. Paper/live safety assertions and reconciliation were not executed during this read-only audit.

### High — unresolved or partial

1. **PIT integration is partial.** Availability now fails closed against actual ingestion, but FRED uses current `fredgraph.csv`, macro/COT/news revision replay is incomplete, and the main CLI/briefing loaders are not uniformly PIT-backed.
2. **Journaling is not uniformly transactional.** Price snapshots have lock/natural-key/conflict checks; decision, timeframe and outcome append paths do not yet share that contract.
3. **Lockbox/governance state is not durable.** A process-local one-time marker and caller-supplied evidence are insufficient for independent approval.
4. **Legacy reports are not authoritative.** Older commercial-readiness cost sensitivity is post-hoc. Only full-engine reruns may support a cost gate.
5. **Market microstructure fidelity is insufficient.** Static spread/slippage proxies omit historical bid/ask, depth, latency, rejection, partial fills, venue/broker state, financing and source disagreement.
6. **Data advantage is absent.** Public/retail observations—Legacy COT proxies, current-only FRED downloads, Stooq, RSS and an unofficial TradingView scanner—are not proprietary order flow or an institutional information moat.
7. **Release attestation is incomplete.** The branch is no longer behind `main` and current-head CI is green, but PR #26 still requires human diff review. A concurrent commit (`10d6cbe`) contains a much broader safety change set than its test-only subject states, so reviewers must inspect the actual diff rather than infer scope from the message. This governance defect does not justify rewriting published history.

### Medium — unresolved

1. Source-specific parsers still need durable raw-response archives, schema-drift alarms, release validation and duplicate/revision policies.
2. Direct critical operations notification and signal-board summaries need separate credentials and deployment verification; documentation alone is not evidence.
3. The Python environment can run tests, but `.venv/bin/python -m pip` raises an internal pip import error. `requirements.lock` has exact versions but no hashes, so the runbook's `--require-hashes` migration gate intentionally stops until a reviewed hash-pinned lock is produced and a clean venv passes `pip check`.
4. Missing-feature/performance drift now abstains safely, but production thresholds, label maturity and rollback behavior need real shadow evidence.

## Implemented hardening

### Data integrity and time semantics

- `PointInTimeRecord` normalizes legal availability to no earlier than declared publication, revision and actual ingestion; future as-of joins are rejected.
- Payloads are canonicalized and frozen; supplied hashes must equal a recomputed content hash. Strict QA checks timezone, metadata, ordering, identity, duplicates and hash shape.
- Technical and briefing capture timestamps are taken after acquisition rather than before a network request.
- Source documentation now distinguishes the actual CFTC Legacy `6dca-aqww`, current-only FRED CSV, Stooq/RSS/FairEconomy and unofficial scanner paths from target vendor contracts.

These changes close a concrete ingestion-time leakage path. They do not yet implement full two-stage revision materialization or connect every loader.

### Labels, validation and inference

- Next-open triple-barrier labels implement stop-first ambiguity, gap-through stops, first-touch exits, MFE/MAE cutoff, cost-adjusted `R` and `label_end_time`.
- Label-aware purge/embargo, anchored/rolling folds, CPCV-like folds and five chronological model partitions are available.
- PSR, DSR, PBO input checks, MTRL, block-bootstrap CI, block sign tests, Holm adjustment and stability diagnostics are available.
- Overlapping test folds and non-finite overfitting evidence fail closed.

The components pass unit tests but are not yet a completed institutional experiment pipeline.

### Calibration, drift and decision safety

- ML schema v3 uses a calibration-window intercept null, requires Brier/log-loss improvement, nonempty feature importance and test AUC ≥0.55; constant features cannot become usable merely through prevalence shift.
- Drift requires the exact feature schema. Missing/unexpected features abstain; immature realized labels require human review. Uncertainty intervals must contain the stated probability.
- Missing costs, uncalibrated conviction, stale/invalid operational evidence and weak expectancy evidence block decisions instead of becoming neutral/default inputs.
- Descriptive one-sample expectancy no longer passes: evidence must be independent-test, net-of-costs and have a positive confidence lower bound.

### Execution and risk simulation

- Gross leverage is checked at entry and marked to market; a breach latches the risk state and schedules portfolio reduction.
- Each symbol is closed on its own last available bar, preventing early-ending instruments from remaining invisibly open.
- Pending market entries expire after a configured TTL instead of filling on an arbitrarily distant next bar.
- Metrics now include Sortino/downside deviation, median USD/R, 5% expected shortfall in R, longest loss streak, holding time, fees and turnover.

### Operational controls prepared locally

- The canonical briefing path can require a recent `overall=ok` freshness report; missing, malformed, future-dated, stale, warning and critical states all veto a decision.
- Price snapshots have an OS lock, writer ID, 5-minute natural key, idempotent replay and conflicting-writer rejection.
- Launchd templates/scripts define one 5-minute price writer, hourly briefing and 5-minute health monitor; installation refuses detected manual/direct/cron writers and verifies bootout before replacing a plist.
- Failed Discord delivery is retried without waiting for cooldown, and `--no-notify` does not consume canonical notification state.
- Status treats missing, malformed, stale, future-dated and unknown freshness evidence as critical; it detects legacy labels and writer candidates. Restart preflights all labels before any kickstart.
- Install/uninstall retain plists and return nonzero on bootout/legacy-disable failures. The briefing wrapper runs both modes but propagates any failure.
- The migration runbook fails closed when Docker state cannot be inspected and refuses to reuse an old venv; a reviewed hash-pinned lock is a deliberate blocker.
- `--promote-live` is disabled; legacy macro/ML members are fixed to non-influential shadow; saved paper/live/unknown states fail closed to shadow; and the local broker execution stack has been removed.

These controls were prepared and tested locally; they were not installed on the Mac mini by this audit.

## Independent reviews

Three independent domain reviews were performed and their adverse findings were retained rather than averaged away. The final row records the main-agent verification after applying the operations review:

| Review | Key challenge | Resolution status |
|---|---|---|
| Data/macro/PIT | Ingestion later than nominal release could leak; payload/hash mutable; capture time preceded fetch; source ledger overstated FRED/COT/TradingView semantics; legacy evaluator accepted future/naive timestamps | Ingestion/hash/capture defects fixed, ledger corrected, and legacy evaluation now rejects naive/future rows. Revision replay and primary-loader integration remain open. |
| Quant/risk/ML | Skipped PBO/DSR, prevalence-shift usability, weak one-sample expectancy, missing drift columns, interval inconsistency, entry-only leverage, stale pending orders, early-ending symbols and non-independent legacy auto-promotion | Fail-closed ML/drift/expectancy and engine controls added; legacy members are shadow-only. Durable lockbox, complete trials and end-to-end governance remain open. |
| Repository/operations | Contradictory Mac mini topology, rollback causing two writers, unsafe `git add -A`, alert-only freshness, hidden wrapper failures, stale status, centralized notification and no migration evidence | Canonical topology, safe migration/rollback rules, freshness veto/status and direct-notification design documented/prepared. Remote migration and journal-wide locking remain open. |
| Repository/operations follow-up | `--no-notify` consumed notification state; failed sends could suppress retry; manual expectancy command was a second writer; status/restart missed direct/cron/legacy writers; uninstall and Docker checks failed open | Notification state/retry, manual procedure, writer detection, preflight restart, install/uninstall and Docker assertions were corrected and regression-tested. Mac mini deployment remains deliberately unperformed. |

## Maturity before and after

Score meaning: 0 = absent/evaluation unavailable; 1 = ad hoc; 2 = coded and tested but partial or undeployed; 3 = integrated research-grade; 4 = independently reproduced paper-grade; 5 = independently audited live-grade. Scores measure evidenced maturity, not ambition.

| Required axis | Initial | Final | Evidence and ceiling |
|---|---:|---:|---|
| Data integrity | 1 | 2 | `point_in_time.py` and locked/idempotent price capture improve contracts; no promotion-admissible dataset |
| Point-in-time integrity | 1 | 2 | UTC/as-of/availability/hash checks exist; revisions and primary loaders remain incomplete |
| Label quality | 1 | 2 | Next-open triple barrier, gaps, stop-first, MFE/MAE and net R are tested; no real PIT label corpus |
| Validation rigor | 1 | 2 | Purge/embargo, chronological partitions, CPCV-like and statistical checks exist; no authoritative orchestrator/trial matrix |
| Model performance | 0 | 0 | Real performance is evaluation-unavailable; synthetic baselines fail sample, confidence and cost gates |
| Probability calibration | 1 | 2 | Separate calibration partition and Brier/log-loss/AUC gates exist; no mature real-data reliability evidence |
| Execution reproducibility | 1 | 2 | Next-open fills, costs, gaps, TTL and per-symbol closure are deterministic; no broker/venue replay |
| Risk management | 1 | 2 | Data/risk vetoes, leverage latch and exposure controls are tested; no paper execution reconciliation |
| Reproducibility | 1 | 2 | Seeds, hashes, artifacts and benchmark manifest improved; local pip is broken and the lock lacks hashes |
| Monitoring | 1 | 2 | Freshness veto, retry/state semantics and drift checks are tested; Mac mini deployment is unverified |
| Governance | 1 | 2 | Evidence schema, shadow-only legacy path and promotion policy exist; lockbox/approval is not durable/end-to-end |
| Operational safety | 1 | 2 | Single-writer launchd design, fail-closed install/restart/uninstall and rollback exist; migration was not executed |

Unweighted evidence score moves from **0.92/5 to 1.83/5**. The increase is process-control maturity only; model performance remains 0/5 and prevents any stage promotion.

| Dimension | Initial state | Final state | Promotion effect |
|---|---|---|---|
| PIT/data validity | Ambiguous availability; mutable evidence; inconsistent source claims | Stronger immutable primitive and source ledger | Partial only; no admissible dataset |
| Leakage-resistant research | Legacy split/reuse risks; weak statistical evidence handling | Label-aware splits, calibration and inference primitives | Partial only; no orchestrated experiment |
| Decision fail-closed behavior | Missing/stale/cost/uncertainty evidence could degrade softly | Freshness, cost, calibration, drift and risk vetoes hardened | Safer research/shadow behavior |
| Concurrency/operations | Duplicate writers, stale logs, hidden failures | Price-writer lock and canonical launchd/runbook prepared | Not deployed; other journals remain unsafe |
| Execution/risk simulation | Entry-only leverage and stale-order/end-of-data gaps | Mark-to-market latch, TTL and per-symbol close | Simulation correctness improved |
| Performance maturity | Evaluation unavailable | Evaluation unavailable; synthetic baselines fail | No improvement claim |
| Governance stage | Research | Research | No candidate promoted |

Process-control maturity improved. Data-edge, statistical-evidence and deployment maturity did not become institutional-grade.

## Verification

Final local checks on code HEAD `c84bd76` plus the audit/report worktree:

- `pytest -q`: **489 passed, 1 skipped** in 14.66 seconds.
- `ruff check .`: passed.
- `black --check .`: 118 files unchanged.
- `mypy fx_backtester fx_intel *.py`: 64 source files, no issues.
- zsh/bash syntax checks selected by each script shebang: passed.
- staged and unstaged `git diff --check`: passed.
- Deterministic synthetic base and 1×/1.5×/2×/3× full-engine cost reruns completed; see [institutional benchmark](../../reports/institutional_benchmark_20260711.md).
- GitHub Actions run `29134903400` failed only on the same two mypy findings reproduced locally. The focused finite-float normalization fix is in `5693d44`; current-head run `29135088717` then passed on Python 3.11 and 3.12.

## Exit criteria for the next stage

Before `validated` can be considered:

1. Complete human diff review, record the final artifact/dependency hashes, and do not infer commit scope from subject lines alone.
2. Migrate the Mac mini using the runbook with pre-state evidence, paper-safe assertions, one writer, freshness/gap checks and rollback evidence; do not enable live.
3. Materialize immutable raw and PIT datasets with contractual timestamps, revisions, first ingestion, licenses and hashes; quarantine current contaminated journals.
4. Connect one label path, five chronological partitions, a complete aligned trial ledger, calibration, full cost stress and a durable one-time lockbox to an authoritative manifest.
5. Run a pre-registered real-data experiment with adequate effective samples, positive net-R confidence lower bound, acceptable PBO/DSR/calibration/coverage/tails and no major incidents.
6. Obtain independent reproduction and named human approval for an adjacent transition only.

Until every applicable item is evidenced, the correct outcome is `evaluation unavailable` or `promotion denied`, never a waiver.
