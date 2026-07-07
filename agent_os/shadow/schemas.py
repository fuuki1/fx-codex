"""Shadow Mode schemas for Agent OS phase 4."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _list_of_text,
    _validate_iso_datetime,
    utc_now_iso,
)

SHADOW_STATUSES = {"created", "running", "completed", "failed", "blocked"}
SHADOW_OUTCOMES = {"better", "same", "worse", "blocked"}
PROHIBITED_SIDE_EFFECTS = {"local_write", "external_write", "production_change", "live_trade"}
ALLOWED_SIDE_EFFECTS = {"read_only", "dry_run", "synthetic_diff", "eval_replay", "proposal"}


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


def _list_of_dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AgentOSValidationError(f"{key} must be a list of objects")
    return [dict(item) for item in value]


def _number_map(data: dict[str, Any], key: str) -> dict[str, float]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise AgentOSValidationError(f"{key} must be an object")
    result: dict[str, float] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, (int, float)):
            raise AgentOSValidationError(f"{key} must be a numeric map")
        result[item_key] = float(item_value)
    return result


def _status(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


@dataclass(frozen=True)
class ShadowProposal:
    """A no-side-effect candidate output for a real session context."""

    candidate: str
    base_session_id: str
    plan: str
    proposed_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    synthetic_diff: str = ""
    eval_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "base_session_id": self.base_session_id,
            "plan": self.plan,
            "proposed_tool_calls": [dict(item) for item in self.proposed_tool_calls],
            "synthetic_diff": self.synthetic_diff,
            "eval_run_id": self.eval_run_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShadowProposal:
        return cls(
            candidate=_require_text(data, "candidate"),
            base_session_id=_require_text(data, "base_session_id"),
            plan=str(data.get("plan", "")),
            proposed_tool_calls=_list_of_dicts(data, "proposed_tool_calls"),
            synthetic_diff=str(data.get("synthetic_diff", "")),
            eval_run_id=_optional_text(data, "eval_run_id"),
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class ShadowRun:
    """Top-level record for one Shadow Mode comparison."""

    shadow_run_id: str
    base_session_id: str
    candidate: str
    status: str
    started_at: str
    ended_at: str | None = None
    proposal_path: str = "shadow_proposal.json"
    report_path: str = "shadow_report.json"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_run_id": self.shadow_run_id,
            "base_session_id": self.base_session_id,
            "candidate": self.candidate,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "proposal_path": self.proposal_path,
            "report_path": self.report_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShadowRun:
        ended_at = _optional_text(data, "ended_at")
        if ended_at is not None:
            _validate_iso_datetime(ended_at, "ended_at")
        return cls(
            shadow_run_id=_require_text(data, "shadow_run_id"),
            base_session_id=_require_text(data, "base_session_id"),
            candidate=_require_text(data, "candidate"),
            status=_status(_require_text(data, "status"), SHADOW_STATUSES, "status"),
            started_at=_validate_iso_datetime(_require_text(data, "started_at"), "started_at"),
            ended_at=ended_at,
            proposal_path=str(data.get("proposal_path", "shadow_proposal.json")),
            report_path=str(data.get("report_path", "shadow_report.json")),
            metadata=_dict_value(data, "metadata"),
        )

    def with_status(self, status: str) -> ShadowRun:
        return ShadowRun(
            shadow_run_id=self.shadow_run_id,
            base_session_id=self.base_session_id,
            candidate=self.candidate,
            status=_status(status, SHADOW_STATUSES, "status"),
            started_at=self.started_at,
            ended_at=utc_now_iso(),
            proposal_path=self.proposal_path,
            report_path=self.report_path,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class ShadowReport:
    """Comparison result between current session behavior and a shadow candidate."""

    shadow_run_id: str
    base_session_id: str
    candidate: str
    outcome: str
    scores: dict[str, float]
    notable_differences: list[str] = field(default_factory=list)
    promotion_recommendation: str = ""
    blocked_reasons: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_run_id": self.shadow_run_id,
            "base_session_id": self.base_session_id,
            "candidate": self.candidate,
            "outcome": self.outcome,
            "scores": dict(self.scores),
            "notable_differences": list(self.notable_differences),
            "promotion_recommendation": self.promotion_recommendation,
            "blocked_reasons": list(self.blocked_reasons),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShadowReport:
        return cls(
            shadow_run_id=_require_text(data, "shadow_run_id"),
            base_session_id=_require_text(data, "base_session_id"),
            candidate=_require_text(data, "candidate"),
            outcome=_status(_require_text(data, "outcome"), SHADOW_OUTCOMES, "outcome"),
            scores=_number_map(data, "scores"),
            notable_differences=_list_of_text(data, "notable_differences"),
            promotion_recommendation=str(data.get("promotion_recommendation", "")),
            blocked_reasons=_list_of_text(data, "blocked_reasons"),
            created_at=_validate_iso_datetime(_require_text(data, "created_at"), "created_at"),
            metadata=_dict_value(data, "metadata"),
        )
