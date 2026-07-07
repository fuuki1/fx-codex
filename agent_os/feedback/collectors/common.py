"""Shared helpers for deterministic feedback collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.feedback.normalizer import candidate_from_feedback
from agent_os.feedback.schemas import FeedbackEvent
from agent_os.feedback.store import FeedbackStore
from agent_os.sessions.schemas import utc_now_iso


@dataclass(frozen=True)
class FeedbackCollectionResult:
    """Summary of a collector persistence pass."""

    session_id: str
    source: str
    event_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    skipped_items: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "event_ids": list(self.event_ids),
            "candidate_ids": list(self.candidate_ids),
            "skipped_items": [dict(item) for item in self.skipped_items],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


def persist_collected_feedback(
    feedback_store: FeedbackStore,
    *,
    session_id: str,
    source: str,
    events: list[FeedbackEvent],
    metadata: dict[str, Any] | None = None,
) -> FeedbackCollectionResult:
    """Persist collector output as FeedbackEvent and FeedbackCandidate records.

    The function is intentionally idempotent by feedback_id/candidate_id so
    rerunning a file-based collector does not duplicate review artifacts.
    """

    existing_event_ids = {event.feedback_id for event in feedback_store.read_events(session_id)}
    existing_candidate_ids = {
        candidate.candidate_id for candidate in feedback_store.read_candidates(session_id)
    }
    event_ids: list[str] = []
    candidate_ids: list[str] = []
    skipped: list[dict[str, Any]] = []

    for event in events:
        if event.session_id != session_id:
            skipped.append(
                {
                    "id": event.feedback_id,
                    "reason": f"event belongs to session {event.session_id}",
                }
            )
            continue
        if event.feedback_id in existing_event_ids:
            skipped.append({"id": event.feedback_id, "reason": "feedback event already exists"})
        else:
            feedback_store.record_event(event)
            existing_event_ids.add(event.feedback_id)
            event_ids.append(event.feedback_id)

        candidate = candidate_from_feedback(event)
        if candidate.candidate_id in existing_candidate_ids:
            skipped.append(
                {"id": candidate.candidate_id, "reason": "feedback candidate already exists"}
            )
            continue
        feedback_store.record_candidate(candidate)
        existing_candidate_ids.add(candidate.candidate_id)
        candidate_ids.append(candidate.candidate_id)

    feedback_store.session_store.append_event(
        session_id,
        "feedback_collection_finished",
        {
            "source": source,
            "events": len(event_ids),
            "candidates": len(candidate_ids),
            "skipped": len(skipped),
        },
    )
    return FeedbackCollectionResult(
        session_id=session_id,
        source=source,
        event_ids=event_ids,
        candidate_ids=candidate_ids,
        skipped_items=skipped,
        metadata=metadata or {},
    )
