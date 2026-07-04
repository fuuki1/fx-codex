"""AI learning dashboard の時間足別採点(_evaluate_journal)のテスト。

dashboard は fx_intel 非依存の独立ツールなので、パス経由で import する。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

_SERVER_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_learning_dashboard" / "server.py"


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("dashboard_server", _SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)  # 月曜(オープン中)


def _row(ts, timeframe, horizon, direction, close, atr=0.10):
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "horizon_hours": horizon,
        "direction": direction,
        "conviction": 50,
        "close": close,
        "atr": atr,
    }


def test_scores_each_timeframe_at_its_horizon(server) -> None:
    entries = [
        # 1h long: 1時間後に上昇 → hit
        _row(START, "1h", 1.0, "long", 156.0),
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 156.4),
        # 15m long: 15分後に下落 → miss
        _row(START, "15m", 0.25, "long", 150.0, atr=0.05),
        _row(START + timedelta(minutes=15), "15m", 0.25, "long", 149.8, atr=0.05),
    ]
    result = server._evaluate_journal(entries)
    assert result["evaluated"] == 2
    assert result["hits"] == 1
    by_tf = result["by_timeframe"]
    assert by_tf["1h"] == {"evaluated": 1, "hits": 1, "flat": 0}
    assert by_tf["15m"] == {"evaluated": 1, "hits": 0, "flat": 0}


def test_legacy_rows_without_timeframe_use_24h(server) -> None:
    # timeframe 無し(融合1判断)は 24h ホライズンで採点、by_timeframe には出ない
    entries = [
        {
            "ts": START.isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "close": 156.0,
            "atr": 0.10,
        },
        {
            "ts": (START + timedelta(hours=24)).isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "close": 157.0,
            "atr": 0.10,
        },
    ]
    result = server._evaluate_journal(entries)
    assert result["evaluated"] == 1
    assert result["hits"] == 1
    assert result["by_timeframe"] == {}  # 旧行は時間足別内訳に入らない


def test_series_separated_by_timeframe(server) -> None:
    """同じ ts でも 15m と 1h は別系列で採点される(混ざらない)。"""
    entries = [
        _row(START, "15m", 0.25, "long", 150.0, atr=0.05),
        _row(START, "1h", 1.0, "long", 150.0),
        # 15m の15分後(上昇=hit)
        _row(START + timedelta(minutes=15), "15m", 0.25, "long", 150.5, atr=0.05),
        # 1h の1時間後(下落=miss)。15m系列と混ざらないこと
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 149.5),
    ]
    result = server._evaluate_journal(entries)
    assert result["by_timeframe"]["15m"]["hits"] == 1
    assert result["by_timeframe"]["1h"]["hits"] == 0


def test_recent_outcomes_include_timeframe(server) -> None:
    entries = [
        _row(START, "4h", 4.0, "long", 156.0, atr=0.3),
        _row(START + timedelta(hours=4), "4h", 4.0, "long", 157.0, atr=0.3),
    ]
    result = server._evaluate_journal(entries)
    assert result["recent_outcomes"][0]["timeframe"] == "4h"


def test_tolerance_scales_with_horizon(server) -> None:
    assert server._tolerance_for(0.25) < server._tolerance_for(24.0)
    assert server._tolerance_for(999.0) == 2.0
