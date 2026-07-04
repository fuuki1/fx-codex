#!/bin/zsh
# ニュース×経済指標×テクニカル統合ブリーフィングを毎時10分に送信し続けるループ。
# (tv_notify_loop.sh が毎時5分なので5分ずらしている)
#
# 毎時2通を連続送信する:
#   1. 融合1判断モード(委員会・ML・昇格ゲートあり=本命パイプライン)
#   2. 時間足別モード(--per-timeframe。15m/1h/4h/1d を独立採点・学習)
# 両モードはジャーナル・学習ファイルを分けており互いに干渉しない。片方が
# 失敗しても他方は実行する(|| true で連鎖停止を防ぐ)。時間足別の採点用に
# fx_tf_snapshot_loop.sh(5分ごとの価格記録)も別途起動しておくこと。
#
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# ターミナルから直接起動すること。
#   ./fx_briefing_loop.sh &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

while true; do
  .venv/bin/python fx_briefing.py >> logs/fx_briefing.log 2>&1 || true
  .venv/bin/python fx_briefing.py --per-timeframe >> logs/fx_briefing_tf.log 2>&1 || true
  now=$(date +%s)
  next=$(( (now / 3600 + 1) * 3600 + 600 ))
  sleep $(( next - now ))
done
