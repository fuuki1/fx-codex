# FX Codex operating rules

## Purpose

Build a reproducible FX research and decision-support system. Optimize for point-in-time correctness, cost-adjusted out-of-sample expectancy, calibrated uncertainty, and safe abstention—not headline win rate. Public data can support an institutional-grade research process; it is not proprietary dealer flow.

## Important paths

- `fx_backtester/`: research engine, labels, validation, simulated execution, risk, governance.
- `fx_intel/`: briefing, macro/news inputs, learning, decision journals.
- `tools/`, `scripts/`, `ops/`: monitoring and local launchd operations (analysis→Discord services).
- `docs/`, `reports/`, `runs/`: protocols, evidence, and immutable run artifacts.
- `.codex/skills/`: repeatable audit, validation, promotion, and incident workflows.

The system is **analysis-only**: it produces a Discord signal board and never sends real orders. The former `trader/` order-execution stack and the strategy-parameter optimization pipeline (`auto_optimize.py` / `promote_params.py` / `params_gate.py` / `strategy_params.json`) were removed on 2026-07-10; do not attempt to recreate or reference them.

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

- The system is analysis-only and must never place real orders. Do not add a live/broker execution path, and never mutate Mac mini processes without explicit human approval. If order execution is ever reintroduced, it must be a separate component gated behind multi-stage risk checks and explicit human sign-off.
- Preserve dirty worktrees, user files, journals, and runtime data. Do not reset, delete, initialize, or silently rewrite them.
- Use aware UTC internally. Record event, publication, availability, ingestion, source, revision, hash, run, and writer metadata when known. Reject ambiguous DST and future as-of matches.
- Fit transforms, features, thresholds, models, and calibrators only inside their permitted train/tune/calibration windows. Use purging, embargo, a separate test, and a one-time lockbox. Never use random splits for temporal claims.
- Include spread, slippage, commission, financing when available, and gap/stop behavior. Missing required costs or data quality is a no-trade/evaluation-unavailable result, not zero cost or success.
- Data-quality and risk vetoes cannot be overruled by confidence or committee votes. Uncalibrated conviction is explanatory only.
- Record all trials, seeds, hashes, windows, failures, and dirty state. Unprovenanced files under `runs/data/` are inadmissible for promotion.
- Distinguish synthetic functional tests, historical research, shadow, paper, and live. Never claim predictive improvement or institutional-grade readiness without matching evidence.

## Completion and review

A change is complete only after proportional lint, formatting, typing, tests, artifact/benchmark checks, documentation, and a diff review. For material data, validation, execution, or risk changes, obtain an independent adversarial review and re-run checks after fixes. Report blockers and negative results explicitly.
