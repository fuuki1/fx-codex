# Runbook: research-v3 integration and Mac mini migration

## 1. Target

- integration branch: `integration/research-v3`
- review PR: #41
- target branch: `main`
- runtime checkout: `/Users/fuuki/srv/fx-codex`
- operating boundary: research, validation, data collection and notification only
- broker orders and the former `trader/` execution stack: prohibited

This document supersedes the former research-v2 stacked-PR migration sequence.
PR #26 and #29-#38 were consolidated into PR #41 and closed as superseded.

## 2. Preconditions before merging PR #41

All of the following are required:

1. PR #41 remains mergeable against current `main`.
2. `required-ci` succeeds for the current PR head.
3. Python 3.11 and 3.12 matrix jobs succeed.
4. Ruff, Black, Mypy, full pytest and safety invariants succeed.
5. `main` branch protection requires `required-ci`.
6. A human reviews the large deletion boundary, especially the permanent removal
   of broker execution surfaces.
7. The Mac mini is not changed during PR review.

Do not treat historical evidence bundles as current production operation.

## 3. Pre-migration evidence on the Mac mini

Run from the Mac mini before changing services:

```bash
set -euo pipefail
umask 077

ROOT=/Users/fuuki/srv/fx-codex
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
AUDIT_ROOT="$HOME/fx-codex-audit/$RUN_ID"
mkdir -p "$AUDIT_ROOT"
chmod 700 "$AUDIT_ROOT"

cd "$ROOT"
date -u +%FT%TZ > "$AUDIT_ROOT/observed_at_utc.txt"
hostname > "$AUDIT_ROOT/hostname.txt"
git status --short --branch > "$AUDIT_ROOT/git-status-before.txt"
git rev-parse HEAD > "$AUDIT_ROOT/head-before.txt"
git branch --show-current > "$AUDIT_ROOT/branch-before.txt"
launchctl list | rg 'fx-codex|trader' > "$AUDIT_ROOT/launchctl-list.txt" || true
pgrep -fl 'fx_briefing|fx_tf_snapshot|fx_quote_collector|tv_discord_notify|trader' \
  > "$AUDIT_ROOT/processes.txt" || true
crontab -l > "$AUDIT_ROOT/crontab.txt" 2>/dev/null || true
```

Do not copy `.env` contents, credential values, webhook URLs or authenticated
remote URLs into the audit bundle.

Stop if the working tree contains unreviewed local changes. Preserve them on a
separate rescue branch or external archive before migration.

## 4. Clean runtime construction

After PR #41 is merged and the exact main SHA is approved:

```bash
cd /Users/fuuki/srv/fx-codex
git fetch origin
git checkout main
git reset --hard <APPROVED_MAIN_SHA>
git clean -fd

python3 -m venv .venv.new
.venv.new/bin/python -m pip install --upgrade pip
.venv.new/bin/python -m pip install --require-hashes -r requirements.lock
.venv.new/bin/python -m pip install --no-deps --no-build-isolation .
.venv.new/bin/python -m pip check
.venv.new/bin/python -m pytest -q
```

Do not use partial `rsync` or file-by-file checkout. The code, lock file,
scripts, tests and launchd templates must come from the same approved SHA.

Only after validation:

```bash
mv .venv .venv.previous
mv .venv.new .venv
```

Keep `.venv.previous` until post-migration validation is complete.

## 5. Existing analysis services

The established snapshot, briefing and freshness services are managed by:

```bash
./scripts/install_launchd.sh --dry-run
./scripts/install_launchd.sh
./scripts/status_fx_services.sh
```

Before installation, verify there are no manual loops, cron writers, old plist
jobs or alternate checkouts writing the same logs. The exclusive locks do not
protect against every differently named or differently located writer.

## 6. Read-only bid/ask collector

The quote collector is a separate continuous read-only service.

```bash
scripts/quote_collector_launchd.sh dry-run
scripts/quote_collector_launchd.sh install
scripts/quote_collector_launchd.sh status
```

Follow `docs/runbooks/REAL_BIDASK_COLLECTOR.md`. Installation is prohibited
until the credential file exists with mode 0600 and the dry-run succeeds.

The wrapper prevents restart loops for expected operator-action exits. It does
not convert unexpected runtime or I/O failures to success.

## 7. Post-migration validation

Verify:

```bash
git rev-parse HEAD
git status --short
./scripts/status_fx_services.sh
scripts/quote_collector_launchd.sh status
```

Then confirm:

- no `trader/` process or execution container is active
- only approved writers own each journal and price log
- snapshot and briefing timestamps advance on schedule
- freshness monitoring can emit warning and recovery notifications
- quote raw/log/state paths are writable only by the runtime user
- collector dry-run masks token and account values
- no duplicate-writer incidents appear under normal operation
- `last_run.json` and incident JSON remain parseable after controlled stop tests

## 8. Daily evidence and the 30-day clock

The 30-day clock does not start merely because the collector process is running.
For each day, generate a prospective daily report with:

```bash
python -m tools.data_platform_daily_report ...
```

A qualifying day requires same-day evidence for:

- verified immutable raw data
- deterministic replay
- zero critical incidents
- live primary coverage for USDJPY, EURUSD and GBPUSD
- independent secondary-source availability

The current historical evidence bundles do not count as prospective days.

## 9. Rollback

Code rollback:

```bash
cd /Users/fuuki/srv/fx-codex
git reset --hard <PREVIOUS_APPROVED_SHA>
rm -rf .venv
mv .venv.previous .venv
```

Service rollback:

```bash
scripts/quote_collector_launchd.sh rollback
./scripts/uninstall_launchd.sh
```

Restore only the previous research/notification configuration. Do not restore
the former broker execution stack into this repository.

## 10. Remaining gates after merge

PR #41 establishes structure and fail-closed operation. It does not by itself
prove alpha or production data maturity. Remaining evidence includes:

- approved live non-demo primary collection
- independent prospective secondary collection
- same-day replay evidence
- 30 qualifying trading days
- untouched future lockbox evidence
- positive cost-adjusted out-of-sample expectancy with uncertainty bounds

Until those are earned, promotion claims remain denied.
