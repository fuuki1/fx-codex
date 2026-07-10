---
name: fx-incident-triage
description: Triage stale data, duplicate writers, process crash loops, broker disconnects, reconciliation failures, spread/slippage anomalies, drift, and unexpected positions in fx-codex. Use during alerts or degraded paper operation.
---

# FX incident triage

## Purpose and inputs

Contain risk first, preserve evidence, identify the failure domain, and produce host-specific recovery and rollback. Inputs are alert time/type, affected host, logs/monitor snapshots, expected service topology, and current paper position state.

## Procedure

1. Confirm host, UTC clock, scope, and `TRADING_MODE=paper` / `ALLOW_LIVE=0`. If data, broker, reconciliation, writer, or risk state is uncertain, enforce no-trade/abstain before diagnosis.
2. Preserve logs and hashes. Inspect status, heartbeats, freshness, locks, process parents/start times, launchd/cron/Docker, restart counts, disk, permissions, and recent config/code changes.
3. Classify: data/source, duplicate writer, application, infrastructure, broker/execution, reconciliation, model/drift, risk-limit, or clock.
4. Check for open/unexpected positions and pending orders through approved read-only interfaces. Escalate human review; do not improvise liquidation or live actions.
5. Select the smallest reversible paper-safe recovery. Validate freshness, one writer, reconciliation, health, and a dry-run before restoring decision output.
6. Record timeline, impact, root cause vs contributing factors, detection gap, actions, evidence, recovery criteria, rollback, and follow-up owner.

## Commands

```bash
scripts/status_launchd.sh
.venv/bin/python tools/data_freshness_monitor.py --help
.venv/bin/python tools/journal_gap_audit.py --help
ps ax -o pid,ppid,lstart,command
launchctl list | rg 'fx|trading'
```

Use `docs/OPERATIONS_RUNBOOK.md`; label every command “MacBook” or “Mac mini” with its working directory.

## Pass and fail conditions

Recovery passes only when critical data is fresh, natural keys are unique, exactly one writer owns each journal, health/reconciliation are green, no unexpected positions remain, paper/live-off guards are proven, and the incident does not recur during the observation window. Otherwise keep abstention and escalate.

## Output format

Severity/status; affected host/services/data; UTC timeline; containment; evidence; root cause; recovery checks; exact commands; rollback; residual risk; follow-ups.

## Prohibited actions

Do not delete logs, break locks manually while an owner may live, restart everything blindly, expose secrets, change live guards, modify remote state without approval, or call unchanged stale state healthy.

## Example

“Use `$fx-incident-triage` for a stale `briefing_tf_prices.jsonl` alert and possible duplicate launchd/manual writer; contain to no-trade and give a reversible recovery.”
