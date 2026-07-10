from __future__ import annotations

import pandas as pd

from fx_backtester.engine import BacktestConfig
from fx_backtester.execution import ExecutionConfig
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies.base import Strategy
from fx_backtester.stress import rerun_cost_stress


class AlwaysLong(Strategy):
    @property
    def name(self) -> str:
        return "always_long"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {"target_position": 1, "stop_distance": 0.02},
                index=data.index,
            ),
        )


def test_cost_stress_reruns_sizing_and_execution_for_each_multiplier() -> None:
    index = pd.date_range("2024-01-01", periods=12, freq="h")
    prices = [1.0 + offset * 0.002 for offset in range(len(index))]
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": prices,
                "high": [price + 0.003 for price in prices],
                "low": [price - 0.003 for price in prices],
                "close": prices,
                "spread_pips": 1.0,
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.01, risk_cap_pct=0.01, max_leverage=5),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 0.2},
            commission_per_million_usd=30.0,
        ),
    )

    report = rerun_cost_stress(data, AlwaysLong, config)

    assert report["cost_multiplier"].tolist() == [1.0, 1.5, 2.0, 3.0]
    assert report["method"].eq("full_engine_rerun").all()
    assert float(report.iloc[-1]["final_equity"]) < float(report.iloc[0]["final_equity"])
    assert report.iloc[-1]["execution_config"]["commission_per_million_usd"] == 90.0
    assert data["EURUSD"]["spread_pips"].eq(1.0).all()  # caller input stays immutable
