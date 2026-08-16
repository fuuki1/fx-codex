#!/bin/zsh
# One analysis-only virtual-portfolio consumer cycle.
# launchd owns cadence; run_exclusive.py owns process exclusion.
set -eu

cd "$(dirname "$0")/.." || exit 1

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX virtual portfolio Pythonが実行できません: $PYTHON"
  exit 1
fi

arguments=(
  tools/virtual_portfolio.py
  --db
  "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3"
  --as-run-db
  "$PWD/logs/fx_virtual_portfolio.sqlite3"
  --counterfactual-db
  "$PWD/logs/fx_virtual_portfolio_counterfactual.sqlite3"
  --writer-id
  com.fx-codex.virtual-portfolio-v2
  cycle
  --snapshot-prices
  "$PWD/logs/briefing_tf_prices.jsonl"
  --quotes
  "$PWD/collect/log/quotes.jsonl"
  --quote-index
  "$PWD/collect/index/quotes.sqlite3"
  --audit-run-root
  "$PWD/runs/virtual_portfolio"
  --heartbeat-config
  "$PWD/ops/virtual_portfolio_required_heartbeats.json"
  --stage-timing-log
  "$PWD/logs/decision_pipeline_stage_timings.jsonl"
  --skip-counterfactual
)
if [ "${1:-}" = "--close-session" ]; then
  arguments+=(--close-session)
elif [ "$#" -gt 0 ]; then
  print -u2 "unsupported argument: $1"
  exit 2
fi

"$PYTHON" "${arguments[@]}" </dev/null
