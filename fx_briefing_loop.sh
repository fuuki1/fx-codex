#!/bin/zsh
# ニュース×経済指標×テクニカル統合ブリーフィングを毎時10分に送信し続けるループ。
# (tv_notify_loop.sh が毎時5分なので5分ずらしている)
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# ターミナルから直接起動すること。
#   ./fx_briefing_loop.sh &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

while true; do
  .venv/bin/python fx_briefing.py >> logs/fx_briefing.log 2>&1
  now=$(date +%s)
  next=$(( (now / 3600 + 1) * 3600 + 600 ))
  sleep $(( next - now ))
done
