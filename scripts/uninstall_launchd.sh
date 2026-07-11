#!/bin/zsh
# fx-codexのlaunchdサービスを停止・除去する(rollback用)。
# ジャーナル・学習データ・ログには一切触れない。
set -u

AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
overall_status=0

for label in $LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      echo "ERROR: bootout失敗。plistを残します: $label" >&2
      overall_status=2
      continue
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "ERROR: bootout後もserviceがロード済み。plistを残します: $label" >&2
      overall_status=2
      continue
    fi
    echo "bootout: $label"
  fi
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    if ! rm "$AGENTS_DIR/$label.plist"; then
      echo "ERROR: plistを削除できません: $AGENTS_DIR/$label.plist" >&2
      overall_status=2
      continue
    fi
    echo "removed: $AGENTS_DIR/$label.plist"
  fi
done
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      echo "ERROR: legacy bootout失敗。plistを残します: $label" >&2
      overall_status=2
      continue
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "ERROR: legacy serviceがbootout後もロード済み: $label" >&2
      overall_status=2
      continue
    fi
  fi
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    disabled="$AGENTS_DIR/$label.plist.disabled-uninstall-$(date +%Y%m%d%H%M%S)"
    if ! mv "$AGENTS_DIR/$label.plist" "$disabled"; then
      echo "ERROR: legacy plistを退避できません: $label" >&2
      overall_status=2
      continue
    fi
    echo "legacy plist退避: $disabled"
  fi
done
if [ "$overall_status" -eq 0 ]; then
  echo "完了。raw loopは起動しないでください。復旧はOPERATIONS_RUNBOOKのpin済みplist手順に従ってください。"
else
  echo "未停止serviceがあります。writerが残っている前提でrollbackを中止してください。" >&2
fi
exit "$overall_status"
