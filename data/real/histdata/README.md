# HistData 1h CSVs (v2 — corrected labels, declared bid basis)

`{pair}_2024_1h.csv` are research-only close-only 1h bars derived from
HistData.com free ASCII M1 downloads by `scripts/fetch_histdata.py`. Each CSV
has a `*.meta.json` sidecar that declares what the numbers MEAN — consumers
must not guess.

## Price semantics

- **Prices are BID quotes** (HistData supplies no ask and no real volume).
  Verified empirically against the Dukascopy bid/ask datasets: 2024 close
  disagreement vs `bid_close` has **p50 = 0.00 pips** on all three pairs
  (p95 1.9–3.9 pips), while vs `mid_close` the p50 is 0.15–0.40 pips ≈ half
  the spread — exactly the signature of a bid series.
- Close-only means no spread information: any cost model applied to this data
  must declare its spread; the file is not admissible for promotion claims.

## Timestamp semantics (v2 fix)

- Labels are the **bar OPEN time in UTC**. The 10:00 row covers [10:00,11:00).
- v1 files (committed 2026-07-13 .. 2026-07-14) were labelled **+1h** by a
  `label="right"` resample bug — measured against Dukascopy/FXCM UTC hours as
  p50 6.5 pips at lag 0 vs 1–2 pips at −1h (incident INC-20260714-M1 in
  `reports/evidence/data-platform-maximization-20260714/`). v2 regenerates the
  files from fresh HistData raw ZIPs with `label="left", closed="left"`;
  after the fix, **lag 0 is the best alignment** on all pairs.
- Provider timestamps are US/Eastern **with DST** (verified empirically:
  post-fix alignment is uniform across winter and summer months), converted
  to UTC. DST-transition ambiguous/nonexistent minutes are dropped (NaT).

## Provenance

Raw ZIP and CSV hashes are pinned in each `*.meta.json`
(`source_zip_sha256`, `csv_sha256`). Historical evidence bundles under
`reports/evidence/histdata-*-20260713/` recorded the v1 files they actually
used and are intentionally left untouched; a uniform label shift does not
affect their leak-freedom conclusions, but session-of-day analyses must use
v2. Validation evidence for the v2 relabel:
`reports/evidence/histdata-v2-relabel-20260715/`.

For research prefer `data/real/dukascopy/` (true bid/ask with measured
spreads); these files remain useful mainly as an independent divergence
check.
