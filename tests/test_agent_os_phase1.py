from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.cli import main as agent_os_main
from agent_os.sessions.schemas import (
    AgentOSValidationError,
    DecisionRecord,
    ToolCall,
    utc_now_iso,
)
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


def test_session_store_creates_loads_and_finishes_session(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AgentSessionStore(tmp_path / "sessions")

    session = store.create_session(
        role="strategy_engineer",
        user_request="implement phase 1",
        repo=repo,
        metadata={"phase": 1},
        session_id="agent_test_strategy",
    )

    assert session.status == "created"
    assert (tmp_path / "sessions" / session.session_id / "session.json").is_file()
    assert (tmp_path / "sessions" / session.session_id / "events.jsonl").is_file()
    assert (tmp_path / "sessions" / session.session_id / "artifacts").is_dir()

    loaded = store.load_session(session.session_id)
    assert loaded.session_id == session.session_id
    assert loaded.metadata["phase"] == 1

    finished = store.finish_session(session.session_id, "completed", reason="tests passed")
    assert finished.status == "completed"
    assert finished.metadata["finish_reason"] == "tests passed"

    events = store.read_events(session.session_id)
    assert [event["event"] for event in events] == ["session_created", "session_finished"]


def test_store_records_tools_and_decisions_with_audit_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AgentSessionStore(tmp_path / "sessions")
    session = store.create_session(
        role="test_engineer",
        user_request="record audit",
        repo=repo,
        session_id="agent_test_audit",
    )

    call = ToolCall(
        tool_call_id="tool_001",
        session_id=session.session_id,
        tool="shell.test",
        started_at=utc_now_iso(),
        ended_at=utc_now_iso(),
        cwd=str(repo),
        input_redacted={"cmd": "pytest tests/test_agent_os_phase1.py"},
        exit_code=0,
        stdout_summary="2 passed",
        changed_files=[],
        status="success",
    )
    store.record_tool_call(call)

    decision = DecisionRecord(
        decision_id="decision_001",
        session_id=session.session_id,
        ts=utc_now_iso(),
        actor="risk_reviewer",
        action="phase1_scope",
        policy_result="allow",
        rationale="phase 1 only writes agent_os artifacts",
        evidence_paths=["docs/AGENT_OPERATING_SYSTEM_DESIGN.md"],
    )
    store.record_decision(decision)

    assert store.read_tool_calls(session.session_id) == [call]
    assert store.read_decisions(session.session_id) == [decision]
    events = [event["event"] for event in store.read_events(session.session_id)]
    assert events == ["session_created", "tool_recorded", "decision_recorded"]


def test_store_reports_jsonl_parse_errors_with_path_and_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AgentSessionStore(tmp_path / "sessions")
    session = store.create_session(
        role="test_engineer",
        user_request="bad jsonl",
        repo=repo,
        session_id="agent_bad_jsonl",
    )
    (store.session_dir(session.session_id) / "tools.jsonl").write_text(
        '{"tool_call_id": "ok"}\n{bad json}\n',
        encoding="utf-8",
    )

    with pytest.raises(AgentOSStorageError, match=r"tools\.jsonl:2"):
        store.read_tool_calls(session.session_id)


def test_schema_rejects_invalid_status_and_unsafe_session_id(tmp_path: Path) -> None:
    store = AgentSessionStore(tmp_path / "sessions")
    with pytest.raises(AgentOSValidationError, match="unsafe"):
        store.session_dir("../escape")

    repo = tmp_path / "repo"
    repo.mkdir()
    session = store.create_session(
        role="test_engineer",
        user_request="invalid status",
        repo=repo,
        session_id="agent_invalid_status",
    )
    with pytest.raises(AgentOSValidationError, match="finish status"):
        store.finish_session(session.session_id, "running")


def test_cli_start_record_and_finish_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = tmp_path / "sessions"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(store_root),
                "start",
                "--role",
                "strategy_engineer",
                "--request",
                "cli smoke",
                "--repo",
                str(repo),
                "--session-id",
                "agent_cli",
            ]
        )
        == 0
    )
    start_payload = json.loads(capsys.readouterr().out)
    assert start_payload["session_id"] == "agent_cli"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(store_root),
                "record-tool",
                "agent_cli",
                "--tool-call-id",
                "tool_cli",
                "--tool",
                "shell.test",
                "--cwd",
                str(repo),
                "--input-json",
                '{"cmd": "pytest"}',
                "--exit-code",
                "0",
                "--stdout-summary",
                "passed",
            ]
        )
        == 0
    )
    tool_payload = json.loads(capsys.readouterr().out)
    assert tool_payload["input_redacted"]["cmd"] == "pytest"

    assert (
        agent_os_main(
            [
                "--store-root",
                str(store_root),
                "finish",
                "agent_cli",
                "--status",
                "failed",
                "--reason",
                "intentional test failure",
            ]
        )
        == 0
    )
    finish_payload = json.loads(capsys.readouterr().out)
    assert finish_payload["status"] == "failed"
    assert finish_payload["metadata"]["finish_reason"] == "intentional test failure"
