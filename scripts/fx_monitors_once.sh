#!/bin/zsh
# 判断が読む3学習cache+manifestだけを一世代として定期公開する。
# 重い診断は別serviceへ隔離し、次のcache公開を遅らせない。
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX monitor Pythonが実行できません: $PYTHON"
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

publish_code=0
run_monitor timeframe-learning logs/timeframe_learning_monitor.json \
  tools/timeframe_learning_monitor.py \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" || publish_code=$?
[ "$publish_code" -eq 0 ] && refresh_freshness_report
exit "$publish_code"
