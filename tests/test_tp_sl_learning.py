"""TP/SL先着学習MVPのテスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_intel import tp_sl_learning

NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


def _decision(
    ts: datetime,
    *,
    symbol: str = "USDJPY",
    timeframe: str = "1h",
    direction: str = "long",
    conviction: int = 70,
) -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "horizon_hours": 1.0,
        "direction": direction,
        "conviction": conviction,
        "composite": 0.7 if direction == "long" else -0.7,
        "tech_score": 0.7 if direction == "long" else -0.7,
        "news_score": 0.1,
        "close": 100.0,
        "atr": 1.0,
        "stop": 99.0 if direction == "long" else 101.0,
        "target1": 101.0 if direction == "long" else 99.0,
        "target2": 102.0 if direction == "long" else 98.0,
        "data_quality": 1.0,
        "features": {},
        "components": [],
    }


def _price(ts: datetime, close: float, *, symbol: str = "USDJPY", timeframe: str = "1h") -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": "neutral",
        "conviction": 0,
        "close": close,
        "atr": 1.0,
        "data_quality": 1.0,
    }


def test_evaluate_timeframe_tp_sl_calls_uses_first_touch() -> None:
    rows = [
        _decision(NOW, direction="long"),
        _price(NOW + timedelta(minutes=20), 100.2),
        _price(NOW + timedelta(minutes=40), 101.1),
        _price(NOW + timedelta(hours=1), 101.3),
        _decision(NOW + timedelta(hours=3), symbol="EURUSD", direction="short"),
        _price(NOW + timedelta(hours=3, minutes=20), 100.3, symbol="EURUSD"),
        _price(NOW + timedelta(hours=3, minutes=40), 101.1, symbol="EURUSD"),
        _price(NOW + timedelta(hours=4), 101.2, symbol="EURUSD"),
    ]

    calls = tp_sl_learning.evaluate_timeframe_tp_sl_calls(rows, "1h")

    outcomes = {(call.symbol, call.direction): call.outcome for call in calls}
    assert outcomes == {("USDJPY", "long"): "hit", ("EURUSD", "short"): "miss"}


def test_derive_tp_sl_profile_builds_non_blocking_confidence_adjustment() -> None:
    calls = [
        tp_sl_learning.TpSlCall(
            symbol="USDJPY",
            timeframe="1h",
            direction="long",
            conviction=80,
            outcome="miss",
            ts=(NOW + timedelta(hours=i)).isoformat(),
            first_touch="sl",
            path_quality=0.7,
        )
        for i in range(100)
    ]

    profile = tp_sl_learning.derive_tp_sl_profile(calls, now=NOW)
    factor, reason = profile.adjustment("long")

    assert profile.evaluated == 100
    assert profile.hits == 0
    assert factor == tp_sl_learning.FACTOR_MIN
    assert "TP/SL先着" in reason
    assert profile.adjusted_brier < profile.brier


def test_timeframe_learning_lookup_never_blocks() -> None:
    calls = [
        tp_sl_learning.TpSlCall(
            symbol="USDJPY",
            timeframe="1h",
            direction="long",
            conviction=80,
            outcome="miss",
            ts=(NOW + timedelta(hours=i)).isoformat(),
            first_touch="sl",
            path_quality=0.7,
        )
        for i in range(100)
    ]
    profile = tp_sl_learning.derive_tp_sl_profile(calls, now=NOW)
    learning = tp_sl_learning.TimeframeTpSlLearning(
        generated_at=NOW.isoformat(),
        profiles={("USDJPY", "1h"): profile},
        per_timeframe={"1h": profile},
    )

    adjuster = learning.expectancy_lookup("USDJPY", "1h")
    assert adjuster is not None
    factor, reason, block = adjuster("USDJPY", "long", 80)

    assert factor == tp_sl_learning.FACTOR_MIN
    assert reason
    assert block is False


def test_save_timeframe_tp_sl_learning_writes_json(tmp_path) -> None:
    calls = [
        tp_sl_learning.TpSlCall(
            symbol="USDJPY",
            timeframe="1h",
            direction="long",
            conviction=60,
            outcome="hit" if i < 60 else "miss",
            ts=(NOW + timedelta(hours=i)).isoformat(),
            first_touch="tp1" if i < 60 else "sl",
            path_quality=0.7,
        )
        for i in range(100)
    ]
    profile = tp_sl_learning.derive_tp_sl_profile(calls, now=NOW)
    learning = tp_sl_learning.TimeframeTpSlLearning(
        generated_at=NOW.isoformat(),
        profiles={("USDJPY", "1h"): profile},
        per_timeframe={"1h": profile},
    )
    path = tmp_path / "tp_sl_learning.json"

    tp_sl_learning.save_timeframe_tp_sl_learning(learning, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["profiles"]["USDJPY|1h"]["evaluated"] == 100
    assert payload["per_timeframe"]["1h"]["hit_rate"] == pytest.approx(0.6)
