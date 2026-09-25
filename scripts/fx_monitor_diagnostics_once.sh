#!/bin/zsh
# Fast operational feedback plus lightweight historical diagnostics.
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
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


refresh_freshness_report() {
  # 公開直後に鮮度レポートを作り直し、5分周期の health を待たずに新しい世代を
  # briefing の鮮度ゲートへ反映する。health と同じ排他 lock を使い、実行中なら
  # 譲る(直後の health が反映する)。失敗しても公開そのものの結果は変えない。
  "$PYTHON" tools/run_exclusive.py --name fx-health --locks-dir "$PWD/logs/locks" \
    --busy-exit-code 75 -- \
    "$PYTHON" tools/data_freshness_monitor.py --root "$PWD" \
    --config ops/freshness_targets_timeframe.json </dev/null >> logs/fx_monitors.log 2>&1 || true
}

overall=0
# 旧fusion journalの再採点はhistorical/diagnostic専用。canonicalな判断方針を
# 書き換えないよう、改善registryは読み取りのみとする。
run_monitor trade-outcome logs/trade_outcome_monitor.json \
  tools/trade_outcome_monitor.py \
  --no-registry-update || overall=$?
feedback_code=0
run_monitor decision-feedback-refresh logs/decision_feedback_refresh_monitor.json \
  tools/decision_expectancy_monitor.py \
  --compact-feedback-refresh \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" || feedback_code=$?
if [ "$feedback_code" -eq 0 ]; then
  refresh_freshness_report
else
  overall=$feedback_code
fi
exit "$overall"
