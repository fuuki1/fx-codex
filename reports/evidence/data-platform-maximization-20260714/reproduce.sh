#!/usr/bin/env bash
# Reproduce the data-platform maximization evidence from public sources.
# Requires network to: datafeed.dukascopy.com, candledata.fxcorporate.com,
# webrates.truefx.com, alfred.stlouisfed.org. No credentials of any kind.
#
# Raw mirror + collector logs are NOT committed (logs/ is git-ignored); this
# script rebuilds them from the providers. Historical datasets are
# deterministic: given the same mirrored bytes, every dataset_sha256 in
# data/real/*/dataset_registry.jsonl reproduces exactly (replay_report.json).
# The TrueFX live capture is a live stream — a re-run collects a DIFFERENT
# window by definition; its verification is raw-hash + replay over whatever
# was captured, not byte-equality with this bundle.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"
PY="${PY:-python3}"

# 1) mirror the public candle archives (idempotent, manifest-verified)
$PY scripts/fetch_public_candles.py fxcm-h1 \
  --pairs USDJPY EURUSD GBPUSD --start 2021-01 --end 2025-12 --workers 8
$PY scripts/fetch_public_candles.py dukascopy-h1 \
  --pairs USDJPY EURUSD GBPUSD --start 2019-01 --end 2026-06 --workers 6
$PY scripts/fetch_public_candles.py dukascopy-m1 \
  --pairs USDJPY EURUSD GBPUSD --start 2024-01 --end 2024-12 --workers 6

# 2) build every dataset (raw-first; hashes must match the committed registry)
$PY scripts/build_candle_datasets.py \
  --stats-out logs/data_platform/candle_dataset_stats.json

# 3) collect a live TrueFX window through the production daemon (no creds)
$PY tools/fx_quote_collector.py --source truefx \
  --output-root logs/data_platform/collect/truefx --max-duration-minutes 60

# 4) daily continuous-operation report for today
$PY -m tools.data_platform_daily_report \
  --live-root logs/data_platform/collect/truefx \
  --mirror-root logs/data_platform/mirror \
  --ops-dir logs/data_platform/ops

# 5) assemble the evidence bundle (fresh ALFRED capture + full replay rebuild)
$PY scripts/assemble_data_platform_evidence.py \
  --bundle-dir reports/evidence/data-platform-maximization-20260714

# 6) machine-judged scorecard
$PY -m tools.data_platform_scorecard \
  --evidence-dir reports/evidence/data-platform-maximization-20260714 \
  --operations-dir logs/data_platform/ops
