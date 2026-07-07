"""Skill lifecycle schemas for Agent OS phase 5."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _list_of_text,
    _validate_iso_datetime,
    utc_now_iso,
)

SKILL_STATES = {"candidate", "shadow", "active", "deprecated", "retired", "rejected"}
TRANSITION_STATES = SKILL_STATES - {"candidate"}


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentOSValidationError(f"{key} must be a non-empty string")
    return value


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentOSValidationError(f"{key} must be a string or null")
    return value


def _dict_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise AgentOSValidationError(f"{key} must be an object")
    return dict(value)


def _state(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


def _version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-][A-Za-z0-9]+)?", value):
        raise AgentOSValidationError("version must be semver-like, for example 0.1.0")
    return value


@dataclass(frozen=True)
class SkillRecord:
    """Versioned Skill lifecycle record.

    A record can only be created in candidate state. Promotion to active is
    enforced by SkillRegistry.transition with eval and shadow evidence.
    """

    skill_id: str
    version: str
    state: str
    title: str
    summary: str
    owner: str
    created_from_feedback_ids: list[str] = field(default_factory=list)
    required_eval_ids: list[str] = field(default_factory=list)
    shadow_run_ids: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    previous_version: str | None = None
    body_path: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    review_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "state": self.state,
            "title": self.title,
            "summary": self.summary,
            "owner": self.owner,
            "created_from_feedback_ids": list(self.created_from_feedback_ids),
            "required_eval_ids": list(self.required_eval_ids),
            "shadow_run_ids": list(self.shadow_run_ids),
            "evidence_paths": list(self.evidence_paths),
            "previous_version": self.previous_version,
            "body_path": self.body_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillRecord:
        previous_version = _optional_text(data, "previous_version")
        if previous_version is not None:
            _version(previous_version)
        reviewed_at = _optional_text(data, "reviewed_at")
        if reviewed_at is not None:
            _validate_iso_datetime(reviewed_at, "reviewed_at")
        return cls(
            skill_id=_require_text(data, "skill_id"),
            version=_version(_require_text(data, "version")),
            state=_state(_require_text(data, "state"), SKILL_STATES, "state"),
            title=_require_text(data, "title"),
            summary=_require_text(data, "summary"),
            owner=_require_text(data, "owner"),
            created_from_feedback_ids=_list_of_text(data, "created_from_feedback_ids"),
            required_eval_ids=_list_of_text(data, "required_eval_ids"),
            shadow_run_ids=_list_of_text(data, "shadow_run_ids"),
            evidence_paths=_list_of_text(data, "evidence_paths"),
            previous_version=previous_version,
            body_path=_optional_text(data, "body_path"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            updated_at=_validate_iso_datetime(_require_text(data, "updated_at"), "updated_at"),
            reviewed_at=reviewed_at,
            reviewed_by=_optional_text(data, "reviewed_by"),
            review_note=str(data.get("review_note", "")),
            metadata=_dict_value(data, "metadata"),
        )

    def with_transition(
        self,
        *,
        state: str,
        actor: str,
        evidence_paths: list[str],
        eval_run_id: str | None = None,
        shadow_run_id: str | None = None,
        review_note: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SkillRecord:
        now = utc_now_iso()
        required_eval_ids = list(self.required_eval_ids)
        if eval_run_id and eval_run_id not in required_eval_ids:
            required_eval_ids.append(eval_run_id)
        shadow_run_ids = list(self.shadow_run_ids)
        if shadow_run_id and shadow_run_id not in shadow_run_ids:
            shadow_run_ids.append(shadow_run_id)
        merged_evidence = list(dict.fromkeys([*self.evidence_paths, *evidence_paths]))
        merged_metadata = dict(self.metadata)
        if metadata:
            merged_metadata.update(metadata)
        return SkillRecord(
            skill_id=self.skill_id,
            version=self.version,
            state=_state(state, SKILL_STATES, "state"),
            title=self.title,
            summary=self.summary,
            owner=self.owner,
            created_from_feedback_ids=list(self.created_from_feedback_ids),
            required_eval_ids=required_eval_ids,
            shadow_run_ids=shadow_run_ids,
            evidence_paths=merged_evidence,
            previous_version=self.previous_version,
            body_path=self.body_path,
            created_at=self.created_at,
            updated_at=now,
            reviewed_at=now,
            reviewed_by=actor,
            review_note=review_note,
            metadata=merged_metadata,
        )


@dataclass(frozen=True)
class SkillTransition:
    """Audit record for a Skill lifecycle transition."""

    transition_id: str
    skill_id: str
    version: str
    from_state: str
    to_state: str
    actor: str
    reason: str
    evidence_paths: list[str] = field(default_factory=list)
    eval_run_id: str | None = None
    shadow_run_id: str | None = None
    ts: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor": self.actor,
            "reason": self.reason,
            "evidence_paths": list(self.evidence_paths),
            "eval_run_id": self.eval_run_id,
            "shadow_run_id": self.shadow_run_id,
            "ts": self.ts,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillTransition:
        return cls(
            transition_id=_require_text(data, "transition_id"),
            skill_id=_require_text(data, "skill_id"),
            version=_version(_require_text(data, "version")),
            from_state=_state(_require_text(data, "from_state"), SKILL_STATES, "from_state"),
            to_state=_state(_require_text(data, "to_state"), TRANSITION_STATES, "to_state"),
            actor=_require_text(data, "actor"),
            reason=_require_text(data, "reason"),
            evidence_paths=_list_of_text(data, "evidence_paths"),
            eval_run_id=_optional_text(data, "eval_run_id"),
            shadow_run_id=_optional_text(data, "shadow_run_id"),
            ts=_validate_iso_datetime(_require_text(data, "ts"), "ts"),
            metadata=_dict_value(data, "metadata"),
        )
