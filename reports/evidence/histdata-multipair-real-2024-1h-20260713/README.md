# Cross-pair real-data evaluation — USD/JPY · EUR/USD · GBP/USD 2024 (1h)

Extends the single-pair [USD/JPY run](../histdata-usdjpy-real-2024-1h-20260713/README.md)
to three major pairs, addressing the **pair-concentration** concern: does the "no edge
after costs" result hold across pairs, or was it one lucky/unlucky series?

Each pair is a **separate experiment** (the manifest binds one symbol per run by design),
run through the authoritative pipeline on real HistData 2024 hourly bars.

## Cross-pair result (all promotion DENIED)

| pair | selected candidate | net expectancy R | CI lower | DSR | PBO | OOS samples | PIT viol. |
|---|---|---:|---:|---:|---:|---:|---:|
| USD/JPY | `gbdt-small` | **−0.065** | −0.203 | 0.167 | 0.20 | 249 | 0 |
| EUR/USD | `always-long` | **−0.118** | −0.255 | 0.00 | 0.057 | 229 | 0 |
| GBP/USD | `always-long` | **−0.064** | −0.206 | 0.00 | 0.31 | 229 | 0 |

**Reading:** all three pairs produce a **negative cost-net expectancy** with a **negative
bootstrap CI lower bound** — i.e. no-trade dominates on every pair. DSR is at/below
significance and promotion is denied for all three. The **selected candidate differs by
pair** (GBDT for USD/JPY, always-long for EUR/USD & GBP/USD), which shows the framework is
not cherry-picking one model that happens to look good — each pair's tune step chose
independently and none survived costs on test. This is a robust, consistent negative result.

## Why the selected candidate is not "always-long = a directional bet"

`always-long` was merely the least-bad candidate on tune for EUR/USD and GBP/USD; on the
untouched test it still nets **negative** after costs (−0.118 / −0.064 R) with negative CI
lower bounds, so it is correctly rejected. Selection ≠ endorsement — the promotion gates,
not the selection step, decide admissibility.

## A cost-model bug we caught (documented for honesty)

The first EUR/USD & GBP/USD runs used `pip_size: 0.01` (copied from USD/JPY). EUR/USD and
GBP/USD are quoted to 4–5 decimals, so their pip is `0.0001`. The wrong pip inflated the
declared spread ~100× and produced absurd R values (−14, −10) plus LAPACK degeneracy. This
was **caught and corrected** (`pip_size: 0.0001`) before recording these results — a
reminder that cost parameters must match each instrument's quoting convention. The lockbox
also (correctly) refused to re-run the changed experiment under the old id, so the corrected
runs use a fresh `…-v2` experiment_id.

## What this does NOT establish

- **No edge on any pair.** Negative by design, as expected for efficient hourly FX with a
  realistic spread.
- **Close-only data** (HistData, no bid/ask, no real volume). Not promotion-admissible.
- **Single year (2024), single timeframe (1h).** Not multi-year; not a live/shadow record.
- Each pair is a separate run; the pipeline still reports `pair_count: 1` per experiment and
  fails the `pair_coverage` gate. "Cross-pair" here means three independent runs compared by
  hand, not a single multi-pair evaluation.

## Files

`cross_pair_summary.json` (the table above), and per pair `eurusd/` & `gbpusd/`
(`promotion_decision.json`, `evaluation.json`, `cost_stress.json`, `data_lineage.json`,
`manifest.json`, `git.json`, `run_info.json`). USD/JPY lives in its own bundle.

## Reproduce

```bash
python3 scripts/fetch_histdata.py --pair EURUSD --year 2024 --out data/real/histdata/eurusd_2024_1h.csv
python3 scripts/fetch_histdata.py --pair GBPUSD --year 2024 --out data/real/histdata/gbpusd_2024_1h.csv
python3 -m fx_backtester.experiment_pipeline run \
  --experiment-manifest experiments/eurusd-histdata-real-2024-1h-20260713.json \
  --output-root <SCRATCH> --trial-ledger <SCRATCH>/tl.jsonl --lockbox-registry <SCRATCH>/lb.jsonl
# …and likewise for gbpusd. Deterministic: EUR/USD hash 0a18713c, GBP/USD hash 8bbdbf9f.
```
