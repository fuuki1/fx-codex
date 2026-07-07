from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.candidates import CandidateArtifactStore, MemoryCandidate
from agent_os.cli import main as agent_os_main
from agent_os.feedback import FeedbackStore
from agent_os.feedback.schemas import FeedbackCandidate
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.sessions.store import AgentSessionStore


def _session(tmp_path: Path) -> tuple[AgentSessionStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_store = AgentSessionStore(tmp_path / "sessions")
    session = session_store.create_session(
        role="memory_curator",
        user_request="materialize candidates",
        repo=repo,
        session_id="agent_candidates",
    )
    return session_store, session.session_id


def _candidate(
    *,
    candidate_id: str,
    session_id: str,
    target: str,
    title: str,
    status: str = "candidate",
) -> FeedbackCandidate:
    return FeedbackCandidate(
        candidate_id=candidate_id,
        session_id=session_id,
        target=target,
        title=title,
        rationale=f"Rationale for {title}",
        source_feedback_ids=[f"fb_{candidate_id}"],
        evidence=[f"runs/agent_sessions/{session_id}/feedback_candidates.jsonl"],
        status=status,
    )


def test_materializes_memory_and_skill_candidates_with_logs(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    store = CandidateArtifactStore(session_store)
    memory = _candidate(
        candidate_id="cand_memory",
        session_id=session_id,
        target="memory",
        title="Remember CI run recency",
    )
    skill = _candidate(
        candidate_id="cand_skill",
        session_id=session_id,
        target="skill",
        title="CI Failure Triage Workflow",
    )
    issue = _candidate(
        candidate_id="cand_issue",
        session_id=session_id,
        target="issue",
        title="Open follow-up issue",
    )

    result = store.materialize_feedback_candidates(
        session_id=session_id,
        candidates=[memory, skill, issue],
        skill_version="0.2.0",
        metadata={"phase": "candidate-flow"},
    )

    assert result.memory_candidate_ids == ["cand_memory"]
    assert result.skill_candidate_paths == ["skill_candidates/ci_failure_triage_workflow/SKILL.md"]
    assert result.skipped_candidates == [
        {
            "candidate_id": "cand_issue",
            "reason": "target issue is not a memory or skill artifact",
        }
    ]
    loaded_memory = store.read_memory_candidates(session_id)
    assert loaded_memory[0].candidate_id == "cand_memory"
    assert loaded_memory[0].target == "memory"
    assert "Rationale for Remember CI run recency" in loaded_memory[0].body
    assert store.read_materializations(session_id) == [result]

    skill_path = session_store.session_dir(session_id) / result.skill_candidate_paths[0]
    body = skill_path.read_text(encoding="utf-8")
    assert "state: candidate" in body
    assert "version: 0.2.0" in body
    assert "Keep this candidate inactive" in body

    events = [event["event"] for event in session_store.read_events(session_id)]
    assert events == [
        "session_created",
        "memory_candidate_materialized",
        "skill_candidate_materialized",
        "candidate_materialization_skipped",
        "candidate_materialization_finished",
    ]


def test_materialization_does_not_overwrite_existing_skill_candidate(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    skill = _candidate(
        candidate_id="cand_skill",
        session_id=session_id,
        target="skill",
        title="Existing Skill Candidate",
    )
    existing = (
        session_store.session_dir(session_id)
        / "skill_candidates"
        / "existing_skill_candidate"
        / "SKILL.md"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("manual review draft\n", encoding="utf-8")

    result = CandidateArtifactStore(session_store).materialize_feedback_candidates(
        session_id=session_id,
        candidates=[skill],
    )

    assert result.skill_candidate_paths == []
    assert result.skipped_candidates == [
        {
            "candidate_id": "cand_skill",
            "reason": "skill candidate already exists at skill_candidates/existing_skill_candidate/SKILL.md",
        }
    ]
    assert existing.read_text(encoding="utf-8") == "manual review draft\n"


def test_candidate_artifact_schema_rejects_active_memory_candidate() -> None:
    with pytest.raises(AgentOSValidationError, match="status"):
        MemoryCandidate.from_dict(
            {
                "candidate_id": "cand_bad",
                "session_id": "agent_candidates",
                "target": "memory",
                "title": "bad",
                "body": "bad",
                "source_feedback_ids": ["fb_bad"],
                "status": "active",
                "created_at": "2026-07-06T00:00:00+00:00",
            }
        )


def test_materialization_rejects_cross_session_candidate(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    candidate = _candidate(
        candidate_id="cand_other",
        session_id="other_session",
        target="memory",
        title="Other session",
    )

    with pytest.raises(AgentOSValidationError, match="other_session"):
        CandidateArtifactStore(session_store).materialize_feedback_candidates(
            session_id=session_id,
            candidates=[candidate],
        )


def test_cli_materializes_recorded_feedback_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_store, session_id = _session(tmp_path)
    feedback_store = FeedbackStore(session_store)
    feedback_store.record_candidate(
        _candidate(
            candidate_id="cand_cli_memory",
            session_id=session_id,
            target="decision_rule",
            title="Keep PR check recency explicit",
        )
    )
    feedback_store.record_candidate(
        _candidate(
            candidate_id="cand_cli_skill",
            session_id=session_id,
            target="skill",
            title="PR Check Cleanup Workflow",
        )
    )

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "candidate",
                "materialize",
                "--session-id",
                session_id,
                "--skill-version",
                "0.3.0",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory_candidate_ids"] == ["cand_cli_memory"]
    assert payload["skill_candidate_paths"] == [
        "skill_candidates/pr_check_cleanup_workflow/SKILL.md"
    ]
    assert (session_store.session_dir(session_id) / "memory_candidates.jsonl").is_file()
    assert (
        session_store.session_dir(session_id)
        / "skill_candidates"
        / "pr_check_cleanup_workflow"
        / "SKILL.md"
    ).is_file()
