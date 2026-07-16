"""Promotion-admissible source adapter contracts and data-quality SLOs.

This module defines the *contract* every ingestion source must eventually
satisfy — typed schemas, typed failure states and measurable quality SLOs —
and states honestly which sources implement it today. Declaring a schema here
is not evidence that any real data satisfies it; unimplemented sources report
``unavailable`` instead of pretending.

Prohibitions encoded here:
- OHLC closes cannot masquerade as bid/ask (quotes require true bid/ask);
- estimated spreads cannot be recorded as observed spreads;
- broker server time and local receive time are separate mandatory fields;
- macro actuals must keep initial and revised values apart so a revised
  figure can never be backdated onto its original release.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure


class SourceKind(StrEnum):
    PRICE_BID_ASK = "price_bid_ask"
    MACRO_FRED = "macro_fred"
    ECONOMIC_CALENDAR = "economic_calendar"
    NEWS = "news"
    SCANNER_TECHNICAL = "scanner_technical"
    COT = "cot"


class SourceImplementation(StrEnum):
    IMPLEMENTED_RESEARCH_ONLY = "implemented_research_only"
    UNIMPLEMENTED = "unimplemented"


# Honest registry of what exists today. COT has a PIT adapter (fx_intel.cot_pit);
# everything else is a declared contract without a promotion-admissible adapter.
SOURCE_ADAPTER_STATUS: dict[SourceKind, SourceImplementation] = {
    SourceKind.PRICE_BID_ASK: SourceImplementation.UNIMPLEMENTED,
    SourceKind.MACRO_FRED: SourceImplementation.UNIMPLEMENTED,
    SourceKind.ECONOMIC_CALENDAR: SourceImplementation.UNIMPLEMENTED,
    SourceKind.NEWS: SourceImplementation.UNIMPLEMENTED,
    SourceKind.SCANNER_TECHNICAL: SourceImplementation.UNIMPLEMENTED,
    SourceKind.COT: SourceImplementation.IMPLEMENTED_RESEARCH_ONLY,
}


def require_source_adapter(kind: SourceKind) -> None:
    """Fail closed when a pipeline asks for a source that does not exist yet."""

    status = SOURCE_ADAPTER_STATUS.get(kind, SourceImplementation.UNIMPLEMENTED)
    if status is not SourceImplementation.IMPLEMENTED_RESEARCH_ONLY:
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "no promotion-admissible adapter exists for this source",
            context={"source_kind": kind.value, "status": status.value},
        )


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypedFailure(
            FailureReason.INVALID,
            f"{name} must be a timezone-aware datetime",
            context={"observed": str(value)},
        )
    return value.astimezone(UTC)


def _finite_positive(value: float, name: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypedFailure(FailureReason.INVALID, f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise TypedFailure(
            FailureReason.INVALID,
            f"{name} must be positive and finite",
            context={"observed": value},
        )
    return number


def _sha256_of(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BrokerQuote:
    """One observed two-sided quote. OHLC closes are not admissible here."""

    symbol: str
    bid: float
    ask: float
    source_event_time: datetime
    broker_server_time: datetime
    received_at: datetime
    ingested_at: datetime
    sequence_id: int
    source: str
    spread_observed: bool
    quality_flags: tuple[str, ...] = ()
    max_clock_skew: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.source.strip():
            raise TypedFailure(FailureReason.INVALID, "quote symbol and source are required")
        bid = _finite_positive(self.bid, "bid")
        ask = _finite_positive(self.ask, "ask")
        if ask <= bid:
            raise TypedFailure(
                FailureReason.INVALID,
                "ask must exceed bid; a one-sided or synthetic quote is not a quote",
                context={"bid": bid, "ask": ask},
            )
        if not self.spread_observed:
            raise TypedFailure(
                FailureReason.INVALID,
                "estimated spreads cannot be recorded as observed broker quotes",
                context={"symbol": self.symbol},
            )
        if self.sequence_id < 0:
            raise TypedFailure(FailureReason.INVALID, "sequence_id must be >= 0")
        event = _aware(self.source_event_time, "source_event_time")
        server = _aware(self.broker_server_time, "broker_server_time")
        received = _aware(self.received_at, "received_at")
        ingested = _aware(self.ingested_at, "ingested_at")
        if received < event - self.max_clock_skew or received < server - self.max_clock_skew:
            raise TypedFailure(
                FailureReason.CLOCK_SKEW,
                "quote was received before its source/server time beyond tolerance",
                context={
                    "source_event_time": event.isoformat(),
                    "broker_server_time": server.isoformat(),
                    "received_at": received.isoformat(),
                },
            )
        if ingested < received:
            raise TypedFailure(
                FailureReason.CLOCK_SKEW,
                "ingested_at cannot precede received_at",
            )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def raw_hash(self) -> str:
        return _sha256_of(
            {
                "symbol": self.symbol,
                "bid": self.bid,
                "ask": self.ask,
                "source_event_time": self.source_event_time.isoformat(),
                "broker_server_time": self.broker_server_time.isoformat(),
                "received_at": self.received_at.isoformat(),
                "sequence_id": self.sequence_id,
                "source": self.source,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread": self.spread,
            "source_event_time": self.source_event_time.isoformat(),
            "broker_server_time": self.broker_server_time.isoformat(),
            "received_at": self.received_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "sequence_id": self.sequence_id,
            "source": self.source,
            "raw_hash": self.raw_hash,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class MacroCalendarEvent:
    """One scheduled release with initial and revised figures kept apart."""

    event_id: str
    indicator: str
    country: str
    scheduled_release_at: datetime
    actual_release_at: datetime | None
    first_observed_at: datetime | None
    period: str
    actual: float | None
    consensus: float | None
    previous_initial: float | None
    previous_revised: float | None
    revision: int
    source: str

    def __post_init__(self) -> None:
        for name in ("event_id", "indicator", "country", "period", "source"):
            if not str(getattr(self, name)).strip():
                raise TypedFailure(FailureReason.INVALID, f"{name} is required")
        _aware(self.scheduled_release_at, "scheduled_release_at")
        if self.revision < 0:
            raise TypedFailure(FailureReason.INVALID, "revision must be >= 0")
        if self.actual is not None:
            if self.actual_release_at is None or self.first_observed_at is None:
                raise TypedFailure(
                    FailureReason.INCOMPLETE,
                    "an actual value requires actual_release_at and first_observed_at",
                    context={"event_id": self.event_id},
                )
            released = _aware(self.actual_release_at, "actual_release_at")
            observed = _aware(self.first_observed_at, "first_observed_at")
            if observed < released:
                raise TypedFailure(
                    FailureReason.REVISION_CONFLICT,
                    "a value cannot be observed before its release; "
                    "revised figures must never be backdated",
                    context={
                        "event_id": self.event_id,
                        "actual_release_at": released.isoformat(),
                        "first_observed_at": observed.isoformat(),
                    },
                )
        if self.revision > 0 and self.previous_revised is None and self.previous_initial is None:
            raise TypedFailure(
                FailureReason.INCOMPLETE,
                "a revision must carry the figures it revises",
                context={"event_id": self.event_id},
            )

    @property
    def raw_hash(self) -> str:
        return _sha256_of(
            {
                "event_id": self.event_id,
                "indicator": self.indicator,
                "country": self.country,
                "period": self.period,
                "actual": self.actual,
                "revision": self.revision,
                "source": self.source,
            }
        )


@dataclass(frozen=True)
class DataQualitySlo:
    """Measured quality against declared limits; unmeasured metrics stay None."""

    freshness_seconds: float | None
    completeness: float | None
    duplicate_rate: float
    late_arrival_rate: float
    out_of_order_rate: float
    clock_skew_violations: int
    schema_violation_rate: float
    revision_rate: float | None = None
    cross_source_divergence: float | None = None
    raw_reconstruction_success: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def measure_quote_slo(
    quotes: Sequence[BrokerQuote],
    *,
    now: datetime,
    expected_interval: timedelta,
    late_threshold: timedelta,
) -> DataQualitySlo:
    """Measure the SLO subset computable from a quote stream alone."""

    if now.tzinfo is None:
        raise TypedFailure(FailureReason.INVALID, "now must be timezone-aware")
    if not quotes:
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "no quotes were supplied; quality is unavailable, not perfect",
        )
    total = len(quotes)
    seen: set[tuple[str, int]] = set()
    duplicates = 0
    out_of_order = 0
    late = 0
    previous_sequence: dict[str, int] = {}
    for quote in quotes:
        key = (quote.symbol, quote.sequence_id)
        if key in seen:
            duplicates += 1
        seen.add(key)
        last = previous_sequence.get(quote.symbol)
        if last is not None and quote.sequence_id < last:
            out_of_order += 1
        previous_sequence[quote.symbol] = max(quote.sequence_id, last or 0)
        if quote.received_at - quote.source_event_time > late_threshold:
            late += 1
    newest = max(quote.received_at for quote in quotes)
    span = max(quote.source_event_time for quote in quotes) - min(
        quote.source_event_time for quote in quotes
    )
    expected_count = int(span / expected_interval) + 1 if expected_interval > timedelta(0) else None
    completeness = (
        min(1.0, len({q.source_event_time for q in quotes}) / expected_count)
        if expected_count
        else None
    )
    return DataQualitySlo(
        freshness_seconds=(now.astimezone(UTC) - newest).total_seconds(),
        completeness=completeness,
        duplicate_rate=duplicates / total,
        late_arrival_rate=late / total,
        out_of_order_rate=out_of_order / total,
        clock_skew_violations=0,  # construction rejects skewed quotes fail-closed
        schema_violation_rate=0.0,  # construction rejects invalid quotes fail-closed
        notes=(
            "revision_rate/cross_source_divergence/raw_reconstruction not measurable "
            "from a single quote stream; they remain unavailable, not zero",
        ),
    )


def enforce_quote_slo(
    slo: DataQualitySlo,
    *,
    max_freshness_seconds: float,
    min_completeness: float,
    max_duplicate_rate: float,
    max_late_arrival_rate: float,
    max_out_of_order_rate: float,
) -> None:
    """Reject the window outright when quality misses the declared SLO."""

    violations: dict[str, Any] = {}
    if slo.freshness_seconds is None or slo.freshness_seconds > max_freshness_seconds:
        violations["freshness_seconds"] = slo.freshness_seconds
    if slo.completeness is None or slo.completeness < min_completeness:
        violations["completeness"] = slo.completeness
    if slo.duplicate_rate > max_duplicate_rate:
        violations["duplicate_rate"] = slo.duplicate_rate
    if slo.late_arrival_rate > max_late_arrival_rate:
        violations["late_arrival_rate"] = slo.late_arrival_rate
    if slo.out_of_order_rate > max_out_of_order_rate:
        violations["out_of_order_rate"] = slo.out_of_order_rate
    if violations:
        raise TypedFailure(
            FailureReason.INVALID,
            "data quality misses the declared SLO; the window is rejected, not warned",
            context={"violations": violations},
        )
