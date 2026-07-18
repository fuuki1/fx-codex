#!/bin/zsh
# 学習ダッシュボード用の期待値監視JSONを定期更新する。
# exit=1 は「監視結果が警告/失敗」を表しJSON自体は生成済みなので許容する。
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

overall=0
run_monitor trade-outcome logs/trade_outcome_monitor.json tools/trade_outcome_monitor.py || overall=$?
run_monitor decision-expectancy logs/decision_expectancy_monitor.json tools/decision_expectancy_monitor.py || overall=$?
exit "$overall"
