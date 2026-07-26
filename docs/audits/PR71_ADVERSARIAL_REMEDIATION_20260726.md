# PR #71 adversarial remediation record

Review date: 2026-07-26 (Asia/Tokyo)

Base: `bee7427ec0272fbb2fce85345a22e39e8ceb9cf7` (`origin/main`)

Status: **LOCAL REMEDIATION PASSED; DRAFT PR; NOT DEPLOYED**

The Mac mini canonical collector, materializer, and health topology remains
stopped. This record covers repository changes and local synthetic evidence
only. It is not approval to deploy and is not evidence of predictive
performance, provider-captured capacity, a 30-trading-day prospective corpus,
or an independent secondary price source.

## Remediation by functional unit

1. `9a9b561` — canonical runtime and installer
   - removed unsupported nested lock-wrapper arguments from launchd templates;
   - parses the real collector, materializer, freshness, and wrapper CLIs during
     dry-run;
   - validates the exact official HTTPS pricing host, account pricing-stream
     path, and query before attaching a potentially trading-capable token;
   - bounds production streaming results instead of retaining one result object
     per provider message;
   - requires a clean checkout at an exact lowercase 40-hex approved SHA and
     uses clean-install-only label/plist semantics with scoped rollback.

2. `593f00e` — atomic completed-bar materialization
   - moved completed bid/ask snapshots and the bridge checkpoint into one
     SQLite WAL database;
   - commits snapshot inserts and checkpoint advancement in the same
     `BEGIN IMMEDIATE` transaction;
   - enforces the `(event_time, symbol, timeframe)` identity and immutable
     snapshot schema;
   - binds lineage to journal identity, commit sequence, raw hash, and terminal
     hash;
   - computes freshness from logical event time and validates the full four-pair
     by four-timeframe latest slot;
   - rejects trade-outcome bars whose open time predates the prediction.

3. `7e80a1f` — journal capacity, tail verification, and restore safety
   - reserves a bounded 4 MiB terminal allowance before accepting raw bytes and
     rechecks capacity inside both raw and terminal transactions;
   - replaces an oversized terminal with a bounded `QUARANTINED` disposition;
   - verifies exact table/index/trigger schemas and anchored active/previous
     shard tails;
   - avoids duplicate full-history verification in production bridge/collector
     startup paths;
   - bases journal freshness on the last logical event rather than file mtime;
   - rejects symlinked restore destinations before creating output.

4. `6daa3e6` — prospective qualification and capacity verdicts
   - requires usable live OANDA streaming rows for USDJPY, EURUSD, GBPUSD, and
     AUDUSD, with valid provider-event and receipt timestamps;
   - requires typed, same-day, distinct primary/secondary/replay evidence with
     distinct paths, hashes, and source identifiers;
   - makes the soak command return nonzero unless throughput, visibility,
     recovery, replay, file-layout, and post-run disk-reserve checks all pass;
   - labels probe RSS as test-process evidence, not production-daemon memory
     evidence.

No order creation, modification, cancellation, closing, position mutation, or
account-risk mutation path was added.

## Post-remediation evidence

| Artifact | SHA-256 | Result |
|---|---|---|
| `CAPTURE_JOURNAL_POST_REVIEW_SOAK_20260726_10000.json` | `247e14e67f74447808fa977ceda2ad9b2f418644cb9ba87bd849bb33aa27d2d8` | `passed=true`; 10,000/10,000 replayed; 386.979 msg/s; post-run 10 GiB reserve passed |
| `CAPTURE_JOURNAL_ATOMIC_STORE_CRASH_PROBE_20260726.json` | `1b8fdb801bbadf42e3f91382536f026beba0e4917d5fa24c5c2e7aad28de99bc` | `passed=true`; seven process boundaries; materialized rows/checkpoint both absent before resume and 4/1 after resume |

The soak artifact records repository revision
`6daa3e6f54468ca5109bc1df10fd840e2117e8ab` and
`worktree_dirty=false`. Its payload is generated OANDA-shaped replay data.
Earlier capacity artifacts remain historical pre-remediation evidence; they
must not be interpreted as post-review validation or production-daemon RSS
evidence.

## Validation

- Ruff: passed.
- Black check: 248 files unchanged.
- mypy: 91 source files, no issues.
- pytest: 1,100 passed, 1 skipped.
- post-remediation collector/materializer/journal/freshness/report targeted
  suite: 181 passed.
- briefing dry-run: completed and abstained from entry.
- synthetic sample backtest: completed with negative expectancy; functional
  check only and inadmissible for a performance or promotion claim.
- `git diff --check`: passed.
- tracked-diff credential-pattern scan: no Discord webhook, OANDA token, bearer
  token, or GitHub token match.

## Remaining blockers

- No approved clean deployment SHA has been installed on the Mac mini.
- No provider-captured sizing, production-daemon RSS soak, target filesystem
  `ENOSPC` drill, full-size off-host archive, or independent restore has passed.
- The independently governed prospective secondary source and typed daily
  evidence producers are not connected.
- The qualifying 30-trading-day prospective window remains at zero.
- Canonical prices remain isolated shadow output and are not decision inputs.

These blockers keep the PR draft and the deployment abstention in force.
