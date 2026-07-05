#!/bin/zsh
# 時間足別採点用の価格スナップショットを5分ごとに記録し続けるループ。
# fx_briefing_loop.sh(毎時10分の判断)とは独立。短い足(特に15m)の採点窓に
# 入る将来価格を密に供給し、15m/1h/4h/1d の全時間足を採点可能にする。
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# ターミナルから直接起動すること。
#   nohup ./fx_tf_snapshot_loop.sh </dev/null > logs/fx_tf_snapshot_loop.out 2> logs/fx_tf_snapshot_loop.err &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

while true; do
  .venv/bin/python fx_tf_snapshot.py </dev/null >> logs/fx_tf_snapshot.log 2>&1
  now=$(date +%s)
  # 次の5分境界(00/05/10…分)へ寄せる。毎時:10の回は判断ループ(fx_briefing_loop.sh)と
  # 重なるが、こちらは価格取得のみの軽い処理なので同時に走っても実害はない。
  next=$(( (now / 300 + 1) * 300 ))
  sleep $(( next - now ))
done
