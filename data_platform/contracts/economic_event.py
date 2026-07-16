"""Economic calendar event payload and its PIT envelope.

An event is keyed by a stable ``event_id`` (never the free-text indicator name).
The first-release ``actual`` and any later ``actual_revised`` are kept apart so a
revision cannot be backdated. Surprise is standardised against the historical
forecast-error scale, and is only computed from the *first release* actual and
the forecast that was known before the event.
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


def _optional_finite(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PITContractError(f"{field_name} must be a number or None")
    number = float(value)
    if not math.isfinite(number):
        raise PITContractError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class EconomicEvent:
    """One scheduled macro event with first-release and revised actuals apart."""

    source_id: str
    event_id: str
    country: str
    currency: str
    indicator: str
    reference_period: str
    scheduled_at: datetime
    first_seen_at: datetime
    ingested_at: datetime
    available_at: datetime
    importance: str
    writer_id: str
    published_at: datetime | None = None
    forecast: float | None = None
    previous_as_known: float | None = None
    actual_first_release: float | None = None
    actual_revised: float | None = None
    revision_of: str | None = None

    def __post_init__(self) -> None:
        for name in ("forecast", "previous_as_known", "actual_first_release", "actual_revised"):
            _optional_finite(getattr(self, name), field_name=name)
        if self.actual_revised is not None and self.revision_of is None:
            raise PITContractError("a revised actual must reference the event_id it revises")

    def surprise_z(self, historical_forecast_error_std: float) -> float | None:
        """Standardised surprise of the first release vs forecast.

        Returns None when the surprise cannot be formed (missing actual/forecast)
        rather than substituting 0 — an unknown surprise is not a zero surprise.
        The revised actual is intentionally never used here.
        """

        std = _optional_finite(
            historical_forecast_error_std, field_name="historical_forecast_error_std"
        )
        if std is None or std <= 0:
            raise PITContractError("historical_forecast_error_std must be positive and finite")
        if self.actual_first_release is None or self.forecast is None:
            return None
        return (self.actual_first_release - self.forecast) / std

    def raw_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "country": self.country,
            "currency": self.currency,
            "indicator": self.indicator,
            "reference_period": self.reference_period,
            "scheduled_at": self.scheduled_at.isoformat(),
            "forecast": self.forecast,
            "previous_as_known": self.previous_as_known,
            "actual_first_release": self.actual_first_release,
            "actual_revised": self.actual_revised,
            "revision_of": self.revision_of,
            "importance": self.importance,
            "source_id": self.source_id,
        }

    def to_pit_record(self) -> PITRecord:
        return PITRecord(
            source_id=self.source_id,
            instrument=self.currency,
            event_time=self.scheduled_at,
            published_at=self.published_at,
            first_seen_at=self.first_seen_at,
            ingested_at=self.ingested_at,
            available_at=self.available_at,
            revision_id=self.revision_of,
            raw_sha256=canonical_json_sha256(self.raw_payload()),
            writer_id=self.writer_id,
            schema_version=1,
            payload=self.raw_payload(),
        )
