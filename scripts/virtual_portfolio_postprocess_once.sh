#!/bin/zsh
# Canonical outcome export. This must never hold the canonical intake lock.
set -u

cd "$(dirname "$0")/.." || exit 1
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX virtual portfolio Pythonが実行できません: $PYTHON"
  exit 1
fi

overall=0
"$PYTHON" tools/virtual_portfolio_canonical_outcomes.py \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --store "$PWD/logs/canonical_v2_outcomes.jsonl" \
  --label-evidence-store "$PWD/logs/canonical_v2_label_evidence.jsonl" \
  --quote-log "$PWD/collect/log/quotes.jsonl" \
  --raw-store "$PWD/collect/raw" \
  </dev/null || overall=$?

exit "$overall"
