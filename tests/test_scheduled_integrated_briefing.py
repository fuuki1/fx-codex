"""定期Discord通知が統合ブリーフィング経路を使うことを検証する。"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fx_briefing_once.sh"


def test_scheduled_briefing_uses_integrated_timeframe_payload() -> None:
    script = WRAPPER.read_text(encoding="utf-8")
    per_timeframe_command = script

    assert "--per-timeframe" in per_timeframe_command
    assert "--signal-board" not in script
    assert "--require-freshness" in per_timeframe_command
    assert "--no-price-write" in per_timeframe_command
    assert re.search(r"--symbols\s+USDJPY\s+EURUSD", per_timeframe_command)
    assert "GBPUSD" not in per_timeframe_command
    assert "fx_integrated_briefing.log" in script
    assert "tools/fusion_capture_schedule.py" not in script
    assert "--defer-decision-scoring" in per_timeframe_command
    assert "--decision-latest-output" not in per_timeframe_command
    assert "fx_fusion_capture.log" not in script
