#!/bin/zsh
# fx-codex収集サービスの状態を1画面で確認する。
set -u
setopt NULL_GLOB
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFLIGHT_LIB="$ROOT/scripts/writer_preflight.sh"
if [ ! -r "$PREFLIGHT_LIB" ]; then
  echo "CRITICAL: writer preflight libraryがありません: $PREFLIGHT_LIB" >&2
  exit 2
fi
source "$PREFLIGHT_LIB"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
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
wrapper_writers=$(pgrep -fl "[f]x_briefing_once.sh|[r]un_exclusive.py.*fx-(briefing|snapshot)" || true)
cron_stdout=$(mktemp "${TMPDIR:-/tmp}/fx-codex-status-crontab.XXXXXX") || {
  echo "  CRITICAL: crontab検証用一時ファイルを作成できません"
  overall_status=2
  cron_stdout=""
}
cron_stderr=$(mktemp "${TMPDIR:-/tmp}/fx-codex-status-crontab-error.XXXXXX") || {
  echo "  CRITICAL: crontab検証用一時ファイルを作成できません"
  [ -z "$cron_stdout" ] || rm -f "$cron_stdout"
  overall_status=2
  cron_stdout=""
}
cron_writers=""
if [ -n "$cron_stdout" ] && [ -n "$cron_stderr" ]; then
  crontab -l > "$cron_stdout" 2> "$cron_stderr"
  cron_status=$?
  if [ "$cron_status" -eq 0 ]; then
    cron_writers=$(fx_filter_writer_lines < "$cron_stdout" || true)
  elif fx_crontab_is_absent "$cron_status" "$cron_stderr"; then
    :
  else
    echo "  CRITICAL: crontabを検証できません"
    sed -n '1,3p' "$cron_stderr" | sed 's/^/  /'
    overall_status=2
  fi
  rm -f "$cron_stdout" "$cron_stderr"
else
  [ -z "${cron_stdout:-}" ] || rm -f "$cron_stdout"
  [ -z "${cron_stderr:-}" ] || rm -f "$cron_stderr"
fi
if [ -n "$loops$direct_writers$wrapper_writers$cron_writers" ]; then
  [ -z "$loops" ] || echo "$loops"
  [ -z "$direct_writers" ] || echo "$direct_writers"
  [ -z "$wrapper_writers" ] || echo "$wrapper_writers"
  [ -z "$cron_writers" ] || echo "$cron_writers"
  echo "  activeな正規launchd子プロセスか、手動/cron/別checkoutかを親PID/cwdで確認してください。"
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
    report_age = (datetime.now(UTC) - monitored.astimezone(UTC)).total_seconds()
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
