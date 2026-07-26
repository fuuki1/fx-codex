# Runbook: analysis-only bid/ask collector

## 1. Scope and current status

This runbook covers only `data_platform/collect/` and the OANDA pricing-stream
`GET` endpoint. The application is structurally pricing-only and does not
authorize broker orders or account mutation. The OANDA personal access token
must nevertheless be treated as potentially trading-capable: the safety
boundary is the endpoint allowlist and absence of mutation code, not token
scope.

Current state:

- OANDA pricing adapter: implemented, credentials not committed
- raw-first capture: hash-chained, daily-sharded SQLite transaction journal
- completed one-minute materializer: implemented as a separate shadow service
- canonical freshness monitor: isolated from the existing briefing gate
- Mac mini installation: blocked until a clean approved SHA, credential file,
  and legacy execution-checkout isolation are all present
- 30 qualifying trading days: 0 until prospective operation starts

Practice/demo data may validate connectivity but does not count as production
market-data evidence.

The pricing transport does not follow HTTP redirects. A redirect is a
fail-closed connection error because the personal token must not be forwarded
outside the hard-coded pricing host/path contract.

## 2. Credential file

Create the file outside the repository:

```bash
mkdir -p ~/.config/fx-codex
cat > ~/.config/fx-codex/collector.env <<'EOF'
FX_OANDA_API_TOKEN=<personal access token; treat as trading-capable secret>
FX_OANDA_ACCOUNT_ID=<account id>
FX_OANDA_ENV=practice
EOF
chmod 600 ~/.config/fx-codex/collector.env
```

Only the three `FX_OANDA_*` keys are accepted. The daemon parses the file as
data; it does not source or evaluate it as shell code. Token and account values
are masked in dry-run output and must never be committed.

Use `FX_OANDA_ENV=live` only when the account and token are explicitly approved
for prospective non-demo data collection. This repository still calls only the
pricing-stream endpoint; no environment value authorizes execution code.

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

A successful dry-run validates all three plists and the collector configuration
without printing credential values.

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

Production streaming disables the old per-message raw files and compatibility
JSONL. One UTC-day SQLite shard contains insert-only raw events, terminal
events, and quote rows; SQLite WAL/SHM companions mean at most three active
files per daily shard. Evidence tables reject update/delete, events are
SHA-256 chained across daily shards, and an 8 GiB active-shard ceiling fails
closed before accepting another raw message. A terminal transaction is still
allowed to finish an already-durable raw message.

The materializer reads only hash-verified rows whose journal state is
`COMMITTED`. Raw-only interrupted captures and terminal `QUARANTINED` or
`UNAVAILABLE` captures are not visible. Startup recovery appends
`UNAVAILABLE`; it never edits raw evidence. The bridge checkpoint is bound to
journal root, genesis hash, commit-entry hash, configuration, and output path;
a mismatch stops advancement for an explicit audited migration.

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

The output is synthetic functional evidence, not provider-captured data or
trading evidence. It must report throughput above 16 messages/second, no
raw-only visibility, successful unavailable recovery, no more than three files
for one daily shard, and successful full verified replay. `--full-day`
represents 1,382,400 messages over one logical UTC day and refuses to start
unless its target volume has the configured disk reserve. The probe runs as
fast as the host permits; its logical event-time span, not its wall time, is
one day. The process probe covers termination after the raw commit, during the
terminal transaction, after the terminal commit, before and after UTC rotation,
after a synthetic active-shard capacity rejection, and after materialized rows
are fsynced but before their replay checkpoint is replaced. That capacity case
is not evidence of target-host filesystem `ENOSPC` behavior.

Seal only a non-active historical shard. Sealing verifies the complete hot
journal, checkpoints that shard, creates a deterministic gzip snapshot and a
canonical hash manifest, and verifies the resulting archive. It never removes
the source shard:

```bash
python tools/capture_journal_archive.py seal \
  --journal-root "$HOME/srv/fx-codex/collect/log/capture_journal" \
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
retention deletion exists. Do not remove hot shards until an approved disk
budget, off-host backup, independent full-prefix restore and evidence-retention
policy are all recorded. At roughly 5 GB/day, even a 30-day hot window would
require about 150 GB before filesystem and safety headroom, so a policy must be
based on target-host measurement rather than that illustrative number.

OANDA documents that the pricing stream sends at most four prices per second
per instrument and may omit intermediate prices. Therefore the output records
provider sampling coverage as unmeasured and never describes the stream as
complete ticks, dealer flow, or consolidated OTC FX data.

## 6. Runtime state and incidents

Terminal state:

```text
~/srv/fx-codex/collect/state/last_run.json
```

Incidents:

```text
~/srv/fx-codex/collect/state/incidents/*.json
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

`max_reconnects` limits consecutive failed connections. A valid PRICE or
HEARTBEAT message resets the consecutive-failure budget. The lifetime
`reconnect_count` remains cumulative for audit reporting.

Each disconnect opens an explicit gap. Heartbeat timeout marks the connection
non-tradable before a late quote is processed. Token rejection never retries.
Exhausting the transient reconnect budget exits 69 so launchd may restart the
process rather than silently treating source loss as a successful stop.

## 8. Prospective daily report

Generate a report after the trading day closes:

```bash
python -m tools.data_platform_daily_report \
  --collection-root "$HOME/srv/fx-codex/collect" \
  --date 2026-07-14 \
  --primary-evidence /path/to/primary_health_2026-07-14.json \
  --secondary-evidence /path/to/secondary_health_2026-07-14.json \
  --replay-evidence /path/to/replay_health_2026-07-14.json \
  --output-dir "$HOME/srv/fx-codex/collect/operations"
```

The three supporting files must be same-day JSON objects:

```json
{"report_date": "2026-07-14", "primary_up": true}
```

```json
{"report_date": "2026-07-14", "secondary_up": true}
```

```json
{"report_date": "2026-07-14", "replay_ok": true}
```

The generator binds each file by SHA-256 and independently verifies that the
accepted log contains usable live OANDA quotes for USDJPY, EURUSD and GBPUSD.
A single quote or a health declaration without three-pair coverage does not make
`primary_up=true`.

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
```

Renaming or copying a report to manufacture another day is rejected because the
filename must equal `daily_report_<report_date>.json`.

## 9. Known remaining operational blockers

Before the 30-day clock can legitimately start:

1. connect an approved live non-demo OANDA pricing stream while treating the
   token as potentially trading-capable
2. create an independently measured same-day primary-health artifact
3. connect an independent prospective secondary source
4. generate same-day deterministic replay evidence
5. schedule the daily-report command under a reviewed single-writer service
6. connect alerting for token failure, incidents, stale data and non-qualifying days
7. confirm clock synchronization, measured disk budget, off-host backup and an
   independently verified archive/restore retention policy on the Mac mini

Until these are complete, the data-platform score remains evidence-capped.
