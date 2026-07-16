---
name: fx-incident-triage
description: Triage stale FX data, duplicate writers, process crash loops, read-only source disconnects, spread anomalies, drift, and notification failures in fx-codex. Use during analysis, collection, or monitoring alerts.
---

# FX incident triage

## Purpose and inputs

Contain analysis output first, preserve evidence, identify the failure domain, and produce host-specific recovery and rollback. Inputs are alert time/type, affected host, logs/monitor snapshots, and expected read-only service topology.

## Procedure

1. Confirm host, UTC clock, and scope. If data, source, writer, or model state is uncertain, enforce abstention before diagnosis.
2. Preserve logs and hashes. Inspect status, heartbeats, freshness, locks, process parents/start times, launchd/cron/Docker, restart counts, disk, permissions, and recent config/code changes.
3. Classify: data/source, duplicate writer, application, infrastructure, notification, model/drift, or clock.
4. Confirm no prohibited broker-order, position-mutation, or account-risk process has appeared. Preserve evidence and escalate if one exists; do not interact with it from this repository.
5. Select the smallest reversible analysis-safe recovery. Validate freshness, complete coverage, one writer, health, and a dry-run before restoring decision output.
6. Record timeline, impact, root cause vs contributing factors, detection gap, actions, evidence, recovery criteria, rollback, and follow-up owner.

## Commands

```bash
scripts/status_fx_services.sh
.venv/bin/python tools/data_freshness_monitor.py --help
.venv/bin/python tools/journal_gap_audit.py --help
ps ax -o pid,ppid,lstart,command
launchctl list | rg 'fx-codex|trader'
```

Use `docs/OPERATIONS_RUNBOOK.md`; label every command “MacBook” or “Mac mini” with its working directory.

## Pass and fail conditions

Recovery passes only when critical data is fresh and complete, natural keys are unique, exactly one writer owns each journal, health is green, prohibited execution processes are absent, and the incident does not recur during the observation window. Otherwise keep abstention and escalate.

## Output format

Severity/status; affected host/services/data; UTC timeline; containment; evidence; root cause; recovery checks; exact commands; rollback; residual risk; follow-ups.

## Prohibited actions

Do not delete logs, break locks manually while an owner may live, restart everything blindly, expose secrets, change live guards, modify remote state without approval, or call unchanged stale state healthy.

## Example

“Use `$fx-incident-triage` for a stale `briefing_tf_prices.jsonl` alert and possible duplicate launchd/manual writer; contain to no-trade and give a reversible recovery.”
