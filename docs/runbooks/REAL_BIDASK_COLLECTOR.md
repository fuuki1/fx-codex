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
EOF
chmod 600 ~/.config/fx-codex/collector.env
```

Only the three `FX_OANDA_*` keys are accepted. The daemon parses the file as
data; it does not source or evaluate it as shell code. Token and account values
are masked in dry-run output and must never be committed.

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

## 9. Known remaining operational blockers

Before the 30-day clock can legitimately start:

1. connect an approved live non-demo OANDA read-only stream
2. create an independently measured same-day primary-health artifact
3. connect an independent prospective secondary source
4. generate same-day deterministic replay evidence
5. schedule the daily-report command under a reviewed single-writer service
6. connect alerting for token failure, incidents, stale data and non-qualifying days
7. confirm clock synchronization and backup/retention on the Mac mini

Until these are complete, the data-platform score remains evidence-capped.
