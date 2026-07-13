"""Shadow-execution intent payload and its PIT envelope (no real orders).

This records what *would* have happened: the quote seen at intent time, the
side and size intended, and — once the next tradable quote is known — the
hypothetical fill, spread cost, slippage and mark-outs. It never places an
order. It is the raw material for transaction-cost analysis (PR-I′); this
module only fixes the contract and its point-in-time envelope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data_platform.contracts.pit_record import (
    PITContractError,
    PITRecord,
    canonical_json_sha256,
)

INTENDED_SIDES = frozenset({"long", "short", "flat"})


def _finite_nonneg(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PITContractError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PITContractError(f"{field_name} must be non-negative and finite")
    return number


@dataclass(frozen=True)
class ExecutionIntent:
    """A hypothetical order intent. Mark-out fields are optional until measured."""

    source_id: str
    instrument: str
    decision_time: datetime
    intent_time: datetime
    quote_at_intent_bid: float
    quote_at_intent_ask: float
    intended_side: str
    intended_size: float
    available_at: datetime
    writer_id: str
    next_tradable_bid: float | None = None
    next_tradable_ask: float | None = None
    hypothetical_fill: float | None = None
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        if self.intended_side not in INTENDED_SIDES:
            raise PITContractError(f"intended_side must be one of {sorted(INTENDED_SIDES)}")
        _finite_nonneg(self.intended_size, field_name="intended_size")
        for name in ("quote_at_intent_bid", "quote_at_intent_ask"):
            _finite_nonneg(getattr(self, name), field_name=name)
        if self.quote_at_intent_ask <= self.quote_at_intent_bid:
            raise PITContractError("quote_at_intent ask must exceed bid")
        if self.latency_ms is not None:
            _finite_nonneg(self.latency_ms, field_name="latency_ms")

    @property
    def spread_cost(self) -> float | None:
        """Half-spread cost of crossing at intent, or None if not applicable.

        Flat intents pay nothing. Returns None (not 0) when the fill side cannot
        be priced, so an unmeasured cost is never recorded as a free trade.
        """

        if self.intended_side == "flat":
            return 0.0
        if self.hypothetical_fill is None:
            return None
        mid = (self.quote_at_intent_bid + self.quote_at_intent_ask) / 2.0
        return abs(self.hypothetical_fill - mid)

    def raw_payload(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "decision_time": self.decision_time.isoformat(),
            "intent_time": self.intent_time.isoformat(),
            "quote_at_intent_bid": self.quote_at_intent_bid,
            "quote_at_intent_ask": self.quote_at_intent_ask,
            "intended_side": self.intended_side,
            "intended_size": self.intended_size,
            "source_id": self.source_id,
        }

    def to_pit_record(self) -> PITRecord:
        return PITRecord(
            source_id=self.source_id,
            instrument=self.instrument,
            event_time=self.intent_time,
            published_at=None,
            first_seen_at=self.intent_time,
            ingested_at=self.intent_time,
            available_at=self.available_at,
            revision_id=None,
            raw_sha256=canonical_json_sha256(self.raw_payload()),
            writer_id=self.writer_id,
            schema_version=1,
            payload=self.raw_payload(),
        )
