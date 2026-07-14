# Automated trading policy

## Decision

`fx-codex` is permanently a research, data-collection, validation,
decision-support, monitoring, and notification repository.

It has no automated-trading start phase and no broker-execution promotion path.

## Allowed

- read-only market and macro data collection
- historical research and point-in-time datasets
- backtests and offline execution simulation
- shadow predictions and decision journals
- model evaluation, calibration, abstention, governance, and monitoring
- Discord or other informational notifications

## Prohibited

- broker order creation, modification, cancellation, or closing
- broker position or account-risk mutation
- live or paper broker execution
- restoring the former `trader/` stack
- restoring `ALLOW_LIVE`, order executors, or parameter-to-order wiring
- treating research promotion as permission to send orders
- proposing live deployment as a future repository milestone
- copying execution code from rescue branches, backups, history, or another PR

## Terminal stage

The terminal deployment stage is shadow decision generation plus offline
simulation. Evidence labels such as `live market data` describe the data source,
not permission to trade.

Any future broker-execution project must be a separate repository with separate
credentials, infrastructure, governance, and explicit human ownership.
It must not be added to `fx-codex`.
