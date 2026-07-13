#!/usr/bin/env bash
# Reproduce the real CFTC COT PIT evidence bundle from scratch.
#
# This fetches LIVE public CFTC Legacy Futures-Only COT data (no credentials, no
# order flow) through the repo's own research-only adapter, materializes a
# point-in-time dataset, and re-runs the audit + as-of proofs.
#
# It writes into a scratch directory OUTSIDE the repo (the raw capture is ~82MB
# and must never be committed). Only the small manifest / attestation / transcript
# are copied back into this bundle.
#
# Usage:  reports/evidence/cot-cftc-real-pit-20260713/reproduce.sh [WORKDIR]
#
# WHY the timestamps below: CFTC report date 2026-07-07 (Tuesday data) is released
# the following Friday (2026-07-11 ~20:30 UTC / 15:30 ET). The attestation records
# that release; the "available_time" a downstream model may use is max(release,
# capture-completion) — i.e. when we actually first held the bytes, never the
# report date. Adjust --report-date / --released-at for the latest report when you
# re-run this later.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

WORK="${1:-$(mktemp -d)}"
echo "workdir: $WORK"
mkdir -p "$WORK/capture" "$WORK/dataset"

REPORT_DATE="2026-07-07"
RELEASED_AT="2026-07-11T20:30:00+00:00"
EVIDENCE_URI="https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm"

echo "== 1/5 capture (LIVE CFTC fetch, count-bounded pagination) =="
CAP_JSON=$(python3 -m tools.cot_pit_pipeline capture \
  --capture-root "$WORK/capture" --writer-id "reproduce:cot-real")
echo "$CAP_JSON"
CAP=$(python3 -c "import json,sys;print(json.loads('''$CAP_JSON''')['capture_path'])")

echo "== 2/5 attest (bind the official release-schedule page as local evidence) =="
python3 -c "
import urllib.request
req=urllib.request.Request('$EVIDENCE_URI', headers={'User-Agent':'fx-codex-cot-pit/2.0 (+https://github.com/fuuki1)'})
open('$WORK/release_evidence.html','wb').write(urllib.request.urlopen(req, timeout=30).read())
"
python3 -m tools.cot_pit_pipeline attest \
  --output "$WORK/attestation.json" --evidence "$WORK/release_evidence.html" \
  --report-date "$REPORT_DATE" --basis actual_release_notice \
  --released-at "$RELEASED_AT" --evidence-uri "$EVIDENCE_URI" \
  --writer-id "reproduce:cot-real"

echo "== 3/5 materialize point-in-time dataset =="
python3 -m tools.cot_pit_pipeline materialize \
  --root "$WORK/dataset" --capture "$CAP" \
  --release "$WORK/attestation.json" "$WORK/release_evidence.html"
DS=$(ls -d "$WORK/dataset"/*/ | head -1)

echo "== 4/5 audit (deterministic replay from raw) =="
python3 -m tools.cot_pit_pipeline audit "$DS"

echo "== 5/5 PIT gate proof (before capture = unavailable, after = usable) =="
echo "-- prediction BEFORE the capture instant --"
python3 -m tools.cot_pit_pipeline as-of "$DS" \
  --prediction-time 2026-07-13T12:00:00+00:00 --required-currencies JPY EUR GBP || true
echo "-- prediction AFTER capture + release --"
python3 -m tools.cot_pit_pipeline as-of "$DS" \
  --prediction-time 2026-07-14T00:00:00+00:00 \
  --required-currencies JPY EUR GBP AUD CAD CHF NZD USD

echo
echo "Done. Compare the audit dataset_id and net positions against pit_dataset_manifest.json"
echo "and run_transcript.txt in this bundle. The raw ~82MB capture stays in: $WORK/capture"
