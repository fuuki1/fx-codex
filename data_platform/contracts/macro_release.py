"""Macro data release payload and its PIT envelope (vintage-aware).

A macro series is revised over time. The platform keeps the *vintage* explicit:
the value as first released and the value as later revised are distinct records
with distinct ``available_at`` — a revised figure is never backdated onto the
first-release research window. The final revised value must never be used to
backfill periods it was not yet known for.
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


def _finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PITContractError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PITContractError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class MacroRelease:
    """One vintage of one macro observation.

    ``revision_number`` 0 is the first release; higher numbers are later
    vintages of the same ``(series_id, observation_date)``.
    """

    source_id: str
    series_id: str
    observation_date: datetime
    value: float
    release_time: datetime
    vintage_date: datetime
    revision_number: int
    first_seen_at: datetime
    ingested_at: datetime
    available_at: datetime
    writer_id: str

    def __post_init__(self) -> None:
        _finite(self.value, field_name="value")
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int):
            raise PITContractError("revision_number must be an integer")
        if self.revision_number < 0:
            raise PITContractError("revision_number must be >= 0")

    def raw_payload(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "observation_date": self.observation_date.isoformat(),
            "value": self.value,
            "release_time": self.release_time.isoformat(),
            "vintage_date": self.vintage_date.isoformat(),
            "revision_number": self.revision_number,
            "source_id": self.source_id,
        }

    def to_pit_record(self) -> PITRecord:
        # The revision number is the revision identity, so the same observation's
        # first release and its revisions are distinct, non-overwriting records.
        revision_id = f"{self.series_id}:{self.observation_date.date()}:r{self.revision_number}"
        return PITRecord(
            source_id=self.source_id,
            instrument=self.series_id,
            event_time=self.observation_date,
            published_at=self.release_time,
            first_seen_at=self.first_seen_at,
            ingested_at=self.ingested_at,
            available_at=self.available_at,
            revision_id=revision_id,
            raw_sha256=canonical_json_sha256(self.raw_payload()),
            writer_id=self.writer_id,
            schema_version=1,
            payload=self.raw_payload(),
        )
