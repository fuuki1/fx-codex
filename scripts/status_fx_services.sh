#!/bin/zsh
# fx-codex収集サービスの状態を1画面で確認する。
set -u
setopt NULL_GLOB
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
overall_status=0
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi

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
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    echo "  $label : LEGACY LOADED (競合)"
    overall_status=2
  fi
done

echo ""
echo "=== 競合writer候補(あれば二重起動リスク) ==="
loops=$(pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh" || true)
direct_writers=$(pgrep -fl "[p]ython.*(fx_briefing.py|fx_tf_snapshot.py)" || true)
cron_writers=$(crontab -l 2>/dev/null | grep -E "fx_briefing.py|fx_tf_snapshot.py|fx_.*_loop.sh" || true)
if [ -n "$loops$direct_writers$cron_writers" ]; then
  [ -z "$loops" ] || echo "$loops"
  [ -z "$direct_writers" ] || echo "$direct_writers"
  [ -z "$cron_writers" ] || echo "$cron_writers"
  echo "  activeな正規launchd子プロセスか、手動/cron/別checkoutかを親PID/cwdで確認してください。"
  overall_status=2
else
  echo "  (なし)"
fi

echo ""
echo "=== データ鮮度(最新レポート) ==="
if [ -f "$ROOT/logs/freshness_report.json" ]; then
  if [ -z "$PYTHON" ]; then
    echo "  CRITICAL: freshness report確認用Pythonが見つからない"
    exit 2
  fi
  "$PYTHON" - "$ROOT/logs/freshness_report.json" <<'PYEOF'
from datetime import datetime, timezone
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except (OSError, json.JSONDecodeError, UnicodeError) as error:
    print(f"  CRITICAL: freshness reportを読めない: {error}")
    raise SystemExit(2)
if not isinstance(report, dict):
    print("  CRITICAL: freshness reportがobjectではない")
    raise SystemExit(2)
print(f"  監視時刻: {report.get('monitor_timestamp')}  総合: {report.get('overall')}")
targets = report.get("targets")
if not isinstance(targets, list) or not all(isinstance(t, dict) for t in targets):
    print("  CRITICAL: freshness targetsがobject配列ではない")
    raise SystemExit(2)
for t in targets:
    age = t.get("age_seconds")
    age_s = f"{age:.0f}s" if isinstance(age, (int, float)) else "?"
    print(f"  [{t.get('status'):8}] {t.get('name'):20} age={age_s:>8}  {t.get('reason') or ''}")
try:
    monitored = datetime.fromisoformat(str(report.get("monitor_timestamp")))
    if monitored.tzinfo is None:
        raise ValueError("timezone missing")
    report_age = (datetime.now(timezone.utc) - monitored.astimezone(timezone.utc)).total_seconds()
except (TypeError, ValueError):
    print("  CRITICAL: monitor_timestampが不正")
    raise SystemExit(2)
if report_age > 600:
    print(f"  CRITICAL: freshness report自体が古い({report_age:.0f}s)")
    raise SystemExit(2)
if report_age < -60:
    print(f"  CRITICAL: freshness reportが未来時刻({-report_age:.0f}s先)")
    raise SystemExit(2)
if report.get("overall") == "critical":
    raise SystemExit(2)
if report.get("overall") == "warning":
    raise SystemExit(1)
if report.get("overall") != "ok":
    print(f"  CRITICAL: freshness overallが未知: {report.get('overall')!r}")
    raise SystemExit(2)
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
