from fx_backtester.strategies.base import Strategy
from fx_backtester.strategies.ai_logistic import AILogisticStrategy
from fx_backtester.strategies.donchian_breakout import DonchianBreakout
from fx_backtester.strategies.moving_average_cross import MovingAverageCross
from fx_backtester.strategies.rsi_mean_reversion import RSIMeanReversion

STRATEGY_REGISTRY = {
    "ai_logistic": AILogisticStrategy,
    "ma_cross": MovingAverageCross,
    "donchian": DonchianBreakout,
    "rsi_mean_reversion": RSIMeanReversion,
}

DEFAULT_PARAM_GRIDS: dict[str, dict[str, list[object]]] = {
    "ai_logistic": {
        "min_train_bars": [200, 300],
        "long_threshold": [0.54, 0.58],
        "short_threshold": [0.46, 0.42],
        "stop_atr_multiple": [1.5],
        "epochs": [120],
    },
    "ma_cross": {
        "fast_window": [10, 20],
        "slow_window": [40, 60],
        "stop_atr_multiple": [1.5, 2.0],
    },
    "donchian": {
        "entry_window": [20, 40],
        "exit_window": [10, 20],
        "stop_atr_multiple": [1.5, 2.0],
    },
    "rsi_mean_reversion": {
        "rsi_window": [14],
        "low_threshold": [25, 30],
        "high_threshold": [70, 75],
        "stop_atr_multiple": [1.0, 1.5],
    },
}

__all__ = [
    "AILogisticStrategy",
    "DEFAULT_PARAM_GRIDS",
    "DonchianBreakout",
    "MovingAverageCross",
    "RSIMeanReversion",
    "STRATEGY_REGISTRY",
    "Strategy",
]
