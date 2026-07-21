"""定期Discord通知が統合ブリーフィング経路を使うことを検証する。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fx_briefing_once.sh"


def test_scheduled_briefing_uses_integrated_timeframe_payload() -> None:
    script = WRAPPER.read_text(encoding="utf-8")
    per_timeframe_command = script.split("per_timeframe_status=0", 1)[1].split(
        "# Discord通知失敗", 1
    )[0]
    fusion_command = script.split('case "$schedule_status" in', 1)[1]

    assert "--per-timeframe" in per_timeframe_command
    assert "--signal-board" not in script
    assert "--require-freshness" in per_timeframe_command
    assert "--no-price-write" in per_timeframe_command
    assert "--symbols USDJPY EURUSD" in per_timeframe_command
    assert "GBPUSD" not in per_timeframe_command
    assert "fx_integrated_briefing.log" in script
    assert "tools/fusion_capture_schedule.py" in script
    assert "--minimum-interval-minutes 55" in script
    assert "--no-discord" in fusion_command
    assert "fx_fusion_capture.log" in script
    assert "--symbols USDJPY EURUSD GBPUSD" in fusion_command
