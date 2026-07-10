#!/bin/zsh
# fx-codex収集サービスを再起動する(kickstart -k = 実行中なら殺してから再実行)。
set -u
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
for label in $LABELS; do
  if launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; then
    echo "restarted: $label"
  else
    echo "NOT LOADED: $label (scripts/install_launchd.sh を先に実行)"
  fi
done

