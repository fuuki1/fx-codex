# Canonical v2 replay and prospective OOS gate

The legacy `fx_virtual_portfolio.sqlite3` is the immutable As-run ledger. Modeled
five-minute fills belong only in `fx_virtual_portfolio_counterfactual.sqlite3`.
The sole automatic writer updates only
`fx_virtual_portfolio_canonical_v2.sqlite3`, using anchored executable bid/ask
ticks and complete JPY cost accounting.

Shadow must remain stopped until all of the following are true:

1. The frozen database, configuration, code SHA, decision journals, quote log,
   quote index, and raw collector files have a verified backup manifest.
2. A clean release at one Git SHA passes Ruff, Black, mypy, full pytest, the data
   integrity audit, and service topology checks.
3. `tools/virtual_portfolio_full_replay_v2.py` creates a new database and a
   create-only replay manifest with `eligible_to_resume_shadow=true`.
4. SQLite integrity, Canonical v2 journal audit, zero replay conflicts, and zero
   mature waiting decisions all pass.
5. Required output heartbeats are healthy. A missing, stale, future-dated, or
   unhealthy heartbeat rejects new risk.

The prospective evaluation contract is fixed in
`ops/canonical_v2_oos_preregistration.json`. The OOS clock begins at the first
UTC day after clean same-SHA deployment, eligible replay, and shadow restart.
The 90-day test has no early stopping. Fusion weights, thresholds, stop/target
policy, risk policy, and ML are frozen for that window. Any change needs a new
untouched future window and cannot replace the registered result.

This repository remains analysis-only. No replay, shadow decision, evaluation,
or dashboard output may be connected to a broker order endpoint.
