"""Filesystem-backed Agent OS session store."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import (
    AgentOSValidationError,
    AgentSession,
    DecisionRecord,
    EnvironmentSnapshot,
    GitSnapshot,
    ToolCall,
    path_to_text,
    utc_now_iso,
)


class AgentOSStorageError(RuntimeError):
    """Raised when a session artifact cannot be read or written."""


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return slug[:48] or "agent"


def _run_git(repo: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def snapshot_git(repo: Path) -> GitSnapshot:
    branch = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = _run_git(repo, ["rev-parse", "HEAD"])
    status_text = _run_git(repo, ["status", "--short"])
    status_short = [line for line in status_text.splitlines() if line]
    return GitSnapshot(
        branch=branch, head=head, dirty=bool(status_short), status_short=status_short
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def snapshot_environment(repo: Path) -> EnvironmentSnapshot:
    dependency_files: dict[str, str] = {}
    for name in ("pyproject.toml", "requirements.lock", "requirements.txt"):
        path = repo / name
        if path.is_file():
            try:
                dependency_files[name] = _sha256_file(path)
            except OSError:
                dependency_files[name] = "unreadable"
    return EnvironmentSnapshot(
        python=sys.version.split()[0],
        platform=platform.platform(),
        cwd=path_to_text(Path.cwd()),
        timezone=os.environ.get("TZ", ""),
        dependency_files=dependency_files,
    )


class AgentSessionStore:
    """Persist Agent OS sessions as JSON and JSONL files.

    The store root should normally be `runs/agent_sessions`. Each session has a
    dedicated directory so later phases can attach artifacts without changing the
    phase 1 schema.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def create_session(
        self,
        *,
        role: str,
        user_request: str,
        repo: str | Path,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AgentSession:
        repo_path = Path(repo).expanduser().resolve()
        now = utc_now_iso()
        session = AgentSession(
            session_id=session_id or self.new_session_id(role),
            role=role,
            user_request=user_request,
            repo=str(repo_path),
            created_at=now,
            updated_at=now,
            status="created",
            git=snapshot_git(repo_path),
            environment=snapshot_environment(repo_path),
            metadata=metadata or {},
        )
        self._ensure_session_dir(session.session_id)
        self.save_session(session)
        self._write_text(session.session_id, "transcript.md", "")
        self._ensure_session_dir(session.session_id, "artifacts")
        self.append_event(
            session.session_id,
            "session_created",
            {"role": role, "repo": str(repo_path), "dirty": session.git.dirty},
        )
        return session

    @staticmethod
    def new_session_id(role: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        return f"agent_{timestamp}_{_slug(role)}"

    def session_dir(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.root / session_id

    def artifact_dir(self, session_id: str) -> Path:
        path = self.session_dir(session_id) / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_session(self, session: AgentSession) -> None:
        # Validate by round-tripping through the schema before writing.
        session = AgentSession.from_dict(session.to_dict())
        self._write_json(session.session_id, "session.json", session.to_dict())

    def load_session(self, session_id: str) -> AgentSession:
        data = self._read_json(session_id, "session.json")
        try:
            return AgentSession.from_dict(data)
        except AgentOSValidationError as exc:
            raise AgentOSStorageError(
                f"Invalid session schema in {self.session_dir(session_id) / 'session.json'}: {exc}"
            ) from exc

    def finish_session(self, session_id: str, status: str, *, reason: str = "") -> AgentSession:
        if status not in {"completed", "failed", "blocked", "archived"}:
            raise AgentOSValidationError(
                "finish status must be one of ['archived', 'blocked', 'completed', 'failed']"
            )
        session = self.load_session(session_id)
        updated = session.with_status(
            status, metadata={"finish_reason": reason} if reason else None
        )
        self.save_session(updated)
        self.append_event(session_id, "session_finished", {"status": status, "reason": reason})
        return updated

    def record_tool_call(self, call: ToolCall) -> None:
        ToolCall.from_dict(call.to_dict())
        self._append_jsonl(call.session_id, "tools.jsonl", call.to_dict())
        self.append_event(
            call.session_id,
            "tool_recorded",
            {"tool_call_id": call.tool_call_id, "tool": call.tool, "status": call.status},
        )

    def read_tool_calls(self, session_id: str) -> list[ToolCall]:
        return [
            ToolCall.from_dict(item)
            for item in self._read_jsonl(session_id, "tools.jsonl", missing_ok=True)
        ]

    def record_decision(self, decision: DecisionRecord) -> None:
        DecisionRecord.from_dict(decision.to_dict())
        self._append_jsonl(decision.session_id, "decisions.jsonl", decision.to_dict())
        self.append_event(
            decision.session_id,
            "decision_recorded",
            {
                "decision_id": decision.decision_id,
                "policy_result": decision.policy_result,
                "action": decision.action,
            },
        )

    def read_decisions(self, session_id: str) -> list[DecisionRecord]:
        return [
            DecisionRecord.from_dict(item)
            for item in self._read_jsonl(session_id, "decisions.jsonl", missing_ok=True)
        ]

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if not event_type:
            raise AgentOSValidationError("event_type must be non-empty")
        self._append_jsonl(
            session_id,
            "events.jsonl",
            {"ts": utc_now_iso(), "event": event_type, "payload": dict(payload)},
        )

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(session_id, "events.jsonl", missing_ok=True)

    def _ensure_session_dir(self, session_id: str, *parts: str) -> Path:
        path = self.session_dir(session_id).joinpath(*parts)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to create {path}: {exc}") from exc
        return path

    def _write_json(self, session_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_session_dir(session_id)
        path = self.session_dir(session_id) / name
        try:
            path.write_text(
                json.dumps(
                    data, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _read_json(self, session_id: str, name: str) -> dict[str, Any]:
        path = self.session_dir(session_id) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AgentOSStorageError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentOSStorageError(f"Expected JSON object in {path}")
        return data

    def _append_jsonl(self, session_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_session_dir(session_id)
        path = self.session_dir(session_id) / name
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(data, ensure_ascii=False, sort_keys=True, default=_json_default)
                    + "\n"
                )
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {path}: {exc}") from exc

    def _read_jsonl(self, session_id: str, name: str, *, missing_ok: bool) -> list[dict[str, Any]]:
        path = self.session_dir(session_id) / name
        if missing_ok and not path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {path}:{line_no}")
            records.append(item)
        return records

    def _write_text(self, session_id: str, name: str, text: str) -> None:
        self._ensure_session_dir(session_id)
        path = self.session_dir(session_id) / name
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id):
            raise AgentOSValidationError("session_id contains unsafe characters")
