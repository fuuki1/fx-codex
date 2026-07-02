"""fx_backtester — 先読みなし・コスト考慮・OOS 検証つきの FX 分析エンジン。"""
from __future__ import annotations

from .costs import CostModel
from .runner import run_backtest, run_result

__all__ = ["CostModel", "run_backtest", "run_result"]
__version__ = "0.1.0"
