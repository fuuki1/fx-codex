#!/bin/zsh
# fx-codex収集サービスの状態を1画面で確認する。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
overall_status=0

echo "=== launchd サービス ==="
for label in $LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    state=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -E "state =|last exit" | head -2 | tr -s " " | tr "\n" " ")
    echo "  $label : LOADED ($state)"
  else
    echo "  $label : NOT LOADED"
    overall_status=2
  fi
done

echo ""
echo "=== 手動ループの残存(あれば二重起動リスク) ==="
if loops=$(pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh"); then
  echo "$loops"
  overall_status=2
else
  echo "  (なし)"
fi

echo ""
echo "=== データ鮮度(最新レポート) ==="
if [ -f "$ROOT/logs/freshness_report.json" ]; then
  python3 - "$ROOT/logs/freshness_report.json" <<'PYEOF'
from datetime import UTC, datetime
import json, sys
report = json.load(open(sys.argv[1]))
print(f"  監視時刻: {report.get('monitor_timestamp')}  総合: {report.get('overall')}")
for t in report.get("targets", []):
    age = t.get("age_seconds")
    age_s = f"{age:.0f}s" if isinstance(age, (int, float)) else "?"
    print(f"  [{t.get('status'):8}] {t.get('name'):20} age={age_s:>8}  {t.get('reason') or ''}")
try:
    monitored = datetime.fromisoformat(str(report.get("monitor_timestamp")))
    if monitored.tzinfo is None:
        raise ValueError("timezone missing")
    report_age = (datetime.now(UTC) - monitored.astimezone(UTC)).total_seconds()
except (TypeError, ValueError):
    print("  CRITICAL: monitor_timestampが不正")
    raise SystemExit(2)
if report_age > 600:
    print(f"  CRITICAL: freshness report自体が古い({report_age:.0f}s)")
    raise SystemExit(2)
if report.get("overall") == "critical":
    raise SystemExit(2)
if report.get("overall") == "warning":
    raise SystemExit(1)
PYEOF
  report_status=$?
  if [ "$report_status" -gt "$overall_status" ]; then
    overall_status=$report_status
  fi
else
  echo "  (レポート未生成。com.fx-codex.health の初回実行待ち)"
  overall_status=2
fi

echo ""
echo "=== 主要ログの最終更新 ==="
for f in logs/briefing_tf_prices.jsonl logs/briefing_journal.jsonl logs/briefing_tf_journal.jsonl; do
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
exit "$overall_status"
