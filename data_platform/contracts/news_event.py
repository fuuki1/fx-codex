"""News article payload and its PIT envelope (first-seen governs).

A publisher may quietly change an article's stated publish time after the fact.
The platform records ``first_seen_at`` as the binding instant: research at time
``t`` may use the article only if this system had already seen it, regardless of
a later-edited ``published_at``. Sentiment is not reduced to a single polarity;
structured stance tags are carried so different strategies can read what they
need.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from data_platform.contracts.pit_record import (
    PITContractError,
    PITRecord,
    canonical_json_sha256,
)

# Structured event types a headline can be tagged with. A free-text sentiment
# score is deliberately not the contract; these let strategies condition on the
# kind of news rather than a single polarity number.
NEWS_EVENT_TYPES = frozenset(
    {
        "central_bank_stance",
        "hawkish_shift",
        "dovish_shift",
        "intervention_risk",
        "geopolitical_shock",
        "fiscal_policy",
        "trade_policy",
        "rating_action",
        "energy_shock",
        "risk_on",
        "risk_off",
    }
)


@dataclass(frozen=True)
class NewsEvent:
    """One news article as first observed, with structured stance tags."""

    source_id: str
    article_id: str
    source: str
    original_url_hash: str
    first_seen_at: datetime
    ingested_at: datetime
    available_at: datetime
    headline_original: str
    writer_id: str
    published_at: datetime | None = None
    updated_at: datetime | None = None
    summary_or_body: str = ""
    currency_tags: tuple[str, ...] = ()
    entity_tags: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    correction_flag: bool = False
    payload_extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.article_id.strip():
            raise PITContractError("article_id is required")
        if not self.headline_original.strip():
            raise PITContractError("headline_original is required")
        unknown = sorted(set(self.event_types) - NEWS_EVENT_TYPES)
        if unknown:
            raise PITContractError(f"unknown news event_types: {unknown}")

    def content_hash(self) -> str:
        return canonical_json_sha256(
            {
                "article_id": self.article_id,
                "source": self.source,
                "original_url_hash": self.original_url_hash,
                "headline_original": self.headline_original,
                "summary_or_body": self.summary_or_body,
            }
        )

    def raw_payload(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "source": self.source,
            "original_url_hash": self.original_url_hash,
            "headline_original": self.headline_original,
            "summary_or_body": self.summary_or_body,
            "currency_tags": list(self.currency_tags),
            "entity_tags": list(self.entity_tags),
            "event_types": list(self.event_types),
            "correction_flag": self.correction_flag,
            "source_id": self.source_id,
        }

    def to_pit_record(self) -> PITRecord:
        # event_time is the (possibly edited) publish time when known, but
        # availability is floored at first_seen_at by PITRecord itself, so an
        # edited-earlier publish time can never move this record's usable window.
        event_time = self.published_at or self.first_seen_at
        return PITRecord(
            source_id=self.source_id,
            instrument=self.currency_tags[0] if self.currency_tags else "GLOBAL",
            event_time=event_time,
            published_at=self.published_at,
            first_seen_at=self.first_seen_at,
            ingested_at=self.ingested_at,
            available_at=self.available_at,
            revision_id=self.article_id if self.correction_flag else None,
            raw_sha256=self.content_hash(),
            writer_id=self.writer_id,
            schema_version=1,
            payload=self.raw_payload(),
        )
