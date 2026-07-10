---
name: fx-repo-audit
description: Audit the fx-codex repository, local runtime, launchd, cron, Docker, writers, logs, CI, and safety configuration. Use before broad changes, deployments, incident reviews, or institutional-readiness assessments.
---

# FX repository audit

## Purpose and inputs

Establish the real repository and runtime state without changing it. Inputs are the repository root, optional Mac mini host/path supplied by the user, and the period/logs in scope.

## Procedure

1. Read `AGENTS.md`, `docs/OPERATIONS_RUNBOOK.md`, git status/branch/log, remotes, and open worktree changes. Treat every pre-existing change as user-owned.
2. Inventory Python environments, CI, Docker/Compose, launchd, cron, `nohup`, loop scripts, PID/lock files, and process command lines.
3. Map every writer to its output. Check duplicate schedules, advisory locks, stale-lock recovery, idempotency keys, and atomic writes.
4. Measure journal rows, natural-key and slot duplicates, timestamp reversals, malformed rows, last update, OHLC/bid/ask coverage, and file permissions. Use `tools/journal_gap_audit.py` and `tools/data_freshness_monitor.py` when applicable.
5. Confirm `.env` and `trader/.env` retain paper/live-off guards without printing secrets.
6. Run read-only quality checks. Keep root and `trader/` results separate.
7. Record severity, evidence path/line/command result, current impact, safest remediation, and rollback. If a remote host is inspected, label MacBook and Mac mini evidence separately.

## Commands

```bash
git status --short && git branch --show-current && git log -10 --oneline
ps ax -o pid,ppid,lstart,command
launchctl list | rg 'fx|trading'
crontab -l
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/mypy fx_backtester fx_intel
.venv/bin/pytest -q
.venv/bin/python tools/journal_gap_audit.py --help
.venv/bin/python tools/data_freshness_monitor.py --help
```

## Pass and fail conditions

Pass only when writer ownership is unique, locks and recovery are effective, required feeds are fresh, journals are monotonic/idempotent, quality checks pass, and paper/live-off state is proven. Missing access or evidence is `unknown`, not pass. Duplicate writers, stale critical data, unsafe permissions, process crash loops, live enablement, or untracked production code are failures.

## Output format

Return: scope/time; git state; runtime topology; writer matrix; journal metrics; safety config; check results; findings ordered P0–P3; exact host-specific remediation and rollback. Update `docs/audits/INSTITUTIONAL_READINESS_AUDIT.md` for full readiness work.

## Prohibited actions

Do not deploy, restart, kill, edit remote files, delete locks/logs, reveal secrets, or normalize dirty changes during an audit unless explicitly authorized.

## Example

“Use `$fx-repo-audit` to verify whether briefing and snapshot writers overlap on the MacBook and Mac mini, then report evidence without changing services.”
