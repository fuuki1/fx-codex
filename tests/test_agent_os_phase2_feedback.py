from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.feedback import FeedbackStore, candidate_from_feedback, normalize_pytest_output
from agent_os.feedback.schemas import FeedbackCandidate, FeedbackEvent
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


def _session(tmp_path: Path) -> tuple[AgentSessionStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_store = AgentSessionStore(tmp_path / "sessions")
    session = session_store.create_session(
        role="test_engineer",
        user_request="phase2 feedback",
        repo=repo,
        session_id="agent_feedback",
    )
    return session_store, session.session_id


def test_normalize_pytest_output_and_candidate_generation(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    evidence = tmp_path / "pytest.log"
    evidence.write_text(
        "FAILED tests/test_example.py::test_case - AssertionError\n", encoding="utf-8"
    )

    event = normalize_pytest_output(
        session_id=session_id,
        output=evidence.read_text(encoding="utf-8"),
        exit_code=1,
        evidence_path=evidence,
    )
    assert event.source == "pytest"
    assert event.category == "test_failure"
    assert event.severity == "medium"
    assert "FAILED tests/test_example.py::test_case" in event.summary
    assert event.evidence == [str(evidence)]

    candidate = candidate_from_feedback(event)
    assert candidate.target == "issue"
    assert candidate.source_feedback_ids == [event.feedback_id]
    assert candidate.evidence == [str(evidence)]

    feedback_store = FeedbackStore(session_store)
    feedback_store.record_event(event)
    feedback_store.record_candidate(candidate)

    assert feedback_store.read_events(session_id) == [event]
    assert feedback_store.read_candidates(session_id) == [candidate]
    audit_events = [item["event"] for item in session_store.read_events(session_id)]
    assert audit_events == [
        "session_created",
        "feedback_event_recorded",
        "feedback_candidate_recorded",
    ]


def test_feedback_schema_rejects_active_or_unknown_targets() -> None:
    with pytest.raises(AgentOSValidationError, match="status"):
        FeedbackEvent.from_dict(
            {
                "feedback_id": "fb_invalid",
                "source": "pytest",
                "session_id": "agent_feedback",
                "severity": "medium",
                "category": "test_failure",
                "summary": "failed",
                "status": "active",
                "created_at": "2026-07-06T00:00:00+00:00",
            }
        )

    with pytest.raises(AgentOSValidationError, match="target"):
        FeedbackCandidate.from_dict(
            {
                "candidate_id": "cand_invalid",
                "session_id": "agent_feedback",
                "target": "active_memory",
                "title": "bad target",
                "rationale": "should stay candidate",
                "source_feedback_ids": ["fb_invalid"],
                "created_at": "2026-07-06T00:00:00+00:00",
            }
        )


def test_feedback_store_reports_missing_session(tmp_path: Path) -> None:
    feedback_store = FeedbackStore(AgentSessionStore(tmp_path / "sessions"))
    event = normalize_pytest_output(
        session_id="missing_session",
        output="FAILED tests/test_example.py::test_case",
        exit_code=1,
        evidence_path=tmp_path / "pytest.log",
    )

    with pytest.raises(AgentOSStorageError, match="missing_session"):
        feedback_store.record_event(event)
