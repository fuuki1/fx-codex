#!/bin/zsh
# ブリーフィング2モード(融合+時間足別)を1回ずつ実行するワンショットスクリプト。
# launchd(com.fx-codex.briefing)から毎時:10に、run_exclusive.pyのロック下で呼ばれる。
# 片方の失敗が他方を止めないよう `|| true` で分離する(旧fx_briefing_loop.shと同じ契約)。
# stdinは/dev/nullに固定: 2026-07-05にstdin待ちでハングした事故の再発防止。
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

.venv/bin/python fx_briefing.py </dev/null >> logs/fx_briefing.log 2>&1 || true
.venv/bin/python fx_briefing.py --per-timeframe </dev/null >> logs/fx_briefing_tf.log 2>&1 || true
