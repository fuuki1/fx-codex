#!/bin/zsh
# 時間足別統合通知と、必要時だけ融合判断を生成する。
# launchd(com.fx-codex.briefing)から5分ごとにrun_exclusive.pyのロック下で呼ばれる。
# 価格系列はcom.fx-codex.snapshotが管理するため、ここでは価格を書き込まない。
# 融合判断は最大1時間ごとで、Discordへ重複通知しない。
# stdinは/dev/nullに固定し、対話待ちによる常駐停止を防ぐ。
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX briefing Pythonが実行できません: $PYTHON"
  exit 1
fi

per_timeframe_status=0

"$PYTHON" fx_briefing.py \
  --per-timeframe \
  --no-price-write \
  --require-freshness \
  --symbols USDJPY EURUSD \
  </dev/null >> logs/fx_integrated_briefing.log 2>&1 || per_timeframe_status=$?

# Discord通知失敗(5)は判断ジャーナルの保存後にのみ返るため、融合取得とは分離する。
# それ以外の先行失敗では、部分更新を広げないよう融合writerを開始しない。
if [ "$per_timeframe_status" -ne 0 ] && [ "$per_timeframe_status" -ne 5 ]; then
  print -u2 "per-timeframe briefing failed; fusion capture skipped: $per_timeframe_status"
  exit "$per_timeframe_status"
fi

overall_status=$per_timeframe_status

"$PYTHON" tools/fusion_capture_schedule.py \
  --journal logs/briefing_journal.jsonl \
  --minimum-interval-minutes 15 \
  </dev/null >> logs/fx_fusion_capture.log 2>&1
schedule_status=$?

case "$schedule_status" in
  0)
    fusion_status=0
    "$PYTHON" fx_briefing.py \
      --no-discord \
      --no-price-write \
      --require-freshness \
      --symbols USDJPY EURUSD GBPUSD \
      </dev/null >> logs/fx_fusion_capture.log 2>&1 || fusion_status=$?
    if [ "$fusion_status" -ne 0 ]; then
      overall_status=$fusion_status
    fi
    ;;
  3)
    ;;
  *)
    print -u2 "fusion capture schedule check failed: $schedule_status"
    overall_status=$schedule_status
    ;;
esac

exit "$overall_status"
