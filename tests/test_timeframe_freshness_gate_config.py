"""時間足別定期通知が現行パイプラインだけを鮮度ゲートに使うことを検証する。"""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ops" / "freshness_targets_timeframe.json"
PLIST = ROOT / "ops" / "launchd" / "com.fx-codex.health.plist.tmpl"


def test_timeframe_freshness_config_contains_only_active_gating_targets() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    names = {row["name"] for row in payload["targets"]}

    assert names == {"tf_price_snapshot", "tf_journal"}
    assert "fusion_journal" not in names
    assert "promotion_state" not in names
    prices = next(row for row in payload["targets"] if row["name"] == "tf_price_snapshot")
    assert prices["required_symbols"] == ["GBPUSD", "EURUSD", "USDJPY"]
    assert prices["required_timeframes"] == ["15m", "1h", "4h", "1d"]


def test_health_launchd_selects_timeframe_freshness_config() -> None:
    rendered = (
        PLIST.read_text(encoding="utf-8")
        .replace("__FX_ROOT__", "/Users/example/srv/fx-codex")
        .replace("__PYTHON__", "/Users/example/srv/fx-codex/.venv/bin/python")
    )
    root = ET.fromstring(rendered)
    arguments = [node.text or "" for node in root.iter("string")]

    config_index = arguments.index("--config")
    assert arguments[config_index + 1] == "ops/freshness_targets_timeframe.json"
