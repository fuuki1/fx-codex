# Institutional benchmark — 2026-07-11

## Verdict

This run verifies deterministic engine behavior and the new risk/measurement controls. It does **not** establish an investable edge. Three of four strategies lose money at the configured baseline-cost proxy; the only positive result has 11 trades, a confidence interval spanning zero, DSR 0.428 and becomes negative at 1.5× costs. No candidate is admissible for validation, paper or live promotion.

## Reproduction boundary

- Run: 2026-07-11 00:16–00:20 JST.
- Code: branch `feat/decision-pipeline-checklist`, commit `81fcb6df9a6db5dcf6240c4122687003fc37345f`, dirty worktree.
- Input: `examples/sample_prices.csv`, 2,700 rows plus header; 900 hourly bars for each of USDJPY, EURUSD and GBPUSD.
- Generator: `examples/generate_sample_data.py`, NumPy RNG seed 42.
- Dataset SHA-256: `b93513ba74070117edc02f404d285670b13f53772ac4ce91a10c87b6c398e427`.
- Initial cash: USD 100,000; default filtered strategy wrappers; no event file.
- Cost proxy at 1×: EURUSD 0.6-pip spread/0.1-pip slippage, GBPUSD 0.9/0.15, USDJPY 0.8/0.2, USD 30 per million commission. Configured hour multipliers remain in force.
- Execution: next-bar simulated market fills with spread, adverse slippage and commission. This is not broker or venue replay.

Base command, repeated for each strategy:

```bash
.venv/bin/python -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy <ma_cross|rsi_mean_reversion|donchian|ai_logistic> \
  --output-trades <path> \
  --output-metrics <path>
```

## Base results

Returns and drawdowns below are percentages; expected values and tail loss are in initial-risk units (`R`).

| Strategy | Return | Trades | Net expectancy | Sharpe | Sortino | Max DD | Profit factor | Median R | 5% ES R | Longest loss streak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| MA cross | -1.1558% | 79 | -0.01309 | -0.8139 | -1.1292 | 4.6782% | 0.9233 | -0.08894 | -0.89457 | 5 |
| RSI mean reversion | +0.1102% | 11 | +0.01073 | +0.3449 | +0.5040 | 1.3344% | 1.0701 | -0.10908 | -0.38996 | 6 |
| Donchian | -2.0516% | 72 | -0.02719 | -1.5411 | -2.1675 | 6.3756% | 0.8521 | -0.08729 | -0.85269 | 6 |
| AI logistic | -5.1374% | 167 | -0.03139 | -7.5481 | -9.8259 | 5.6731% | 0.6187 | -0.02478 | -0.46572 | 9 |

| Strategy | Win rate | Avg hold | Median hold | Fees | Round-trip turnover |
|---|---:|---:|---:|---:|---:|
| MA cross | 40.51% | 12.53 h | 8.0 h | USD 953.59 | 29,143,038 units |
| RSI mean reversion | 36.36% | 4.09 h | 3.0 h | USD 177.34 | 5,445,314 units |
| Donchian | 41.67% | 13.83 h | 6.5 h | USD 869.76 | 26,629,046 units |
| AI logistic | 41.92% | 1.70 h | 1.0 h | USD 1,969.14 | 60,634,040 units |

The RSI headline is not credible evidence: 11 trades are far below the configured governance minimum, its median trade is negative, and its longest loss streak is six.

## Full-engine cost stress

Every cell is a fresh CLI/engine run. Spread, slippage and commission are scaled together; position sizing, fills, stops, exits and equity are recomputed. No post-hoc PnL subtraction is used.

| Multiplier | EURUSD spread/slip | GBPUSD spread/slip | USDJPY spread/slip | Commission/million |
|---:|---:|---:|---:|---:|
| 1× | 0.6 / 0.1 | 0.9 / 0.15 | 0.8 / 0.2 | USD 30 |
| 1.5× | 0.9 / 0.15 | 1.35 / 0.225 | 1.2 / 0.3 | USD 45 |
| 2× | 1.2 / 0.2 | 1.8 / 0.3 | 1.6 / 0.4 | USD 60 |
| 3× | 1.8 / 0.3 | 2.7 / 0.45 | 2.4 / 0.6 | USD 90 |

| Strategy | 1× return / E[R] | 1.5× return / E[R] | 2× return / E[R] | 3× return / E[R] |
|---|---:|---:|---:|---:|
| MA cross | -1.1558% / -0.01309 | -2.1446% / -0.02589 | -3.2307% / -0.04005 | -5.1742% / -0.06579 |
| RSI mean reversion | +0.1102% / +0.01073 | -0.1066% / -0.00902 | -0.3148% / -0.02803 | -0.7076% / -0.06397 |
| Donchian | -2.0516% / -0.02719 | -3.0551% / -0.04153 | -4.0192% / -0.05545 | -5.9875% / -0.08424 |
| AI logistic | -5.1374% / -0.03139 | -7.3227% / -0.04535 | -9.3975% / -0.05890 | -13.1567% / -0.08426 |

All four fail the 2× cost gate. Cost sensitivity is monotonic and material.

## Descriptive statistical diagnostics

The four predefined baselines are treated only as a disclosed illustrative family. Trade `R` sequences use a fixed five-trade circular block, 2,000 bootstrap resamples and 5,000 block sign permutations, seed 42. This block choice was not pre-registered, multi-symbol trade ordering is not a synchronized return panel, and these results are therefore diagnostics rather than promotion evidence.

| Strategy | n | PSR | DSR (4-candidate family) | 95% block CI for E[R] | Raw p | Holm p | MTRL observations |
|---|---:|---:|---:|---:|---:|---:|---:|
| MA cross | 79 | 0.4140 | 0.1656 | [-0.11244, +0.08859] | 0.6043 | 1.0000 | ∞ (nonpositive edge) |
| RSI mean reversion | 11 | 0.5378 | 0.4276 | [-0.18766, +0.20778] | 0.5085 | 1.0000 | 3,011 |
| Donchian | 72 | 0.3284 | 0.1247 | [-0.14483, +0.09345] | 0.6617 | 1.0000 | ∞ (nonpositive edge) |
| AI logistic | 167 | 0.0105 | 0.0002 | [-0.06238, -0.00060] | 0.9644 | 1.0000 | ∞ (nonpositive edge) |

PBO is **unavailable and therefore cannot pass**: there is no complete, timestamp-aligned return matrix for all searched hyperparameter trials. Filling missing timestamps with zero or treating these four unequal trade sequences as aligned trials would be invalid.

## Before/after interpretation

| Measure | Before safety hardening | Final code | Interpretation |
|---|---:|---:|---|
| MA return / trades / E[R] | -1.1558% / 79 / -0.01309 | -1.1558% / 79 / -0.01309 | No alpha change |
| RSI return / trades / E[R] | +0.1102% / 11 / +0.01073 | +0.1102% / 11 / +0.01073 | No alpha change; insufficient sample |
| Donchian return / trades / E[R] | -2.0516% / 72 / -0.02719 | -2.0516% / 72 / -0.02719 | No alpha change |
| AI return / trades / E[R] | -5.1374% / 167 / -0.03139 | -5.1374% / 167 / -0.03139 | No alpha change |

The work improves failure handling, provenance checks, risk measurement and test coverage. It does not improve measured strategy performance, and no such claim is made.

## Non-admissible evidence and missing work

- Synthetic random-walk OHLC has no economic hypothesis, legal market-data lineage, bid/ask history, depth, volume, source disagreement or revision history.
- Macro, COT, calendar, news and scanner inputs were absent; end-to-end point-in-time replay was not tested.
- There was no pre-registered train/tune/calibration/test/lockbox experiment, complete trial ledger, CPCV evidence or durable one-time lockbox.
- Static spread/slippage proxies omit latency, rejection, partial fill, venue/broker state, financing, rollover and outage behavior.
- The positive strategy has only 11 trades; nominal counts are not effective independent sample sizes.
- The run is from a dirty, diverged branch and is not an independently reproduced release artifact.

Final research decision: **functional verification passed; performance validation unavailable; promotion denied**.
