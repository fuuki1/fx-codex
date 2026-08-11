"""Immutable raw-event contract for the shadow data-truth ledger.

The payload is retained byte-for-byte.  Metadata describes when the source
created/published the event and when this repository received/ingested it;
those clocks are never collapsed into a single ambiguous timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import math
import re
from types import MappingProxyType
from typing import Any

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_QUALITY_STATES = frozenset({"received", "duplicate", "quarantined"})


class RawEventContractError(ValueError):
    """Raised when a raw event cannot satisfy point-in-time invariants."""


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise RawEventContractError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise RawEventContractError(f"{field_name} must be non-empty")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RawEventContractError(f"{field_name} must be valid UTF-8 text") from error
    return text


def _optional_text(value: object | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)


def _aware_utc(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RawEventContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RawEvent:
    """One unmodified source delivery plus its bitemporal audit envelope."""

    event_id: str
    source: str
    event_type: str
    receive_time: datetime
    ingest_time: datetime
    payload_bytes: bytes = field(repr=False)
    payload_hash: str
    provenance: Mapping[str, str]
    source_event_id: str | None = None
    canonical_event_id: str | None = None
    duplicate_of: str | None = None
    quality_status: str = "received"
    quarantine_reason: str | None = None
    source_trust_score: float | None = None
    symbol: str | None = None
    schema_version: int = 1
    event_time: datetime | None = None
    publish_time: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_event_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "source", "event_type"):
            object.__setattr__(self, name, _required_text(getattr(self, name), field_name=name))
        for name in (
            "source_event_id",
            "canonical_event_id",
            "duplicate_of",
            "quarantine_reason",
            "symbol",
            "supersedes_event_id",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), field_name=name),
            )

        if not isinstance(self.payload_bytes, (bytes, bytearray)):
            raise RawEventContractError("payload_bytes must be bytes")
        payload = bytes(self.payload_bytes)
        object.__setattr__(self, "payload_bytes", payload)
        if not isinstance(self.payload_hash, str) or not _SHA256_RE.fullmatch(self.payload_hash):
            raise RawEventContractError("payload_hash must be a lowercase SHA-256")
        observed_hash = hashlib.sha256(payload).hexdigest()
        if observed_hash != self.payload_hash:
            raise RawEventContractError(
                f"payload_hash does not match payload_bytes: {self.payload_hash} != {observed_hash}"
            )

        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise RawEventContractError("schema_version must be an integer")
        if self.schema_version < 1:
            raise RawEventContractError("schema_version must be >= 1")
        if not isinstance(self.quality_status, str) or self.quality_status not in _QUALITY_STATES:
            raise RawEventContractError(
                f"quality_status must be one of {sorted(_QUALITY_STATES)}, got {self.quality_status!r}"
            )
        if self.quality_status == "quarantined" and self.quarantine_reason is None:
            raise RawEventContractError("quarantined events require quarantine_reason")
        if self.quality_status == "duplicate" and self.duplicate_of is None:
            raise RawEventContractError("duplicate events require duplicate_of")
        if self.duplicate_of == self.event_id:
            raise RawEventContractError("duplicate_of cannot reference event_id itself")
        if self.supersedes_event_id == self.event_id:
            raise RawEventContractError("supersedes_event_id cannot reference event_id itself")
        if self.duplicate_of is not None and self.supersedes_event_id is not None:
            raise RawEventContractError("an event cannot be both a duplicate and a revision")

        normalized_provenance: dict[str, str] = {}
        if not isinstance(self.provenance, Mapping):
            raise RawEventContractError("provenance must be a mapping of strings")
        for raw_key, raw_value in self.provenance.items():
            key = _required_text(raw_key, field_name="provenance key")
            if not isinstance(raw_value, str):
                raise RawEventContractError("provenance values must be strings")
            normalized_provenance[key] = _required_text(
                raw_value, field_name=f"provenance[{key!r}]"
            )
        if not normalized_provenance:
            raise RawEventContractError("provenance must contain at least one entry")
        object.__setattr__(self, "provenance", MappingProxyType(normalized_provenance))

        for name in (
            "event_time",
            "publish_time",
            "receive_time",
            "ingest_time",
            "valid_from",
            "valid_to",
        ):
            object.__setattr__(
                self,
                name,
                _aware_utc(getattr(self, name), field_name=name),
            )
        if self.receive_time is None or self.ingest_time is None:
            raise RawEventContractError("receive_time and ingest_time are required")
        if self.receive_time > self.ingest_time:
            raise RawEventContractError("receive_time must be <= ingest_time")
        # Source clocks may describe a future scheduled event or may be skewed.
        # Domain ordering belongs to post-ledger quality checks; raw receipt
        # preserves the evidence first.
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_from > self.valid_to:
                raise RawEventContractError("valid_from must be <= valid_to")

        if self.source_trust_score is not None:
            if isinstance(self.source_trust_score, bool) or not isinstance(
                self.source_trust_score, (int, float)
            ):
                raise RawEventContractError("source_trust_score must be a number or None")
            score = float(self.source_trust_score)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise RawEventContractError("source_trust_score must be between 0 and 1")
            object.__setattr__(self, "source_trust_score", score)

    def metadata_dict(self) -> dict[str, Any]:
        """Return the append-ledger row without copying or transforming raw bytes."""

        def stamp(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "event_id": self.event_id,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "canonical_event_id": self.canonical_event_id,
            "duplicate_of": self.duplicate_of,
            "quality_status": self.quality_status,
            "quarantine_reason": self.quarantine_reason,
            "source_trust_score": self.source_trust_score,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "schema_version": self.schema_version,
            "payload_hash": self.payload_hash,
            "payload_size": len(self.payload_bytes),
            "provenance": dict(sorted(self.provenance.items())),
            "event_time": stamp(self.event_time),
            "publish_time": stamp(self.publish_time),
            "receive_time": stamp(self.receive_time),
            "ingest_time": stamp(self.ingest_time),
            "valid_from": stamp(self.valid_from),
            "valid_to": stamp(self.valid_to),
            "supersedes_event_id": self.supersedes_event_id,
        }
