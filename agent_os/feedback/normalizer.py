"""Deterministic feedback normalizers.

These functions intentionally produce candidates, not active Memory or Skills.
Later phases can add richer clustering and review workflows on top.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .schemas import FeedbackCandidate, FeedbackEvent
from agent_os.sessions.schemas import utc_now_iso


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _first_relevant_pytest_line(output: str) -> str:
    patterns = (
        r"^FAILED\s+.+$",
        r"^ERROR\s+.+$",
        r"^E\s+.+$",
        r"=+\s+FAILURES\s+=+",
        r"=+\s+ERRORS\s+=+",
    )
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    for pattern in patterns:
        for line in lines:
            if re.search(pattern, line):
                return line.strip()
    return lines[-1].strip() if lines else "pytest produced no output"


def _pytest_severity(output: str, exit_code: int | None) -> str:
    lowered = output.lower()
    if exit_code == 0:
        return "low"
    if "error collecting" in lowered or "internalerror" in lowered:
        return "high"
    if "failed" in lowered or "error" in lowered:
        return "medium"
    return "low"


def normalize_pytest_output(
    *,
    session_id: str,
    output: str,
    exit_code: int | None,
    evidence_path: str | Path,
    feedback_id: str | None = None,
) -> FeedbackEvent:
    """Normalize pytest output into a FeedbackEvent."""

    evidence = str(evidence_path)
    summary = _first_relevant_pytest_line(output)
    category = "test_failure" if exit_code not in (0, None) else "test_observation"
    return FeedbackEvent(
        feedback_id=feedback_id or _stable_id("fb", session_id, "pytest", summary, evidence),
        source="pytest",
        session_id=session_id,
        severity=_pytest_severity(output, exit_code),
        category=category,
        summary=summary[:500],
        evidence=[evidence],
        root_cause="",
        recommended_change=(
            {
                "target": "issue",
                "title": "Investigate pytest failure",
            }
            if category == "test_failure"
            else {}
        ),
        status="candidate",
        created_at=utc_now_iso(),
        metadata={"exit_code": exit_code},
    )


def candidate_from_feedback(event: FeedbackEvent) -> FeedbackCandidate:
    """Create a reviewable improvement candidate from normalized feedback."""

    target = str(event.recommended_change.get("target") or "")
    if target not in {"memory", "skill", "issue", "runbook", "tool_policy", "decision_rule"}:
        target = _default_target(event)
    title = str(event.recommended_change.get("title") or _default_title(event))
    return FeedbackCandidate(
        candidate_id=_stable_id("cand", event.feedback_id, target, title),
        session_id=event.session_id,
        target=target,
        title=title[:200],
        rationale=f"{event.source}:{event.category}: {event.summary}",
        source_feedback_ids=[event.feedback_id],
        evidence=list(event.evidence),
        status="candidate",
        created_at=utc_now_iso(),
        metadata={"severity": event.severity, "category": event.category},
    )


def _default_target(event: FeedbackEvent) -> str:
    if event.category in {"test_failure", "test_observation"}:
        return "issue"
    if event.category in {"repeatable_workflow", "missing_workflow"}:
        return "skill"
    if event.category in {"project_fact", "failure_pattern"}:
        return "memory"
    if event.category in {"tool_failure", "unsafe_tool_use"}:
        return "tool_policy"
    if event.category in {"runbook_gap", "ops_event"}:
        return "runbook"
    return "issue"


def _default_title(event: FeedbackEvent) -> str:
    if event.category == "test_failure":
        return "Investigate pytest failure"
    return f"Review {event.category} feedback"
