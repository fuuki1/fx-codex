#!/bin/zsh
# 時間足別採点用の価格スナップショットを5分ごとに記録し続けるループ。
# fx_briefing_loop.sh(毎時10分の判断)とは独立。短い足(特に15m)の採点窓に
# 入る将来価格を密に供給し、15m/1h/4h/1d の全時間足を採点可能にする。
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# ターミナルから直接起動すること。
#   ./fx_tf_snapshot_loop.sh &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

while true; do
  .venv/bin/python fx_tf_snapshot.py >> logs/fx_tf_snapshot.log 2>&1
  now=$(date +%s)
  # 次の5分境界(00/05/10…分)へ寄せる。別のprice writerとの併走は禁止。
  next=$(( (now / 300 + 1) * 300 ))
  sleep $(( next - now ))
done
