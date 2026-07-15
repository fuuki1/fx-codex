"""定期Discord通知が統合ブリーフィング経路を使うことを検証する。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fx_briefing_once.sh"


def test_scheduled_briefing_uses_integrated_timeframe_payload() -> None:
    script = WRAPPER.read_text(encoding="utf-8")

    assert "--per-timeframe" in script
    assert "--signal-board" not in script
    assert "--require-freshness" in script
    assert "--no-price-write" in script
    assert "--symbols USDJPY EURUSD" in script
    assert "fx_integrated_briefing.log" in script
