#!/bin/zsh
# Daily complete-audit scoring. It never overwrites operational decision feedback.
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  print -u2 "FX decision outcome audit Pythonが実行できません: $PYTHON"
  exit 1
fi

before=0
[ -f logs/decision_expectancy_monitor.json ] && \
  before=$(stat -f %m logs/decision_expectancy_monitor.json 2>/dev/null || echo 0)
result_code=0
"$PYTHON" tools/decision_expectancy_monitor.py \
  --feedback-json none \
  --stage-timing-log "$PWD/logs/decision_pipeline_stage_timings.jsonl" \
  --quiet </dev/null >> logs/fx_monitors.log 2>&1 || result_code=$?
if [ "$result_code" -gt 1 ]; then
  print -u2 "decision outcome audit failed: exit=$result_code"
  exit "$result_code"
fi
after=0
[ -f logs/decision_expectancy_monitor.json ] && \
  after=$(stat -f %m logs/decision_expectancy_monitor.json 2>/dev/null || echo 0)
if [ "$after" -le "$before" ]; then
  print -u2 "decision outcome audit did not refresh decision_expectancy_monitor.json"
  exit 2
fi
# exit=1 can mean valid negative-expectancy findings, so inspect data integrity
# separately instead of treating every research warning as a crashed service.
if ! "$PYTHON" - <<'PYEOF'
import json
from pathlib import Path

payload = json.loads(Path("logs/decision_expectancy_monitor.json").read_text(encoding="utf-8"))
runner = payload.get("runner", {})
summary = payload.get("summary", {})
price_health = summary.get("price_health", {})
scored = summary.get("scored_outcomes")
snapshots = runner.get("decision_source_snapshots")
snapshot_evidence_ok = (
    isinstance(snapshots, list)
    and bool(snapshots)
    and all(
        isinstance(row, dict)
        and bool(row.get("source_path"))
        and isinstance(row.get("read_offset_bytes"), int)
        and not isinstance(row.get("read_offset_bytes"), bool)
        and row["read_offset_bytes"] >= 0
        and isinstance(row.get("source_sha256"), str)
        and len(row["source_sha256"]) == 64
        for row in snapshots
    )
)
if (
    runner.get("scope") != "complete_audit"
    or price_health.get("status") != "pass"
    or not isinstance(scored, int)
    or isinstance(scored, bool)
    or scored <= 0
    or not snapshot_evidence_ok
):
    raise SystemExit(1)
PYEOF
then
  print -u2 "decision outcome audit published invalid PIT evidence"
  exit 2
fi
exit 0
