#!/bin/zsh
# fx-codexのlaunchdサービスを停止・除去する(rollback用)。
# ジャーナル・学習データ・ログには一切触れない。
set -u

AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health com.fx-codex.horizon com.fx-codex.monitors)

for label in $LABELS; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "bootout: $label"
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    rm "$AGENTS_DIR/$label.plist"
    echo "removed: $AGENTS_DIR/$label.plist"
  fi
done
echo "完了。旧方式へ戻す場合はREADMEどおり nohup ./fx_briefing_loop.sh 等を手動起動してください。"
