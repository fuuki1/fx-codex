# Decision cadence gap incident — 2026-07-27

## Summary

- Severity: SEV-2 (decision-support cadence degraded; no broker execution exists)
- Affected host: `trader-mini`
- Affected service: `com.fx-codex.briefing`
- Affected output: `logs/briefing_tf_journal.jsonl` and the read-only dashboard
- Status: resolved at 2026-07-27 01:10 UTC after two consecutive recovered slots

Between 2026-07-26 20:00 UTC and 22:50 UTC (2026-07-27 05:00–07:50
JST), USDJPY 15m had 11 unique decision cycles instead of the 35 expected
five-minute cycles. The 24 missing cycles (68.6%) are retained as missing
evidence and were not backfilled.

## Impact

The dashboard omitted intermittent five-minute analysis decisions. The same 11
cycle timestamps were present for every monitored symbol/timeframe combination,
so this was a producer-cadence incident rather than a USDJPY dashboard filter.
The repository remained analysis-only; no broker order or account mutation path
was involved.

## Evidence

Evidence was captured read-only at 2026-07-27 00:27:22 UTC before remediation.

| Artifact | Observation |
| --- | --- |
| `briefing_tf_journal.jsonl` | 11 unique USDJPY 15m timestamps in the affected window |
| `briefing_tf_prices.jsonl` | 33 USDJPY 15m snapshots in the same window |
| `briefing_decisions.jsonl` | 19,583 rows, approximately 1.017 GB |
| `briefing_decision_outcomes.json` | approximately 231.7 MB |
| running `fx_briefing.py` | journal append completed, then CPU remained high in complete-history scoring |

Pre-change SHA-256:

- `briefing_decisions.jsonl`: `7a06c8d673eaf6d879778a84b68e63dbe1277b9821a56653311ae16eccc151e2`
- `briefing_tf_journal.jsonl`: `c818c49c5404d30bc4a98dedbebb670e7217f1e635db964a8c4734b50a64d11d`
- `briefing_tf_prices.jsonl`: `461d04ddc550dab06cb79b99d7becd63402d415b8ee9518b64aa38cd11f63d3a`
- `scripts/fx_briefing_once.sh`: `1949e9c561d59860a699534ac8ceb5ecbc6644d3fc16a39916d496e3f55315d9`
- briefing launchd plist: `7c841ff7fbfeb3d76ea0451511271a3554dbe8f728a7c0e2bbd4e1ca240b1cb9`

## Root cause

Every five-minute briefing run appended its new decisions and then synchronously
read the complete decision audit, rescored the full corpus, rewrote the large
outcome report, and rewrote feedback. Runtime therefore grew with history and
exceeded the five-minute launchd interval. launchd coalesced later starts while
the prior one-shot was still active. The dedicated 15-minute expectancy monitor
also performed the same scoring/writes, creating duplicate writer ownership and
additional contention.

At 2026-07-27 00:45 UTC the old process appended its decision within about two
minutes, but remained active in historical scoring for more than seven minutes.
At 00:52 UTC it overlapped the expectancy monitor, reproducing the missed-slot
condition immediately before deployment.

## Remediation

- The briefing hot path now only appends the immutable audit batch and updates
  the latest snapshot.
- Full outcome scoring and feedback writes are owned only by
  `decision_expectancy_monitor`.
- JSONL inputs are streamed, avoiding a second full raw-log materialization.
- The freshness monitor now checks unique UTC five-minute buckets over a
  30-minute window and reports critical below 80% coverage. Retries within one
  bucket cannot hide a missed slot.

The fix was deployed to `trader-mini` as commit `65532b5` without modifying its
existing dashboard worktree changes and without restarting services or deleting
runtime evidence.

## Validation and recovery

- Clean deployment worktree: 1,044 tests passed.
- Focused remote post-deploy suite: 48 tests passed; Ruff passed.
- Independent adversarial review: no release-blocking or high findings. Its
  duplicate-retry cadence finding was fixed before deployment.
- Recovered USDJPY 15m slots:
  - 2026-07-27 01:00:05.749869 UTC
  - 2026-07-27 01:05:02.009272 UTC
- The 01:05 run exited before launchd started the 01:10 run.

## Residual risk and rollback

The dedicated monitor remains O(history) for transformed scoring entries and
will need incremental/checkpointed scoring as the corpus grows. It is no longer
on the five-minute producer's completion path or a competing feedback writer.

If regression requires rollback, revert `65532b5` on the deployment branch.
Rollback would restore the known cadence defect, so the preferred containment is
to preserve the audit log and disable only the separate monitor while
investigating. No automatic rollback is authorized.
