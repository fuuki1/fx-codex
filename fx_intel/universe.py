"""Canonical analysis-only FX universe and symbol normalization.

The operational pipeline previously repeated the same three-pair list across
collector, materializer, briefing, learning and freshness configuration.  This
module is the code-level source of truth.  Static launchd/JSON configuration is
kept under contract tests because those files cannot import Python constants.

``pip_size`` is research metadata used for normalization and assertions.  The
live decision cost path uses observed price spread divided by stop distance,
so it does not silently substitute this value for missing bid/ask evidence.
Actual broker instrument availability remains account/division dependent.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import math


@dataclass(frozen=True)
class FxPairSpec:
    symbol: str
    display_name: str
    oanda_instrument: str
    base_currency: str
    quote_currency: str
    pip_size: float
    expected_pip_location: int

    def __post_init__(self) -> None:
        if self.symbol != f"{self.base_currency}{self.quote_currency}":
            raise ValueError(f"inconsistent FX pair specification: {self.symbol}")
        if self.display_name != f"{self.base_currency}/{self.quote_currency}":
            raise ValueError(f"inconsistent FX display name: {self.display_name}")
        if self.oanda_instrument != f"{self.base_currency}_{self.quote_currency}":
            raise ValueError(f"inconsistent OANDA instrument: {self.oanda_instrument}")
        if self.pip_size != 10.0**self.expected_pip_location:
            raise ValueError(f"pip size/location mismatch for {self.symbol}")

    def price_distance_in_pips(self, distance: float) -> float:
        if isinstance(distance, bool) or not isinstance(distance, int | float):
            raise ValueError("price distance must be numeric")
        value = float(distance)
        if not math.isfinite(value):
            raise ValueError("price distance must be finite")
        return value / self.pip_size


MVP_PAIR_SPECS: tuple[FxPairSpec, ...] = (
    FxPairSpec("USDJPY", "USD/JPY", "USD_JPY", "USD", "JPY", 0.01, -2),
    FxPairSpec("EURUSD", "EUR/USD", "EUR_USD", "EUR", "USD", 0.0001, -4),
    FxPairSpec("GBPUSD", "GBP/USD", "GBP_USD", "GBP", "USD", 0.0001, -4),
    FxPairSpec("AUDUSD", "AUD/USD", "AUD_USD", "AUD", "USD", 0.0001, -4),
)

MVP_SYMBOLS: tuple[str, ...] = tuple(spec.symbol for spec in MVP_PAIR_SPECS)
MVP_OANDA_INSTRUMENTS: tuple[str, ...] = tuple(spec.oanda_instrument for spec in MVP_PAIR_SPECS)
_BY_SYMBOL = {spec.symbol: spec for spec in MVP_PAIR_SPECS}


def normalize_symbol(symbol: str) -> str:
    """Normalize ``GBP/USD``, ``GBP_USD`` and ``GBPUSD`` to ``GBPUSD``."""

    if not isinstance(symbol, str):
        raise ValueError(f"FX symbol must be text, got {type(symbol).__name__}")
    value = symbol.strip().upper()
    if ":" in value:
        value = value.rsplit(":", 1)[-1]
    cleaned = value
    for separator in ("/", "_", "-", " "):
        cleaned = cleaned.replace(separator, "")
    if len(cleaned) != 6 or not cleaned.isalpha() or not cleaned.isascii():
        raise ValueError(f"FX symbol must be six ASCII letters: {symbol!r}")
    return cleaned


def pair_spec(symbol: str) -> FxPairSpec:
    """Return the supported operational-pair specification or fail closed."""

    normalized = normalize_symbol(symbol)
    try:
        return _BY_SYMBOL[normalized]
    except KeyError as error:
        raise ValueError(
            f"unsupported operational FX pair {normalized}; expected one of {MVP_SYMBOLS}"
        ) from error


def normalize_mvp_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    """Normalize, validate and de-duplicate operational symbols in input order."""

    resolved = tuple(dict.fromkeys(pair_spec(symbol).symbol for symbol in symbols))
    if not resolved:
        raise ValueError("at least one operational FX pair is required")
    return resolved


def normalize_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    """Normalize and de-duplicate valid FX symbols without restricting research use."""

    resolved = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
    if not resolved:
        raise ValueError("at least one FX pair is required")
    return resolved


def to_oanda_instrument(symbol: str) -> str:
    """Convert a valid FX symbol alias to OANDA's ``BASE_QUOTE`` notation."""

    normalized = normalize_symbol(symbol)
    return f"{normalized[:3]}_{normalized[3:]}"


def symbol_currencies(symbol: str) -> tuple[str, str]:
    normalized = normalize_symbol(symbol)
    return normalized[:3], normalized[3:]
