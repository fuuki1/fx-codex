"""Candidate artifact schemas for Agent OS memory and skill review flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _list_of_text,
    _validate_iso_datetime,
    utc_now_iso,
)

MEMORY_TARGETS = {"memory", "runbook", "tool_policy", "decision_rule"}
CANDIDATE_STATUSES = {"candidate", "accepted", "rejected", "resolved"}


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentOSValidationError(f"{key} must be a non-empty string")
    return value


def _dict_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise AgentOSValidationError(f"{key} must be an object")
    return dict(value)


def _list_of_dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise AgentOSValidationError(f"{key} must be a list")
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AgentOSValidationError(f"{key} must contain objects")
        records.append(dict(item))
    return records


def _one_of(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


@dataclass(frozen=True)
class MemoryCandidate:
    """Reviewable candidate for durable memory-like Agent OS knowledge."""

    candidate_id: str
    session_id: str
    target: str
    title: str
    body: str
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
            "body": self.body,
            "source_feedback_ids": list(self.source_feedback_ids),
            "evidence": list(self.evidence),
            "status": self.status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryCandidate:
        return cls(
            candidate_id=_require_text(data, "candidate_id"),
            session_id=_require_text(data, "session_id"),
            target=_one_of(_require_text(data, "target"), MEMORY_TARGETS, "target"),
            title=_require_text(data, "title"),
            body=_require_text(data, "body"),
            source_feedback_ids=_list_of_text(data, "source_feedback_ids"),
            evidence=_list_of_text(data, "evidence"),
            status=_one_of(str(data.get("status", "candidate")), CANDIDATE_STATUSES, "status"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class CandidateMaterialization:
    """Audit summary for one candidate artifact materialization pass."""

    materialization_id: str
    session_id: str
    memory_candidate_ids: list[str] = field(default_factory=list)
    skill_candidate_paths: list[str] = field(default_factory=list)
    skipped_candidates: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "materialization_id": self.materialization_id,
            "session_id": self.session_id,
            "memory_candidate_ids": list(self.memory_candidate_ids),
            "skill_candidate_paths": list(self.skill_candidate_paths),
            "skipped_candidates": [dict(item) for item in self.skipped_candidates],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateMaterialization:
        return cls(
            materialization_id=_require_text(data, "materialization_id"),
            session_id=_require_text(data, "session_id"),
            memory_candidate_ids=_list_of_text(data, "memory_candidate_ids"),
            skill_candidate_paths=_list_of_text(data, "skill_candidate_paths"),
            skipped_candidates=_list_of_dicts(data, "skipped_candidates"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            metadata=_dict_value(data, "metadata"),
        )
