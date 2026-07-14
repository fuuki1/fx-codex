#!/usr/bin/env bash
# Reproduce the HistData v2 relabel + validation.
# Requires network to histdata.com (token+cookie form flow) and the committed
# Dukascopy bid/ask datasets under data/real/dukascopy/ as reference.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"
PY="${PY:-python3}"

# 1) regenerate the three 1h CSVs (+ .meta.json sidecars) from fresh raw ZIPs
for PAIR in USDJPY EURUSD GBPUSD; do
  $PY scripts/fetch_histdata.py --pair "$PAIR" --year 2024 \
    --out "data/real/histdata/$(echo "$PAIR" | tr 'A-Z' 'a-z')_2024_1h.csv" --resample 1h
done

# 2) validate alignment: lag scan vs Dukascopy 2024 1h bars. Expected result:
#    lag 0 is the best alignment on every pair, with
#    p50 |close - bid_close| = 0.00 pips (bid basis) — see validation.json.
#    (HistData raw is a live download; a later re-fetch may differ slightly if
#    the provider revises history, so compare shapes, not byte hashes.)
$PY - <<'PYEOF'
import csv, json
from datetime import datetime, timedelta
from pathlib import Path
from data_platform.materialize.candle_bars import bars_from_csv_bytes

for pair, pip in (("usdjpy", 0.01), ("eurusd", 0.0001), ("gbpusd", 0.0001)):
    bars = {
        b.open_time: b
        for b in bars_from_csv_bytes(
            Path(f"data/real/dukascopy/{pair}_2019-2026_1h_bidask.csv.gz").read_bytes()
        )
        if b.open_time.year == 2024
    }
    closes = {}
    with open(f"data/real/histdata/{pair}_2024_1h.csv") as f:
        for row in csv.DictReader(f):
            closes[datetime.fromisoformat(row["timestamp"])] = float(row["close"])
    scan = {}
    for lag in (-1, 0, 1):
        diffs = sorted(
            abs(bars[t + timedelta(hours=lag)].bid_close - v) / pip
            for t, v in closes.items()
            if t + timedelta(hours=lag) in bars
        )
        scan[lag] = diffs[len(diffs) // 2]
    best = min(scan, key=scan.get)
    print(pair, "bid p50 by lag:", {k: round(v, 2) for k, v in scan.items()}, "best:", best)
    assert best == 0, f"{pair}: lag 0 must be the best alignment after the v2 fix"
PYEOF
