from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from numbers import Real

from fx_backtester.models import instrument_for, notional_usd, price_distance_to_usd_per_unit

MAX_INPUT_MAGNITUDE = 1e15
MAX_PRICE = 1e12
MAX_PIPS = 1e6
MAX_FEE_USD = 1e12


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

    def __post_init__(self) -> None:
        _validate_symbol_values(self.spread_pips, "spread_pips")
        _validate_symbol_values(self.slippage_pips, "slippage_pips")
        for name in (
            "commission_per_million_usd",
            "fixed_fee_usd",
            "minimum_fee_usd",
        ):
            _bounded_real(
                getattr(self, name),
                name,
                lower=0.0,
                upper=MAX_FEE_USD,
            )
        _validate_time_multipliers(self.spread_time_multipliers, "spread_time_multipliers")
        _validate_time_multipliers(
            self.slippage_time_multipliers,
            "slippage_time_multipliers",
        )


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
        if not isinstance(side, int) or isinstance(side, bool) or side not in (-1, 1):
            raise ValueError("side must be 1 for buy or -1 for sell")
        mid_price = _bounded_real(
            mid_price,
            "mid_price",
            lower=0.0,
            upper=MAX_PRICE,
            lower_open=True,
        )
        inst = instrument_for(symbol)
        cost_distance = inst.pip_size * (
            self.spread_pips(symbol, bar) / 2 + self.slippage_pips(symbol, bar)
        )
        price = mid_price + side * cost_distance
        if not isfinite(price) or price <= 0 or price > MAX_PRICE:
            raise ValueError("fill price must be positive, finite, and within the supported bound")
        return price

    def commission(
        self,
        symbol: str,
        units: float,
        price: float,
        conversion_rates: dict[str, float] | None = None,
    ) -> float:
        units = _bounded_real(
            units,
            "units",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        price = _bounded_real(
            price,
            "price",
            lower=0.0,
            upper=MAX_PRICE,
            lower_open=True,
        )
        _validate_conversion_rates(conversion_rates)
        if units == 0:
            return 0.0
        variable_fee = (
            notional_usd(symbol, units, price, conversion_rates)
            / 1_000_000
            * self.config.commission_per_million_usd
        )
        fee = max(variable_fee + self.config.fixed_fee_usd, self.config.minimum_fee_usd)
        if not isfinite(fee) or fee < 0 or fee > MAX_FEE_USD:
            raise ValueError("commission must be finite and within the supported bound")
        return fee

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
        if not isinstance(order_type, str) or not order_type.strip() or len(order_type) > 128:
            raise ValueError("order_type must be a non-empty string of at most 128 characters")
        _bounded_real(
            units,
            "units",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
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
        price = _bounded_real(
            price,
            "price",
            lower=0.0,
            upper=MAX_PRICE,
            lower_open=True,
        )
        _validate_conversion_rates(conversion_rates)
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
        total = market_cost + commission_cost
        if not isfinite(total) or total < 0 or total > MAX_INPUT_MAGNITUDE:
            raise ValueError("round-trip cost must be finite and within the supported bound")
        return total

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
        return _bounded_real(
            value,
            f"bar[{column!r}]",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )


def _bounded_real(
    value: object,
    name: str,
    *,
    lower: float,
    upper: float,
    lower_open: bool = False,
) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError(f"{name} must be a real number, not a boolean")
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    below = numeric <= lower if lower_open else numeric < lower
    if below or numeric > upper:
        interval = f"({lower}, {upper}]" if lower_open else f"[{lower}, {upper}]"
        raise ValueError(f"{name} must be within {interval}")
    return numeric


def _validate_symbol_values(values: dict[str, float], name: str) -> None:
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{name} must be a non-empty dictionary")
    for symbol, value in values.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"{name} keys must be non-empty symbols")
        instrument_for(symbol)
        _bounded_real(
            value,
            f"{name}[{symbol!r}]",
            lower=0.0,
            upper=MAX_PIPS,
            lower_open=True,
        )


def _validate_time_multipliers(values: dict[int, float], name: str) -> None:
    if not isinstance(values, dict):
        raise ValueError(f"{name} must be a dictionary")
    for hour, value in values.items():
        if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            raise ValueError(f"{name} keys must be integer UTC hours in [0, 23]")
        _bounded_real(
            value,
            f"{name}[{hour}]",
            lower=0.0,
            upper=MAX_PIPS,
            lower_open=True,
        )


def _validate_conversion_rates(conversion_rates: dict[str, float] | None) -> None:
    if conversion_rates is None:
        return
    if not isinstance(conversion_rates, dict):
        raise ValueError("conversion_rates must be a dictionary")
    for symbol, rate in conversion_rates.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("conversion_rates keys must be non-empty symbols")
        instrument_for(symbol)
        _bounded_real(
            rate,
            f"conversion_rates[{symbol!r}]",
            lower=0.0,
            upper=MAX_PRICE,
            lower_open=True,
        )
