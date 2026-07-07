"""Filesystem persistence for FeedbackEvent and FeedbackCandidate records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import FeedbackCandidate, FeedbackEvent
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


class FeedbackStore:
    """Store feedback records inside an existing AgentSession directory."""

    def __init__(self, session_store: AgentSessionStore):
        self.session_store = session_store

    def record_event(self, event: FeedbackEvent) -> None:
        FeedbackEvent.from_dict(event.to_dict())
        self._append_jsonl(event.session_id, "feedback_events.jsonl", event.to_dict())
        self.session_store.append_event(
            event.session_id,
            "feedback_event_recorded",
            {
                "feedback_id": event.feedback_id,
                "source": event.source,
                "category": event.category,
                "severity": event.severity,
            },
        )

    def read_events(self, session_id: str) -> list[FeedbackEvent]:
        return [
            FeedbackEvent.from_dict(item)
            for item in self._read_jsonl(session_id, "feedback_events.jsonl", missing_ok=True)
        ]

    def record_candidate(self, candidate: FeedbackCandidate) -> None:
        FeedbackCandidate.from_dict(candidate.to_dict())
        self._append_jsonl(candidate.session_id, "feedback_candidates.jsonl", candidate.to_dict())
        self.session_store.append_event(
            candidate.session_id,
            "feedback_candidate_recorded",
            {
                "candidate_id": candidate.candidate_id,
                "target": candidate.target,
                "status": candidate.status,
            },
        )

    def read_candidates(self, session_id: str) -> list[FeedbackCandidate]:
        return [
            FeedbackCandidate.from_dict(item)
            for item in self._read_jsonl(session_id, "feedback_candidates.jsonl", missing_ok=True)
        ]

    def _append_jsonl(self, session_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / name
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {path}: {exc}") from exc

    def _read_jsonl(self, session_id: str, name: str, *, missing_ok: bool) -> list[dict[str, Any]]:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / name
        if missing_ok and not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        records: list[dict[str, Any]] = []
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

    def _ensure_session_exists(self, session_id: str) -> None:
        try:
            self.session_store.load_session(session_id)
        except (AgentOSStorageError, AgentOSValidationError) as exc:
            raise AgentOSStorageError(
                f"Cannot store feedback for missing or invalid session {session_id!r}: {exc}"
            ) from exc


def feedback_log_path(session_store: AgentSessionStore, session_id: str) -> Path:
    return session_store.session_dir(session_id) / "feedback_events.jsonl"
