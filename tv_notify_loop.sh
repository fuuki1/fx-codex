#!/bin/zsh
# TradingView分析のDiscord通知を1時間足の確定後(毎時5分)に送信し続けるループ。
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# sync-fx-codex.sh と同様にターミナルから直接起動すること。
#   ./tv_notify_loop.sh &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

while true; do
  .venv/bin/python tv_discord_notify.py >> logs/tv_notify.log 2>&1
  now=$(date +%s)
  next=$(( (now / 3600 + 1) * 3600 + 300 ))
  sleep $(( next - now ))
done
