#!/bin/zsh
# canonical仮想口座へ渡す時間足別統合判断だけを生成する。
# launchd(com.fx-codex.briefing)から5分ごとにrun_exclusive.pyのロック下で呼ばれる。
# 価格系列はcom.fx-codex.snapshotが管理するため、ここでは価格を書き込まない。
# 融合判断は観測専用かつ全履歴処理を含むため、5分critical pathでは実行しない。
# stdinは/dev/nullに固定し、対話待ちによる常駐停止を防ぐ。
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX briefing Pythonが実行できません: $PYTHON"
  exit 1
fi

"$PYTHON" fx_briefing.py \
  --per-timeframe \
  --no-price-write \
  --technical-cache-only \
  --require-freshness \
  --cached-timeframe-learning \
  --no-auxiliary-horizon-reports \
  --no-approved-tp-sl-policy \
  --defer-decision-scoring \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" \
  --symbols USDJPY EURUSD \
  </dev/null >> logs/fx_integrated_briefing.log 2>&1
