"""tools/learning_capture.py のテスト。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "learning_capture.py"


def _module():
    spec = importlib.util.spec_from_file_location("learning_capture", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_steps_use_no_discord_for_briefing_runs() -> None:
    module = _module()
    args = module.build_parser().parse_args(["--symbols", "USDJPY", "EURUSD"])

    steps = module.build_steps(args)
    commands = {step.name: step.command for step in steps}

    assert "timeframe-price-snapshot" in commands
    assert "--no-discord" in commands["fusion-briefing-capture"]
    assert "--no-discord" in commands["timeframe-briefing-capture"]
    assert "--per-timeframe" in commands["timeframe-briefing-capture"]
    assert "--no-llm" in commands["fusion-briefing-capture"]
    assert "--symbols" in commands["timeframe-price-snapshot"]
    monitor = next(step for step in steps if step.name == "trade-outcome-monitor")
    assert monitor.allowed_exit_codes == (0, 1)
    decision_monitor = next(step for step in steps if step.name == "decision-expectancy-monitor")
    assert decision_monitor.allowed_exit_codes == (0, 1)


def test_build_steps_can_skip_network_heavy_briefing_parts() -> None:
    module = _module()
    args = module.build_parser().parse_args(
        ["--skip-fusion", "--skip-timeframe", "--skip-snapshot"]
    )

    steps = module.build_steps(args)

    assert [step.name for step in steps] == [
        "trade-outcome-monitor",
        "decision-expectancy-monitor",
    ]
