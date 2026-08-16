#!/bin/zsh
# Historical/diagnostic monitor outputs. Never owns canonical learning caches.
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX diagnostic monitor Pythonが実行できません: $PYTHON"
  exit 1
fi

run_monitor() {
  name="$1"
  output="$2"
  shift 2
  before=0
  [ -f "$output" ] && before=$(stat -f %m "$output" 2>/dev/null || echo 0)
  result_code=0
  "$PYTHON" "$@" --quiet </dev/null >> logs/fx_monitors.log 2>&1 || result_code=$?
  if [ "$result_code" -gt 1 ]; then
    print -u2 "$name failed: exit=$result_code"
    return "$result_code"
  fi
  after=0
  [ -f "$output" ] && after=$(stat -f %m "$output" 2>/dev/null || echo 0)
  if [ "$after" -le "$before" ]; then
    print -u2 "$name did not refresh $output"
    return 2
  fi
  return 0
}

overall=0
# 旧fusion journalの再採点はhistorical/diagnostic専用。canonicalな判断方針を
# 書き換えないよう、改善registryは読み取りのみとする。
run_monitor trade-outcome logs/trade_outcome_monitor.json \
  tools/trade_outcome_monitor.py \
  --no-registry-update || overall=$?
run_monitor decision-expectancy logs/decision_expectancy_monitor.json \
  tools/decision_expectancy_monitor.py \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" || overall=$?
exit "$overall"
