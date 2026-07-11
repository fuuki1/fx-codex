#!/bin/zsh
# fx-codex収集サービスを再起動する(kickstart -k = 実行中なら殺してから再実行)。
set -u
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
overall_status=0

# 部分restartを避けるため、全labelと競合writerを変更前に検証する。
for label in $LABELS; do
  if ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    echo "NOT LOADED: $label (restartは未実行)" >&2
    overall_status=2
  fi
done
if [ "$overall_status" -ne 0 ]; then
  exit "$overall_status"
fi
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    echo "LEGACY LOADED: $label (restartは未実行)" >&2
    exit 2
  fi
done

loops=$(pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh" || true)
direct_writers=$(pgrep -fl "[p]ython.*(fx_briefing.py|fx_tf_snapshot.py)" || true)
cron_writers=$(crontab -l 2>/dev/null | grep -E "fx_briefing.py|fx_tf_snapshot.py|fx_.*_loop.sh" || true)
if [ -n "$loops$direct_writers$cron_writers" ]; then
  echo "競合writer候補を検知したためrestartは未実行です。親PID/cwdを確認してください。" >&2
  [ -z "$loops" ] || echo "$loops" >&2
  [ -z "$direct_writers" ] || echo "$direct_writers" >&2
  [ -z "$cron_writers" ] || echo "$cron_writers" >&2
  exit 2
fi

for label in $LABELS; do
  if launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; then
    echo "restarted: $label"
  else
    echo "NOT LOADED: $label (scripts/install_launchd.sh を先に実行)" >&2
    overall_status=2
  fi
done
exit "$overall_status"
