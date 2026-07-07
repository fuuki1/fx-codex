from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.cli import main as agent_os_main
from agent_os.sessions.schemas import ToolCall, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore
from agent_os.shadow import (
    ShadowProposal,
    ShadowStore,
    compare_shadow,
    load_shadow_proposal,
    run_shadow,
)


def _base_session(tmp_path: Path) -> tuple[AgentSessionStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_store = AgentSessionStore(tmp_path / "sessions")
    session = session_store.create_session(
        role="strategy_engineer",
        user_request="real task",
        repo=repo,
        session_id="agent_shadow_base",
    )
    session_store.record_tool_call(
        ToolCall(
            tool_call_id="tool_base",
            session_id=session.session_id,
            tool="shell.test",
            started_at=utc_now_iso(),
            ended_at=utc_now_iso(),
            cwd=str(repo),
            input_redacted={"cmd": "pytest"},
            exit_code=0,
            status="success",
        )
    )
    return session_store, session.session_id


def test_shadow_compare_blocks_prohibited_side_effects() -> None:
    proposal = ShadowProposal(
        candidate="skill:test@0.1.0",
        base_session_id="agent_shadow_base",
        plan="write directly",
        proposed_tool_calls=[
            {"tool": "filesystem.write", "side_effect": "local_write", "execute": False},
            {"tool": "github.create_pr", "side_effect": "external_write", "execute": True},
        ],
    )

    report = compare_shadow(shadow_run_id="shadow_blocked", proposal=proposal)

    assert report.outcome == "blocked"
    assert report.scores["policy_compliance"] == 0.0
    assert report.scores["tool_risk"] == 1.0
    assert any("prohibited side effect local_write" in reason for reason in report.blocked_reasons)
    assert any("attempts execution" in reason for reason in report.blocked_reasons)
    assert report.promotion_recommendation == "reject_until_policy_clean"


def test_shadow_run_persists_report_and_events(tmp_path: Path) -> None:
    session_store, session_id = _base_session(tmp_path)
    shadow_store = ShadowStore(tmp_path / "shadow_runs")
    proposal = ShadowProposal(
        candidate="agent_spec:strategy_engineer@0.2.0",
        base_session_id=session_id,
        plan="dry-run only plan",
        proposed_tool_calls=[{"tool": "git.diff", "side_effect": "read_only"}],
        synthetic_diff="diff --git a/agent_os/x b/agent_os/x\n+safe\n",
        eval_run_id="eval_passed",
    )

    run, report = run_shadow(
        proposal=proposal,
        shadow_store=shadow_store,
        session_store=session_store,
        shadow_run_id="shadow_phase4",
        baseline_scores={"diff_lines": 10},
    )

    assert run.status == "completed"
    assert report.outcome == "better"
    assert report.scores["policy_compliance"] == 1.0
    assert report.scores["tool_risk"] == 0.0
    assert report.scores["diff_size_delta"] < 0
    assert shadow_store.load_run("shadow_phase4") == run
    assert shadow_store.load_report("shadow_phase4") == report
    assert shadow_store.load_proposal("shadow_phase4") == proposal
    assert (shadow_store.run_dir("shadow_phase4") / "shadow_run.json").is_file()
    assert (shadow_store.run_dir("shadow_phase4") / "shadow_report.json").is_file()
    events = [event["event"] for event in shadow_store.read_events("shadow_phase4")]
    assert events == [
        "shadow_run_created",
        "shadow_report_recorded",
        "shadow_run_finished",
    ]


def test_load_shadow_proposal_and_missing_session_error(tmp_path: Path) -> None:
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "candidate": "skill:test@0.1.0",
                "base_session_id": "missing_session",
                "plan": "dry-run",
                "proposed_tool_calls": [],
            }
        ),
        encoding="utf-8",
    )
    proposal = load_shadow_proposal(proposal_path)
    assert proposal.candidate == "skill:test@0.1.0"

    with pytest.raises(AgentOSStorageError, match="missing_session"):
        run_shadow(
            proposal=proposal,
            shadow_store=ShadowStore(tmp_path / "shadow_runs"),
            session_store=AgentSessionStore(tmp_path / "sessions"),
        )


def test_cli_shadow_run_persists_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_store, session_id = _base_session(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "candidate": "skill:cli@0.1.0",
                "base_session_id": session_id,
                "plan": "dry-run with eval",
                "proposed_tool_calls": [{"tool": "git.diff", "side_effect": "read_only"}],
                "synthetic_diff": "+safe\n",
                "eval_run_id": "eval_cli",
            }
        ),
        encoding="utf-8",
    )
    shadow_root = tmp_path / "shadow_runs"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "shadow",
                "run",
                "--proposal",
                str(proposal_path),
                "--shadow-root",
                str(shadow_root),
                "--shadow-run-id",
                "shadow_cli",
                "--baseline-scores-json",
                '{"diff_lines": 5}',
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "better"
    assert payload["scores"]["policy_compliance"] == 1.0
    assert (shadow_root / "shadow_cli" / "shadow_run.json").is_file()
    assert (shadow_root / "shadow_cli" / "shadow_report.json").is_file()
