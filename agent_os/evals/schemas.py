"""Eval Suite schemas for Agent OS phase 3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_os.sessions.schemas import (
    AgentOSValidationError,
    _list_of_text,
    _validate_iso_datetime,
    utc_now_iso,
)

EVAL_RUN_STATUSES = {"created", "running", "completed", "failed"}
GRADER_TYPES = {"rule", "diff", "command", "artifact"}


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
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AgentOSValidationError(f"{key} must be a list of objects")
    return [dict(item) for item in value]


def _number(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)):
        raise AgentOSValidationError(f"{key} must be numeric")
    return float(value)


def _integer(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    if not isinstance(value, int):
        raise AgentOSValidationError(f"{key} must be an integer")
    return value


def _status(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


@dataclass(frozen=True)
class EvalCase:
    """One deterministic evaluation case."""

    case_id: str
    category: str
    role: str
    input: dict[str, Any]
    expected: dict[str, Any]
    grader: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "role": self.role,
            "input": dict(self.input),
            "expected": dict(self.expected),
            "grader": dict(self.grader),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        grader = _dict_value(data, "grader")
        grader_type = str(grader.get("type", ""))
        if grader_type not in GRADER_TYPES:
            raise AgentOSValidationError(f"grader.type must be one of {sorted(GRADER_TYPES)}")
        return cls(
            case_id=_require_text(data, "case_id"),
            category=_require_text(data, "category"),
            role=_require_text(data, "role"),
            input=_dict_value(data, "input"),
            expected=_dict_value(data, "expected"),
            grader=grader,
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class EvalSuite:
    """A versioned set of EvalCases."""

    suite_id: str
    description: str
    cases: list[EvalCase]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "description": self.description,
            "cases": [case.to_dict() for case in self.cases],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalSuite:
        cases = [EvalCase.from_dict(item) for item in _list_of_dicts(data, "cases")]
        case_ids = [case.case_id for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise AgentOSValidationError("suite case_id values must be unique")
        return cls(
            suite_id=_require_text(data, "suite_id"),
            description=str(data.get("description", "")),
            cases=cases,
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class EvalResult:
    """Result for one EvalCase."""

    result_id: str
    eval_run_id: str
    case_id: str
    grader_type: str
    passed: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str = field(default_factory=utc_now_iso)
    error_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "eval_run_id": self.eval_run_id,
            "case_id": self.case_id,
            "grader_type": self.grader_type,
            "passed": self.passed,
            "score": self.score,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error_summary": self.error_summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalResult:
        passed = data.get("passed")
        if not isinstance(passed, bool):
            raise AgentOSValidationError("passed must be a boolean")
        return cls(
            result_id=_require_text(data, "result_id"),
            eval_run_id=_require_text(data, "eval_run_id"),
            case_id=_require_text(data, "case_id"),
            grader_type=_status(_require_text(data, "grader_type"), GRADER_TYPES, "grader_type"),
            passed=passed,
            score=_number(data, "score"),
            reasons=_list_of_text(data, "reasons"),
            evidence=_list_of_text(data, "evidence"),
            started_at=_validate_iso_datetime(_require_text(data, "started_at"), "started_at"),
            ended_at=_validate_iso_datetime(_require_text(data, "ended_at"), "ended_at"),
            error_summary=str(data.get("error_summary", "")),
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class EvalRun:
    """Top-level record for one suite execution."""

    eval_run_id: str
    suite_id: str
    status: str
    started_at: str
    ended_at: str | None = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    safety_pass_rate: float = 0.0
    case_results_path: str = "case_results.jsonl"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eval_run_id": self.eval_run_id,
            "suite_id": self.suite_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "safety_pass_rate": self.safety_pass_rate,
            "case_results_path": self.case_results_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalRun:
        ended_at = data.get("ended_at")
        if ended_at is not None:
            if not isinstance(ended_at, str):
                raise AgentOSValidationError("ended_at must be a string or null")
            _validate_iso_datetime(ended_at, "ended_at")
        return cls(
            eval_run_id=_require_text(data, "eval_run_id"),
            suite_id=_require_text(data, "suite_id"),
            status=_status(_require_text(data, "status"), EVAL_RUN_STATUSES, "status"),
            started_at=_validate_iso_datetime(_require_text(data, "started_at"), "started_at"),
            ended_at=ended_at,
            total=_integer(data, "total"),
            passed=_integer(data, "passed"),
            failed=_integer(data, "failed"),
            safety_pass_rate=_number(data, "safety_pass_rate"),
            case_results_path=str(data.get("case_results_path", "case_results.jsonl")),
            metadata=_dict_value(data, "metadata"),
        )
