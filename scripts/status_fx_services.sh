#!/bin/zsh
# fx-codex収集サービスの状態を1画面で確認する。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health com.fx-codex.horizon com.fx-codex.monitors)

echo "=== launchd サービス ==="
for label in $LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    state=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -E "state =|last exit" | head -2 | tr -s " " | tr "\n" " ")
    echo "  $label : LOADED ($state)"
  else
    echo "  $label : NOT LOADED"
  fi
done

echo ""
echo "=== 手動ループの残存(あれば二重起動リスク) ==="
pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh" || echo "  (なし)"

echo ""
echo "=== データ鮮度(最新レポート) ==="
if [ -f "$ROOT/logs/freshness_report.json" ]; then
  python3 - "$ROOT/logs/freshness_report.json" <<'PYEOF'
import json, sys
report = json.load(open(sys.argv[1]))
print(f"  監視時刻: {report.get('monitor_timestamp')}  総合: {report.get('overall')}")
for t in report.get("targets", []):
    age = t.get("age_seconds")
    age_s = f"{age:.0f}s" if isinstance(age, (int, float)) else "?"
    print(f"  [{t.get('status'):8}] {t.get('name'):20} age={age_s:>8}  {t.get('reason') or ''}")
PYEOF
else
  echo "  (レポート未生成。com.fx-codex.health の初回実行待ち)"
fi

echo ""
echo "=== 主要ログの最終更新 ==="
for f in logs/briefing_tf_prices.jsonl logs/briefing_journal.jsonl logs/briefing_tf_journal.jsonl logs/briefing_horizon_forecasts.jsonl logs/briefing_horizon_learning.json logs/trade_outcome_monitor.json logs/decision_expectancy_monitor.json; do
  if [ -f "$ROOT/$f" ]; then
    ls -laT "$ROOT/$f" | awk '{printf "  %s %s %s %s  %s\n", $6, $7, $8, $9, $NF}'
  fi
done

echo ""
echo "=== launchdログの直近エラー ==="
for f in "$ROOT"/logs/launchd/*.err.log; do
  [ -f "$f" ] || continue
  tail -2 "$f" 2>/dev/null | sed "s|^|  $(basename $f): |"
done
exit 0
