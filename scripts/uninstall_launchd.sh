#!/bin/zsh
# fx-codexのlaunchdサービスを停止・除去する(rollback用)。
# ジャーナル・学習データ・ログには一切触れない。
set -u

AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)

for label in $LABELS; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "bootout: $label"
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    rm "$AGENTS_DIR/$label.plist"
    echo "removed: $AGENTS_DIR/$label.plist"
  fi
done
echo "完了。raw loopは起動しないでください。復旧はOPERATIONS_RUNBOOKのpin済みplist手順に従ってください。"
