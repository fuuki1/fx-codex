#!/bin/zsh
# Design A: one isolated five-minute shadow forecast cycle (no Discord).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
.venv/bin/python fx_briefing.py \
  --horizon-only \
  --no-discord \
  --no-llm \
  --no-export-events \
  --no-event-archive \
  </dev/null >> logs/fx_horizon.log 2>&1
