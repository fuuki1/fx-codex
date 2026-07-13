# Real USD/JPY 2024 authoritative-pipeline run — 2026-07-13

**First end-to-end run of the authoritative research pipeline on real market price data.**
Input: HistData USD/JPY 2024, M1 → resampled to 1h (6,265 bars), committed at
[`data/real/histdata/usdjpy_2024_1h.csv`](../../../data/real/histdata/usdjpy_2024_1h.csv)
(`raw_sha256 9e2b632d…`, pinned by the manifest).

Manifest: [`experiments/usdjpy-histdata-real-2024-1h-20260713.json`](../../../experiments/usdjpy-histdata-real-2024-1h-20260713.json)
(`synthetic: false`).

## The result: honest negative (no edge, promotion correctly denied)

10 candidates (7 baselines + logistic-ridge + ridge + **GBDT**) were fit on train,
selected on tune, and evaluated once on the untouched test partition.

| Fact | Value |
|---|---|
| Selected candidate (best on tune) | **`gbdt-small`** (GBDT is fully wired & competitive) |
| `synthetic_data` | **false** (real data recognized by the gate) |
| Net expectancy R (OOS, selected) | **−0.065** (loses after declared costs) |
| Expectancy CI lower (bootstrap) | −0.203 → **no-trade dominates** |
| OOS test trades | 917 (selected) / sample_count 249 effective |
| Win rate / profit factor | 34.4% / **0.907** (<1) |
| Sharpe per trade | −0.046 |
| DSR probability | 0.167 (far below significance) |
| PBO probability | 0.20 |
| Brier improvement | 0.0009 (≈ no calibration skill) |
| **Holm-adjusted p-values (all 10)** | **all 1.0** — nothing beats noise after correction |
| Cost-stress 2× net expectancy | −0.129 (worse under stress) |
| PIT / future-feature violations | **0 / 0** (no leakage on real data) |
| Promotion | **DENIED** — failures: `net_expectancy, expectancy_confidence_interval, deflated_sharpe, cost_stress_2x, untouched_lockbox, pair_coverage, clean_worktree, …` |

**Why this is the important result, not a disappointment:** an efficient hourly FX
series with a realistic spread *should not* yield a positive technical edge. The
pipeline (a) ran to completion on real data, (b) selected the strongest candidate,
(c) measured a small negative cost-net expectancy, (d) confirmed via DSR/PBO/Holm
that it is indistinguishable from noise, and (e) refused promotion. This is direct
evidence that **the framework does not manufacture false alpha** — the same machinery
that denied the synthetic self-test also denies a real dataset with no genuine edge.

## Deterministic replay (verified)

Two independent runs produced the identical `deterministic_result_sha256`
`fedc9d83ed2598efed4699601f3d69f94c7963d6e216699b2e5f29de82bdb539` and both selected
`gbdt-small`. Deterministic replay holds on **real** data.

## What this does NOT establish (non-claims)

- **No positive edge, no strategy to deploy.** The result is negative by design.
- **Close-only data.** HistData M1 has no bid/ask and no real volume. The declared
  static spread (0.8 pip) is an assumption, not measured; label quality is capped and
  this dataset is **NOT admissible for a promotion claim** (see manifest `license_note`).
- **Single pair, single year, one timeframe.** No `pair_coverage`, no multi-year OOS,
  no 30-trading-day live operation.
- **Exploratory run** (`dirty_worktree_allowed: true`); the `clean_worktree` gate
  therefore also fails. A formal claim would require a clean, committed tree.

## Files

`promotion_decision.json` (gate evidence + failures), `evaluation.json` (full
per-candidate OOS statistics, 177 KB), `cost_stress.json`, `data_lineage.json`,
`trial_ledger_snapshot.jsonl` (all 10 candidates incl. failures), `manifest.json`,
`artifact_hashes.json`, `git.json`, `environment.json`, `run_info.json`.
The 2.9 MB `dataset_rows.jsonl` is **not committed** (regenerable from the pinned CSV).

## Reproduce

```bash
python3 -m fx_backtester.experiment_pipeline run \
  --experiment-manifest experiments/usdjpy-histdata-real-2024-1h-20260713.json \
  --output-root <SCRATCH>/real_runs --trial-ledger <SCRATCH>/real_runs/ledger.jsonl
```

The USD/JPY CSV was produced from HistData M1 via `scripts/fetch_histdata.py`
(EST→UTC, 1h resample). Re-fetching requires network access to `www.histdata.com`.
