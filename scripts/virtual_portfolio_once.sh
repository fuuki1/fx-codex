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
)
if [ "${1:-}" = "--close-session" ]; then
  arguments+=(--close-session)
elif [ "$#" -gt 0 ]; then
  print -u2 "unsupported argument: $1"
  exit 2
fi

"$PYTHON" "${arguments[@]}" </dev/null

# Fail closed unless every mature intraday virtual fill is present in the
# append-only canonical net-R store. The connector is idempotent, so a later
# cycle safely retries any interruption between the SQLite close and JSONL append.
"$PYTHON" tools/virtual_portfolio_canonical_outcomes.py \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --store "$PWD/logs/canonical_v2_outcomes.jsonl" \
  --label-evidence-store "$PWD/logs/canonical_v2_label_evidence.jsonl" \
  --quote-log "$PWD/collect/log/quotes.jsonl" \
  --raw-store "$PWD/collect/raw" \
  </dev/null

# A persisted close request may settle in a later 5-minute cycle after delayed
# bid/ask arrives.  The report command is create-only and delivery-receipted, so
# every cycle can safely retry without duplicating a completed daily report.
"$PYTHON" tools/virtual_portfolio_daily_report.py \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --run-root "$PWD/runs/virtual_portfolio_daily" \
  --root "$PWD" \
  --outbox "$PWD/logs/virtual_portfolio_daily_discord_outbox.json" \
  --send-discord \
  --if-ready \
  </dev/null
