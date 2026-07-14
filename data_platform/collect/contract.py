"""Collected-quote contract for the read-only market-data collector.

``CollectedQuote`` is the normalized record every collector source must emit.
It is a superset of :class:`data_platform.contracts.market_quote.MarketQuote`
(the bar materializer's input): provenance (provider / environment / connection
/ raw hash / endpoint class) is carried here, and ``to_market_quote`` bridges
into the existing bar pipeline without touching that contract.

Honesty rules enforced at construction (fail-closed, never repaired):
- bid/ask must be finite and positive; ``bid >= ask`` is rejected (a crossed or
  zero-width book is quarantined upstream, never "fixed")
- ``mid`` and ``spread`` are always computed from bid/ask, never accepted from
  the provider
- naive datetimes are rejected; ``provider_event_time`` in the future relative
  to ``received_at`` (beyond a small declared clock-skew allowance) is rejected
- fields the provider does not supply stay ``None`` and are flagged
  ``provider_does_not_supply_<field>`` — they are never zero-filled or guessed

This package is import-isolated from any order/execution path; see
``tests/test_collect_no_order_path.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import Any

from data_platform.quality.state import QualityState

COLLECT_SCHEMA_VERSION = 1
INGEST_VERSION = "collect_v1"
# Provider clocks may run slightly ahead of ours; beyond this the event time is
# treated as a future-data violation and the quote is rejected.
MAX_PROVIDER_CLOCK_AHEAD_SECONDS = 2.0

FLAG_NO_BID_SIZE = "provider_does_not_supply_bid_size"
FLAG_NO_ASK_SIZE = "provider_does_not_supply_ask_size"
FLAG_NO_SEQUENCE = "provider_does_not_supply_sequence_id"
FLAG_NO_EVENT_TIME = "provider_does_not_supply_event_time"


class QuoteContractError(ValueError):
    """A quote violates the contract. The raw payload must be quarantined."""


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise QuoteContractError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _finite_positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuoteContractError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise QuoteContractError(f"{field_name} must be finite and positive, got {value!r}")
    return number


def _optional_size(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuoteContractError(f"{field_name} must be a number or None")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise QuoteContractError(f"{field_name} must be finite and non-negative")
    return number


@dataclass(frozen=True)
class CollectedQuote:
    """One normalized, provenance-complete quote from a read-only source."""

    provider: str
    account_environment: str  # "live" | "practice" | "datafeed" (public feed)
    instrument: str
    provider_event_time: datetime | None
    received_at: datetime
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None
    tradable: bool
    sequence_id: int | None
    connection_id: str
    writer_id: str
    revision_id: str | None
    raw_payload_sha256: str
    source_endpoint_class: str  # "streaming_pricing" | "historical_datafeed" | "replay_fixture"
    collection_mode: str  # "live_stream" | "historical_download" | "replay"
    quality_state: QualityState = QualityState.USABLE
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = COLLECT_SCHEMA_VERSION
    ingest_version: str = INGEST_VERSION

    def __post_init__(self) -> None:
        for name in ("provider", "instrument", "connection_id", "writer_id"):
            if not str(getattr(self, name)).strip():
                raise QuoteContractError(f"{name} must be non-empty")
        if self.account_environment not in ("live", "practice", "datafeed"):
            raise QuoteContractError(
                f"account_environment must be live/practice/datafeed, got "
                f"{self.account_environment!r}"
            )
        if len(self.raw_payload_sha256) != 64:
            raise QuoteContractError("raw_payload_sha256 must be a hex sha256")
        bid = _finite_positive(self.bid, "bid")
        ask = _finite_positive(self.ask, "ask")
        if bid >= ask:
            raise QuoteContractError(f"crossed/zero-width book rejected: bid={bid} ask={ask}")
        received = _aware(self.received_at, "received_at")
        object.__setattr__(self, "received_at", received)
        flags = list(self.quality_flags)
        if self.provider_event_time is not None:
            event = _aware(self.provider_event_time, "provider_event_time")
            ahead = (event - received).total_seconds()
            if ahead > MAX_PROVIDER_CLOCK_AHEAD_SECONDS:
                raise QuoteContractError(
                    f"future provider_event_time rejected ({ahead:.3f}s ahead of received_at)"
                )
            object.__setattr__(self, "provider_event_time", event)
        elif FLAG_NO_EVENT_TIME not in flags:
            flags.append(FLAG_NO_EVENT_TIME)
        object.__setattr__(self, "bid_size", _optional_size(self.bid_size, "bid_size"))
        object.__setattr__(self, "ask_size", _optional_size(self.ask_size, "ask_size"))
        if self.bid_size is None and FLAG_NO_BID_SIZE not in flags:
            flags.append(FLAG_NO_BID_SIZE)
        if self.ask_size is None and FLAG_NO_ASK_SIZE not in flags:
            flags.append(FLAG_NO_ASK_SIZE)
        if self.sequence_id is not None:
            if isinstance(self.sequence_id, bool) or not isinstance(self.sequence_id, int):
                raise QuoteContractError("sequence_id must be int or None")
            if self.sequence_id < 0:
                raise QuoteContractError("sequence_id must be non-negative")
        elif FLAG_NO_SEQUENCE not in flags:
            flags.append(FLAG_NO_SEQUENCE)
        object.__setattr__(self, "quality_flags", tuple(flags))

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Measured spread only — always ``ask - bid``, never provider-supplied."""

        return self.ask - self.bid

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ingest_version": self.ingest_version,
            "provider": self.provider,
            "account_environment": self.account_environment,
            "instrument": self.instrument,
            "provider_event_time": (
                self.provider_event_time.isoformat() if self.provider_event_time else None
            ),
            "received_at": self.received_at.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "mid": self.mid,
            "spread": self.spread,
            "tradable": self.tradable,
            "sequence_id": self.sequence_id,
            "connection_id": self.connection_id,
            "writer_id": self.writer_id,
            "revision_id": self.revision_id,
            "raw_payload_sha256": self.raw_payload_sha256,
            "source_endpoint_class": self.source_endpoint_class,
            "collection_mode": self.collection_mode,
            "quality_state": str(self.quality_state),
            "quality_flags": list(self.quality_flags),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CollectedQuote:
        def _dt(key: str) -> datetime | None:
            raw = payload.get(key)
            if raw is None:
                return None
            return datetime.fromisoformat(str(raw))

        received = _dt("received_at")
        if received is None:
            raise QuoteContractError("received_at is required")
        return cls(
            provider=str(payload["provider"]),
            account_environment=str(payload["account_environment"]),
            instrument=str(payload["instrument"]),
            provider_event_time=_dt("provider_event_time"),
            received_at=received,
            bid=float(payload["bid"]),
            ask=float(payload["ask"]),
            bid_size=payload.get("bid_size"),
            ask_size=payload.get("ask_size"),
            tradable=bool(payload["tradable"]),
            sequence_id=payload.get("sequence_id"),
            connection_id=str(payload["connection_id"]),
            writer_id=str(payload["writer_id"]),
            revision_id=(
                None if payload.get("revision_id") is None else str(payload["revision_id"])
            ),
            raw_payload_sha256=str(payload["raw_payload_sha256"]),
            source_endpoint_class=str(payload["source_endpoint_class"]),
            collection_mode=str(payload["collection_mode"]),
            quality_state=QualityState(str(payload.get("quality_state", "usable"))),
            quality_flags=tuple(payload.get("quality_flags", ())),
        )

    def with_quality(self, state: QualityState, *extra_flags: str) -> CollectedQuote:
        merged = tuple(dict.fromkeys((*self.quality_flags, *extra_flags)))
        return CollectedQuote(
            provider=self.provider,
            account_environment=self.account_environment,
            instrument=self.instrument,
            provider_event_time=self.provider_event_time,
            received_at=self.received_at,
            bid=self.bid,
            ask=self.ask,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            tradable=self.tradable,
            sequence_id=self.sequence_id,
            connection_id=self.connection_id,
            writer_id=self.writer_id,
            revision_id=self.revision_id,
            raw_payload_sha256=self.raw_payload_sha256,
            source_endpoint_class=self.source_endpoint_class,
            collection_mode=self.collection_mode,
            quality_state=state,
            quality_flags=merged,
        )
