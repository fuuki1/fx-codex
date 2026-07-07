from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.cli import main as agent_os_main
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.skills import SkillRecord, SkillRegistry
from agent_os.sessions.store import AgentOSStorageError


def _candidate(
    skill_id: str = "fx_backtester_validation_workflow", version: str = "0.1.0"
) -> SkillRecord:
    return SkillRecord(
        skill_id=skill_id,
        version=version,
        state="candidate",
        title="FX backtester validation workflow",
        summary="Review validation gates before strategy changes.",
        owner="skill_maintainer",
        created_from_feedback_ids=["fb_001"],
        evidence_paths=["runs/agent_sessions/agent_001/feedback_candidates.jsonl"],
        body_path="runs/agent_sessions/agent_001/skill_candidates/fx/SKILL.md",
    )


def test_skill_registry_registers_candidate_and_logs(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    record = registry.register_candidate(_candidate())

    assert record.state == "candidate"
    assert registry.get(record.skill_id, record.version) == record
    assert (tmp_path / "skills" / "registry.json").is_file()
    assert (tmp_path / "skills" / "events.jsonl").is_file()
    events = registry.read_events()
    assert [event["event"] for event in events] == ["skill_registered"]


def test_skill_lifecycle_requires_gates_for_active(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    record = registry.register_candidate(_candidate())

    with pytest.raises(AgentOSValidationError, match="cannot transition candidate -> active"):
        registry.transition(
            skill_id=record.skill_id,
            version=record.version,
            to_state="active",
            actor="skill_maintainer",
            reason="skip shadow",
            evidence_paths=["review.md"],
            eval_run_id="eval_001",
            shadow_run_id="shadow_001",
        )

    shadowed, transition = registry.transition(
        skill_id=record.skill_id,
        version=record.version,
        to_state="shadow",
        actor="skill_maintainer",
        reason="ready for no-side-effect observation",
        evidence_paths=["review.md"],
        review_note="candidate reviewed",
    )
    assert shadowed.state == "shadow"
    assert transition.from_state == "candidate"
    assert transition.to_state == "shadow"
    assert shadowed.reviewed_by == "skill_maintainer"

    with pytest.raises(AgentOSValidationError, match="eval_run_id is required"):
        registry.transition(
            skill_id=record.skill_id,
            version=record.version,
            to_state="active",
            actor="risk_reviewer",
            reason="missing eval",
            evidence_paths=["shadow_report.json"],
            shadow_run_id="shadow_001",
        )

    with pytest.raises(AgentOSValidationError, match="shadow_run_id is required"):
        registry.transition(
            skill_id=record.skill_id,
            version=record.version,
            to_state="active",
            actor="risk_reviewer",
            reason="missing shadow",
            evidence_paths=["eval_run.json"],
            eval_run_id="eval_001",
        )

    active, active_transition = registry.transition(
        skill_id=record.skill_id,
        version=record.version,
        to_state="active",
        actor="risk_reviewer",
        reason="eval and shadow gates passed",
        evidence_paths=["eval_run.json", "shadow_report.json"],
        eval_run_id="eval_001",
        shadow_run_id="shadow_001",
        review_note="approved for active use",
    )
    assert active.state == "active"
    assert active.required_eval_ids == ["eval_001"]
    assert active.shadow_run_ids == ["shadow_001"]
    assert active_transition.to_state == "active"
    assert [event["event"] for event in registry.read_events()] == [
        "skill_registered",
        "skill_transitioned",
        "skill_transitioned",
    ]


def test_registry_blocks_duplicate_active_skill_versions(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    first = registry.register_candidate(_candidate(version="0.1.0"))
    second = registry.register_candidate(_candidate(version="0.2.0"))
    for record, eval_id, shadow_id in [
        (first, "eval_001", "shadow_001"),
        (second, "eval_002", "shadow_002"),
    ]:
        registry.transition(
            skill_id=record.skill_id,
            version=record.version,
            to_state="shadow",
            actor="skill_maintainer",
            reason="shadow",
            evidence_paths=["review.md"],
        )
        if record is first:
            registry.transition(
                skill_id=record.skill_id,
                version=record.version,
                to_state="active",
                actor="risk_reviewer",
                reason="approved",
                evidence_paths=["eval.json", "shadow.json"],
                eval_run_id=eval_id,
                shadow_run_id=shadow_id,
            )

    with pytest.raises(AgentOSValidationError, match="active skill already exists"):
        registry.transition(
            skill_id=second.skill_id,
            version=second.version,
            to_state="active",
            actor="risk_reviewer",
            reason="would conflict",
            evidence_paths=["eval.json", "shadow.json"],
            eval_run_id="eval_002",
            shadow_run_id="shadow_002",
        )


def test_skill_reject_deprecate_and_retire_paths(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    rejected = registry.register_candidate(_candidate(skill_id="reject_me"))
    rejected_record, _ = registry.transition(
        skill_id=rejected.skill_id,
        version=rejected.version,
        to_state="rejected",
        actor="memory_curator",
        reason="not repeatable",
        evidence_paths=["review.md"],
    )
    assert rejected_record.state == "rejected"

    active = registry.register_candidate(_candidate(skill_id="retire_me"))
    registry.transition(
        skill_id=active.skill_id,
        version=active.version,
        to_state="shadow",
        actor="skill_maintainer",
        reason="shadow",
        evidence_paths=["review.md"],
    )
    registry.transition(
        skill_id=active.skill_id,
        version=active.version,
        to_state="active",
        actor="risk_reviewer",
        reason="approved",
        evidence_paths=["eval.json", "shadow.json"],
        eval_run_id="eval_001",
        shadow_run_id="shadow_001",
    )
    deprecated, _ = registry.transition(
        skill_id=active.skill_id,
        version=active.version,
        to_state="deprecated",
        actor="skill_maintainer",
        reason="superseded",
        evidence_paths=["replacement.md"],
    )
    retired, _ = registry.transition(
        skill_id=active.skill_id,
        version=active.version,
        to_state="retired",
        actor="skill_maintainer",
        reason="no longer used",
        evidence_paths=["retire.md"],
    )
    assert deprecated.state == "deprecated"
    assert retired.state == "retired"


def test_skill_schema_rejects_active_registration_and_bad_version(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    with pytest.raises(AgentOSValidationError, match="candidate state"):
        registry.register_candidate(
            SkillRecord(
                skill_id="bad",
                version="0.1.0",
                state="active",
                title="bad",
                summary="bad",
                owner="skill_maintainer",
            )
        )

    with pytest.raises(AgentOSValidationError, match="semver"):
        SkillRecord.from_dict(
            {
                "skill_id": "bad",
                "version": "v1",
                "state": "candidate",
                "title": "bad",
                "summary": "bad",
                "owner": "skill_maintainer",
                "created_at": "2026-07-06T00:00:00+00:00",
                "updated_at": "2026-07-06T00:00:00+00:00",
            }
        )


def test_skill_registry_reports_corrupt_jsonl_with_line(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    registry.register_candidate(_candidate())
    registry.events_path.write_text('{"event": "ok"}\n{bad json}\n', encoding="utf-8")

    with pytest.raises(AgentOSStorageError, match=r"events\.jsonl:2"):
        registry.read_events()


def test_cli_skill_register_transition_and_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_root = tmp_path / "skills"

    assert (
        agent_os_main(
            [
                "skill",
                "--skill-root",
                str(skill_root),
                "register",
                "--skill-id",
                "cli_skill",
                "--version",
                "0.1.0",
                "--title",
                "CLI Skill",
                "--summary",
                "Registered from CLI",
                "--owner",
                "skill_maintainer",
                "--feedback-id",
                "fb_cli",
                "--evidence",
                "feedback_candidates.jsonl",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "candidate"

    assert (
        agent_os_main(
            [
                "skill",
                "--skill-root",
                str(skill_root),
                "transition",
                "--skill-id",
                "cli_skill",
                "--version",
                "0.1.0",
                "--to-state",
                "shadow",
                "--actor",
                "skill_maintainer",
                "--reason",
                "reviewed",
                "--evidence",
                "review.md",
            ]
        )
        == 0
    )
    transition_payload = json.loads(capsys.readouterr().out)
    assert transition_payload["skill"]["state"] == "shadow"
    assert transition_payload["transition"]["from_state"] == "candidate"

    assert (
        agent_os_main(
            [
                "skill",
                "--skill-root",
                str(skill_root),
                "show",
                "--skill-id",
                "cli_skill",
                "--version",
                "0.1.0",
            ]
        )
        == 0
    )
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["state"] == "shadow"

    assert agent_os_main(["skill", "--skill-root", str(skill_root), "list"]) == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert [record["skill_id"] for record in list_payload["skills"]] == ["cli_skill"]
