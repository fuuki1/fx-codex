# Evidence bundle: data-platform maximization (2026-07-14)

**Machine-judged score: 72.67 / 100 (scorecard v1.1.0, status=capped)** — up from
61.0 on the 2026-07-14 baseline bundle. Every point cites a recomputed artifact
in this directory; every cap is an honest limit of what credential-free public
sources can prove:

- **cap ≤80** — the only live source is TrueFX, an unauthenticated *aggregator*
  (indicative rates, always `tradable=false`). A live **broker** stream needs
  OANDA read-only credentials, which this environment does not hold.
- **cap ≤85** — 1 of 30 qualifying continuous-operation days. Only elapsed
  trading days can raise this; the daemon + daily-report loop that counts them
  is installed and exercised (day 1 is in this bundle).

## What was collected (all real, all credential-free)

| source | kind | window | records |
|---|---|---|---|
| Dukascopy (bank datafeed) | h1 bid+ask candles | 2019-01 .. 2026-06, 3 pairs | 277k |
| Dukascopy (bank datafeed) | m1 bid+ask candles | 2024 full year, 3 pairs | 6.2M |
| FXCM (broker archive) | H1 bid+ask candles | 2023 .. 2025, 3 pairs | 107k |
| TrueFX (aggregator) | LIVE top-of-book | 2026-07-14 session (~5h) | see daily report |
| ALFRED | macro vintages | GDPC1/CPIAUCSL/UNRATE | 14 (fresh capture) |

Materialized into 12 registered datasets under `data/real/{dukascopy,fxcm}/`
(46k×3 hourly bars 2019-2026, 75k×3 five-minute bars 2024 with minute-boundary
spread stats, 6.25k×3 m1-derived hourly bars, 17.8k×3 FXCM hourly bars), each
with an append-only lineage manifest (`dataset_registry.jsonl`) pinning every
raw blob hash and fetch cutoff.

## Key findings (dual-source verification doing its job)

1. **Dukascopy vs FXCM 2024**: p50 mid disagreement 0.05-0.1 pips, p95 <1 pip
   across ~5,896 matched hours/pair — two independent institutions agree to
   sub-pip on the year. Worst single hours (5-20 pips at rollover/news)
   exercised the breach policy on real thresholds: states degraded/quarantined,
   values never averaged (`divergence_report.json`).
2. **Committed HistData 1h CSVs carry a uniform +1h label shift**: lag-scan
   shows p50 6.5 pips at lag 0 vs 1-2 pips at −1h, consistent across months
   (`incident_report.json` INC-20260714-M1). The new Dukascopy bid/ask datasets
   supersede HistData for research.
3. **FXCM 2021 archive is untrustworthy** (2.3-6.3% crossed-boundary rows, up
   to 10 pips inverted); 2023-2025 measured exactly clean. Years excluded by
   measurement, not silently repaired (INC-20260714-M2).

## Verification chain

- `replay_report.json` — every dataset re-materialized from the mirrored raw
  bytes in a temp directory: 12/12 hashes match the registry; every TrueFX raw
  blob re-parsed against the accepted log (match).
- `independent_reproduction.json` — fresh detached git worktree at the same
  commit, separate raw store: 12/12 dataset hashes reproduced (match).
- `fault_injection_report.json` — 54/54 fail-closed scenarios pass (corrupt
  payloads, crossed books, stale/duplicate quotes, transport failures,
  tampered raw blobs, lock collisions, no-order-path isolation).
- `secrets_scan.json` — 0 leaks over every git-tracked file; collector imports
  no order/executor path.
- `macro_pit_report.json` — fresh ALFRED capture: GDPC1 2023Q4 second estimate
  22672.859 (vintage 2024-02-01) vs third 22679.255 (vintage 2024-04-05) stored
  separately; as-of query blocks pre-availability reads.
- `daily_report_2026-07-14.json` — day 1 of continuous operation: live poller
  up, mirror up, raw hashes + replay recomputed from the store.

## Honesty notes

- Historical candles are tagged `historical_download`; nothing here claims a
  tradable live book. TrueFX quotes are indicative (`tradable=false`) and are
  scored only as aggregator-live (7/15 + cap 80) under scorecard v1.1.
- Dukascopy/FXCM record counts are single-side candle records (unit declared
  in `collection_summary.json`), not tick quotes.
- Provider padding (closed market), zero-width and crossed boundary rows are
  excluded **with counts** (`quality_report.json`) — never repaired, never
  silently dropped.
- A regression found during the live run is fixed and regression-tested:
  `received_at` is stamped after the fetch completes, so slow fetches no longer
  mis-flag fresh snapshots as future data.

Reproduce: `bash reproduce.sh` (network to the four public endpoints; no
credentials). Historical dataset hashes reproduce exactly; a TrueFX re-run
collects a different live window by definition.
