#!/bin/zsh
# fx-codex収集サービスの状態を1画面で確認する。
set -u
setopt NULL_GLOB
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(
  com.fx-codex.snapshot
  com.fx-codex.briefing
  com.fx-codex.health
  com.fx-codex.horizon
  com.fx-codex.monitors
  com.fx-codex.monitor-diagnostics
  com.fx-codex.decision-outcome-audit
  com.fx-codex.dukascopy-history
  com.fx-codex.quote-index
  com.fx-codex.virtual-portfolio
  com.fx-codex.virtual-portfolio-postprocess
  com.fx-codex.virtual-portfolio-report
  com.fx-codex.virtual-portfolio-counterfactual
  com.fx-codex.virtual-portfolio-read
  com.fx-codex.dashboard-state
)
LEGACY_LABELS=(com.fx-codex.briefing.hourly com.fx-codex.virtual-portfolio-close)
overall_status=0
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi

has_managed_ancestor() {
  local child_pid="$1"
  local hops=0
  local parent_pid
  local parent_command
  while [ "$hops" -lt 4 ]; do
    parent_pid="$(ps -o ppid= -p "$child_pid" 2>/dev/null | tr -d ' ')"
    [ -n "$parent_pid" ] || return 1
    [ "$parent_pid" -gt 1 ] 2>/dev/null || return 1
    parent_command="$(ps -o command= -p "$parent_pid" 2>/dev/null || true)"
    case "$parent_command" in
      *"/tools/run_exclusive.py"*) return 0 ;;
    esac
    child_pid="$parent_pid"
    hops=$((hops + 1))
  done
  return 1
}

unmanaged_direct_writers() {
  local writer_pattern="[p]ython.*(fx_briefing.py|fx_tf_snapshot.py|tools/virtual_portfolio.py)"
  local pid
  local command
  for pid in $(pgrep -f "$writer_pattern" 2>/dev/null || true); do
    if has_managed_ancestor "$pid"; then
      continue
    fi
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    [ -z "$command" ] || echo "$pid $command"
  done
}

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
direct_writers=$(unmanaged_direct_writers)
cron_writers=$(crontab -l 2>/dev/null | grep -E "fx_briefing.py|fx_tf_snapshot.py|virtual_portfolio.py|fx_.*_loop.sh" || true)
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
echo "=== 仮想100万円canonical台帳・query-only API ==="
CANONICAL_PORTFOLIO_DB="$ROOT/logs/fx_virtual_portfolio_canonical_v2.sqlite3"
if [ -z "$PYTHON" ]; then
  echo "  CRITICAL: 仮想台帳確認用Pythonが見つからない"
  overall_status=2
elif [ ! -f "$CANONICAL_PORTFOLIO_DB" ]; then
  echo "  CRITICAL: canonical台帳が存在しない: $CANONICAL_PORTFOLIO_DB"
  overall_status=2
else
  "$PYTHON" - "$ROOT" "$CANONICAL_PORTFOLIO_DB" <<'PYEOF'
import json
from pathlib import Path
import sqlite3
import sys
from urllib.request import urlopen

root = Path(sys.argv[1]).resolve()
database = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(root))
from fx_intel.virtual_portfolio import SCHEMA_VERSION, portfolio_snapshot

connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
finally:
    connection.close()
snapshot = portfolio_snapshot(database)
print(
    "  DB:",
    f"name={database.name}",
    f"integrity={integrity}",
    f"schema={schema}",
    f"balance={snapshot.get('balance_jpy')}",
    f"open={snapshot.get('open_position_count')}",
    f"closed={snapshot.get('closed_trade_count')}",
    f"reconciled={snapshot.get('reconciliation', {}).get('passed')}",
)
if integrity != "ok" or schema != SCHEMA_VERSION or snapshot.get("configured") is not True:
    raise SystemExit(2)
try:
    with urlopen("http://127.0.0.1:8771/health", timeout=3) as response:
        health = json.load(response)
except Exception as error:
    print(f"  API: CRITICAL {type(error).__name__}")
    raise SystemExit(2)
print(
    "  API:",
    f"ok={health.get('ok')}",
    f"read_only={health.get('read_only')}",
    f"broker_connected={health.get('broker_connected')}",
)
if (
    health.get("ok") is not True
    or health.get("read_only") is not True
    or health.get("broker_connected") is not False
):
    raise SystemExit(2)
PYEOF
  virtual_status=$?
  if [ "$virtual_status" -gt "$overall_status" ]; then
    overall_status=$virtual_status
  fi
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
for f in logs/tradingview_technicals.json logs/briefing_tf_prices.jsonl logs/briefing_journal.jsonl logs/briefing_tf_journal.jsonl logs/briefing_horizon_forecasts.jsonl logs/briefing_horizon_learning.json logs/trade_outcome_monitor.json logs/decision_feedback_refresh_monitor.json logs/decision_expectancy_monitor.json; do
  if [ -f "$ROOT/$f" ]; then
    ls -laT "$ROOT/$f" | awk '{printf "  %s %s %s %s  %s\n", $6, $7, $8, $9, $NF}'
  fi
done

echo ""
echo "=== launchdログの直近エラー ==="
now_epoch=$(date +%s)
for f in "$ROOT"/logs/launchd/*.err.log; do
  [ -f "$f" ] || continue
  modified_epoch=$(stat -f %m "$f" 2>/dev/null || echo 0)
  age_seconds=$((now_epoch - modified_epoch))
  if [ "$age_seconds" -le 86400 ]; then
    tail -2 "$f" 2>/dev/null | sed "s|^|  $(basename $f) (${age_seconds}s前): |"
  elif [ -s "$f" ]; then
    echo "  $(basename $f): 過去ログのみ (${age_seconds}s前)"
  fi
done
exit "$overall_status"
