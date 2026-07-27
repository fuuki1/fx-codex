# SQLite operational store

`fx_intel.operational_store` removes repeated full-history JSONL scans from the
candidate query path. It is a transactional operational index; append-only raw
evidence remains authoritative, and JSONL remains the active runtime path until
an explicit, repeatedly parity-verified reader cutover.

## Safety contract

- SQLite must contain the March 2026 WAL-reset fix: 3.51.3+, 3.50.7+, or 3.44.6
  within those supported backport branches. Startup fails closed otherwise.
- Every mutation goes through `open_operational_writer`. A database-specific
  `flock` permits one writer process across briefing, snapshot, horizon, and
  monitor jobs.
- WAL uses `synchronous=FULL`, foreign keys, `STRICT` tables, a finite
  `busy_timeout`, a dedicated SQLite `application_id`, and bounded automatic
  checkpoints.
- Prediction, price, and outcome natural keys are immutable and idempotent.
  Reusing a key with different content is an error. Database triggers block
  history updates/deletes even if a caller bypasses the Python insert methods;
  only a prediction's `pending` status may move forward to a terminal state.
- Times are aware UTC and stored as integer nanoseconds. Prediction availability
  after prediction time, future ingestion, crossed quotes, invalid OHLC, and
  backward checkpoints fail closed.
- Backups use SQLite's Online Backup API, never overwrite an existing target,
  and report the completed snapshot's SHA-256.
- Shadow synchronization pins each raw source by path, device, inode, byte
  offset, and the SHA-256 of its last complete line. Replacement, truncation,
  or a changed boundary fails closed.
- Decision and price appenders hold an exclusive source-file lock through
  flush+`fsync`; shadow readers hold the matching shared lock while capturing a
  byte range. This preserves complete writer batches without making SQLite the
  evidence authority.
- A new PIT-eligible decision is visible only after a three-step cross-log
  transaction: fsync the full decision batch, fsync the compact journal batch
  and its local marker, then fsync a self-hashed cross-log commit marker into
  the full log. The marker names the exact decision IDs and both canonical
  batch hashes. Readers fail closed on a missing, conflicting, or mismatched
  marker; the shadow cursor stops before the newest pending full batch. If a
  crashed producer leaves it orphaned and a later transaction commits, sync
  records the orphan as an explicit rejection and continues from the later
  commit instead of permanently stalling the contiguous cursor.
- A cursor can advance only in the same transaction as a contiguous SHA-256
  chunk manifest. Malformed complete lines advance only after an append-only
  rejection record is committed. An incomplete trailing line is left unread.
- The two-source scheduled sync is one SQLite transaction. If either source
  fails, neither cursor advances.
- `audit` checks SQLite/FK integrity plus chunk continuity, the final
  cursor-to-chunk boundary, orphan chunks, and whether each rejection belongs
  to a consumed chunk.

## Commands

The default development virtual environment currently links an unsafe SQLite
build, so operational commands must use Python linked to an approved SQLite.
The isolated validation runtime used for this candidate is
`runs/python314-safe-venv/bin/python` (Python 3.14 / SQLite 3.53.4). Do not
substitute the system `sqlite3` 3.51.0 CLI.

```bash
runs/python314-safe-venv/bin/python tools/operational_store.py \
  --db logs/fx_operational.sqlite3 init \
  --writer-id manual-schema-init

runs/python314-safe-venv/bin/python tools/operational_store.py \
  --db logs/fx_operational.sqlite3 audit

runs/python314-safe-venv/bin/python tools/operational_store.py \
  --db logs/fx_operational.sqlite3 checkpoint \
  --writer-id scheduled-checkpoint \
  --mode PASSIVE

runs/python314-safe-venv/bin/python tools/operational_store.py \
  --db logs/fx_operational.sqlite3 backup \
  --output backups/fx_operational-20260727.sqlite3
```

Candidate migration uses stopped-writer snapshots, never changing active logs:

```bash
runs/python314-safe-venv/bin/python tools/operational_store_migrate.py \
  --db runs/operational-migration/candidate.sqlite3 \
  --report runs/operational-migration/migration.json \
  --decisions snapshots/briefing_decisions.jsonl \
  --decisions-sha256 '<sha256>' \
  --prices snapshots/briefing_tf_prices.jsonl \
  --prices-sha256 '<sha256>' \
  --writer-id migration-operator

runs/python314-safe-venv/bin/python tools/operational_store_parity.py \
  --db runs/operational-migration/candidate.sqlite3 \
  --report runs/operational-migration/parity.json \
  --decisions snapshots/briefing_decisions.jsonl \
  --decisions-sha256 '<sha256>' \
  --prices snapshots/briefing_tf_prices.jsonl \
  --prices-sha256 '<sha256>'

runs/python314-safe-venv/bin/python tools/operational_store_benchmark.py \
  --db runs/operational-migration/candidate.sqlite3 \
  --decisions snapshots/briefing_decisions.jsonl \
  --prices snapshots/briefing_tf_prices.jsonl \
  --output runs/operational-migration/benchmark.json
```

Migration and parity also discover every compact journal referenced by a valid
cross-log marker, pin its path/size/SHA-256 in the report, and verify it again
after the scan. A compact append, replacement, missing file, or hash mismatch
invalidates the candidate even when the full decision log itself is unchanged.

After exact parity has been independently reported, bootstrap the active raw
EOF exactly once. The bootstrap refuses a changed database hash, a non-zero WAL,
changed source bytes, path drift, or an incomplete final JSONL line:

```bash
runs/python314-safe-venv/bin/python tools/operational_store_shadow_sync.py \
  bootstrap \
  --db runs/operational-migration/candidate.sqlite3 \
  --parity-report runs/operational-migration/parity.json \
  --report runs/operational-migration/shadow-bootstrap.json \
  --decisions logs/briefing_decisions.jsonl \
  --prices logs/briefing_tf_prices.jsonl \
  --writer-id shadow-bootstrap

runs/python314-safe-venv/bin/python tools/operational_store_shadow_sync.py \
  sync \
  --db runs/operational-migration/candidate.sqlite3 \
  --report runs/operational-migration/shadow-sync-20260727T130000Z.json \
  --decisions logs/briefing_decisions.jsonl \
  --prices logs/briefing_tf_prices.jsonl \
  --writer-id shadow-sync-20260727T130000Z

runs/python314-safe-venv/bin/python tools/operational_store_full_replay.py \
  --db runs/operational-migration/candidate.sqlite3 \
  --report runs/operational-migration/full-replay-20260727.json \
  --decisions logs/briefing_decisions.jsonl \
  --prices logs/briefing_tf_prices.jsonl \
  --writer-id full-replay-20260727

runs/python314-safe-venv/bin/python tools/operational_read_api.py \
  --db runs/operational-migration/candidate.sqlite3 \
  --port 8770
```

For a large source that continues to receive canonical locked appends, migration
may run against an immutable APFS clone instead. Bootstrap can then bind the
verified clone bytes to the matching prefix of the live inode:

```bash
runs/python314-safe-venv/bin/python tools/operational_store_shadow_sync.py \
  bootstrap \
  --allow-source-prefix \
  --db runs/operational-migration/candidate.sqlite3 \
  --parity-report runs/operational-migration/parity.json \
  --report runs/operational-migration/shadow-bootstrap.json \
  --decisions logs/briefing_decisions.jsonl \
  --prices logs/briefing_tf_prices.jsonl \
  --writer-id shadow-bootstrap
```

This mode hashes exactly the clone's reported byte count from each live source
under the source-file shared lock, requires the prefix to end on a complete
line, and pins the live device/inode at that byte offset. Any later complete
lines are consumed by the first normal sync. It does not accept truncation,
prefix drift, identity replacement, or a non-append-only producer.

`sync` returns exit 1 after durably recording a malformed row or a new
PIT-ineligible decision. Integrity/identity failures return exit 2 and do not
advance that source. A no-change run returns exit 0 without creating a chunk.
Reports are create-only; use a unique path for every scheduled run.

The full replay holds the operational single-writer lease and shared raw-file
locks while it:

- re-hashes every manifest chunk and recomputes its complete-line count and
  final-line boundary;
- re-projects every decision and price through the current adapters;
- verifies each PIT decision's cross-log marker against the locked compact
  journal batch before accepting the full batch;
- compares exact canonical payloads, stored record SHA-256 values, timestamps,
  PIT eligibility, due times, OHLC fields, populations, and incremental
  rejection entries;
- fails on raw/DB payload drift, missing or unexpected populations, an
  unconsumed complete suffix, or an incomplete source tail.

`full_replay_verified` means projection integrity is exact. Known legacy
exclusions and PIT-ineligible decisions remain separately visible as
`data_quality_status=usable_with_exclusions`; they are not relabeled as clean.

The read API is intentionally loopback-only and independent of the active
dashboard:

- `GET /v1/decisions?limit=50&symbol=USDJPY&timeframe=15m&pit_eligible=all`
- `GET /v1/prices?limit=50&symbol=USDJPY&timeframe=15m`
- `GET /v1/meta`
- `GET /healthz`

List responses omit the large raw payload, use an opaque keyset cursor, and
pin `max(rowid)` plus the source snapshot token on page 1. Rows appended
between requests therefore appear only in a new traversal, including
late-arriving rows whose event time is older than the current page. Cursor
reuse with changed filters or an invalid HMAC signature returns HTTP 400.
Signatures use a process-local random secret, so a traversal restarts after an
API process restart.

Decision items retain only the compact explanation needed by the UI:
final/analysis direction, analysis and composite scores, direction threshold,
primary abstention gate, quote availability, and cost-adjusted expected R.
This distinguishes a neutral final decision caused by a score deadband,
expectancy veto, stale-data veto, or missing executable quote without exposing
the multi-kilobyte raw feature payload.

Every successful response has a strong SHA-256 ETag over the exact canonical
JSON bytes. Strong or weak `If-None-Match` validators use weak comparison for
GET/HEAD and return HTTP 304 with no body. The API opens a new SQLite URI
`mode=ro` connection and also enables `query_only`; turning the pragma off
still cannot mutate operational state. It caps concurrent workers at 16,
applies a 10-second socket timeout, and closes each response connection. It has
no launchd entry and is not yet the production dashboard reader.

Before any reader switch, compare the active dashboard's recent decision rows
with the independent API. The comparator normalizes UTC representations and
compares a multiset signature, so historical duplicate rows cannot disappear
through dictionary overwrite:

```bash
runs/python314-safe-venv/bin/python tools/operational_dual_read.py \
  --dashboard-url http://127.0.0.1:8768/api/state \
  --read-api-url http://127.0.0.1:8770 \
  --symbol USDJPY \
  --output runs/operational-evidence/dual-read-20260727.json
```

The comparison is read-only and create-only. A missing row or drift in final
direction, analysis direction/score, primary abstention gate, or PIT
eligibility returns exit 1. Transport/schema failures return exit 2.

`RESTART` and `TRUNCATE` checkpoints are administrative actions for a confirmed
reader gap. They must not run from an independent periodic process while normal
readers and writers are active.

## Schema and query path

- `audit_events(event_id)` retains structurally valid legacy and current
  decisions for audit/display. PIT-ineligible history is never promoted into
  the scoring queue.
- `predictions(status, due_time_ns, prediction_id)` selects only matured,
  unscored work.
- `price_points(symbol, timeframe, event_time_ns, available_time_ns)` supplies a
  bounded, sorted path suitable for NumPy `searchsorted`.
- `outcomes(prediction_id, label_version)` is the immutable labeling key.
- `work_checkpoints(job_name)` records monotonic high-water marks and the exact
  source-manifest hash.
- `source_cursors(source_name)` records the next unread byte and immutable file
  identity.
- `source_chunks(source_name, start_offset, end_offset)` proves every consumed
  range and prevents gaps or cursor-only advancement.
- `ingest_rejections(source_name, line_start_offset)` records the hash and
  reason for a rejected complete line without duplicating the authoritative raw
  payload.
- schema v4 adds global and symbol/timeframe page indexes for newest-first
  keyset scans. The read model uses the immutable base tables and does not add a
  second mutable cache.

The remaining cutover stages are:

1. load the optional scheduler only after its dry-run, initial parity, and
   bootstrap evidence have been reviewed;
2. run incremental shadow sync under one scheduler-owned process, never one
   SQLite writer per source, and run daily full raw replay with create-only
   reports;
3. produce several days of zero-unexplained-drift reports and compare the
   independent read API with the active dashboard;
4. switch readers only after the parity evidence is reviewed;
5. retain original raw files, manifests, and rollback path.

## Optional scheduler and loopback read service

The optional launchd jobs are deliberately excluded from
`scripts/install_launchd.sh`. The sync and replay jobs have no `RunAtLoad` and
call the scheduled command directly: it records immutable intent before
attempting the canonical SQLite writer lease, so a collision produces a
non-zero exit and a failure report instead of an invisible outer-lock skip.
Sync runs at minutes 03/08/.../58, after the normal five-minute producer
boundary. Full replay runs at 04:46 in the Mac's local timezone.

The third job is the small read API. It uses `RunAtLoad`/`KeepAlive`, binds
inside the Python entry point to `127.0.0.1` only, opens SQLite with kernel
read-only/query-only enforcement, and exposes bounded pages plus ETags. The
port defaults to 8770 and can be changed with
`FX_OPERATIONAL_READ_PORT`. It is not a production read cutover.

The scheduled full replay holds shared locks on both authoritative raw files
and every compact journal referenced by committed PIT decisions, then performs
catch-up sync and full replay inside one SQLite writer lease, followed by a
`TRUNCATE` checkpoint. Appenders cannot change the full, compact, or price
snapshot between catch-up and verification.

Dry-run requires explicit absolute paths and performs a read-only database
audit plus `plutil` validation. It does not change launchd state:

```bash
FX_OPERATIONAL_PYTHON="$PWD/runs/python314-safe-venv/bin/python" \
FX_OPERATIONAL_DB="$PWD/runs/operational-migration/candidate.sqlite3" \
FX_OPERATIONAL_EVIDENCE_DIR="$PWD/runs/operational-evidence" \
FX_OPERATIONAL_READ_PORT=8770 \
  ./scripts/operational_store_launchd.sh dry-run
```

Only after initial parity/bootstrap evidence is approved, use the same
environment with `install`. `status` does not need the environment. A
rollback unloads all three jobs and archives their plists while retaining the
database, raw evidence, and reports:

```bash
./scripts/operational_store_launchd.sh status
./scripts/operational_store_launchd.sh rollback
```

The scheduler invokes `tools/operational_store_scheduled.py`, which creates a
unique UTC/PID run id. Every started attempt first writes `*.intent.json`, then
exactly one `*.result.json` or `*.failure.json`, all create-only and linked by
the intent SHA-256. A quality failure, writer collision, or replay drift stays
non-zero; it is never converted into success.
