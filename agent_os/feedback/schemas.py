"""Feedback schemas for Agent OS phase 2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _list_of_text,
    _validate_iso_datetime,
    utc_now_iso,
)

FEEDBACK_SEVERITIES = {"low", "medium", "high", "critical"}
FEEDBACK_STATUSES = {"candidate", "accepted", "rejected", "resolved"}
CANDIDATE_TARGETS = {"memory", "skill", "issue", "runbook", "tool_policy", "decision_rule"}


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentOSValidationError(f"{key} must be a non-empty string")
    return value


def _optional_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise AgentOSValidationError(f"{key} must be a string")
    return value


def _dict_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise AgentOSValidationError(f"{key} must be an object")
    return dict(value)


def _one_of(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


@dataclass(frozen=True)
class FeedbackEvent:
    """Normalized feedback from tests, reviews, logs, or user corrections."""

    feedback_id: str
    source: str
    session_id: str
    severity: str
    category: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    root_cause: str = ""
    recommended_change: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "source": self.source,
            "session_id": self.session_id,
            "severity": self.severity,
            "category": self.category,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "root_cause": self.root_cause,
            "recommended_change": dict(self.recommended_change),
            "status": self.status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeedbackEvent:
        return cls(
            feedback_id=_require_text(data, "feedback_id"),
            source=_require_text(data, "source"),
            session_id=_require_text(data, "session_id"),
            severity=_one_of(_require_text(data, "severity"), FEEDBACK_SEVERITIES, "severity"),
            category=_require_text(data, "category"),
            summary=_require_text(data, "summary"),
            evidence=_list_of_text(data, "evidence"),
            root_cause=_optional_text(data, "root_cause"),
            recommended_change=_dict_value(data, "recommended_change"),
            status=_one_of(str(data.get("status", "candidate")), FEEDBACK_STATUSES, "status"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class FeedbackCandidate:
    """Reviewable candidate derived from one or more FeedbackEvents."""

    candidate_id: str
    session_id: str
    target: str
    title: str
    rationale: str
    source_feedback_ids: list[str]
    evidence: list[str] = field(default_factory=list)
    status: str = "candidate"
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "session_id": self.session_id,
            "target": self.target,
            "title": self.title,
            "rationale": self.rationale,
            "source_feedback_ids": list(self.source_feedback_ids),
            "evidence": list(self.evidence),
            "status": self.status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeedbackCandidate:
        return cls(
            candidate_id=_require_text(data, "candidate_id"),
            session_id=_require_text(data, "session_id"),
            target=_one_of(_require_text(data, "target"), CANDIDATE_TARGETS, "target"),
            title=_require_text(data, "title"),
            rationale=_require_text(data, "rationale"),
            source_feedback_ids=_list_of_text(data, "source_feedback_ids"),
            evidence=_list_of_text(data, "evidence"),
            status=_one_of(str(data.get("status", "candidate")), FEEDBACK_STATUSES, "status"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            metadata=_dict_value(data, "metadata"),
        )
