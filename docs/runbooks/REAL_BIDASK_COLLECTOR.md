# Runbook: analysis-only bid/ask collector

## 1. Scope and current status

This runbook covers the production Tiingo Forex WebSocket collector under
`data_platform/collect/`. The application is structurally market-data-only and
has no broker account or order API. The legacy OANDA adapter remains available
for historical replay and compatibility tests, but the production daemon does
not import or call it.

Tiingo's terms permit local storage for internal consumption while the
applicable subscription is active. They prohibit redistribution and require
deletion of Tiingo data when the subscription expires, is cancelled, or is
terminated. Creating or retaining derived data requires express written
approval, with additional limits after termination. The operator must read the
current terms before installation:

- <https://api.tiingo.com/tos/>
- <https://www.tiingo.com/documentation/websockets/forex>
- <https://www.tiingo.com/about/pricing>

Current state:

- Tiingo top-of-book WebSocket adapter: implemented, credentials not committed
- Tiingo provider connectivity and payload shape on the target host: not yet
  validated; the Forex WebSocket documentation currently labels the API beta
- legacy OANDA adapter: retained for historical replay/tests, not the
  production daemon
- raw-first capture: hash-chained, daily-sharded SQLite transaction journal
- completed one-minute materializer: implemented as a separate shadow service
- canonical freshness monitor: isolated from the existing briefing gate
- Mac mini installation: stopped and not installed; a future attempt remains
  blocked until a clean approved SHA, credential file, reviewed clean-install
  window, and legacy execution-checkout isolation are all present
- 30 qualifying trading days: 0 until prospective operation starts

The Tiingo `free` plan may validate connectivity but must not be assumed to
have enough bandwidth for prospective all-update collection. Realized message
rate and plan bandwidth must be measured before the 30-day clock starts.

The transport permits only `wss://api.tiingo.com/fx`, disables redirects and
does not log provider error bodies. Any endpoint deviation fails closed before
the API token is sent.

## 2. Credential file

Create the file outside the repository:

```bash
mkdir -p ~/.config/fx-codex
cat > ~/.config/fx-codex/collector.env <<'EOF'
FX_TIINGO_API_TOKEN=<Tiingo market-data API token>
FX_TIINGO_PLAN=power
FX_TIINGO_USAGE_SCOPE=internal_nonredisplay_active_subscription
FX_TIINGO_DERIVED_DATA_APPROVAL_REF=<Tiingo written approval or agreement reference>
EOF
chmod 600 ~/.config/fx-codex/collector.env
```

Only these four `FX_TIINGO_*` keys are accepted. `FX_TIINGO_PLAN` must be
`free`, `power`, or `commercial` and must match the plan actually attached to
the token. The usage-scope value is an explicit operator attestation that the
collection is internal, non-display use and the applicable subscription is
active. `FX_TIINGO_DERIVED_DATA_APPROVAL_REF` must identify Tiingo's express
written approval for creation and retention of the derived quote/bar evidence
this pipeline produces. Neither setting is a substitute for the actual
approval or Tiingo contract.

The daemon parses the file as data; it does not source or evaluate it as shell
code. The token is masked in dry-run output and must never be committed.

Tiingo data is isolated under `collect/tiingo-v1/` and materialized into
`logs/briefing_tf_bidask_prices_tiingo_v1.sqlite3`. This prevents a new Tiingo
journal or schema-v4 snapshot store from silently mixing with historical OANDA
evidence.

### Subscription termination

Before a Tiingo subscription expires or is cancelled:

1. uninstall the collector topology and verify all three launchd labels are
   stopped
2. inventory the Tiingo raw journal, snapshots, reports, archives, backups and
   any derived datasets
3. obtain explicit review of the deletion target and preservation obligations
4. delete Tiingo data as required by the then-current terms, including copies
   and derived data unless written retention approval exists
5. record the stop time, deletion scope, reviewer and evidence hashes without
   recording the token

There is intentionally no automatic deletion command. Deletion is destructive,
and this repository must preserve user data until the exact Tiingo-owned scope
has been independently reviewed.

## 3. Pre-install validation

```bash
cd ~/srv/fx-codex
FX_CODEX_APPROVED_SHA=<approved-40-character-commit> \
  scripts/canonical_bidask_shadow_launchd.sh dry-run
```

The command must fail when:

- the credential file is absent
- its mode is not `0600`
- a required key is missing
- an unknown or duplicate key exists
- the approved virtual-environment Python is unavailable
- the plist is malformed
- the Python collector configuration is invalid
- `HEAD` differs from `FX_CODEX_APPROVED_SHA`
- the runtime checkout is dirty
- a separate known checkout still tracks `trader/` or `executor.py`

A successful dry-run validates all three plists, parses the nested collector,
materializer and freshness command arguments with their real CLI parsers, and
checks the collector configuration without printing credential values.

## 4. launchd lifecycle

```bash
FX_CODEX_APPROVED_SHA=<approved-40-character-commit> \
  scripts/canonical_bidask_shadow_launchd.sh install
scripts/canonical_bidask_shadow_launchd.sh status
scripts/canonical_bidask_shadow_launchd.sh uninstall
```

The topology installs separate collector, materializer, and canonical-health
labels. It does not replace `com.fx-codex.snapshot`, change the existing
briefing freshness report, or wire canonical prices into decisions. The
collector plist launches `/bin/sh scripts/run_quote_collector.sh --launchd
...`. The wrapper loads the mode-600 credential file through the daemon's
narrow `--env-file` parser and refuses to fall back to an unreviewed system
Python.

Installation is deliberately clean-install only: it refuses to overwrite an
existing canonical plist or loaded label. If a later label fails during the
same installation attempt, the installer rolls back only the labels/plists it
created in that attempt. Replacing a prior canonical installation requires an
explicit reviewed uninstall and a new approved SHA.

All repository launchd templates set `Umask=63` (decimal `0077`) so newly
created service logs and runtime files are private by default. This is defense
in depth, not secret scrubbing: preserve and restrict any historical log that
contains a credential, revoke/reissue that credential at the provider, and do
not treat `chmod` alone as incident recovery. Discord and provider transport
errors must report only sanitized error type/status information, never a
request URL.

Expected operator-action exits are translated to wrapper exit 0 so launchd does
not loop:

| Daemon code | Meaning | launchd behavior |
|---:|---|---|
| 75 | duplicate writer rejected | stop; inspect active writer |
| 77 | token rejected/expired | stop; replace credentials |
| 78 | invalid/missing configuration | stop; repair configuration |

Transient or unexpected failures remain nonzero and are eligible for launchd
restart after `ThrottleInterval`:

| Daemon code | Meaning |
|---:|---|
| 69 | source unavailable after consecutive reconnect budget |
| 70 | unexpected software failure |
| 74 | I/O or storage failure |

## 5. Raw-first data path

```text
provider bytes
  -> raw_durable SQLite transaction (exact bytes + SHA-256)
  -> committed read-back SHA-256 verification
  -> schema validation
  -> normalized quote
  -> quality classification
  -> atomic terminal transaction (rows + disposition + hash binding)
  -> COMMITTED journal state
  -> completed one-minute bid/ask shadow rows
```

The collector never forward-fills, averages conflicting providers, converts
missing values to zero, or marks injected/replay transport as live. Only the
production daemon explicitly assigns `collection_mode=live_stream`.

The sole pre-raw security exception is an inbound provider message containing
the exact configured API token (including its JSON-escaped form). Such a
credential-reflection payload is not made durable; the collector stops as an
authorization incident. This prevents raw-first evidence from becoming a
credential leak.

Production streaming disables the old per-message raw files and compatibility
JSONL. One UTC-day SQLite shard contains insert-only raw events, terminal
events, and quote rows; SQLite WAL/SHM companions mean at most three active
files per daily shard. Evidence tables reject update/delete, events are
SHA-256 chained across daily shards, and an 8 GiB active-shard ceiling reserves
space for the raw payload plus a bounded 4 MiB terminal transaction before
accepting another raw message. The limit is rechecked inside both write
transactions. A terminal payload that exceeds its bound is replaced by a
minimal `QUARANTINED` terminal record; an unbounded terminal cannot consume the
reserved capacity.

The materializer reads only hash-verified rows whose journal state is
`COMMITTED`. Raw-only interrupted captures and terminal `QUARANTINED` or
`UNAVAILABLE` captures are not visible. Startup recovery appends
`UNAVAILABLE`; it never edits raw evidence. Materialized snapshots and the
bridge checkpoint live in one SQLite WAL database and commit in one
`BEGIN IMMEDIATE` transaction. The checkpoint is bound to journal root, genesis
hash, commit-entry hash, configuration, and output path; a mismatch stops
advancement for an explicit audited migration. Deleting the database deletes
both snapshots and checkpoint, so the next run reconstructs from journal
genesis instead of accepting an orphan checkpoint.

Run the bounded functional probe, full logical-day capacity probe, and process
crash injection before approval:

```bash
python tools/capture_journal_soak.py --messages 10000
python tools/capture_journal_soak.py \
  --full-day \
  --report /path/outside/the/hot/journal/full_day_soak.json
python tools/capture_journal_crash_probe.py \
  --report /path/outside/the/hot/journal/crash_probe.json
python tools/capture_journal_archive_probe.py \
  --report /path/outside/the/hot/journal/archive_restore_probe.json
```

The output is synthetic functional evidence, not provider-captured data,
production-daemon RSS evidence, or trading evidence. It exits nonzero unless
the report records `passed=true`, including throughput of at least 16
messages/second, no raw-only visibility, successful unavailable recovery, no
more than three files for one daily shard, successful full verified replay,
and the configured post-run disk reserve. `--full-day`
represents 1,382,400 messages over one logical UTC day and refuses to start
unless its target volume has the configured disk reserve. The probe runs as
fast as the host permits; its logical event-time span, not its wall time, is
one day. The process probe covers termination after the raw commit, during the
terminal transaction, after the terminal commit, before and after UTC rotation,
after a synthetic active-shard capacity rejection, and after materialized rows
are inserted but before the single snapshot/checkpoint transaction commits.
That capacity case is not evidence of target-host filesystem `ENOSPC` behavior.

Seal only a non-active historical shard. Sealing verifies the complete hot
journal, checkpoints that shard, creates a deterministic gzip snapshot and a
canonical hash manifest, and verifies the resulting archive. It never removes
the source shard:

```bash
python tools/capture_journal_archive.py seal \
  --journal-root "$HOME/srv/fx-codex/collect/tiingo-v1/log/capture_journal" \
  --archive-root /approved/separate/archive/root \
  --shard-date 2026-07-20
python tools/capture_journal_archive.py verify \
  --manifest /approved/separate/archive/root/capture-2026-07-20.sqlite3.manifest.json
```

Restore drills require an empty destination and an explicit, contiguous set of
manifests beginning at journal genesis. The command restores create-only
SQLite files, checks archive and database hashes and metadata, then verifies
the complete cross-shard journal:

```bash
python tools/capture_journal_archive.py restore \
  --manifest /approved/separate/archive/root/capture-2026-07-20.sqlite3.manifest.json \
  --manifest /approved/separate/archive/root/capture-2026-07-21.sqlite3.manifest.json \
  --destination-root /approved/empty/restore-drill-root
```

Archive payloads and manifests are mode `0440`; restored SQLite shards are
mode `0640`. Seal, verification, and restore preflight their working-file
requirements and retain at least 1 GiB of filesystem reserve. No automated
routine retention deletion exists. Do not remove hot shards until an approved
disk budget, off-host backup, independent full-prefix restore and
evidence-retention policy are all recorded. The Tiingo
subscription-termination obligation above is a separate contractual deletion
event and takes precedence. Capacity must be based on target-host measurement,
not an assumed message rate.

The collector requests `thresholdLevel=5`, which Tiingo documents as every
top-of-book update available through this feed. This is still a
vendor-normalized aggregate, not venue-native dealer flow or consolidated OTC
FX. The output therefore records provider sampling coverage as unmeasured,
marks the vendor aggregation limitation, and does not infer completeness.

## 6. Runtime state and incidents

Terminal state:

```text
~/srv/fx-codex/collect/tiingo-v1/state/last_run.json
```

Incidents:

```text
~/srv/fx-codex/collect/tiingo-v1/state/incidents/*.json
```

Recorded terminal categories include:

- duplicate writer rejection
- authorization failure
- source unavailable after reconnect exhaustion
- I/O failure
- unexpected runtime failure
- graceful stop

State files use temp-write, file fsync, atomic replace and directory fsync.
Incident/state persistence may itself fail during disk exhaustion; launchd
stderr remains the fallback evidence in that case.

## 7. Reconnect semantics

`max_reconnects` limits consecutive failed connections. A valid Tiingo quote,
heartbeat or informational message (`A`, `H`, or `I`) resets the
consecutive-failure budget. The lifetime `reconnect_count` remains cumulative
for audit reporting.

Each disconnect opens an explicit gap. Heartbeat timeout marks the connection
non-tradable before a late quote is processed. Token rejection never retries.
Exhausting the transient reconnect budget exits 69 so launchd may restart the
process rather than silently treating source loss as a successful stop.

## 8. Prospective daily report

Generate a report after the trading day closes:

```bash
python -m tools.data_platform_daily_report \
  --collection-root "$HOME/srv/fx-codex/collect/tiingo-v1" \
  --date 2026-07-14 \
  --primary-evidence /path/to/primary_health_2026-07-14.json \
  --secondary-evidence /path/to/secondary_health_2026-07-14.json \
  --replay-evidence /path/to/replay_health_2026-07-14.json \
  --output-dir "$HOME/srv/fx-codex/collect/tiingo-v1/operations"
```

The three supporting files must be distinct, same-day JSON objects. Each needs
an aware `observed_at`, its exact `evidence_role`, and a nonempty `source_id`
that differs from the other roles:

```json
{
  "report_date": "2026-07-14",
  "observed_at": "2026-07-14T23:59:00+00:00",
  "evidence_role": "primary_health",
  "source_id": "tiingo_primary_health",
  "primary_up": true
}
```

```json
{
  "report_date": "2026-07-14",
  "observed_at": "2026-07-14T23:59:00+00:00",
  "evidence_role": "independent_secondary",
  "source_id": "approved_secondary_feed",
  "secondary_up": true
}
```

```json
{
  "report_date": "2026-07-14",
  "observed_at": "2026-07-14T23:59:00+00:00",
  "evidence_role": "deterministic_replay",
  "source_id": "capture_journal_replay",
  "replay_ok": true
}
```

The generator requires different resolved paths, SHA-256 values and `source_id`
values, then independently verifies that the accepted log contains usable
Tiingo live-stream rows from the exact Forex WebSocket for USDJPY, EURUSD,
GBPUSD and AUDUSD. Every qualifying primary row needs aware provider-event and
receipt timestamps within the future-skew bound. A health declaration without
four-pair coverage does not make `primary_up=true`.

The generator also verifies journal-bound raw bytes, quote counts, freshness,
quarantine flags, critical incidents and disk headroom. Missing, stale or
contradictory evidence produces `qualifying_day=false`; it is never inferred.
Reports older than the prospective generation window are non-qualifying, which
prevents retrospective construction of operational history.

Canonical journal rows and entry metadata are streamed rather than loaded as a
full-history list. Freshness `max` is exact across the report population; p50,
p95 and p99 use a deterministic reservoir of at most 100,000 observations and
record both population and sample counts. If a canonical journal exists but
fails verification, the report does not fall back to legacy JSONL.

Exit codes:

- `0`: qualifying report written
- `2`: non-qualifying report written
- `1`: malformed input; no valid report

The scorecard validates a unique ISO report date and exact filename, then counts
a day only when all conditions pass:

```text
qualifying_day is true
prospective_window_ok is true
raw_hash_verified is true
replay_ok is true
critical_incidents == 0
primary_up is true
secondary_up is true
supporting_evidence_contract_ok is true
supporting_evidence_distinct is true
```

Renaming or copying a report to manufacture another day is rejected because the
filename must equal `daily_report_<report_date>.json`.

## 9. Known remaining operational blockers

Before the 30-day clock can legitimately start:

1. obtain Tiingo's express written Derived Data approval, confirm the active
   plan permits this exact internal auto-save scope, and record its reference
2. validate the beta Forex WebSocket payload and all four pairs on the isolated
   Mac mini without exposing the token
3. measure realized message volume, bandwidth and disk use against the active
   plan and target-host capacity
4. create an independently measured same-day primary-health artifact
5. connect an independent prospective secondary source
6. generate same-day deterministic replay evidence
7. schedule the daily-report command under a reviewed single-writer service
8. connect alerting for token failure, incidents, stale data and non-qualifying days
9. confirm clock synchronization, measured disk budget, off-host backup and an
   independently verified archive/restore retention policy on the Mac mini

Until these are complete, the data-platform score remains evidence-capped.
