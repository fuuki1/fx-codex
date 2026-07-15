# Evidence: real bid/ask bars connected to the authoritative pipeline

First end-to-end run of `fx_backtester.experiment_pipeline` on **real bid/ask
bars with measured per-trade spread costs** — replacing the close-only +
declared-static-spread setup of the 2026-07-13 HistData runs.

- **Data**: `data/real/dukascopy/usdjpy_2024_1h_bidask_from_m1.csv.gz` — 6,250
  hourly bars, USD/JPY 2024 full year, aggregated from Dukascopy m1 bid/ask
  candles (lineage `dukascopy_usdjpy_1h_from_m1_2024_v1`, raw sha pinned in
  `manifest.json`). Price basis: bid OHLC; per-bar `spread_open` measured.
- **Cost model**: `measured_bar_spread_v1` — every trade pays the measured
  spread of its ENTRY bar's open (entry_lag=1 → next bar's opening book
  width) + declared slippage/commission/financing. Measured entry spreads
  across the 5,602 usable rows: p50 0.6 pips, p95 3.9 pips, max 34.1 pips
  (year-open/rollover hours). Missing measurements fail closed — never
  zero-filled, never defaulted.
- **Result** (10 candidates: 7 baselines + logistic/ridge/GBDT):
  selected `always-short`, test net expectancy **−0.0796 R** over 916 trades
  (costs measured, after-cost negative), promotion **DENIED**
  (`net_expectancy`, `expectancy_confidence_interval`, `deflated_sharpe`,
  `probability_of_backtest_overfitting`, `drawdown`, `cost_stress_2x`,
  `untouched_lockbox`, `pair_coverage`, `clean_worktree`,
  `operational_incidents`). No alpha claim; the run demonstrates the
  CONNECTION, not an edge.
- **Determinism**: two runs with independent trial ledgers and lockbox
  registries produced identical `deterministic_result_sha256`
  `8ddab7361b4b93d156e78ba3958f984e5d09689ddb51e1e2b3310585530fc69d`.
- Comparison anchor: the 2026-07-13 close-only HistData run (declared static
  0.8 pip spread) selected `gbdt-small` at −0.065 R. Real measured costs on
  real bid/ask data remain after-cost negative — consistent with "no edge"
  across both data qualities.

Artifacts here are copied from run-a
(`logs/bidask_e2e/run-a/usdjpy-dukascopy-bidask-2024-1h-20260715/`);
`dataset_rows.jsonl` (10 MB, every row carrying `entry_spread_price`) stays
local — its hash is pinned in `artifact_hashes.json`.

Reproduce:

```bash
python3 -m fx_backtester.experiment_pipeline run \
  --experiment-manifest experiments/usdjpy-dukascopy-bidask-2024-1h-20260715.json \
  --output-root logs/bidask_e2e/run-a \
  --trial-ledger logs/bidask_e2e/ledger-a.jsonl \
  --lockbox-registry logs/bidask_e2e/lockbox-a.jsonl
```

(the manifest's `git.commit` binds the run to its code commit; regenerate the
binding if replaying from a different commit)
