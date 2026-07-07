"""Core Agent OS session schemas.

Phase 1 deliberately uses dataclasses and JSON-compatible dictionaries instead
of an external validation dependency. Later phases can wrap these records with
stricter validators without changing the on-disk schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SESSION_STATUSES = {
    "created",
    "context_loaded",
    "planned",
    "running",
    "verifying",
    "completed",
    "failed",
    "blocked",
    "archived",
}
TOOL_STATUSES = {"success", "failed", "blocked", "skipped"}
POLICY_RESULTS = {"allow", "deny", "approval_required"}


class AgentOSValidationError(ValueError):
    """Raised when a persisted Agent OS record does not match the schema."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def _list_of_text(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentOSValidationError(f"{key} must be a list of strings")
    return list(value)


def _validate_iso_datetime(value: str, key: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise AgentOSValidationError(f"{key} must be an ISO datetime") from exc
    return value


def _validate_status(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise AgentOSValidationError(f"{key} must be one of {sorted(allowed)}")
    return value


@dataclass(frozen=True)
class GitSnapshot:
    """Best-effort git state captured when a session starts."""

    branch: str = ""
    head: str = ""
    dirty: bool = False
    status_short: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
            "status_short": list(self.status_short),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GitSnapshot:
        if not isinstance(data.get("dirty", False), bool):
            raise AgentOSValidationError("git.dirty must be a boolean")
        return cls(
            branch=str(data.get("branch", "")),
            head=str(data.get("head", "")),
            dirty=bool(data.get("dirty", False)),
            status_short=_list_of_text(data, "status_short"),
        )


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """Local environment facts that explain why a session behaved as it did."""

    python: str = ""
    platform: str = ""
    cwd: str = ""
    timezone: str = ""
    dependency_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "platform": self.platform,
            "cwd": self.cwd,
            "timezone": self.timezone,
            "dependency_files": dict(self.dependency_files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnvironmentSnapshot:
        dependency_files = data.get("dependency_files", {})
        if not isinstance(dependency_files, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in dependency_files.items()
        ):
            raise AgentOSValidationError("environment.dependency_files must be a string map")
        return cls(
            python=str(data.get("python", "")),
            platform=str(data.get("platform", "")),
            cwd=str(data.get("cwd", "")),
            timezone=str(data.get("timezone", "")),
            dependency_files=dict(dependency_files),
        )


@dataclass(frozen=True)
class AgentSession:
    """Top-level record for one agent work unit."""

    session_id: str
    role: str
    user_request: str
    repo: str
    created_at: str
    updated_at: str
    status: str = "created"
    git: GitSnapshot = field(default_factory=GitSnapshot)
    environment: EnvironmentSnapshot = field(default_factory=EnvironmentSnapshot)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "role": self.role,
            "user_request": self.user_request,
            "repo": self.repo,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "git": self.git.to_dict(),
            "environment": self.environment.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSession:
        created_at = _validate_iso_datetime(_require_text(data, "created_at"), "created_at")
        updated_at = _validate_iso_datetime(_require_text(data, "updated_at"), "updated_at")
        return cls(
            session_id=_require_text(data, "session_id"),
            role=_require_text(data, "role"),
            user_request=_require_text(data, "user_request"),
            repo=_require_text(data, "repo"),
            created_at=created_at,
            updated_at=updated_at,
            status=_validate_status(str(data.get("status", "created")), SESSION_STATUSES, "status"),
            git=GitSnapshot.from_dict(_dict_value(data, "git")),
            environment=EnvironmentSnapshot.from_dict(_dict_value(data, "environment")),
            metadata=_dict_value(data, "metadata"),
        )

    def with_status(self, status: str, *, metadata: dict[str, Any] | None = None) -> AgentSession:
        next_metadata = dict(self.metadata)
        if metadata:
            next_metadata.update(metadata)
        return AgentSession(
            session_id=self.session_id,
            role=self.role,
            user_request=self.user_request,
            repo=self.repo,
            created_at=self.created_at,
            updated_at=utc_now_iso(),
            status=_validate_status(status, SESSION_STATUSES, "status"),
            git=self.git,
            environment=self.environment,
            metadata=next_metadata,
        )


@dataclass(frozen=True)
class ToolCall:
    """Audit record for a tool call with side effects or diagnostic value."""

    tool_call_id: str
    session_id: str
    tool: str
    started_at: str
    cwd: str
    input_redacted: dict[str, Any] = field(default_factory=dict)
    ended_at: str | None = None
    exit_code: int | None = None
    stdout_summary: str = ""
    stderr_summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    approval_id: str | None = None
    status: str = "success"
    error_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cwd": self.cwd,
            "input_redacted": dict(self.input_redacted),
            "exit_code": self.exit_code,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "changed_files": list(self.changed_files),
            "approval_id": self.approval_id,
            "status": self.status,
            "error_summary": self.error_summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        exit_code = data.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            raise AgentOSValidationError("exit_code must be an integer or null")
        ended_at = _optional_text(data, "ended_at")
        if ended_at is not None:
            _validate_iso_datetime(ended_at, "ended_at")
        return cls(
            tool_call_id=_require_text(data, "tool_call_id"),
            session_id=_require_text(data, "session_id"),
            tool=_require_text(data, "tool"),
            started_at=_validate_iso_datetime(_require_text(data, "started_at"), "started_at"),
            ended_at=ended_at,
            cwd=_require_text(data, "cwd"),
            input_redacted=_dict_value(data, "input_redacted"),
            exit_code=exit_code,
            stdout_summary=str(data.get("stdout_summary", "")),
            stderr_summary=str(data.get("stderr_summary", "")),
            changed_files=_list_of_text(data, "changed_files"),
            approval_id=_optional_text(data, "approval_id"),
            status=_validate_status(str(data.get("status", "success")), TOOL_STATUSES, "status"),
            error_summary=str(data.get("error_summary", "")),
            metadata=_dict_value(data, "metadata"),
        )


@dataclass(frozen=True)
class DecisionRecord:
    """Compact, auditable record of a policy or engineering decision."""

    decision_id: str
    session_id: str
    ts: str
    actor: str
    action: str
    policy_result: str
    rationale: str
    evidence_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "policy_result": self.policy_result,
            "rationale": self.rationale,
            "evidence_paths": list(self.evidence_paths),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRecord:
        return cls(
            decision_id=_require_text(data, "decision_id"),
            session_id=_require_text(data, "session_id"),
            ts=_validate_iso_datetime(_require_text(data, "ts"), "ts"),
            actor=_require_text(data, "actor"),
            action=_require_text(data, "action"),
            policy_result=_validate_status(
                _require_text(data, "policy_result"), POLICY_RESULTS, "policy_result"
            ),
            rationale=_require_text(data, "rationale"),
            evidence_paths=_list_of_text(data, "evidence_paths"),
            metadata=_dict_value(data, "metadata"),
        )


def path_to_text(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
