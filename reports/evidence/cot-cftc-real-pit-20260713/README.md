# Real CFTC COT PIT evidence — 2026-07-13

**First non-synthetic real-data artifact in this repository.** Produced by fetching
live public CFTC Legacy Futures-Only Commitments-of-Traders data through the repo's
own research-only adapter (`fx_intel/cot_pit.py` + `tools/cot_pit_pipeline.py`) and
running it through the full point-in-time contract: capture → attest → materialize →
audit → as-of.

`research_only: true`, `promotion_eligible: false` on every artifact. **Nothing here
is a performance claim or an alpha claim.** COT is a public, weekly, cross-contract
*futures*-positioning proxy — not spot FX order flow.

## What this DOES prove (verified, reproducible)

| Property | Evidence |
|---|---|
| Real regulatory data ingests through the adapter | 13,727 FX rows, 8 currencies, report dates **1986-01-15 → 2026-07-07** (1,926 weekly reports) |
| Content-addressed raw storage with integrity | every page stored as `body_sha256`; all pages SHA256-verified on decode |
| Pagination completeness guard | count-before == count-after (`expected_row_count: 13727`), fail-closed on drift |
| Deterministic replay from raw | `audit` reconstructs 13,727 observations from raw bytes, `passed: true, errors: []` |
| **Point-in-time gate is real** | as-of **2026-07-13T12:00Z → `unavailable`** (before the 13:06Z capture); as-of **2026-07-14 → `usable`** with real positioning (JPY net −123,778, EUR −16,227, GBP −87,903, USD +13,269). Data captured at 13:06 cannot be used for a 12:00 decision. |
| Availability normalized honestly | `available_time` = capture/attestation instant, **not** the report date. Flag `availability_normalized_to_actual_use`. |
| Full provenance | manifest pins `code.commit = eb4263c`, `dirty_worktree: false`, raw-input SHA256s, `created_at` |

## What this does NOT prove (explicit non-claims)

- **No model performance, no expectancy, no Sharpe.** This is a *data-integrity* proof only.
- **No promotion eligibility.** Research-only; the promotion gates would (correctly) reject it.
- **Local custody, not independent custody.** The release attestation is a locally-bound
  sidecar; it is not externally signed or independently timestamped
  (flag `release_attestation_is_local_not_independent_custody`).
- **Single snapshot, not continuous operation.** One capture on one day. The
  30-trading-day continuous-collection SLO required for shadow→paper is **not** met by this.
- **COT only.** Says nothing about FRED, prices, news, or system-wide PIT integrity
  (flag: "COT success does not attest FRED, prices, features, or system-wide PIT integrity").
- **Revision detection is limited** to CFTC's stable row id
  (flag `revision_detection_limited_to_stable_cftc_row_id`).

## Files

| File | What it is |
|---|---|
| `pit_dataset_manifest.json` / `.sha256` | materialized dataset manifest (provenance, quality-flag counts, raw-input hashes) |
| `cot_release_attestation.json` | local release sidecar (report 2026-07-07, basis `actual_release_notice`) |
| `capture_bundle_metadata.json` | capture bundle **with the 82MB body bytes stripped** (keeps per-page `body_sha256`) |
| `run_transcript.txt` | audit + before/after as-of proof output |
| `reproduce.sh` | re-fetches live CFTC data and regenerates everything into a scratch dir |

The ~82MB raw capture and the 80MB `records.jsonl` are intentionally **not committed**
(content-addressed, regenerable via `reproduce.sh`).

## Reproduce

```bash
reports/evidence/cot-cftc-real-pit-20260713/reproduce.sh   # fetches live CFTC, prints proofs
```

Requires network access to `publicreporting.cftc.gov` and `www.cftc.gov`. Deterministic
given the same upstream data; if CFTC has published a newer report, adjust `--report-date`
/ `--released-at` in `reproduce.sh` for that week (the older reports remain identical).
