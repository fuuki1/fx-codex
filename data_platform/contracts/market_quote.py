"""Two-sided market quote payload and its PIT envelope.

A quote carries a real bid and ask; an OHLC close is not a quote and cannot be
laundered into one here. This module builds the *payload* and wraps it in a
:class:`~data_platform.contracts.pit_record.PITRecord` so a quote flows through
the platform under the same point-in-time rule as every other source.

It composes with — does not replace — ``fx_intel.source_contracts.BrokerQuote``:
that type owns the fine-grained clock-skew/observed-spread checks used by the
quote-stream SLO; this module owns the platform envelope (availability, hashing,
writer identity). ``spread`` is only ever a measured ``ask - bid``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data_platform.contracts.pit_record import (
    PITContractError,
    PITRecord,
    canonical_json_sha256,
)


def _finite_positive(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PITContractError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise PITContractError(f"{field_name} must be positive and finite")
    return number


@dataclass(frozen=True)
class MarketQuote:
    """One observed bid/ask with the timestamps needed to place it in time."""

    source_id: str
    instrument: str
    bid: float
    ask: float
    source_timestamp: datetime
    received_timestamp: datetime
    available_at: datetime
    sequence_id: int
    writer_id: str
    tradable: bool
    bid_size: float | None = None
    ask_size: float | None = None
    revision_id: str | None = None

    def __post_init__(self) -> None:
        bid = _finite_positive(self.bid, field_name="bid")
        ask = _finite_positive(self.ask, field_name="ask")
        if ask <= bid:
            raise PITContractError(
                "ask must strictly exceed bid; a one-sided or synthetic quote is not a quote"
            )
        if isinstance(self.sequence_id, bool) or not isinstance(self.sequence_id, int):
            raise PITContractError("sequence_id must be an integer")
        if self.sequence_id < 0:
            raise PITContractError("sequence_id must be >= 0")
        for name in ("bid_size", "ask_size"):
            size = getattr(self, name)
            if size is not None:
                _finite_positive(size, field_name=name)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Measured spread. Never an estimate — always ``ask - bid``."""

        return self.ask - self.bid

    def raw_payload(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "bid": self.bid,
            "ask": self.ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "source_timestamp": self.source_timestamp.isoformat(),
            "sequence_id": self.sequence_id,
            "source_id": self.source_id,
            "tradable": self.tradable,
        }

    def to_pit_record(self) -> PITRecord:
        payload: Mapping[str, Any] = {
            **self.raw_payload(),
            "mid": self.mid,
            "spread": self.spread,
            "received_timestamp": self.received_timestamp.isoformat(),
        }
        return PITRecord(
            source_id=self.source_id,
            instrument=self.instrument,
            event_time=self.source_timestamp,
            published_at=None,
            first_seen_at=self.received_timestamp,
            ingested_at=self.received_timestamp,
            available_at=self.available_at,
            revision_id=self.revision_id,
            raw_sha256=canonical_json_sha256(self.raw_payload()),
            writer_id=self.writer_id,
            schema_version=1,
            payload=payload,
        )
