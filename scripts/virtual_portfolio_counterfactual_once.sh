#!/bin/zsh
# Optional explanatory ledger. It has no canonical DB writer access or broker path.
set -eu

cd "$(dirname "$0")/.." || exit 1
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX virtual portfolio Pythonが実行できません: $PYTHON"
  exit 1
fi

"$PYTHON" tools/virtual_portfolio.py \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --as-run-db "$PWD/logs/fx_virtual_portfolio.sqlite3" \
  --counterfactual-db "$PWD/logs/fx_virtual_portfolio_counterfactual.sqlite3" \
  --writer-id com.fx-codex.virtual-portfolio-v2 \
  counterfactual-cycle \
  --snapshot-prices "$PWD/logs/briefing_tf_prices.jsonl" \
  --quotes "$PWD/collect/log/quotes.jsonl" \
  --quote-index "$PWD/collect/index/quotes.sqlite3" \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" \
  </dev/null
