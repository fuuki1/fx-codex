from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACCOUNT_CURRENCY = "USD"


class UnsupportedConversionError(ValueError):
    """Raised when a symbol cannot be valued in the account currency."""


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    quote: str
    pip_size: float


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper().replace("/", "")
    if len(cleaned) != 6:
        raise ValueError(f"FX symbol must be 6 letters or BASE/QUOTE: {symbol!r}")
    return cleaned


def instrument_for(symbol: str) -> Instrument:
    normalized = normalize_symbol(symbol)
    base = normalized[:3]
    quote = normalized[3:]
    pip_size = 0.01 if quote == "JPY" else 0.0001
    return Instrument(symbol=normalized, base=base, quote=quote, pip_size=pip_size)


def quote_amount_to_usd(
    symbol: str,
    amount_quote: float,
    price: float,
    conversion_rates: dict[str, float] | None = None,
) -> float:
    """Convert quote-currency PnL to USD for supported USD FX pairs."""
    inst = instrument_for(symbol)
    if inst.quote == ACCOUNT_CURRENCY:
        return amount_quote
    if inst.base == ACCOUNT_CURRENCY:
        return amount_quote / price
    if conversion_rates:
        usd_quote = f"{ACCOUNT_CURRENCY}{inst.quote}"
        quote_usd = f"{inst.quote}{ACCOUNT_CURRENCY}"
        if usd_quote in conversion_rates:
            return amount_quote / conversion_rates[usd_quote]
        if quote_usd in conversion_rates:
            return amount_quote * conversion_rates[quote_usd]
    raise UnsupportedConversionError(
        f"{inst.symbol} requires a conversion rate to value {inst.quote} in USD "
        f"(provide {ACCOUNT_CURRENCY}{inst.quote} or {inst.quote}{ACCOUNT_CURRENCY})"
    )


def price_distance_to_usd_per_unit(
    symbol: str,
    price_distance: float,
    price: float,
    conversion_rates: dict[str, float] | None = None,
) -> float:
    return abs(quote_amount_to_usd(symbol, price_distance, price, conversion_rates))


def notional_usd(
    symbol: str,
    units: float,
    price: float,
    conversion_rates: dict[str, float] | None = None,
) -> float:
    """Return USD notional for base-currency units."""
    inst = instrument_for(symbol)
    abs_units = abs(units)
    if inst.base == ACCOUNT_CURRENCY:
        return abs_units
    if inst.quote == ACCOUNT_CURRENCY:
        return abs_units * price
    quote_notional = abs_units * price
    return abs(quote_amount_to_usd(symbol, quote_notional, price, conversion_rates))


def pnl_usd(
    symbol: str,
    direction: int,
    units: float,
    entry_price: float,
    exit_price: float,
    conversion_rates: dict[str, float] | None = None,
) -> float:
    pnl_quote = direction * units * (exit_price - entry_price)
    return quote_amount_to_usd(symbol, pnl_quote, exit_price, conversion_rates)


TRADE_LOG_COLUMNS = (
    "symbol",
    "strategy",
    "direction",
    "units",
    "signal_time",
    "order_time",
    "fill_time",
    "side",
    "expected_price",
    "fill_price",
    "spread_pips",
    "slippage_pips",
    "order_type",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "exit_expected_price",
    "exit_fill_price",
    "exit_spread_pips",
    "exit_slippage_pips",
    "exit_order_type",
    "stop_price",
    "take_profit_price",
    "gross_pnl",
    "fees",
    "net_pnl",
    "initial_risk_usd",
    "r_multiple",
    "reason",
    "exit_reason",
)


@dataclass
class Position:
    symbol: str
    direction: int
    units: float
    signal_time: Any
    order_time: Any
    entry_time: Any
    expected_entry_price: float
    entry_price: float
    stop_price: float
    take_profit_price: float | None
    entry_fee: float
    entry_spread_pips: float
    entry_slippage_pips: float
    entry_order_type: str
    initial_risk_usd: float
    strategy: str


@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: int
    units: float
    signal_time: Any
    order_time: Any
    fill_time: Any
    side: str
    expected_price: float
    fill_price: float
    spread_pips: float
    slippage_pips: float
    order_type: str
    entry_time: Any
    exit_time: Any
    entry_price: float
    exit_price: float
    exit_expected_price: float
    exit_fill_price: float
    exit_spread_pips: float
    exit_slippage_pips: float
    exit_order_type: str
    stop_price: float
    take_profit_price: float | None
    gross_pnl: float
    fees: float
    net_pnl: float
    initial_risk_usd: float
    r_multiple: float
    reason: str
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "units": self.units,
            "signal_time": self.signal_time,
            "order_time": self.order_time,
            "fill_time": self.fill_time,
            "side": self.side,
            "expected_price": self.expected_price,
            "fill_price": self.fill_price,
            "spread_pips": self.spread_pips,
            "slippage_pips": self.slippage_pips,
            "order_type": self.order_type,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "exit_expected_price": self.exit_expected_price,
            "exit_fill_price": self.exit_fill_price,
            "exit_spread_pips": self.exit_spread_pips,
            "exit_slippage_pips": self.exit_slippage_pips,
            "exit_order_type": self.exit_order_type,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "net_pnl": self.net_pnl,
            "initial_risk_usd": self.initial_risk_usd,
            "r_multiple": self.r_multiple,
            "reason": self.reason,
            "exit_reason": self.exit_reason,
        }
