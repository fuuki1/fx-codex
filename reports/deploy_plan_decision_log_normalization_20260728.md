# Deployment plan — decision-log normalization and scoring performance

Status: **draft for human review. Not authorized. No production change has been made.**
Target host: `trader-mini` (`/Users/fuuki/srv/fx-codex`)
Source: PR #74, head `d9f7f48`

## Scope

| Change | Production effect |
| --- | --- |
| `d99dc8b` news_items reference folding | **writer behavior changes**: new decision lines carry `news_item_refs` and a `_news.jsonl` sidecar appears |
| `e74eda1` / `ca84749` JSON fast paths | performance only; byte-identical output verified on 8,676 real records |
| `cd49bc5` compact outcome report | `briefing_decision_outcomes.json` written without indentation |
| `ae08308` / `d9f7f48` effective-sample dependence | **evidence gates tighten**; no output format change |

Only the first and last rows change observable behavior. The middle two are
pure performance with verified identical output.

## Pre-deployment facts to re-confirm on the host

The values below come from `reports/incident_decision_cadence_gap_20260727.md`
and must be re-measured immediately before deployment, not assumed.

- `logs/briefing_decisions.jsonl` — was 19,583 rows / ~1.017 GB
- `logs/briefing_decision_outcomes.json` — was ~231.7 MB
- current `HEAD` SHA of the deployment worktree, and its drift from `origin/main`
- whether `com.fx-codex.briefing`, `com.fx-codex.snapshot`, `com.fx-codex.health`
  and the monitors are the only active writers

## Ordering constraint

`ae08308` tightens evidence gates. Effective sample counts will **drop** for
batches holding correlated positions, so `sample_ok` may flip to false where it
was previously true. This is the intended correction, not a regression — but it
must not be discovered at the same time as a writer format change. Deploy and
observe the code change first, then run the backfill.

## Step 1 — deploy code only (no runtime data change)

Nothing in this step rewrites existing logs. New decision lines will start
carrying `news_item_refs`, and the sidecar is created on first write.

1. Capture pre-change evidence read-only: SHA-256 of the three JSONL logs, sizes,
   row counts, current worktree SHA.
2. Deploy the reviewed SHA to the deployment worktree without touching its
   existing uncommitted dashboard changes.
3. Do **not** restart services manually. Let the next scheduled `com.fx-codex.briefing`
   pick it up, so a failure is contained to one slot.
4. Observe two consecutive five-minute slots. Confirm:
   - the briefing exits 0 and appends within its slot
   - `logs/briefing_decisions_news.jsonl` is created and grows
   - new lines contain `news_item_refs`, old lines are untouched
   - freshness coverage stays above the 80% critical threshold

**Rollback**: revert the deployment SHA. Mixed-format logs are safe — the reader
handles both, and `reconcile` treats pre-normalized lines as already normalized.

## Step 2 — backfill existing history (requires a stop window)

This is the only step that rewrites runtime data. `tools/decision_store_admin.py`
defaults to dry-run and, with `--apply`, writes a full backup plus a quarantine
file before atomically replacing the original.

1. `launchctl bootout` `com.fx-codex.briefing` and confirm no `fx_briefing.py`
   process is running. The tool's own docstring requires the writer to be stopped.
2. Run `audit` (read-only) and record the reported duplication.
3. Run `backfill` **without** `--apply` and review the dry-run report.
4. Run `backfill --apply`. Verify the backup directory, the quarantine file, and
   `backfill_report.json` exist.
5. Run `reconcile`. **Require `drift == 0`** (the command itself exits 1 otherwise).
6. Run `tools/decision_store_parity.py` against the backup and the normalized log.
   **Require verdict `parity_verified`.** This is the stronger check: it restores
   every normalized event from the sidecar and compares it to the pre-backfill
   backup event by event, so it proves the rewrite lost nothing. `reconcile`
   alone only proves internal consistency.
7. Restart the briefing service and observe two consecutive recovered slots.

**Rollback**: restore the untouched `<name>.original` copy that `--apply` wrote
into the timestamped backup directory. Note that `decision_store_admin.py`
exposes only `audit`, `backfill`, and `reconcile` — `restore_event()` exists as a
library function used by the parity checker, **not** as a runnable rollback
subcommand. Do not plan a rollback around a `restore` CLI that does not exist.

Duration is dominated by a single pass over ~1 GB. The only measurement available
is local (~187 MB/s on a 25 MB file), which does not transfer to the host: budget
the stop window generously and re-measure rather than assuming.

Exact commands, in order. Steps 2–3 and 5–6 are read-only; only step 4 writes.

```bash
python3 tools/decision_store_admin.py --path logs/briefing_decisions.jsonl audit
python3 tools/decision_store_admin.py --path logs/briefing_decisions.jsonl backfill
python3 tools/decision_store_admin.py --path logs/briefing_decisions.jsonl backfill --apply
python3 tools/decision_store_admin.py --path logs/briefing_decisions.jsonl reconcile
python3 tools/decision_store_parity.py --path logs/briefing_decisions.jsonl
```

`decision_store_parity.py` defaults to the newest `decision_store_admin-*/…original`
backup, so `--backup` only needs to be passed when targeting an older run.

### Rehearsal on a copy (2026-07-28)

The full sequence was executed against a copy of the development
`logs/briefing_decisions.jsonl` (593 rows, 25.9 MB). This is 1/41 of the host's
corpus, so it validates the *procedure*, not the duration.

| Step | Result |
| --- | --- |
| `audit` | 71.9% news bytes, duplication factor 110.9, already-normalized 0 |
| `backfill` (dry-run) | reported the plan; **source file byte-count unchanged**, no directories created |
| `backfill --apply` | 25,911,512 → 9,492,654 bytes (−63.4%), sidecar 188,067 bytes |
| `reconcile` | `drift: 0`, exit 0 |
| `decision_store_parity.py` | `matched: 593/593`, `mismatched: 0`, `restore_failures: 0`, exit 0 |
| rollback from `.original` | restored file is **byte-identical to the pre-run original** (same SHA-256) |

Two findings worth carrying into the real run:

- The dry-run genuinely writes nothing — it is safe to run first on the host.
- `reconcile` reports `normalized_events: 570` out of `checked_events: 593`. The
  23 difference is events that carried no `news_items` to fold, not a failure;
  parity still matched all 593. Do not treat that gap as drift.

## Step 3 — observe the gate change

After Step 1, effective sample counts may fall. Confirm that any newly failing
`sample_ok` corresponds to genuinely correlated batches rather than a defect.
Do not relax `min_samples` to compensate — that would reintroduce the inflated
evidence this change removes.

## Explicitly out of scope

- No launchd unit is added, removed, or re-scheduled.
- No broker, order, or `ALLOW_LIVE` path is involved; the repository remains analysis-only.
- The operational-store scoring connection is **not** part of this deployment.
  It was reverted from PR #74 because its dependencies are untracked, and it will
  be resubmitted separately once `fx_intel/operational_store.py` is in the repository.
- Existing runtime evidence is never deleted. Missing cycles stay missing rather
  than being backfilled with fabricated decisions.
