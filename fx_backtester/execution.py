from __future__ import annotations

from dataclasses import dataclass, field

from fx_backtester.models import instrument_for, notional_usd, price_distance_to_usd_per_unit


@dataclass(frozen=True)
class Fill:
    expected_price: float
    price: float
    fee: float
    side: int
    spread_pips: float
    slippage_pips: float
    order_type: str


@dataclass
class ExecutionConfig:
    spread_pips: dict[str, float] = field(
        default_factory=lambda: {"USDJPY": 0.8, "EURUSD": 0.6, "GBPUSD": 0.9}
    )
    slippage_pips: dict[str, float] = field(
        default_factory=lambda: {"USDJPY": 0.2, "EURUSD": 0.1, "GBPUSD": 0.15}
    )
    commission_per_million_usd: float = 30.0
    fixed_fee_usd: float = 0.0
    minimum_fee_usd: float = 0.0
    spread_time_multipliers: dict[int, float] = field(default_factory=lambda: {21: 2.0, 22: 1.5})
    slippage_time_multipliers: dict[int, float] = field(default_factory=lambda: {21: 2.0, 22: 1.5})


class SimulatedExecution:
    """Execution layer: apply spread, slippage and commission to market orders."""

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()

    def spread_pips(self, symbol: str, bar: object | None = None) -> float:
        inst = instrument_for(symbol)
        if bar is not None:
            spread = self._bar_float(bar, "spread_pips")
            if spread is not None:
                if spread <= 0:
                    raise ValueError(f"{symbol} spread_pips must be positive")
                return spread

            spread_price = self._bar_float(bar, "spread_price")
            if spread_price is not None:
                if spread_price <= 0:
                    raise ValueError(f"{symbol} spread_price must be positive")
                return spread_price / inst.pip_size

            legacy_spread = self._bar_float(bar, "spread")
            if legacy_spread is not None:
                if legacy_spread <= 0:
                    raise ValueError(f"{symbol} spread must be positive")
                return legacy_spread

        spread = self.config.spread_pips.get(inst.symbol, 1.0)
        spread *= self._time_multiplier(self.config.spread_time_multipliers, bar)
        if spread <= 0:
            raise ValueError(f"{symbol} spread must be positive")
        return spread

    def slippage_pips(self, symbol: str, bar: object | None = None) -> float:
        slippage = self.config.slippage_pips.get(instrument_for(symbol).symbol, 0.2)
        slippage *= self._time_multiplier(self.config.slippage_time_multipliers, bar)
        if slippage <= 0:
            raise ValueError(f"{symbol} slippage_pips must be positive")
        return slippage

    def fill_price(
        self, symbol: str, mid_price: float, side: int, bar: object | None = None
    ) -> float:
        if side not in (-1, 1):
            raise ValueError("side must be 1 for buy or -1 for sell")
        inst = instrument_for(symbol)
        cost_distance = inst.pip_size * (
            self.spread_pips(symbol, bar) / 2 + self.slippage_pips(symbol, bar)
        )
        return mid_price + side * cost_distance

    def commission(
        self,
        symbol: str,
        units: float,
        price: float,
        conversion_rates: dict[str, float] | None = None,
    ) -> float:
        if units == 0:
            return 0.0
        variable_fee = (
            notional_usd(symbol, units, price, conversion_rates)
            / 1_000_000
            * self.config.commission_per_million_usd
        )
        return max(variable_fee + self.config.fixed_fee_usd, self.config.minimum_fee_usd)

    def execute_market(
        self,
        symbol: str,
        mid_price: float,
        side: int,
        units: float,
        bar: object | None = None,
        order_type: str = "market",
        conversion_rates: dict[str, float] | None = None,
    ) -> Fill:
        spread = self.spread_pips(symbol, bar)
        slippage = self.slippage_pips(symbol, bar)
        price = self.fill_price(symbol, mid_price, side, bar)
        return Fill(
            expected_price=mid_price,
            price=price,
            fee=self.commission(symbol, units, price, conversion_rates),
            side=side,
            spread_pips=spread,
            slippage_pips=slippage,
            order_type=order_type,
        )

    def round_trip_cost_per_unit_usd(
        self,
        symbol: str,
        price: float,
        bar: object | None = None,
        conversion_rates: dict[str, float] | None = None,
    ) -> float:
        inst = instrument_for(symbol)
        spread_and_slippage = inst.pip_size * (
            self.spread_pips(symbol, bar) + 2 * self.slippage_pips(symbol, bar)
        )
        market_cost = price_distance_to_usd_per_unit(
            symbol,
            spread_and_slippage,
            price,
            conversion_rates,
        )
        commission_cost = (
            notional_usd(symbol, 1.0, price, conversion_rates)
            / 1_000_000
            * self.config.commission_per_million_usd
            * 2
        )
        return market_cost + commission_cost

    def round_trip_fixed_fee_floor_usd(self) -> float:
        """Return a conservative round-trip floor for per-order fixed/minimum fees."""
        per_order_floor = max(self.config.fixed_fee_usd, self.config.minimum_fee_usd)
        return max(per_order_floor, 0.0) * 2

    def _time_multiplier(self, multipliers: dict[int, float], bar: object | None) -> float:
        if not multipliers or bar is None:
            return 1.0
        timestamp = getattr(bar, "name", None)
        hour_attr = getattr(timestamp, "hour", None)
        if hour_attr is None:
            return 1.0
        try:
            hour = int(hour_attr)
        except (TypeError, ValueError):
            return 1.0
        multiplier = multipliers.get(hour, 1.0)
        if multiplier <= 0:
            raise ValueError(f"time multiplier for hour {hour} must be positive")
        return float(multiplier)

    def _bar_float(self, bar: object, column: str) -> float | None:
        try:
            value = bar[column]  # type: ignore[index]
        except (KeyError, TypeError):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric != numeric:
            return None
        return numeric
