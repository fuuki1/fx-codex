"""Agent role and handoff schemas for Agent OS specialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _dict_value,
    _list_of_text,
    _optional_text,
    _require_text,
    _validate_iso_datetime,
    utc_now_iso,
)

HANDOFF_STATUSES = {"proposed", "accepted", "completed", "rejected", "blocked"}
WORK_PLAN_STATUSES = {"planned", "active", "completed", "blocked", "cancelled"}
HANDOFF_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected", "blocked"},
    "accepted": {"completed", "blocked"},
    "blocked": {"accepted", "rejected"},
    "completed": set(),
    "rejected": set(),
}
WORK_PLAN_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active", "blocked", "cancelled"},
    "active": {"completed", "blocked", "cancelled"},
    "blocked": {"active", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _status(value: str) -> str:
    if value not in HANDOFF_STATUSES:
        raise AgentOSValidationError(f"status must be one of {sorted(HANDOFF_STATUSES)}")
    return value


def _work_plan_status(value: str) -> str:
    if value not in WORK_PLAN_STATUSES:
        raise AgentOSValidationError(f"status must be one of {sorted(WORK_PLAN_STATUSES)}")
    return value


def _optional_iso(data: dict[str, Any], key: str) -> str | None:
    value = _optional_text(data, key)
    if value is not None:
        _validate_iso_datetime(value, key)
    return value


@dataclass(frozen=True)
class AgentSpec:
    """Static role contract for a specialized Agent OS role."""

    role: str
    description: str
    prompt_version: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    handoff_targets: list[str] = field(default_factory=list)
    approval_required_for: list[str] = field(default_factory=list)
    outputs_required: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "description": self.description,
            "prompt_version": self.prompt_version,
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "handoff_targets": list(self.handoff_targets),
            "approval_required_for": list(self.approval_required_for),
            "outputs_required": list(self.outputs_required),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSpec:
        spec = cls(
            role=_require_text(data, "role"),
            description=_require_text(data, "description"),
            prompt_version=_require_text(data, "prompt_version"),
            allowed_tools=_list_of_text(data, "allowed_tools"),
            denied_tools=_list_of_text(data, "denied_tools"),
            handoff_targets=_list_of_text(data, "handoff_targets"),
            approval_required_for=_list_of_text(data, "approval_required_for"),
            outputs_required=_list_of_text(data, "outputs_required"),
            metadata=_dict_value(data, "metadata"),
        )
        overlap = sorted(set(spec.allowed_tools).intersection(spec.denied_tools))
        if overlap:
            raise AgentOSValidationError(f"allowed_tools and denied_tools overlap: {overlap}")
        return spec


@dataclass(frozen=True)
class HandoffContract:
    """Session-scoped handoff between specialized roles."""

    handoff_id: str
    session_id: str
    from_role: str
    to_role: str
    approval_role: str
    task: str
    context_summary: str
    acceptance_criteria: list[str]
    required_artifacts: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    status: str = "proposed"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    review_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "session_id": self.session_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "approval_role": self.approval_role,
            "task": self.task,
            "context_summary": self.context_summary,
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_artifacts": list(self.required_artifacts),
            "blocked_actions": list(self.blocked_actions),
            "evidence_paths": list(self.evidence_paths),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffContract:
        contract = cls(
            handoff_id=_require_text(data, "handoff_id"),
            session_id=_require_text(data, "session_id"),
            from_role=_require_text(data, "from_role"),
            to_role=_require_text(data, "to_role"),
            approval_role=_require_text(data, "approval_role"),
            task=_require_text(data, "task"),
            context_summary=_require_text(data, "context_summary"),
            acceptance_criteria=_list_of_text(data, "acceptance_criteria"),
            required_artifacts=_list_of_text(data, "required_artifacts"),
            blocked_actions=_list_of_text(data, "blocked_actions"),
            evidence_paths=_list_of_text(data, "evidence_paths"),
            status=_status(str(data.get("status", "proposed"))),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            updated_at=_validate_iso_datetime(_require_text(data, "updated_at"), "updated_at"),
            reviewed_at=_optional_iso(data, "reviewed_at"),
            reviewed_by=_optional_text(data, "reviewed_by"),
            review_note=str(data.get("review_note", "")),
            metadata=_dict_value(data, "metadata"),
        )
        contract._validate_role_separation()
        if not contract.acceptance_criteria:
            raise AgentOSValidationError("acceptance_criteria must be non-empty")
        return contract

    def with_status(
        self,
        *,
        status: str,
        actor: str,
        reason: str,
        evidence_paths: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> HandoffContract:
        next_status = _status(status)
        if next_status not in HANDOFF_TRANSITIONS[self.status]:
            raise AgentOSValidationError(
                f"cannot transition handoff {self.status} -> {next_status}"
            )
        if not actor.strip():
            raise AgentOSValidationError("actor is required for handoff transitions")
        if not reason.strip():
            raise AgentOSValidationError("reason is required for handoff transitions")
        if next_status == "completed" and not evidence_paths:
            raise AgentOSValidationError("evidence_paths are required to complete a handoff")
        merged_evidence = list(dict.fromkeys([*self.evidence_paths, *evidence_paths]))
        merged_metadata = dict(self.metadata)
        if metadata:
            merged_metadata.update(metadata)
        now = utc_now_iso()
        return HandoffContract(
            handoff_id=self.handoff_id,
            session_id=self.session_id,
            from_role=self.from_role,
            to_role=self.to_role,
            approval_role=self.approval_role,
            task=self.task,
            context_summary=self.context_summary,
            acceptance_criteria=list(self.acceptance_criteria),
            required_artifacts=list(self.required_artifacts),
            blocked_actions=list(self.blocked_actions),
            evidence_paths=merged_evidence,
            status=next_status,
            created_at=self.created_at,
            updated_at=now,
            reviewed_at=now,
            reviewed_by=actor,
            review_note=reason,
            metadata=merged_metadata,
        )

    def _validate_role_separation(self) -> None:
        if self.from_role == self.to_role:
            raise AgentOSValidationError("from_role and to_role must be different")
        if self.approval_role == self.to_role:
            raise AgentOSValidationError("approval_role must be separate from implementation role")
        if self.approval_role == self.from_role:
            raise AgentOSValidationError("approval_role must be separate from delegating role")


@dataclass(frozen=True)
class AgentWorkPlan:
    """Session-scoped plan that groups multiple role handoffs."""

    plan_id: str
    session_id: str
    objective: str
    owner_role: str
    handoff_ids: list[str]
    completion_criteria: list[str]
    risk_level: str = "medium"
    status: str = "planned"
    evidence_paths: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    review_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "objective": self.objective,
            "owner_role": self.owner_role,
            "handoff_ids": list(self.handoff_ids),
            "completion_criteria": list(self.completion_criteria),
            "risk_level": self.risk_level,
            "status": self.status,
            "evidence_paths": list(self.evidence_paths),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentWorkPlan:
        plan = cls(
            plan_id=_require_text(data, "plan_id"),
            session_id=_require_text(data, "session_id"),
            objective=_require_text(data, "objective"),
            owner_role=_require_text(data, "owner_role"),
            handoff_ids=_list_of_text(data, "handoff_ids"),
            completion_criteria=_list_of_text(data, "completion_criteria"),
            risk_level=str(data.get("risk_level", "medium")),
            status=_work_plan_status(str(data.get("status", "planned"))),
            evidence_paths=_list_of_text(data, "evidence_paths"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            updated_at=_validate_iso_datetime(_require_text(data, "updated_at"), "updated_at"),
            reviewed_at=_optional_iso(data, "reviewed_at"),
            reviewed_by=_optional_text(data, "reviewed_by"),
            review_note=str(data.get("review_note", "")),
            metadata=_dict_value(data, "metadata"),
        )
        if not plan.handoff_ids:
            raise AgentOSValidationError("handoff_ids must be non-empty")
        if not plan.completion_criteria:
            raise AgentOSValidationError("completion_criteria must be non-empty")
        return plan

    def with_status(
        self,
        *,
        status: str,
        actor: str,
        reason: str,
        evidence_paths: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> AgentWorkPlan:
        next_status = _work_plan_status(status)
        if next_status not in WORK_PLAN_TRANSITIONS[self.status]:
            raise AgentOSValidationError(
                f"cannot transition work plan {self.status} -> {next_status}"
            )
        if not actor.strip():
            raise AgentOSValidationError("actor is required for work plan transitions")
        if not reason.strip():
            raise AgentOSValidationError("reason is required for work plan transitions")
        if next_status == "completed" and not evidence_paths:
            raise AgentOSValidationError("evidence_paths are required to complete a work plan")
        merged_evidence = list(dict.fromkeys([*self.evidence_paths, *evidence_paths]))
        merged_metadata = dict(self.metadata)
        if metadata:
            merged_metadata.update(metadata)
        now = utc_now_iso()
        return AgentWorkPlan(
            plan_id=self.plan_id,
            session_id=self.session_id,
            objective=self.objective,
            owner_role=self.owner_role,
            handoff_ids=list(self.handoff_ids),
            completion_criteria=list(self.completion_criteria),
            risk_level=self.risk_level,
            status=next_status,
            evidence_paths=merged_evidence,
            created_at=self.created_at,
            updated_at=now,
            reviewed_at=now,
            reviewed_by=actor,
            review_note=reason,
            metadata=merged_metadata,
        )
