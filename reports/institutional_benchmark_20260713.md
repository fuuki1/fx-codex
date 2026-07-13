# Institutional benchmark — 2026-07-13

## Verdict

**No predictive improvement is established and promotion is denied.** The current candidate reproduces four deterministic strategies and full-engine 1×/1.5×/2×/3× cost stress. Three strategies lose at observed-cost proxy. RSI mean reversion is positive only at 1×, has 11 trades, a 95% block-bootstrap interval spanning zero, DSR 0.4273, and turns negative at 1.5×. No result is admissible for `validated`, `shadow`, `paper`, or live use.

This benchmark verifies implementation behavior on synthetic data. It is not evidence of an investable edge.

## Reproduction boundary

- Final rerun: 2026-07-13 15:25–15:28 JST.
- Branch/HEAD: `feat/decision-pipeline-checklist` at `3c5bbc7a9889ebbe411699d48a9b1043a3b01e45`, dirty worktree.
- Frozen implementation manifest: [institutional_candidate_20260713_manifest.json](institutional_candidate_20260713_manifest.json), 141 files including 50 test files. The path-and-content tree SHA-256 is `9e34a4ad4de7b620da2476794e34ccde9ce92061c96817ae43133038bf66bd84`, canonical payload SHA-256 is `d6e772faf932f600b8835e102daf54c80ea8765cdba4792a2e3d09ebd7bdab62`, and retained JSON file SHA-256 is `9847f1c31923f103feb74aa98fe5f1790518ad3aa2fe1aafa884ec9ff4f1d1f8`. The manifest covers `fx_backtester/`, `fx_intel/`, `tools/`, `scripts/`, `ops/`, `tests/`, root Python files and requirements; reports and docs are excluded to avoid self-reference.
- Input: `examples/sample_prices.csv`, 2,700 observations plus header; 900 hourly bars for USDJPY, EURUSD and GBPUSD.
- Dataset SHA-256: `b93513ba74070117edc02f404d285670b13f53772ac4ce91a10c87b6c398e427`.
- Raw 32-file temporary artifact-set SHA-256: `3edb1969a976213922ea12e357d012bb593e6fb3366723c82b41be2e86cbfb27` (ordered content hashes of 16 metrics JSON and 16 trade CSV files).
- Retained machine-readable summary: [institutional_benchmark_20260713_metrics.json](institutional_benchmark_20260713_metrics.json), SHA-256 `0638bf76f324db2e78e0cfe12c72f70f0bbddc267a2a63c2fc7a589df4530e67`.
- Generator: `examples/generate_sample_data.py`, NumPy seed 42.
- Initial cash: USD 100,000; no event file; default filtered strategy wrappers.
- Observed-cost proxy: EURUSD 0.6-pip spread/0.1-pip slippage, GBPUSD 0.9/0.15, USDJPY 0.8/0.2, USD 30 per million commission. Configured hourly multipliers remain active.
- Execution: deterministic next-bar simulated market fills with spread, adverse slippage and commission. There is no venue/broker replay, financing, rejection, partial-fill or latency history.
- Artifact audit: file integrity passes, but the synthetic price CSV has no admissible provenance sidecar, so `promotion_eligible=false` as required.

Base command, repeated for all four strategies:

```bash
cd /Users/takahashifuuki/Desktop/fx-codex
.venv/bin/python -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy <ma_cross|rsi_mean_reversion|donchian|ai_logistic> \
  --initial-cash 100000 \
  --output-trades <path> \
  --output-metrics <path>
```

Cost scenarios rerun the full engine with every pair spread/slippage and commission multiplied by 1.5, 2 or 3 using the corresponding CLI overrides. No post-hoc PnL subtraction is used.

Complete cost-rerun command:

```bash
cd /Users/takahashifuuki/Desktop/fx-codex
OUT=<empty-output-directory>
for multiplier in 1 1.5 2 3; do
  extra=()
  if [[ "$multiplier" != "1" ]]; then
    extra+=(--spread-pips "EURUSD=$(awk "BEGIN {print 0.6*$multiplier}")")
    extra+=(--spread-pips "GBPUSD=$(awk "BEGIN {print 0.9*$multiplier}")")
    extra+=(--spread-pips "USDJPY=$(awk "BEGIN {print 0.8*$multiplier}")")
    extra+=(--slippage-pips "EURUSD=$(awk "BEGIN {print 0.1*$multiplier}")")
    extra+=(--slippage-pips "GBPUSD=$(awk "BEGIN {print 0.15*$multiplier}")")
    extra+=(--slippage-pips "USDJPY=$(awk "BEGIN {print 0.2*$multiplier}")")
    extra+=(--commission-per-million "$(awk "BEGIN {print 30*$multiplier}")")
  fi
  for strategy in ma_cross rsi_mean_reversion donchian ai_logistic; do
    .venv/bin/python -m fx_backtester.cli backtest \
      --data examples/sample_prices.csv --strategy "$strategy" --initial-cash 100000 \
      --output-trades "$OUT/${strategy}_${multiplier}_trades.csv" \
      --output-metrics "$OUT/${strategy}_${multiplier}_metrics.json" \
      "${extra[@]}"
  done
done
```

## Research-design availability

| Required item | Evidence in this run |
|---|---|
| Data period | Synthetic hourly sample; 900 bars per pair |
| Horizon | Strategy-dependent holding period; median 1–8 hours, not a pre-registered fixed target horizon |
| Train/tune/calibration/test | Not present for this deterministic functional benchmark |
| Untouched lockbox | Not present; cannot pass |
| Complete search trial count | Unavailable; four disclosed baseline strategies and 16 cost reruns are not a complete research trial ledger |
| PBO | Unavailable; no complete timestamp-aligned return matrix for all searched trials |
| Probability calibration/Brier | Unavailable; these runs do not emit independently calibrated probabilities |
| Abstention comparison | Unavailable as a causal model comparison; existing strategy filters remain enabled |
| Regime performance | Unavailable; synthetic sample and no pre-registered regime partitions |
| Feature ablation | Unavailable for these deterministic engine checks |

Missing evidence is an evaluation-unavailable result, never a pass.

## Base results

Returns and drawdowns are percentages. Expected values and tail loss are initial-risk units (`R`).

| Strategy | Return | Trades | Net E[R] | Sharpe | Sortino | Max DD | Profit factor | Median R | 5% ES R | Longest loss streak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| MA cross | -1.0653% | 79 | -0.012005 | -0.7490 | -1.0420 | 4.6782% | 0.9272 | -0.09319 | -0.83065 | 5 |
| RSI mean reversion | +0.1102% | 11 | +0.010726 | +0.3449 | +0.5040 | 1.3344% | 1.0701 | -0.10908 | -0.38996 | 6 |
| Donchian | -2.0516% | 72 | -0.027193 | -1.5411 | -2.1675 | 6.3756% | 0.8521 | -0.08729 | -0.85269 | 6 |
| AI logistic | -5.1374% | 167 | -0.031391 | -7.5481 | -9.8259 | 5.6731% | 0.6187 | -0.02478 | -0.46572 | 9 |

| Strategy | Win rate | Avg/median hold | Fees | Round-trip turnover |
|---|---:|---:|---:|---:|
| MA cross | 39.24% | 12.44 h / 8.0 h | USD 953.66 | 29,145,598 units |
| RSI mean reversion | 36.36% | 4.09 h / 3.0 h | USD 177.34 | 5,445,314 units |
| Donchian | 41.67% | 13.83 h / 6.5 h | USD 869.76 | 26,629,046 units |
| AI logistic | 41.92% | 1.70 h / 1.0 h | USD 1,969.14 | 60,634,040 units |

The sole positive headline is not credible evidence: 11 trades are below the governance minimum, its median trade is negative, and its uncertainty interval includes zero.

## Full-engine cost stress

| Strategy | 1× return / E[R] | 1.5× return / E[R] | 2× return / E[R] | 3× return / E[R] |
|---|---:|---:|---:|---:|
| MA cross | -1.0653% / -0.012005 | -2.0621% / -0.024893 | -3.1560% / -0.039137 | -5.1137% / -0.065035 |
| RSI mean reversion | +0.1102% / +0.010726 | -0.1066% / -0.009021 | -0.3148% / -0.028026 | -0.7076% / -0.063968 |
| Donchian | -2.0516% / -0.027193 | -3.0551% / -0.041533 | -4.0192% / -0.055446 | -5.8381% / -0.082066 |
| AI logistic | -5.1374% / -0.031391 | -7.3227% / -0.045345 | -9.3975% / -0.058899 | -13.1567% / -0.084262 |

All four fail the 2× cost gate. The positive 1× RSI result disappears at 1.5×. Cost sensitivity is monotonic and economically material.

## Descriptive statistical diagnostics

These diagnostics use each strategy's trade-R sequence, a fixed five-trade circular block, 2,000 bootstrap resamples, 5,000 block sign permutations and seed 42. The four predefined strategies are only a disclosed illustrative family. Their trade timestamps are not a synchronized trial matrix, so this section cannot substitute for CPCV/PBO or promotion evidence.

Exact diagnostic command, applied to each `<strategy>_1_trades.csv` `r_multiple` column:

```bash
OUT=<output-directory> .venv/bin/python - <<'PY'
import os
from pathlib import Path
import pandas as pd
from fx_backtester.overfitting import deflated_sharpe_ratio, per_period_sharpe
from fx_backtester.statistical_validation import (
    adjust_p_values, block_sign_permutation_test,
    circular_block_bootstrap_mean_ci, minimum_track_record_length,
    probabilistic_sharpe_ratio,
)
names = ["ma_cross", "rsi_mean_reversion", "donchian", "ai_logistic"]
root = Path(os.environ["OUT"])
returns = {name: pd.read_csv(root / f"{name}_1_trades.csv")["r_multiple"] for name in names}
trial_sharpes = [per_period_sharpe(returns[name]) for name in names]
raw_p = []
for name in names:
    r = returns[name]
    print(name, probabilistic_sharpe_ratio(r), deflated_sharpe_ratio(r, trial_sharpes))
    print(circular_block_bootstrap_mean_ci(r, block_size=min(5, len(r)), resamples=2000, seed=42))
    permutation = block_sign_permutation_test(
        r, block_size=min(5, len(r)), permutations=5000, seed=42
    )
    raw_p.append(float(permutation["p_value"]))
    print(permutation, minimum_track_record_length(r))
print("holm", adjust_p_values(raw_p, method="holm"))
PY
```

| Strategy | n | PSR | DSR (4-strategy descriptive family) | 95% block CI for E[R] | Raw p | Holm p | MTRL observations |
|---|---:|---:|---:|---:|---:|---:|---:|
| MA cross | 79 | 0.4193 | 0.1687 | [-0.11207, +0.08859] | 0.6015 | 1.0000 | ∞ (nonpositive edge) |
| RSI mean reversion | 11 | 0.5378 | 0.4273 | [-0.18766, +0.20778] | 0.5085 | 1.0000 | 3,011 |
| Donchian | 72 | 0.3284 | 0.1243 | [-0.14483, +0.09345] | 0.6617 | 1.0000 | ∞ (nonpositive edge) |
| AI logistic | 167 | 0.0105 | 0.0002 | [-0.06238, -0.00060] | 0.9644 | 1.0000 | ∞ (nonpositive edge) |

PBO remains unavailable and therefore cannot pass. Filling missing timestamps with zero or treating unequal trade sequences as aligned trials would be invalid.

## Comparison with the 2026-07-11 historical run

| Strategy | 2026-07-11 return / E[R] | Current return / E[R] | Interpretation |
|---|---:|---:|---|
| MA cross | -1.1558% / -0.01309 | -1.0653% / -0.012005 | Engine/risk accounting changed; still negative; not alpha evidence |
| RSI mean reversion | +0.1102% / +0.01073 | +0.1102% / +0.010726 | No material change; sample remains insufficient |
| Donchian | -2.0516% / -0.02719 | -2.0516% / -0.027193 | No material change |
| AI logistic | -5.1374% / -0.03139 | -5.1374% / -0.031391 | No material change |

The earlier report is retained as historical evidence and must not be presented as the current candidate result. The MA difference reflects implementation changes, not a controlled predictive-model comparison. Code-quality and safety improvements are not predictive-performance improvements.

## Limitations and decision

- Synthetic random-walk OHLC has no economic hypothesis, legal market-data lineage, historical bid/ask, venue, depth, reliable volume, source disagreement or revisions.
- Macro, rates, COT, calendar, options, news and cross-asset inputs are absent.
- There is no pre-registered five-way temporal experiment, complete aligned trial ledger, CPCV result, durable one-time lockbox, calibrated reliability diagram or real-data ablation.
- Static cost proxies omit financing/rollover, gap liquidity, latency, rejection, partial fill and broker reconciliation.
- Pair/session/regime contribution and no-trade coverage are not promotion-admissible in this sample.

Final research decision: **functional and cost-stress reproduction passed; predictive improvement not demonstrated; performance validation unavailable; promotion denied.**
