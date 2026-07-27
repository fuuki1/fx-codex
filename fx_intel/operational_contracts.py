"""Standalone PIT and market-time contracts for the operational projection.

The operational index must be deployable beside an older active application
without importing that application's full journal, briefing, or timeframe
object graph.  Keep these pure contracts synchronized with the canonical
producer contract through local drift tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

DECISION_JOURNAL_PIT_CONTRACT = "decision-journal-pit-v2"
FUSION_PRODUCER = "fusion_raw"
TIMEFRAME_PRODUCER = "timeframe_raw"
FUSION_PRODUCER_VERSION = "fusion-journal-v2"
TIMEFRAME_PRODUCER_VERSION = "timeframe-journal-v2"

MARKET_TIMEZONE = ZoneInfo("America/New_York")
CLOSE_WEEKDAY = 4
CLOSE_LOCAL_TIME = time(16, 59)
REOPEN_WEEKDAY = 6
REOPEN_LOCAL_TIME = time(17, 5)


def is_pit_eligible_entry(entry: Mapping[str, object]) -> bool:
    """Return whether one decision proves identity, provenance, and ordering."""

    if entry.get("pit_eligible") is not True:
        return False
    if entry.get("pit_contract") != DECISION_JOURNAL_PIT_CONTRACT:
        return False
    expected_identity = {
        "fusion": (FUSION_PRODUCER, FUSION_PRODUCER_VERSION),
        "per_timeframe": (TIMEFRAME_PRODUCER, TIMEFRAME_PRODUCER_VERSION),
    }.get(str(entry.get("mode") or ""))
    producer_identity = (
        str(entry.get("producer") or ""),
        str(entry.get("producer_version") or ""),
    )
    if expected_identity is None or producer_identity != expected_identity:
        return False
    if not str(entry.get("decision_id") or "").strip():
        return False
    if not str(entry.get("input_context_id") or "").strip():
        return False
    source_ids = entry.get("source_record_ids")
    if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
        return False
    if not any(str(value).strip() for value in source_ids):
        return False
    recorded = _parse_aware_ts(entry.get("ts"))
    prediction = _parse_aware_ts(entry.get("prediction_time"))
    source_cutoff = _parse_aware_ts(entry.get("source_cutoff"))
    feature_available = _parse_aware_ts(entry.get("max_feature_available_time"))
    if None in (recorded, prediction, source_cutoff, feature_available):
        return False
    assert recorded is not None
    assert prediction is not None
    assert source_cutoff is not None
    assert feature_available is not None
    return recorded == prediction and source_cutoff <= feature_available <= prediction


def open_hours_between(start: datetime, end: datetime) -> float:
    """Return elapsed FX market-open hours using DST-aware New York boundaries."""

    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if end_utc <= start_utc:
        return 0.0
    closed = _closed_hours(start_utc, end_utc, _weekend_closures(start_utc, end_utc))
    return (end_utc - start_utc - closed).total_seconds() / 3600.0


def _parse_aware_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    return moment.astimezone(UTC)


def _closure_for_friday(friday: date) -> tuple[datetime, datetime]:
    close_local = datetime.combine(friday, CLOSE_LOCAL_TIME, tzinfo=MARKET_TIMEZONE)
    reopen_local = datetime.combine(
        friday + timedelta(days=REOPEN_WEEKDAY - CLOSE_WEEKDAY),
        REOPEN_LOCAL_TIME,
        tzinfo=MARKET_TIMEZONE,
    )
    return close_local.astimezone(UTC), reopen_local.astimezone(UTC)


def _closure_on_or_before(moment: datetime) -> tuple[datetime, datetime]:
    utc = _as_utc(moment)
    local_date = utc.astimezone(MARKET_TIMEZONE).date()
    friday = local_date - timedelta(days=(local_date.weekday() - CLOSE_WEEKDAY) % 7)
    close, reopen = _closure_for_friday(friday)
    if close > utc:
        close, reopen = _closure_for_friday(friday - timedelta(days=7))
    return close, reopen


def _weekend_closures(
    start_utc: datetime,
    end_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    closures: list[tuple[datetime, datetime]] = []
    close, reopen = _closure_on_or_before(end_utc)
    while reopen > start_utc:
        closures.append((close, reopen))
        previous_friday = close.astimezone(MARKET_TIMEZONE).date() - timedelta(days=7)
        close, reopen = _closure_for_friday(previous_friday)
    closures.reverse()
    return closures


def _closed_hours(
    start_utc: datetime,
    end_utc: datetime,
    closures: list[tuple[datetime, datetime]],
) -> timedelta:
    closed = timedelta()
    for close, reopen in closures:
        if close >= end_utc:
            break
        overlap = min(reopen, end_utc) - max(close, start_utc)
        if overlap > timedelta():
            closed += overlap
    return closed


__all__ = [
    "DECISION_JOURNAL_PIT_CONTRACT",
    "FUSION_PRODUCER",
    "FUSION_PRODUCER_VERSION",
    "TIMEFRAME_PRODUCER",
    "TIMEFRAME_PRODUCER_VERSION",
    "is_pit_eligible_entry",
    "open_hours_between",
]
