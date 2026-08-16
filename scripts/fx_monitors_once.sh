#!/bin/zsh
# 判断が読む3学習cache+manifestだけを一世代として定期公開する。
# 重い診断は別serviceへ隔離し、次のcache公開を遅らせない。
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

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

run_monitor timeframe-learning logs/timeframe_learning_monitor.json \
  tools/timeframe_learning_monitor.py \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl"
