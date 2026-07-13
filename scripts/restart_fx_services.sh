#!/bin/zsh
# fx-codex収集サービスを再起動する(kickstart -k = 実行中なら殺してから再実行)。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFLIGHT_LIB="$ROOT/scripts/writer_preflight.sh"
if [ ! -r "$PREFLIGHT_LIB" ]; then
  echo "ERROR: writer preflight libraryがありません: $PREFLIGHT_LIB" >&2
  exit 1
fi
source "$PREFLIGHT_LIB"
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
wrapper_writers=$(pgrep -fl "[f]x_briefing_once.sh|[r]un_exclusive.py.*fx-(briefing|snapshot)" || true)
cron_stdout=$(mktemp "${TMPDIR:-/tmp}/fx-codex-restart-crontab.XXXXXX") || exit 1
cron_stderr=$(mktemp "${TMPDIR:-/tmp}/fx-codex-restart-crontab-error.XXXXXX") || {
  rm -f "$cron_stdout"
  exit 1
}
crontab -l > "$cron_stdout" 2> "$cron_stderr"
cron_status=$?
if [ "$cron_status" -eq 0 ]; then
  :
elif fx_crontab_is_absent "$cron_status" "$cron_stderr"; then
  : > "$cron_stdout"
else
  echo "ERROR: crontabを検証できないためrestartは未実行です。" >&2
  sed -n '1,3p' "$cron_stderr" >&2
  rm -f "$cron_stdout" "$cron_stderr"
  exit 2
fi
cron_writers=$(fx_filter_writer_lines < "$cron_stdout" || true)
rm -f "$cron_stdout" "$cron_stderr"
if [ -n "$loops$direct_writers$wrapper_writers$cron_writers" ]; then
  echo "競合writer候補を検知したためrestartは未実行です。親PID/cwdを確認してください。" >&2
  [ -z "$loops" ] || echo "$loops" >&2
  [ -z "$direct_writers" ] || echo "$direct_writers" >&2
  [ -z "$wrapper_writers" ] || echo "$wrapper_writers" >&2
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
