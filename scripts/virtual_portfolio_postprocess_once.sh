#!/bin/zsh
# Raw replay: share the intake lock only for the initial DB capture.
set -u

cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX virtual portfolio Pythonが実行できません: $PYTHON"
  exit 1
fi

overall=0
"$PYTHON" tools/virtual_portfolio_canonical_outcomes.py \
  --lock-db-capture \
  --db "$PWD/logs/fx_virtual_portfolio_canonical_v2.sqlite3" \
  --store "$PWD/logs/canonical_v2_outcomes.jsonl" \
  --label-evidence-store "$PWD/logs/canonical_v2_label_evidence.jsonl" \
  --quote-log "$PWD/collect/log/quotes.jsonl" \
  --raw-store "$PWD/collect/raw" \
  </dev/null || overall=$?

exit "$overall"
