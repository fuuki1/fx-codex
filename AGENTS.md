# FX Codex operating rules

## Purpose

Build a reproducible FX research and decision-support system. Optimize for point-in-time correctness, cost-adjusted out-of-sample expectancy, calibrated uncertainty, and safe abstention—not headline win rate. Public data can support an institutional-grade research process; it is not proprietary dealer flow.

## Important paths

- `fx_backtester/`: research engine, labels, validation, simulated execution, risk, governance.
- `fx_intel/`: briefing, macro/news inputs, learning, decision journals.
- `tools/`, `scripts/`, `ops/`: monitoring and local launchd operations (analysis→Discord services).
- `docs/`, `reports/`, `runs/`: protocols, evidence, and immutable run artifacts.
- `.codex/skills/`: repeatable audit, validation, promotion, and incident workflows.

The system is **permanently analysis-only**. It may collect market data, perform historical research, run offline simulations, produce shadow decisions, and send Discord notifications. It must never connect a decision to a broker order endpoint. The former `trader/` order-execution stack and parameter-promotion execution path are intentionally removed and must not be recreated, restored, copied from rescue branches, or replaced under a different name.

## Setup and checks

Run from the repository root:

```bash
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/mypy fx_backtester fx_intel *.py
.venv/bin/pytest -q
```

Dry-run the briefing with `.venv/bin/python fx_briefing.py --signal-board --dry-run`. Run a reproducible sample backtest with `.venv/bin/python -m fx_backtester.cli backtest --data examples/sample_prices.csv --strategy ma_cross --output-dir runs/local_check`; this sample is synthetic and cannot support promotion or performance claims.

## Non-negotiable safety rules

- The repository has no automated-trading start phase. Broker order creation, modification, cancellation, closing, position mutation, and account-risk mutation are permanently out of scope.
- Do not propose, plan, implement, restore, document, scaffold, or enable live or paper broker execution. A user request to do so requires a separate repository and must not modify this repository.
- Shadow decisions and offline simulated execution are the terminal validation stages. `live` is an evidence label only when describing external data; it is never a deployment target.
- Never restore `trader/`, `executor.py`, `ALLOW_LIVE`, broker-order clients, order endpoints, or automatic parameter-to-order wiring from history, rescue branches, backups, or generated code.
- Mac mini services in this repository are limited to read-only collection, research, monitoring, and notifications.
- Preserve dirty worktrees, user files, journals, and runtime data. Do not reset, delete, initialize, or silently rewrite them.
- Use aware UTC internally. Record event, publication, availability, ingestion, source, revision, hash, run, and writer metadata when known. Reject ambiguous DST and future as-of matches.
- Fit transforms, features, thresholds, models, and calibrators only inside their permitted train/tune/calibration windows. Use purging, embargo, a separate test, and a one-time lockbox. Never use random splits for temporal claims.
- Include spread, slippage, commission, financing when available, and gap/stop behavior. Missing required costs or data quality is a no-trade/evaluation-unavailable result, not zero cost or success.
- Data-quality and risk vetoes cannot be overruled by confidence or committee votes. Uncalibrated conviction is explanatory only.
- Record all trials, seeds, hashes, windows, failures, and dirty state. Unprovenanced files under `runs/data/` are inadmissible for promotion.
- Distinguish synthetic functional tests, historical research, shadow, paper, and live. Never claim predictive improvement or institutional-grade readiness without matching evidence.

## Completion and review

A change is complete only after proportional lint, formatting, typing, tests, artifact/benchmark checks, documentation, and a diff review. For material data, validation, execution, or risk changes, obtain an independent adversarial review and re-run checks after fixes. Report blockers and negative results explicitly.
