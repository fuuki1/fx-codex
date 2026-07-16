#!/bin/zsh
# FXシグナルボードを5分境界(00/05/10…分)ごとにDiscordへ1通だけ送る。
# 従来の融合版・時間足別版の複数通知は送らず、上位3候補、システム状態、
# データ品質を単一メッセージへ集約する。
#
# Desktop配下はmacOSのTCC制限でlaunchd/cronから読めないため、
# ターミナルから直接起動すること。
#   ./fx_briefing_loop.sh &
cd "$(dirname "$0")" || exit 1
mkdir -p logs

# 二重起動によるDiscord重複通知を防ぐ。異常終了で残ったロックはPIDが死んでいれば回収。
lock_dir="logs/fx_signal_board_loop.lock"
if [ -f "$lock_dir/pid" ]; then
  old_pid=$(<"$lock_dir/pid")
  if kill -0 "$old_pid" 2>/dev/null; then
    print -u2 "FXシグナルボードは既に起動しています (PID $old_pid)"
    exit 1
  fi
  rm -rf "$lock_dir"
fi
if ! mkdir "$lock_dir" 2>/dev/null; then
  print -u2 "FXシグナルボードの起動ロックを取得できません: $lock_dir"
  exit 1
fi
print $$ > "$lock_dir/pid"
trap 'rm -rf "$lock_dir"' EXIT INT TERM

while true; do
  .venv/bin/python fx_briefing.py \
    --signal-board \
    --no-price-write \
    --symbols GBPUSD EURUSD USDJPY \
    >> logs/fx_signal_board.log 2>&1 || true
  now=$(date +%s)
  next=$(( (now / 300 + 1) * 300 ))
  sleep $(( next - now ))
done
