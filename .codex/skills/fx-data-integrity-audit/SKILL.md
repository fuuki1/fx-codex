---
name: fx-data-integrity-audit
description: Verify point-in-time integrity, timestamps, revisions, as-of joins, duplicates, freshness, OHLC, quotes, and provenance for FX market, macro, COT, news, calendar, and journal data. Use before labeling, training, backtesting, or promotion.
---

# FX data-integrity audit

## Purpose and inputs

Prove what was knowable at each prediction time and whether data is usable. Inputs are data paths/tables, source/API contracts, expected cadence, decision cutoff, and intended label horizon.

## Procedure

1. Preserve raw inputs and compute SHA-256. Identify source, schema, timezone, cadence, and natural key.
2. Require aware UTC and distinguish `event_time`, `published_time`, `available_time`, `ingested_time`, `source_time`, and `revision_time`. Reject guessed DST and naive timestamps.
3. Verify `available_time <= prediction_time` with backward as-of joins. Replay revisions/vintages; never overwrite historical values with the latest observation.
4. Check duplicates, monotonicity, missing bars, weekends/holidays/rollover, future stamps, staleness, invalid OHLC, nonpositive prices, bid/ask crossing, spreads, stale quotes, volume, and source disagreement.
5. Mark forming-bar snapshots as unsuitable for post-prediction high/low labels. Require an explicit trusted `ohlc_scope` for TP/SL first-touch use.
6. For COT, use actual publication/first-ingested availability—not report date plus a fixed delay. For news/events, preserve actual/revised values and first-seen timestamps.
7. Run leakage fixtures covering future features, revisions, normalization fit, label overlap, forward fills, news availability, and calibration/test reuse.

## Commands

```bash
.venv/bin/pytest -q tests/test_point_in_time.py tests/test_price_history.py tests/test_labeling.py
.venv/bin/python tools/journal_gap_audit.py --help
.venv/bin/python tools/data_freshness_monitor.py --help
```

Primary code is in `fx_backtester/point_in_time.py`, `fx_backtester/labeling.py`, and `fx_intel/price_history.py`.

## Pass and fail conditions

Pass requires zero future matches, immutable/versioned records, reproducible hashes, unique natural keys, acceptable freshness/missingness, explicit OHLC scope, and source-specific publication rules. Missing availability/revision metadata is failure for point-in-time claims. Unknown quality yields abstention/evaluation unavailable.

## Output format

Report dataset hash and window; schema/timestamp map; quality metrics; leakage probes; source limitations; decision (`usable`, `research-only`, `abstain`, or `invalid`); exact failing keys/rows and remediation.

## Prohibited actions

Never coerce naive time to UTC silently, forward-fill across publication boundaries, zero-fill unknown returns, use forming-bar ranges as future paths, mutate raw history, or call a public proxy proprietary order flow.

## Example

“Use `$fx-data-integrity-audit` on the FRED, COT, calendar, and `briefing_tf_prices.jsonl` inputs before constructing a 4h label dataset.”
