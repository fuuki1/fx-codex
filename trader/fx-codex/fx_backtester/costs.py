"""取引コストのモデル化（スプレッド + スリッページ）。

pips を価格に変換してコストを算出する。JPY クロスは pip=0.01、それ以外は 0.0001。
mission-critical な分析ではコストを過小評価しないことが重要なので、ポジション変更
（エントリ/エグジット）ごとに「スプレッド + スリッページ」を価格距離として課す。
"""
from __future__ import annotations

from dataclasses import dataclass


def pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


@dataclass(frozen=True)
class CostModel:
    symbol: str
    spread_pips: float = 0.0
    slippage_pips: float = 0.0

    def cost_price(self) -> float:
        """ポジションを 1 単位変更するごとに課す価格コスト。"""
        return (self.spread_pips + self.slippage_pips) * pip_size(self.symbol)

    @classmethod
    def from_maps(
        cls,
        symbol: str,
        spread_map: dict[str, float] | None = None,
        slippage_map: dict[str, float] | None = None,
    ) -> CostModel:
        spread_map = spread_map or {}
        slippage_map = slippage_map or {}
        sym = symbol.upper()
        return cls(
            symbol=sym,
            spread_pips=float(spread_map.get(sym, 0.0)),
            slippage_pips=float(slippage_map.get(sym, 0.0)),
        )
