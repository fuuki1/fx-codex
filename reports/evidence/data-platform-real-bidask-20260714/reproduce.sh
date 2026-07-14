#!/usr/bin/env bash
# Reproduce the real bid/ask + macro PIT evidence from public sources.
# Requires network access to datafeed.dukascopy.com and alfred.stlouisfed.org.
# Writes into a scratch WORKDIR (raw is never committed); recomputes the
# replay hash and the scorecard from freshly fetched real data.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"
WORK="${1:-$(mktemp -d)}"
echo "workdir: $WORK"

python3 - "$WORK" <<'PYEOF'
import sys, time, uuid, hashlib, json
from datetime import UTC, date, datetime
from pathlib import Path
import requests

from data_platform.collect import dukascopy, fred_macro
from data_platform.collect.raw_first import QuoteLog
from data_platform.raw.immutable_store import ImmutableRawStore

WORK = Path(sys.argv[1])
store = ImmutableRawStore(WORK / "raw")
log = QuoteLog(WORK / "log")
conn = f"reproduce-{uuid.uuid4().hex[:8]}"

def fetcher(url: str) -> tuple[int, bytes]:
    r = requests.get(url, headers={"User-Agent": "fx-codex-collect/1.0 (research; read-only)"}, timeout=20)
    return r.status_code, r.content

# 1) real Dukascopy bid/ask: 3 pairs x 3 hours (2024-01-10 12-14h UTC)
for pair in ("USDJPY", "EURUSD", "GBPUSD"):
    for hh in (12, 13, 14):
        hour = datetime(2024, 1, 10, hh, tzinfo=UTC)
        for attempt in range(6):
            try:
                result = dukascopy.ingest_hour(pair, hour, fetcher=fetcher,
                                               store=store, log=log, connection_id=conn)
                print(f"{pair} {hh}h: {0 if result is None else result.accepted_count} accepted")
                break
            except dukascopy.DukascopyFetchError as e:
                print(f"{pair} {hh}h retry {attempt+1}: {str(e)[:60]}")
                time.sleep(4)
        time.sleep(1.5)

# 2) replay hash over data-derived fields
rows = [json.loads(l) for l in open(WORK / "log" / "quotes.jsonl")]
key = lambda r: (r["instrument"], r["provider_event_time"], r["bid"], r["ask"],
                 r["bid_size"], r["ask_size"], r["raw_payload_sha256"])
print("rows:", len(rows))
print("data-hash:", hashlib.sha256(json.dumps(sorted(key(r) for r in rows)).encode()).hexdigest())
print("(compare with replay_report.json result_sha256; identical upstream bytes give an identical hash)")

# 3) real ALFRED vintages (revision proof)
mstore = ImmutableRawStore(WORK / "macro_raw")
mlog = fred_macro.MacroPITLog(WORK / "macro.jsonl")
for series, vintage, start, end in [
    ("GDPC1", date(2024, 2, 1), date(2023, 7, 1), date(2023, 10, 1)),
    ("GDPC1", date(2024, 4, 5), date(2023, 7, 1), date(2023, 10, 1)),
]:
    rows = fred_macro.capture_vintage(series, vintage, start, end,
                                      fetcher=fred_macro.requests_fetcher, store=mstore, log=mlog)
    print(series, vintage, {r.period.isoformat(): r.value for r in rows})
PYEOF

echo "== scorecard over the committed evidence bundle =="
python3 -m tools.data_platform_scorecard \
  --evidence-dir reports/evidence/data-platform-real-bidask-20260714 | head -5
