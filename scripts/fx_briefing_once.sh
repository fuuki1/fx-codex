#!/bin/zsh
# ブリーフィング2モード(融合+時間足別)を1回ずつ実行するワンショットスクリプト。
# launchd(com.fx-codex.briefing)から毎時:10に、run_exclusive.pyのロック下で呼ばれる。
# 片方が失敗しても他方は実行するが、最終終了コードでは失敗を隠さない。
# stdinは/dev/nullに固定: 2026-07-05にstdin待ちでハングした事故の再発防止。
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

# Bind both modes and every launchd retry to the same hourly :10 schedule.
# Unix epoch avoids platform-specific BSD/GNU date formatting differences; the
# Python CLI converts it to aware UTC and verifies five-minute alignment.
current_epoch=$(/bin/date -u +%s) || exit 1
run_slot=$(( ((current_epoch - 600) / 3600) * 3600 + 600 ))

overall_status=0
.venv/bin/python fx_briefing.py --require-freshness --run-slot "$run_slot" \
  </dev/null >> logs/fx_briefing.log 2>&1 || overall_status=1
.venv/bin/python fx_briefing.py --per-timeframe --require-freshness --run-slot "$run_slot" \
  </dev/null >> logs/fx_briefing_tf.log 2>&1 || overall_status=1
exit "$overall_status"
