"""FX backtesting framework focused on verifiability and risk controls."""

from fx_backtester.engine import BacktestConfig, BacktestEngine, BacktestResult
from fx_backtester.execution import ExecutionConfig, SimulatedExecution
from fx_backtester.risk import RiskConfig, RiskManager

__version__ = "0.2.0"

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "ExecutionConfig",
    "RiskConfig",
    "RiskManager",
    "SimulatedExecution",
    "__version__",
]
