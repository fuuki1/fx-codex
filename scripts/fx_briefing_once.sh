#!/bin/zsh
# FXシグナルボードを1回だけ生成し、Discordへ単一通知を送る。
# launchd(com.fx-codex.briefing)から5分ごとにrun_exclusive.pyのロック下で呼ばれる。
# 価格系列はcom.fx-codex.snapshotが管理するため、ここでは価格を書き込まない。
# stdinは/dev/nullに固定し、対話待ちによる常駐停止を防ぐ。
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX briefing Pythonが実行できません: $PYTHON"
  exit 1
fi

exec "$PYTHON" fx_briefing.py \
  --signal-board \
  --no-price-write \
  --require-freshness \
  --symbols GBPUSD EURUSD USDJPY \
  </dev/null >> logs/fx_signal_board.log 2>&1
