from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.cli import main as agent_os_main
from agent_os.feedback.collectors import (
    collect_git_diff_summary,
    collect_github_review,
    collect_trader_events_jsonl,
    persist_collected_feedback,
)
from agent_os.sessions.schemas import AgentOSValidationError
from agent_os.feedback.store import FeedbackStore
from agent_os.sessions.store import AgentSessionStore


def _session(tmp_path: Path) -> tuple[AgentSessionStore, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_store = AgentSessionStore(tmp_path / "sessions")
    session = session_store.create_session(
        role="eval_reviewer",
        user_request="collect feedback",
        repo=repo,
        session_id="agent_feedback_collectors",
    )
    return session_store, session.session_id


def test_git_diff_collector_persists_scope_risk_and_test_gap_feedback(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text(
        "\n".join(
            [
                "diff --git a/pyproject.toml b/pyproject.toml",
                "+++ b/pyproject.toml",
                "diff --git a/fx_backtester/validation_pipeline.py b/fx_backtester/validation_pipeline.py",
                "+++ b/fx_backtester/validation_pipeline.py",
                "diff --git a/fx_intel/technicals.py b/fx_intel/technicals.py",
                "+++ b/fx_intel/technicals.py",
            ]
        ),
        encoding="utf-8",
    )

    events = collect_git_diff_summary(
        session_id=session_id,
        diff_text=diff_path.read_text(encoding="utf-8"),
        evidence_path=diff_path,
    )
    result = persist_collected_feedback(
        FeedbackStore(session_store),
        session_id=session_id,
        source="git_diff",
        events=events,
    )

    assert [event.category for event in events] == [
        "change_scope",
        "risk_gate_change",
        "missing_test_coverage",
    ]
    assert len(result.event_ids) == 3
    assert len(result.candidate_ids) == 3
    stored_events = FeedbackStore(session_store).read_events(session_id)
    stored_candidates = FeedbackStore(session_store).read_candidates(session_id)
    assert [event.category for event in stored_events] == [
        "change_scope",
        "risk_gate_change",
        "missing_test_coverage",
    ]
    assert [candidate.target for candidate in stored_candidates] == [
        "memory",
        "decision_rule",
        "issue",
    ]
    assert (session_store.session_dir(session_id) / "feedback_candidates.jsonl").is_file()

    second = persist_collected_feedback(
        FeedbackStore(session_store),
        session_id=session_id,
        source="git_diff",
        events=events,
    )
    assert second.event_ids == []
    assert second.candidate_ids == []
    assert any(item["reason"] == "feedback event already exists" for item in second.skipped_items)


def test_github_review_collector_handles_blockers_workflows_and_security(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        """
        {
          "comments": [
            {
              "body": "Blocking: this can regress the risk gate.",
              "path": "fx_backtester/validation_pipeline.py",
              "line": 42,
              "author": {"login": "risk-reviewer"},
              "state": "CHANGES_REQUESTED"
            },
            {
              "body": "We should always follow this checklist before deleting old CI runs.",
              "path": ".github/workflows/ci.yml",
              "line": 31,
              "author": {"login": "release-reviewer"}
            },
            {
              "body": "Security: this logs a credential in plain text.",
              "path": "trader/app/common.py",
              "line": 88,
              "author": {"login": "ops-reviewer"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    events = collect_github_review(
        session_id=session_id,
        review_text=review_path.read_text(encoding="utf-8"),
        evidence_path=review_path,
    )
    result = persist_collected_feedback(
        FeedbackStore(session_store),
        session_id=session_id,
        source="github_review",
        events=events,
    )

    assert [event.category for event in events] == [
        "review_blocker",
        "review_suggestion",
        "unsafe_tool_use",
    ]
    assert [event.severity for event in events] == ["high", "medium", "critical"]
    assert len(result.event_ids) == 3
    candidates = FeedbackStore(session_store).read_candidates(session_id)
    assert [candidate.target for candidate in candidates] == ["issue", "skill", "tool_policy"]
    assert candidates[0].title == "Resolve blocking GitHub review feedback"
    assert candidates[1].title == "Capture repeated review workflow as a Skill candidate"
    assert candidates[2].title == "Review security-sensitive review feedback"


def test_trader_events_collector_filters_actionable_operational_events(tmp_path: Path) -> None:
    session_store, session_id = _session(tmp_path)
    events_path = tmp_path / "trader-events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                '{"ts":"2026-07-06T00:00:00Z","kind":"heartbeat","payload":{"ok":true}}',
                '{"ts":"2026-07-06T00:01:00Z","kind":"params_rejected","payload":{"reason":"DSR below gate","symbol":"USDJPY"}}',
                '{"ts":"2026-07-06T00:02:00Z","kind":"params_missing","payload":{"message":"strategy_params.json missing"}}',
                '{"ts":"2026-07-06T00:03:00Z","kind":"dead_letter_order","payload":"{\\"error\\":\\"broker rejected order\\"}"}',
            ]
        ),
        encoding="utf-8",
    )

    events = collect_trader_events_jsonl(
        session_id=session_id,
        jsonl_text=events_path.read_text(encoding="utf-8"),
        evidence_path=events_path,
    )
    result = persist_collected_feedback(
        FeedbackStore(session_store),
        session_id=session_id,
        source="trader_events",
        events=events,
    )

    assert [event.category for event in events] == [
        "ops_event",
        "runbook_gap",
        "ops_event",
    ]
    assert [event.severity for event in events] == ["medium", "medium", "high"]
    assert "DSR below gate" in events[0].summary
    assert len(result.event_ids) == 3
    assert {
        candidate.target for candidate in FeedbackStore(session_store).read_candidates(session_id)
    } == {"runbook"}


def test_trader_events_collector_rejects_corrupt_jsonl(tmp_path: Path) -> None:
    _session_store, session_id = _session(tmp_path)

    try:
        collect_trader_events_jsonl(
            session_id=session_id,
            jsonl_text='{"kind":"heartbeat"}\n{bad json}',
            evidence_path=tmp_path / "bad.jsonl",
        )
    except AgentOSValidationError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("expected corrupt JSONL to be rejected")


def test_cli_feedback_collect_writes_events_and_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_store, session_id = _session(tmp_path)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        '[{"body":"Blocking: must add regression coverage.","path":"tests/test_ci.py","line":12}]',
        encoding="utf-8",
    )

    assert (
        agent_os_main(
            [
                "--store-root",
                str(session_store.root),
                "feedback",
                "collect",
                "--session-id",
                session_id,
                "--source",
                "github-review",
                "--input",
                str(review_path),
                "--metadata-json",
                '{"phase":"feedback-collectors"}',
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "github-review"
    assert payload["metadata"] == {"phase": "feedback-collectors"}
    assert len(payload["event_ids"]) == 1
    assert len(payload["candidate_ids"]) == 1
    assert (session_store.session_dir(session_id) / "feedback_events.jsonl").is_file()
    assert (session_store.session_dir(session_id) / "feedback_candidates.jsonl").is_file()
    assert FeedbackStore(session_store).read_candidates(session_id)[0].target == "issue"
