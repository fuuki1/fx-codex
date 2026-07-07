from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.agents import (
    AgentSpec,
    AgentSpecRegistry,
    HandoffContract,
    HandoffStore,
    WorkPlanStore,
)
from agent_os.cli import main as agent_os_main
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.sessions.store import AgentSessionStore


def _session(tmp_path: Path) -> tuple[AgentSessionStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_store = AgentSessionStore(tmp_path / "sessions")
    session = session_store.create_session(
        role="orchestrator",
        user_request="split work across specialized agents",
        repo=repo,
        session_id="agent_handoffs",
    )
    return session_store, session.session_id


def test_default_agent_specs_load_and_keep_tool_policy_separate() -> None:
    registry = AgentSpecRegistry("agent_os/agents/specs")
    specs = registry.list_specs()

    assert [spec.role for spec in specs] == [
        "eval_reviewer",
        "memory_curator",
        "ops_reviewer",
        "orchestrator",
        "release_manager",
        "risk_reviewer",
        "skill_maintainer",
        "strategy_engineer",
        "test_engineer",
    ]
    strategy = registry.get("strategy_engineer")
    assert "test_engineer" in strategy.handoff_targets
    assert "broker.place_order" in strategy.denied_tools
    assert not set(strategy.allowed_tools).intersection(strategy.denied_tools)
    orchestrator = registry.get("orchestrator")
    assert {"memory_curator", "skill_maintainer", "eval_reviewer"}.issubset(
        orchestrator.handoff_targets
    )


def test_agent_spec_rejects_allowed_denied_overlap() -> None:
    with pytest.raises(AgentOSValidationError, match="overlap"):
        AgentSpec.from_dict(
            {
                "role": "bad",
                "description": "bad",
                "prompt_version": "2026-07-06.1",
                "allowed_tools": ["git.push"],
                "denied_tools": ["git.push"],
                "handoff_targets": [],
                "approval_required_for": [],
                "outputs_required": [],
            }
        )


def test_handoff_store_records_chain_and_transition_events(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    store = HandoffStore(session_store)

    implementation = store.create_handoff(
        session_id=session_id,
        from_role="orchestrator",
        to_role="strategy_engineer",
        approval_role="risk_reviewer",
        task="Implement feedback collector slice",
        context_summary="Use saved artifacts only; no external writes.",
        acceptance_criteria=["collector persists feedback_candidates.jsonl", "tests pass"],
        required_artifacts=["tests/test_agent_os_phase7_feedback_collectors.py"],
        blocked_actions=["git.push", "broker.place_order"],
        handoff_id="handoff_impl",
    )
    test_handoff = store.create_handoff(
        session_id=session_id,
        from_role="strategy_engineer",
        to_role="test_engineer",
        approval_role="risk_reviewer",
        task="Add focused regression tests",
        context_summary="Validate collector behavior without broad refactors.",
        acceptance_criteria=["phase tests cover success and failure paths"],
        handoff_id="handoff_tests",
    )

    accepted = store.transition_handoff(
        session_id=session_id,
        handoff_id=implementation.handoff_id,
        to_status="accepted",
        actor="strategy_engineer",
        reason="accepted implementation handoff",
    )
    completed = store.transition_handoff(
        session_id=session_id,
        handoff_id=implementation.handoff_id,
        to_status="completed",
        actor="risk_reviewer",
        reason="tests and risk review passed",
        evidence_paths=["pytest.log", "risk_review.md"],
    )

    assert implementation.status == "proposed"
    assert test_handoff.to_role == "test_engineer"
    assert accepted.status == "accepted"
    assert completed.status == "completed"
    assert completed.evidence_paths == ["pytest.log", "risk_review.md"]
    handoffs = store.read_handoffs(session_id)
    assert [handoff.handoff_id for handoff in handoffs] == ["handoff_impl", "handoff_tests"]
    assert handoffs[0].status == "completed"
    events = [event["event"] for event in session_store.read_events(session_id)]
    assert events == [
        "session_created",
        "handoff_recorded",
        "handoff_recorded",
        "handoff_transitioned",
        "handoff_transitioned",
    ]


def test_handoff_schema_requires_separate_approval_role() -> None:
    with pytest.raises(AgentOSValidationError, match="approval_role"):
        HandoffContract.from_dict(
            {
                "handoff_id": "bad",
                "session_id": "agent_handoffs",
                "from_role": "orchestrator",
                "to_role": "strategy_engineer",
                "approval_role": "strategy_engineer",
                "task": "bad",
                "context_summary": "bad",
                "acceptance_criteria": ["bad"],
                "created_at": "2026-07-06T00:00:00+00:00",
                "updated_at": "2026-07-06T00:00:00+00:00",
            }
        )


def test_registry_validated_handoff_rejects_unallowed_target_and_bad_actor(
    tmp_path: Path,
) -> None:
    session_store, session_id = _session(tmp_path)
    store = HandoffStore(session_store, spec_registry=AgentSpecRegistry("agent_os/agents/specs"))

    with pytest.raises(AgentOSValidationError, match="cannot hand off"):
        store.create_handoff(
            session_id=session_id,
            from_role="strategy_engineer",
            to_role="release_manager",
            approval_role="risk_reviewer",
            task="invalid target",
            context_summary="strategy engineer must hand off through tests or risk first",
            acceptance_criteria=["should fail"],
            handoff_id="handoff_invalid_target",
        )

    handoff = store.create_handoff(
        session_id=session_id,
        from_role="strategy_engineer",
        to_role="test_engineer",
        approval_role="risk_reviewer",
        task="valid target",
        context_summary="test engineer validates implementation",
        acceptance_criteria=["tests pass"],
        handoff_id="handoff_validated",
    )
    with pytest.raises(AgentOSValidationError, match="acceptance must be recorded by to_role"):
        store.transition_handoff(
            session_id=session_id,
            handoff_id=handoff.handoff_id,
            to_status="accepted",
            actor="strategy_engineer",
            reason="wrong actor",
        )

    store.transition_handoff(
        session_id=session_id,
        handoff_id=handoff.handoff_id,
        to_status="accepted",
        actor="test_engineer",
        reason="accepted",
    )
    with pytest.raises(
        AgentOSValidationError, match="completion must be recorded by approval_role"
    ):
        store.transition_handoff(
            session_id=session_id,
            handoff_id=handoff.handoff_id,
            to_status="completed",
            actor="test_engineer",
            reason="self approval is forbidden",
            evidence_paths=["pytest.log"],
        )


def test_cli_agent_handoff_create_transition_and_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_store, session_id = _session(tmp_path)

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "handoff-create",
                "--session-id",
                session_id,
                "--from-role",
                "orchestrator",
                "--to-role",
                "strategy_engineer",
                "--approval-role",
                "risk_reviewer",
                "--task",
                "Implement phase 8 slice",
                "--context-summary",
                "Track handoff only.",
                "--acceptance",
                "handoff is persisted",
                "--required-artifact",
                "handoffs.jsonl",
                "--handoff-id",
                "handoff_cli",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "proposed"
    assert created["approval_role"] == "risk_reviewer"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "handoff-transition",
                "--session-id",
                session_id,
                "--handoff-id",
                "handoff_cli",
                "--to-status",
                "accepted",
                "--actor",
                "strategy_engineer",
                "--reason",
                "accepted",
            ]
        )
        == 0
    )
    transitioned = json.loads(capsys.readouterr().out)
    assert transitioned["status"] == "accepted"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "handoff-list",
                "--session-id",
                session_id,
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [handoff["handoff_id"] for handoff in listed["handoffs"]] == ["handoff_cli"]


def test_work_plan_groups_handoffs_and_requires_completed_chain(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    handoffs = HandoffStore(session_store, spec_registry=AgentSpecRegistry("agent_os/agents/specs"))
    plans = WorkPlanStore(session_store, spec_registry=AgentSpecRegistry("agent_os/agents/specs"))

    implementation = handoffs.create_handoff(
        session_id=session_id,
        from_role="orchestrator",
        to_role="strategy_engineer",
        approval_role="risk_reviewer",
        task="Implement maximal phase 8 slice",
        context_summary="Add work plan tracking only.",
        acceptance_criteria=["implementation handoff is audited"],
        handoff_id="handoff_impl_plan",
    )
    tests = handoffs.create_handoff(
        session_id=session_id,
        from_role="strategy_engineer",
        to_role="test_engineer",
        approval_role="risk_reviewer",
        task="Add work plan tests",
        context_summary="Keep tests focused.",
        acceptance_criteria=["tests verify incomplete handoff blocking"],
        handoff_id="handoff_test_plan",
    )
    plan = plans.create_plan(
        session_id=session_id,
        objective="Maximize phase 8 specialization without executing agents",
        owner_role="orchestrator",
        handoff_ids=[implementation.handoff_id, tests.handoff_id],
        completion_criteria=["all handoffs completed", "all tests pass"],
        risk_level="medium",
        plan_id="work_plan_phase8",
    )

    assert plan.status == "planned"
    assert plan.handoff_ids == ["handoff_impl_plan", "handoff_test_plan"]
    with pytest.raises(AgentOSValidationError, match="incomplete handoffs"):
        plans.transition_plan(
            session_id=session_id,
            plan_id=plan.plan_id,
            to_status="completed",
            actor="orchestrator",
            reason="too early",
            evidence_paths=["pytest.log"],
        )

    for handoff in [implementation, tests]:
        handoffs.transition_handoff(
            session_id=session_id,
            handoff_id=handoff.handoff_id,
            to_status="accepted",
            actor=handoff.to_role,
            reason="accepted",
        )
        handoffs.transition_handoff(
            session_id=session_id,
            handoff_id=handoff.handoff_id,
            to_status="completed",
            actor=handoff.approval_role,
            reason="approved",
            evidence_paths=[f"{handoff.handoff_id}.md"],
        )

    active = plans.transition_plan(
        session_id=session_id,
        plan_id=plan.plan_id,
        to_status="active",
        actor="orchestrator",
        reason="handoffs are underway",
    )
    completed = plans.transition_plan(
        session_id=session_id,
        plan_id=plan.plan_id,
        to_status="completed",
        actor="orchestrator",
        reason="all handoffs complete",
        evidence_paths=["pytest.log", "handoff_review.md"],
    )

    assert active.status == "active"
    assert completed.status == "completed"
    assert completed.evidence_paths == ["pytest.log", "handoff_review.md"]
    assert plans.read_plans(session_id)[0].status == "completed"


def test_cli_work_plan_create_transition_and_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_store, session_id = _session(tmp_path)
    handoffs = HandoffStore(session_store, spec_registry=AgentSpecRegistry("agent_os/agents/specs"))
    handoff = handoffs.create_handoff(
        session_id=session_id,
        from_role="orchestrator",
        to_role="strategy_engineer",
        approval_role="risk_reviewer",
        task="CLI work plan handoff",
        context_summary="Prepare a single handoff plan.",
        acceptance_criteria=["handoff exists"],
        handoff_id="handoff_cli_plan",
    )

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "work-plan-create",
                "--session-id",
                session_id,
                "--objective",
                "Track CLI work plan",
                "--owner-role",
                "orchestrator",
                "--handoff-id",
                handoff.handoff_id,
                "--completion",
                "handoff complete",
                "--plan-id",
                "work_plan_cli",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "planned"
    assert created["handoff_ids"] == ["handoff_cli_plan"]

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "work-plan-transition",
                "--session-id",
                session_id,
                "--plan-id",
                "work_plan_cli",
                "--to-status",
                "active",
                "--actor",
                "orchestrator",
                "--reason",
                "started",
            ]
        )
        == 0
    )
    active = json.loads(capsys.readouterr().out)
    assert active["status"] == "active"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "agent",
                "work-plan-list",
                "--session-id",
                session_id,
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [plan["plan_id"] for plan in listed["work_plans"]] == ["work_plan_cli"]
