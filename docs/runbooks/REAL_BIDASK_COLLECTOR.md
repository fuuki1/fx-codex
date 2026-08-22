# Runbook: read-only bid/ask collector

## 1. Scope and current status

This runbook covers only `data_platform/collect/` and the read-only OANDA
pricing stream. It does not authorize broker orders or account mutation.

Current state on `integration/research-v3`:

- OANDA pricing adapter: implemented, credentials not committed
- Dukascopy historical bid/ask evidence: available
- independent historical comparison: available
- prospective secondary live source: not yet connected
- prospective daily-report generator: implemented fail-closed
- Mac mini installation: not yet performed
- 30 qualifying trading days: 0 until prospective operation starts

Practice/demo data may validate connectivity but does not count as production
market-data evidence.

## 2. Credential file

Create the file outside the repository:

```bash
mkdir -p ~/.config/fx-codex
cat > ~/.config/fx-codex/collector.env <<'EOF'
FX_OANDA_API_TOKEN=<read-only token>
FX_OANDA_ACCOUNT_ID=<account id>
FX_OANDA_ENV=practice
FX_RAW_LEDGER_ENABLED=false
EOF
chmod 600 ~/.config/fx-codex/collector.env
```

Only the three `FX_OANDA_*` keys and `FX_RAW_LEDGER_ENABLED` are accepted. The
daemon parses the file as data; it does not source or evaluate it as shell code.
Token and account values are masked in dry-run output and must never be committed.
Keep the raw-ledger flag `false` until a separate activation approval has verified
Mac mini capacity and permissions, prospective all-symbol coverage, backup/restore,
retention, and freshness-alert routing.

Use `FX_OANDA_ENV=live` only when the account and token are explicitly approved
for prospective non-demo data collection. Read-only pricing access does not
permit trading.

## 3. Pre-install validation

```bash
cd ~/srv/fx-codex
scripts/quote_collector_launchd.sh dry-run
```

The command must fail when:

- the credential file is absent
- its mode is not `0600`
- a required key is missing
- an unknown or duplicate key exists
- the approved virtual-environment Python is unavailable
- the plist is malformed
- the Python collector configuration is invalid

The wrapper always launches the collector with
`-I -S -B -X pycache_prefix=/dev/null`. This ignores `PYTHONHOME`, `PYTHONPATH`, user-site,
`.pth`/`sitecustomize`, and repository or site-package bytecode caches. The collector adds only
the reviewed repository root and `.venv` site-packages explicitly. Raw-ledger mode also verifies
these runtime flags and the installed transport lock itself before a dry-run or collection begins.
The OANDA HTTP session sets `trust_env=false`; proxy, CA-bundle, and netrc environment settings
are not accepted by the pricing transport.

A successful dry-run prints the rendered plist and a validation result without
printing credential values.

## 4. launchd lifecycle

```bash
scripts/quote_collector_launchd.sh install
scripts/quote_collector_launchd.sh status
scripts/quote_collector_launchd.sh uninstall
```

The plist launches `/bin/sh scripts/run_quote_collector.sh --launchd ...`.
The wrapper loads the mode-600 credential file through the daemon's narrow
`--env-file` parser and refuses to fall back to an unreviewed system Python.

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
  -> immutable content-addressed raw store
  -> read-back SHA-256 verification
  -> schema validation
  -> normalized quote
  -> quality classification
  -> append-only accepted/quarantine JSONL
```

The collector never forward-fills, averages conflicting providers, converts
missing values to zero, or marks injected/replay transport as live. Only the
production daemon explicitly assigns `collection_mode=live_stream`.

Accepted-log bootstrap streams JSONL line-by-line. A malformed accepted row
stops startup because silently skipping it could invalidate duplicate and
ordering detection.

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

The generator also checks immutable raw blobs, quote counts, freshness,
quarantine flags, critical incidents and disk headroom. Missing, stale or
contradictory evidence produces `qualifying_day=false`; it is never inferred.
Reports older than the prospective generation window are non-qualifying, which
prevents retrospective construction of operational history.

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

## 9. Raw Ledger backup and activation evidence

The backup command uses SQLite's online backup API, copies only blobs referenced by that
database snapshot, verifies every content hash, and writes `completion.json` last. Destinations
are create-only. If a run fails before completion, retain the incomplete directory as incident
evidence and use a new destination; never reuse or overwrite it.
The destination entry is directory-fsynced before completion is published; the final target
directory fsync is the completion commit point.

The backup destination must be on an independently retained device or mount. A snapshot on the
same filesystem is useful for testing but fails the activation gate.

```bash
SNAPSHOT="/Volumes/<reviewed-backup-volume>/fx-codex/raw-ledger/$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python tools/raw_event_ledger_backup.py create \
  --collection-root "$HOME/srv/fx-codex/collect" \
  --destination "$SNAPSHOT"
.venv/bin/python tools/raw_event_ledger_backup.py verify --backup "$SNAPSHOT"
```

After a separately approved, time-bounded shadow trial has produced prospective data for every
required pair, run the comprehensive read-only gate. Every threshold is mandatory and must be
replaced with a reviewed operational value; the example numbers are not approvals.

```bash
.venv/bin/python -I -S -B -X pycache_prefix=/dev/null \
  tools/raw_ledger_activation_preflight.py \
  --collection-root "$HOME/srv/fx-codex/collect" \
  --env-file "$HOME/.config/fx-codex/collector.env" \
  --expected-hostname '<approved-mac-mini-hostname>' \
  --expected-oanda-environment live \
  --required-symbol USD_JPY --required-symbol EUR_USD --required-symbol GBP_USD \
  --stale-after-seconds 60 \
  --max-duplicate-ratio 0.10 \
  --max-quarantine-ratio 0.01 \
  --max-raw-failure-ratio 0 \
  --max-raw-queue-overflow-ratio 0 \
  --max-annotation-failure-ratio 0 \
  --max-annotation-queue-overflow-ratio 0 \
  --max-raw-attempt-rate-per-second '<reviewed-provider-rate-ceiling>' \
  --max-queue-backlog 0 \
  --min-shadow-observation-seconds 86400 \
  --min-raw-attempts '<reviewed-minimum-for-the-trial-window>' \
  --min-live-events-per-symbol '<reviewed-minimum-for-each-required-pair>' \
  --min-free-bytes 21474836480 \
  --min-backup-free-bytes 21474836480 \
  --retention-days 30 \
  --backup-retention-snapshots 30 \
  --max-daily-growth-bytes 1073741824 \
  --backup-root "$SNAPSHOT" \
  --max-backup-age-seconds 86400 \
  --max-backup-lag-events 0 \
  --max-backup-lag-annotations 0 \
  --alert-evidence '<independently-captured-alert-evidence.json>' \
  --max-alert-evidence-age-seconds 86400 \
  --clock-evidence '<independently-captured-clock-evidence.json>' \
  --max-clock-evidence-age-seconds 3600 \
  --max-clock-offset-ms 100 \
  --attestation '<independent-reviewer-attestation.json>' \
  --attestation-signature '<independent-reviewer-attestation.sig>' \
  --attestation-public-key '<approved-reviewer-public-key.pem>' \
  --expected-attestation-public-key-sha256 '<pinned-lowercase-sha256>' \
  --max-attestation-age-seconds 3600 \
  --expected-deployed-sha '<reviewed-40-character-git-sha>' \
  --output '<outside-repository-audit-root>/raw-ledger-preflight.json'
```

The alert evidence contract is `raw-ledger-alert-route-evidence-v1` and must bind the Mac mini
hostname, aware `observed_at`, `route_configured=true`, `delivery_test_passed=true`, and
`raw_ledger_health_routed=true`. The clock contract is
`host-clock-synchronization-evidence-v1` and must bind hostname, aware `observed_at`,
`synchronized=true`, and measured `offset_ms`. The preflight does not manufacture these claims,
send a notification, change the flag, install/restart a service, or delete retained data.

The final attestation contract is `raw-ledger-activation-attestation-v1`. It must be created and
signed by the independent reviewer after the alert, clock, backup, trial metrics, capacity policy,
and deployed release have been inspected. The approved public-key SHA-256 is pinned separately;
supplying a new key beside a new signature is not approval. The signed JSON must contain:

- `hostname` and aware `observed_at`;
- `bindings` equal to the preflight's canonical policy hash, collection-root hash, collector-env
  hash, backup-manifest hash, alert/clock hashes, canonical required-symbol list, and deployed SHA;
- `metrics` as the checkpoint frozen by the signed attestation: its reviewed source-file SHA-256,
  run/observation times, full raw/annotation/aggregate counter snapshot, raw
  attempted/failed/overflow counters,
  per-symbol trial-window counts and event-ID digest, writer PID, process command/executable hashes,
  process start, lock inode, loaded critical-code digest, interpreter hash, installed transport
  environment digest, and reviewed environment-lock hash. The current live metrics may advance
  monotonically after signing, but only under the reviewed raw-attempt rate ceiling and on the same
  run/process/code/environment identity;
- boolean `controls` for independent retention, physical-device separation, single writer,
  deployed-SHA verification, clean worktree, reviewed risk configuration, reviewed daily-growth
  cap, reviewed backup capacity, reviewed Python executable, reviewed collector-environment lock,
  and the full-snapshot capacity model;
  and
- `capacity` with positive observed daily growth, the matching reviewed daily cap and retention
  days, `backup_strategy=full_create_only`, retained snapshot count, collection bytes at review,
  and the matching full-snapshot-series projection.

The local `st_dev` comparison is only a necessary first check: separate APFS volumes on one
physical disk are not independent retention. The signed `backup_physical_device_separate` and
`backup_independently_retained` controls are therefore also mandatory. Alert, clock, reviewed
metrics checkpoint, and backup evidence that are not bound by the reviewer attestation cannot
produce `ready`. Evidence JSON is parsed and hashed from the same stable open file,
and signature verification uses those exact bytes, preventing path-replacement races.
The deployed repository must match `--expected-deployed-sha` and have a clean worktree. A live
metrics writer PID must exist. Its absolute collector script/env/output-root arguments, start time,
approved Python executable hash, critical-code digest, and actual flock ownership are verified;
the `requests` transport closure (`requests`, urllib3, certifi, charset-normalizer, and idna) must
exactly match `ops/raw_ledger_collector_environment.lock.json`; and the reviewer must also
independently attest single-writer topology. The collector process and preflight must both use the
isolated/no-site/no-bytecode-cache flags above. Dependency upgrades require a newly reviewed lock.

Retention is enforced as a capacity projection:
`min_free_bytes + retention_days * max_daily_growth_bytes`. No automatic raw deletion is
implemented. Backups are full create-only snapshots, not incremental. For `n` retained future
snapshots, current collection bytes `C`, and daily growth cap `g`, backup projection is
`n*C + g*n*(n+1)/2`; `min_backup_free_bytes` is added on the backup volume. The attested observed
growth must be positive and no greater than the reviewed cap. A `ready`
report therefore proves the supplied capacity policy at one observation time; it does not replace
periodic monitoring or off-host retention controls.

## 10. Known remaining operational blockers

Before the 30-day clock can legitimately start:

1. connect an approved live non-demo OANDA read-only stream
2. create an independently measured same-day primary-health artifact
3. connect an independent prospective secondary source
4. generate same-day deterministic replay evidence
5. schedule the daily-report command under a reviewed single-writer service
6. connect alerting for token failure, incidents, stale data and non-qualifying days
7. confirm clock synchronization and backup/retention on the Mac mini

Until these are complete, the data-platform score remains evidence-capped.
