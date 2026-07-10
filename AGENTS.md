# FX Codex operating rules

## Purpose

Build a reproducible FX research and decision-support system. Optimize for point-in-time correctness, cost-adjusted out-of-sample expectancy, calibrated uncertainty, and safe abstention—not headline win rate. Public data can support an institutional-grade research process; it is not proprietary dealer flow.

## Important paths

- `fx_backtester/`: research engine, labels, validation, execution, risk, governance.
- `fx_intel/`: briefing, macro/news inputs, learning, decision journals.
- `trader/`: isolated paper execution stack and its own toolchain.
- `tools/`, `scripts/`, `ops/`: monitoring and local launchd operations.
- `docs/`, `reports/`, `runs/`: protocols, evidence, and immutable run artifacts.
- `.codex/skills/`: repeatable audit, validation, promotion, and incident workflows.

## Setup and checks

Run from the repository root:

```bash
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/mypy fx_backtester fx_intel
.venv/bin/pytest -q
```

The `trader/` stack is checked separately:

```bash
cd trader
.venv/bin/ruff check .
.venv/bin/mypy app
.venv/bin/pytest -q
```

Dry-run the briefing with `.venv/bin/python fx_briefing.py --signal-board --dry-run`. Run a reproducible sample backtest with `.venv/bin/python -m fx_backtester.cli backtest --data examples/sample_prices.csv --strategy ma_cross --output-dir runs/local_check`; this sample is synthetic and cannot support promotion or performance claims.

## Non-negotiable safety rules

- Keep `TRADING_MODE=paper` and `ALLOW_LIVE=0`. Never enable limited-live/live or mutate Mac mini processes without explicit human approval.
- Preserve dirty worktrees, user files, journals, and runtime data. Do not reset, delete, initialize, or silently rewrite them.
- Use aware UTC internally. Record event, publication, availability, ingestion, source, revision, hash, run, and writer metadata when known. Reject ambiguous DST and future as-of matches.
- Fit transforms, features, thresholds, models, and calibrators only inside their permitted train/tune/calibration windows. Use purging, embargo, a separate test, and a one-time lockbox. Never use random splits for temporal claims.
- Include spread, slippage, commission, financing when available, and gap/stop behavior. Missing required costs or data quality is a no-trade/evaluation-unavailable result, not zero cost or success.
- Data-quality and risk vetoes cannot be overruled by confidence or committee votes. Uncalibrated conviction is explanatory only.
- Record all trials, seeds, hashes, windows, failures, and dirty state. Unprovenanced files under `runs/data/` are inadmissible for promotion.
- Distinguish synthetic functional tests, historical research, shadow, paper, and live. Never claim predictive improvement or institutional-grade readiness without matching evidence.

## Completion and review

A change is complete only after proportional lint, formatting, typing, tests, artifact/benchmark checks, documentation, and a diff review. For material data, validation, execution, or risk changes, obtain an independent adversarial review and re-run checks after fixes. Report blockers and negative results explicitly.
