"""期待値最大化プロファイルのテスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

from fx_intel import maximization
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.trade_outcome import TradeOutcome

NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


def _outcome(
    r_multiple: float,
    *,
    symbol: str = "USDJPY",
    timeframe_hours: float = 1.0,
    direction: str = "long",
    conviction: int = 70,
    quality: float = 0.75,
    index: int = 0,
) -> TradeOutcome:
    prediction = NOW + timedelta(hours=index * (timeframe_hours + 1))
    realized_net_r = r_multiple - 0.2
    return TradeOutcome(
        symbol=symbol,
        direction=direction,
        ts=prediction.isoformat(),
        horizon_hours=timeframe_hours,
        conviction=conviction,
        data_quality=0.9,
        timeframe="1h",
        decision_id=f"decision-{index}",
        entry=100.0,
        stop=99.0 if direction == "long" else 101.0,
        target1=101.0 if direction == "long" else 99.0,
        target2=102.0 if direction == "long" else 98.0,
        atr=1.0,
        risk_distance=1.0,
        terminal_price=100.0 + r_multiple,
        terminal_r=r_multiple,
        mfe_r=max(r_multiple, 0.2),
        mae_r=max(-r_multiple, 0.2),
        tp1_hit=r_multiple >= 1.0,
        tp2_hit=r_multiple >= 2.0,
        sl_hit=r_multiple <= -1.0,
        first_touch="tp1" if r_multiple > 0 else "sl",
        realized_r=r_multiple,
        realized_net_r=realized_net_r,
        net_label_eligible=True,
        label_version=NET_LABEL_VERSION,
        label_provenance=NET_LABEL_PROVENANCE,
        entry_bid=99.9,
        entry_ask=100.1,
        quote_realized_r=realized_net_r,
        entry_spread_r=0.2,
        slippage_r=0.0,
        commission_r=0.0,
        financing_r=0.0,
        additional_cost_r=0.0,
        execution_cost_r=0.2,
        cost_model_id=DEFAULT_COST_MODEL_ID,
        cost_model_version=DEFAULT_COST_MODEL_VERSION,
        cost_status=DEFAULT_COST_STATUS,
        entry_quote_source="fixture",
        spread_source="fixture",
        slippage_model_id=DEFAULT_SLIPPAGE_MODEL_ID,
        commission_model_id=DEFAULT_COMMISSION_MODEL_ID,
        cost_quality_flags=(),
        path_points=6,
        path_start=(prediction + timedelta(minutes=10)).isoformat(),
        path_end=(prediction + timedelta(hours=timeframe_hours)).isoformat(),
        path_quality=quality,
        quality_flags=("close_only_path",),
    )


def _decision(ts: datetime, direction: str = "long") -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": direction,
        "conviction": 70,
        "close": 100.0,
        "atr": 1.0,
        "stop": 99.0 if direction == "long" else 101.0,
        "target1": 101.0 if direction == "long" else 99.0,
        "target2": 102.0 if direction == "long" else 98.0,
        "data_quality": 1.0,
    }


def _price(ts: datetime, close: float) -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "neutral",
        "conviction": 0,
        "close": close,
        "atr": 1.0,
        "data_quality": 1.0,
    }


def test_negative_expectancy_cell_blocks() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(30)]

    cell = maximization.derive_maximization_cell("USDJPY", "1h", "long", outcomes)

    assert cell.action == "avoid"
    assert cell.block is True
    assert cell.factor == maximization.BLOCK_FACTOR
    assert cell.expectancy_r == -1.2
    assert cell.score < 0


def test_strong_positive_cell_boosts_when_samples_are_mature() -> None:
    outcomes = [_outcome(1.0, conviction=100, index=index) for index in range(100)]

    cell = maximization.derive_maximization_cell("USDJPY", "1h", "long", outcomes)

    assert cell.action == "boost"
    assert cell.block is False
    assert 1.0 < cell.factor <= maximization.BOOST_FACTOR_MAX
    assert cell.score > 0.25
    assert cell.max_drawdown_r == 0
    assert cell.recovery_factor == float("inf")
    assert cell.brier_skill == 1.0
    assert cell.calibration_error == 0.0


def test_positive_but_unstable_cell_dampens() -> None:
    outcomes = [_outcome(2.0, conviction=50, index=index) for index in range(30)] + [
        _outcome(-1.0, conviction=50, index=30 + index) for index in range(30)
    ]

    cell = maximization.derive_maximization_cell("USDJPY", "1h", "long", outcomes)

    assert cell.expectancy_r == 0.3
    assert cell.action == "dampen"
    assert cell.block is False
    assert cell.factor == maximization.DAMPEN_FACTOR
    assert cell.stability_score == 0.0
    assert cell.max_drawdown_r == 36.0
    assert cell.sortino_r is not None and cell.sortino_r > 0


def test_timeframe_maximization_lookup_uses_symbol_timeframe_direction_cell() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(30)]
    cell = maximization.derive_maximization_cell("USDJPY", "1h", "long", outcomes)
    profile = maximization.TimeframeMaximization(
        generated_at=NOW.isoformat(),
        cells={("USDJPY", "1h", "long"): cell},
    )

    adjuster = profile.expectancy_lookup("USDJPY", "1h")
    assert adjuster is not None
    factor, reason, block = adjuster("USDJPY", "long", 70)

    assert factor == maximization.BLOCK_FACTOR
    assert block is True
    assert "最大化" in reason


def test_derive_timeframe_maximization_from_journal_entries_and_save(tmp_path) -> None:
    rows = []
    start = NOW
    for i in range(30):
        ts = start + timedelta(hours=i * 3)
        rows.append(_decision(ts))
        rows.append(_price(ts + timedelta(hours=1), 99.0))

    profile = maximization.derive_timeframe_maximization(rows, now=NOW)
    path = tmp_path / "maximization.json"
    maximization.save_timeframe_maximization(profile, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    cell = payload["cells"]["USDJPY|1h|long"]
    assert cell["action"] == "collect_samples"
    assert cell["block"] is False
    assert cell["expectancy_r"] is None
    assert "max_drawdown_r" in cell
    assert "sortino_r" in cell
    assert "calibration_error" in cell
