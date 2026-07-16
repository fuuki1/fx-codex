"""The common point-in-time (PIT) envelope every data source must satisfy.

``PITRecord`` is the *authoritative* bitemporal contract for the data platform:
one immutable observation, tagged with every timestamp needed to prove it was
already knowable at a research time ``t``. Research at time ``t`` may only read
records whose ``available_at <= t`` — this is the single rule that keeps future
information out of every model.

Relationship to the research engine
-----------------------------------
``fx_backtester.point_in_time.PointInTimeRecord`` is the backtester's *internal*
representation used while materialising a training dataset. ``PITRecord`` is the
*platform-level* ingestion contract that sits in front of it: adapters normalise
raw source payloads into ``PITRecord`` first, and :func:`to_point_in_time_fields`
maps a ``PITRecord`` onto the backtester envelope so the two never drift. This
module is deliberately the one place the platform's field names and PIT rule are
defined; downstream code should build on it rather than re-deriving availability.

Prohibitions encoded here (fail closed, never silently):
- naive datetimes are rejected; every timestamp is normalised to UTC;
- ``available_at`` is never earlier than the record could truly be used — it is
  raised to the latest of declared availability, publication, revision and
  ingestion, so a revised value can never be backdated onto its first release;
- ``first_seen_at`` is a hard floor: a record cannot be "available" before this
  system first saw it, even if the source stamps an earlier availability;
- a missing/failed fetch is never coerced into a zero, a mid, or "now".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class PITContractError(ValueError):
    """Raised when a record cannot satisfy the point-in-time contract.

    A distinct type (not a generic ``ValueError``) so callers can catch a PIT
    contract breach specifically and quarantine the row instead of crashing.
    """


def _require_aware_utc(value: object, *, field_name: str) -> datetime:
    """Return ``value`` as a UTC datetime, or fail closed.

    Naive datetimes are rejected outright: an unknown timezone is a data-quality
    defect, not something to guess. Strings are intentionally *not* accepted here
    so that timezone handling happens once, at the adapter boundary.
    """

    if not isinstance(value, datetime):
        raise PITContractError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        raise PITContractError(f"{field_name} must be timezone-aware; naive datetimes are rejected")
    return value.astimezone(UTC)


def _require_optional_aware_utc(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _require_aware_utc(value, field_name=field_name)


def _require_nonempty_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PITContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def canonical_json_sha256(payload: Any) -> str:
    """Deterministic SHA-256 over a canonical JSON encoding of ``payload``.

    Keys are sorted and separators are tight so the same logical content hashes
    identically across processes and Python versions. ``default=str`` lets
    datetimes and other simple objects serialise without bespoke encoders.
    """

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PITRecord:
    """One immutable, point-in-time-tagged observation from a single source.

    Fields mirror the platform contract exactly. ``available_at`` is the only
    timestamp research is allowed to gate on; it is normalised in
    :meth:`__post_init__` to the latest instant the record could genuinely be
    used, and is never allowed below ``first_seen_at``.
    """

    source_id: str
    instrument: str
    event_time: datetime
    published_at: datetime | None
    first_seen_at: datetime
    ingested_at: datetime
    available_at: datetime
    revision_id: str | None
    raw_sha256: str
    writer_id: str
    schema_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source_id = _require_nonempty_text(self.source_id, field_name="source_id")
        instrument = _require_nonempty_text(self.instrument, field_name="instrument")
        writer_id = _require_nonempty_text(self.writer_id, field_name="writer_id")
        raw_sha256 = _require_nonempty_text(self.raw_sha256, field_name="raw_sha256")
        if len(raw_sha256) != 64 or any(c not in "0123456789abcdef" for c in raw_sha256.lower()):
            raise PITContractError("raw_sha256 must be a 64-char hex SHA-256 digest")

        event_time = _require_aware_utc(self.event_time, field_name="event_time")
        first_seen_at = _require_aware_utc(self.first_seen_at, field_name="first_seen_at")
        ingested_at = _require_aware_utc(self.ingested_at, field_name="ingested_at")
        declared_available = _require_aware_utc(self.available_at, field_name="available_at")
        published_at = _require_optional_aware_utc(self.published_at, field_name="published_at")

        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise PITContractError("schema_version must be an integer")
        if self.schema_version < 1:
            raise PITContractError("schema_version must be >= 1")

        revision_id: str | None = None
        if self.revision_id is not None:
            revision_id = _require_nonempty_text(self.revision_id, field_name="revision_id")

        # A record cannot be used before this system first saw it, nor before it
        # was ingested; and a published/declared-available time only ever *delays*
        # availability. We take the latest so availability is never optimistic.
        effective_available = max(
            instant
            for instant in (declared_available, first_seen_at, ingested_at, published_at)
            if instant is not None
        )

        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "writer_id", writer_id)
        object.__setattr__(self, "raw_sha256", raw_sha256.lower())
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "first_seen_at", first_seen_at)
        object.__setattr__(self, "ingested_at", ingested_at)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "available_at", effective_available)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "payload", _json_ready(self.payload))

    def available_at_or_before(self, t: datetime) -> bool:
        """True iff this record was usable at research time ``t``.

        This is the platform's single point-in-time gate. ``t`` must be
        timezone-aware; a naive ``t`` is a bug in the caller and fails closed.
        """

        cutoff = _require_aware_utc(t, field_name="t")
        return self.available_at <= cutoff

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "instrument": self.instrument,
            "event_time": self.event_time.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "first_seen_at": self.first_seen_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "revision_id": self.revision_id,
            "raw_sha256": self.raw_sha256,
            "writer_id": self.writer_id,
            "schema_version": self.schema_version,
            "payload": dict(self.payload),
        }

    def to_point_in_time_fields(self) -> dict[str, Any]:
        """Map onto ``fx_backtester.point_in_time.PointInTimeRecord`` kwargs.

        Returned as a plain dict (not the object) so this contract module has no
        import dependency on the backtester; callers on the research side
        construct ``PointInTimeRecord(**record.to_point_in_time_fields())``. The
        mapping is intentionally explicit so the two envelopes stay reconcilable.
        """

        # ``content_hash`` is intentionally omitted: PointInTimeRecord derives it
        # from the payload it receives, whereas ``raw_sha256`` here hashes the
        # *raw source* payload. Forcing the raw hash would fail that check. The
        # raw hash is carried inside the payload instead, keeping both envelopes
        # internally consistent.
        return {
            "event_time": self.event_time,
            "available_time": self.available_at,
            "ingested_time": self.ingested_at,
            "source": self.source_id,
            "source_record_id": self.revision_id or self.raw_sha256,
            "payload": {**dict(self.payload), "raw_sha256": self.raw_sha256},
            "published_time": self.published_at,
            "schema_version": self.schema_version,
            "writer_id": self.writer_id,
        }


def filter_available_at(records: list[PITRecord], t: datetime) -> list[PITRecord]:
    """Return only the records usable at research time ``t`` (``available_at <= t``).

    The one helper every consumer should use to slice a corpus at a research
    time, so the PIT rule is applied identically everywhere rather than
    re-implemented per caller.
    """

    cutoff = _require_aware_utc(t, field_name="t")
    return [record for record in records if record.available_at <= cutoff]


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float) and value != value:  # NaN -> null, never silently 0
        return None
    return value
