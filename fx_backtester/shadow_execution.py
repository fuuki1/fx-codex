"""Shadow order intents, simulated execution evidence and TCA decomposition.

This module measures *executability* without ever sending an order:

- ``OrderIntent`` is the immutable record of what a model would have done;
- ``ExecutionEvent`` records what a simulated (mock or replay) venue did with
  an intent — fills here are labeled simulated and are never reported as
  paper or live fills;
- ``evaluate_intent_against_quote`` applies the pre-trade vetoes (stale
  quote, expired intent, spread cap) that a real gateway would apply;
- ``tca_decompose`` splits gross alpha into spread, slippage, latency,
  commission, financing, adverse-selection and rejected-opportunity costs;
- ``DisabledOrderGateway`` is the only "live" surface and every method
  fails closed: this build cannot transmit orders.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure
from fx_intel.source_contracts import BrokerQuote

SIMULATED_VENUES = frozenset({"simulated_mock", "simulated_replay"})


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypedFailure(
            FailureReason.INVALID,
            f"{name} must be a timezone-aware datetime",
            context={"observed": str(value)},
        )
    return value.astimezone(UTC)


def _finite(value: float, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypedFailure(FailureReason.INVALID, f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise TypedFailure(FailureReason.INVALID, f"{name} must be finite")
    if positive and number <= 0:
        raise TypedFailure(
            FailureReason.INVALID, f"{name} must be positive", context={"observed": number}
        )
    return number


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    decision_id: str
    model_id: str
    symbol: str
    side: str
    quantity: float
    decision_time: datetime
    valid_until: datetime
    reference_bid: float
    reference_ask: float
    risk_budget_r: float
    stop_loss: float
    take_profit: float
    reason: str
    data_hash: str
    model_hash: str

    def __post_init__(self) -> None:
        for name in ("intent_id", "decision_id", "model_id", "symbol", "reason"):
            if not str(getattr(self, name)).strip():
                raise TypedFailure(FailureReason.INVALID, f"order intent {name} is required")
        if self.side not in {"long", "short"}:
            raise TypedFailure(
                FailureReason.INVALID,
                "order intent side must be long or short",
                context={"observed": self.side},
            )
        _finite(self.quantity, "quantity", positive=True)
        bid = _finite(self.reference_bid, "reference_bid", positive=True)
        ask = _finite(self.reference_ask, "reference_ask", positive=True)
        if ask <= bid:
            raise TypedFailure(FailureReason.INVALID, "reference_ask must exceed reference_bid")
        _finite(self.risk_budget_r, "risk_budget_r", positive=True)
        _finite(self.stop_loss, "stop_loss", positive=True)
        _finite(self.take_profit, "take_profit", positive=True)
        decision = _aware(self.decision_time, "decision_time")
        valid = _aware(self.valid_until, "valid_until")
        if valid <= decision:
            raise TypedFailure(FailureReason.INVALID, "valid_until must be after decision_time")
        for name in ("data_hash", "model_hash"):
            value = str(getattr(self, name))
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise TypedFailure(FailureReason.INVALID, f"order intent {name} must be a SHA-256")

    @property
    def reference_mid(self) -> float:
        return (self.reference_bid + self.reference_ask) / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "decision_id": self.decision_id,
            "model_id": self.model_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "decision_time": self.decision_time.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "reference_bid": self.reference_bid,
            "reference_ask": self.reference_ask,
            "reference_mid": self.reference_mid,
            "risk_budget_r": self.risk_budget_r,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "reason": self.reason,
            "data_hash": self.data_hash,
            "model_hash": self.model_hash,
        }


@dataclass(frozen=True)
class Fill:
    fill_time: datetime
    price: float
    quantity: float

    def __post_init__(self) -> None:
        _aware(self.fill_time, "fill_time")
        _finite(self.price, "price", positive=True)
        _finite(self.quantity, "quantity", positive=True)


@dataclass(frozen=True)
class ExecutionEvent:
    """What a simulated venue did with one intent. Never a paper or live fill."""

    intent_id: str
    venue: str
    order_send_time: datetime | None
    broker_ack_time: datetime | None
    requested_quantity: float
    fills: tuple[Fill, ...] = ()
    cancel_time: datetime | None = None
    rejected: bool = False
    reject_reason: str | None = None
    commission: float = 0.0
    financing: float = 0.0
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.venue not in SIMULATED_VENUES:
            raise TypedFailure(
                FailureReason.EXECUTION_MODEL_UNAVAILABLE,
                "this build only records simulated execution evidence; "
                "paper/live venues are not available",
                context={"observed_venue": self.venue},
            )
        if not self.intent_id.strip():
            raise TypedFailure(FailureReason.INVALID, "execution event intent_id is required")
        _finite(self.requested_quantity, "requested_quantity", positive=True)
        if self.rejected and not (self.reject_reason or "").strip():
            raise TypedFailure(
                FailureReason.INCOMPLETE,
                "a rejected execution must preserve its reject_reason",
                context={"intent_id": self.intent_id},
            )
        if self.rejected and self.fills:
            raise TypedFailure(
                FailureReason.INVALID,
                "a rejected execution cannot also carry fills",
                context={"intent_id": self.intent_id},
            )
        filled = sum(fill.quantity for fill in self.fills)
        if filled > self.requested_quantity + 1e-9:
            raise TypedFailure(
                FailureReason.INVALID,
                "filled quantity exceeds requested quantity",
                context={"intent_id": self.intent_id, "filled": filled},
            )
        if _finite(self.commission, "commission") < 0 or _finite(self.financing, "financing") < 0:
            raise TypedFailure(FailureReason.INVALID, "costs cannot be negative")

    @property
    def filled_quantity(self) -> float:
        return sum(fill.quantity for fill in self.fills)

    @property
    def partial_fill(self) -> bool:
        return 0.0 < self.filled_quantity < self.requested_quantity - 1e-9

    @property
    def fill_price(self) -> float | None:
        filled = self.filled_quantity
        if filled <= 0:
            return None
        return sum(fill.price * fill.quantity for fill in self.fills) / filled

    @property
    def first_fill_time(self) -> datetime | None:
        return min((fill.fill_time for fill in self.fills), default=None)

    @property
    def final_fill_time(self) -> datetime | None:
        return max((fill.fill_time for fill in self.fills), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "venue": self.venue,
            "order_send_time": (self.order_send_time.isoformat() if self.order_send_time else None),
            "broker_ack_time": self.broker_ack_time.isoformat() if self.broker_ack_time else None,
            "first_fill_time": (self.first_fill_time.isoformat() if self.first_fill_time else None),
            "final_fill_time": (self.final_fill_time.isoformat() if self.final_fill_time else None),
            "cancel_time": self.cancel_time.isoformat() if self.cancel_time else None,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "fill_price": self.fill_price,
            "partial_fill": self.partial_fill,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "commission": self.commission,
            "financing": self.financing,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class PreTradePolicy:
    max_quote_age: timedelta = timedelta(seconds=5)
    max_spread: float = 0.05

    def __post_init__(self) -> None:
        if self.max_quote_age <= timedelta(0):
            raise TypedFailure(FailureReason.INVALID, "max_quote_age must be positive")
        _finite(self.max_spread, "max_spread", positive=True)


def evaluate_intent_against_quote(
    intent: OrderIntent,
    quote: BrokerQuote,
    *,
    now: datetime,
    policy: PreTradePolicy,
) -> dict[str, Any]:
    """Pre-trade vetoes a real gateway would apply; abstention is a result."""

    current = _aware(now, "now")
    if quote.symbol != intent.symbol:
        raise TypedFailure(
            FailureReason.INVALID,
            "quote symbol does not match the intent",
            context={"intent": intent.symbol, "quote": quote.symbol},
        )
    if current > intent.valid_until:
        return {
            "action": "reject",
            "reason": "expired_intent",
            "intent_id": intent.intent_id,
            "valid_until": intent.valid_until.isoformat(),
            "now": current.isoformat(),
        }
    age = current - quote.received_at
    if age > policy.max_quote_age:
        return {
            "action": "reject",
            "reason": "stale_quote",
            "intent_id": intent.intent_id,
            "quote_age_seconds": age.total_seconds(),
        }
    if quote.spread > policy.max_spread:
        return {
            "action": "reject",
            "reason": "max_spread_exceeded",
            "intent_id": intent.intent_id,
            "spread": quote.spread,
        }
    return {
        "action": "proceed",
        "intent_id": intent.intent_id,
        "quote_sequence_id": quote.sequence_id,
        "bid": quote.bid,
        "ask": quote.ask,
    }


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


class MockQuoteAdapter:
    """Deterministic in-memory quotes for tests; read-only by construction."""

    def __init__(self, quotes: Sequence[BrokerQuote]) -> None:
        self._quotes = tuple(quotes)

    def latest(self, symbol: str) -> BrokerQuote:
        matching = [quote for quote in self._quotes if quote.symbol == symbol]
        if not matching:
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "no quote is available for this symbol",
                context={"symbol": symbol},
            )
        return max(matching, key=lambda quote: (quote.received_at, quote.sequence_id))


class ReplayQuoteAdapter:
    """Replays recorded quotes in received order; read-only by construction."""

    def __init__(self, quotes: Sequence[BrokerQuote]) -> None:
        ordered = sorted(quotes, key=lambda quote: (quote.received_at, quote.sequence_id))
        self._quotes = tuple(ordered)

    def __iter__(self) -> Iterator[BrokerQuote]:
        return iter(self._quotes)

    def as_of(self, when: datetime) -> BrokerQuote:
        moment = _aware(when, "when")
        eligible = [quote for quote in self._quotes if quote.received_at <= moment]
        if not eligible:
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "no quote had been received at the requested time",
                context={"when": moment.isoformat()},
            )
        return eligible[-1]


class DisabledOrderGateway:
    """The only order-transmission surface in this build, and it refuses."""

    enabled = False

    def send(self, intent: OrderIntent) -> None:
        raise TypedFailure(
            FailureReason.EXECUTION_MODEL_UNAVAILABLE,
            "live/paper order transmission is disabled in this build; "
            "shadow intents are recorded, never sent",
            context={"intent_id": intent.intent_id},
        )

    def cancel(self, intent_id: str) -> None:
        raise TypedFailure(
            FailureReason.EXECUTION_MODEL_UNAVAILABLE,
            "live/paper order transmission is disabled in this build",
            context={"intent_id": intent_id},
        )


# ---------------------------------------------------------------------------
# TCA
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TcaReport:
    intent_id: str
    gross_alpha: float
    spread_cost: float
    slippage_cost: float
    latency_cost: float
    commission: float
    financing: float
    adverse_selection: float
    rejected_opportunity_cost: float
    realized_net_alpha: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "gross_alpha": self.gross_alpha,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
            "latency_cost": self.latency_cost,
            "commission": self.commission,
            "financing": self.financing,
            "adverse_selection": self.adverse_selection,
            "rejected_opportunity_cost": self.rejected_opportunity_cost,
            "realized_net_alpha": self.realized_net_alpha,
        }


def tca_decompose(
    intent: OrderIntent,
    event: ExecutionEvent,
    *,
    gross_alpha: float,
    decision_mid: float,
    send_mid: float | None,
    post_fill_mid: float | None = None,
) -> TcaReport:
    """Split gross alpha into execution cost components (per unit of quantity).

    All monetary values are in quote-price units per unit of base quantity so
    the identity ``net = gross - Σ costs`` holds exactly. A fully rejected
    intent converts all gross alpha into rejected opportunity cost.
    """

    _finite(gross_alpha, "gross_alpha")
    _finite(decision_mid, "decision_mid", positive=True)
    if event.intent_id != intent.intent_id:
        raise TypedFailure(
            FailureReason.INVALID,
            "execution event does not belong to this intent",
            context={"intent": intent.intent_id, "event": event.intent_id},
        )
    direction = 1.0 if intent.side == "long" else -1.0
    fill_price = event.fill_price
    if event.rejected or fill_price is None:
        return TcaReport(
            intent_id=intent.intent_id,
            gross_alpha=gross_alpha,
            spread_cost=0.0,
            slippage_cost=0.0,
            latency_cost=0.0,
            commission=0.0,
            financing=0.0,
            adverse_selection=0.0,
            rejected_opportunity_cost=gross_alpha,
            realized_net_alpha=0.0,
        )
    if send_mid is None:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "a filled execution requires the mid observed at send time",
            context={"intent_id": intent.intent_id},
        )
    _finite(send_mid, "send_mid", positive=True)
    half_spread = (intent.reference_ask - intent.reference_bid) / 2.0
    latency_cost = direction * (send_mid - decision_mid)
    reference_touch = send_mid + direction * half_spread
    slippage_cost = direction * (fill_price - reference_touch)
    adverse_selection = 0.0
    if post_fill_mid is not None:
        _finite(post_fill_mid, "post_fill_mid", positive=True)
        adverse_selection = direction * (fill_price - post_fill_mid)
    fill_ratio = event.filled_quantity / event.requested_quantity
    rejected_opportunity = gross_alpha * (1.0 - fill_ratio)
    realized_gross = gross_alpha * fill_ratio
    realized_net = (
        realized_gross
        - half_spread
        - slippage_cost
        - latency_cost
        - event.commission
        - event.financing
        - adverse_selection
    )
    return TcaReport(
        intent_id=intent.intent_id,
        gross_alpha=gross_alpha,
        spread_cost=half_spread,
        slippage_cost=slippage_cost,
        latency_cost=latency_cost,
        commission=event.commission,
        financing=event.financing,
        adverse_selection=adverse_selection,
        rejected_opportunity_cost=rejected_opportunity,
        realized_net_alpha=realized_net,
    )


def intent_sha256(intent: OrderIntent) -> str:
    return hashlib.sha256(
        json.dumps(intent.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
