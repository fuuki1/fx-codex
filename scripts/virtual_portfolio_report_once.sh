#!/bin/zsh
# Idempotent daily report delivery, isolated from intake and canonical outcome export.
set -eu

cd "$(dirname "$0")/.." || exit 1
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX virtual portfolio Pythonが実行できません: $PYTHON"
  exit 1
fi

"$PYTHON" tools/virtual_portfolio_daily_report.py \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --run-root "$PWD/runs/virtual_portfolio_daily" \
  --root "$PWD" \
  --outbox "$PWD/logs/virtual_portfolio_daily_discord_outbox.json" \
  --send-discord \
  --if-ready \
  </dev/null
