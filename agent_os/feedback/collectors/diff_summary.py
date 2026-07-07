"""Feedback collector for git diff summaries."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent_os.feedback.schemas import FeedbackEvent
from agent_os.sessions.schemas import utc_now_iso

RISK_PATH_PATTERNS = (
    r"^\.github/workflows/",
    r"^pyproject\.toml$",
    r"^requirements",
    r"^params_gate\.py$",
    r"^promote_params\.py$",
    r"^strategy_params\.json$",
    r"^fx_backtester/validation_pipeline\.py$",
    r"^fx_backtester/risk\.py$",
    r"^fx_backtester/kelly\.py$",
    r"^trader/app/",
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def collect_git_diff_summary(
    *,
    session_id: str,
    diff_text: str,
    evidence_path: str | Path,
    feedback_id_prefix: str = "fb",
) -> list[FeedbackEvent]:
    """Collect reviewable feedback from a git diff or diff summary.

    This collector does not run git. It accepts captured diff text so the same
    input can be replayed in tests, evals, or CI artifacts.
    """

    evidence = str(evidence_path)
    changed_files = _changed_files(diff_text)
    if not changed_files:
        return [
            FeedbackEvent(
                feedback_id=_stable_id(
                    feedback_id_prefix, session_id, "git_diff", "empty", evidence
                ),
                source="git_diff",
                session_id=session_id,
                severity="low",
                category="change_scope",
                summary="No changed files found in diff summary",
                evidence=[evidence],
                recommended_change={
                    "target": "issue",
                    "title": "Verify captured git diff input",
                },
                metadata={"changed_files": []},
                created_at=utc_now_iso(),
            )
        ]

    events: list[FeedbackEvent] = [
        FeedbackEvent(
            feedback_id=_stable_id(
                feedback_id_prefix,
                session_id,
                "git_diff",
                "scope",
                ",".join(changed_files),
                evidence,
            ),
            source="git_diff",
            session_id=session_id,
            severity=_scope_severity(changed_files),
            category="change_scope",
            summary=f"Diff touches {len(changed_files)} files: {', '.join(changed_files[:8])}",
            evidence=[evidence],
            recommended_change={
                "target": "memory",
                "title": "Remember reviewed change scope for this session",
            },
            metadata={"changed_files": changed_files},
            created_at=utc_now_iso(),
        )
    ]

    risky_files = [path for path in changed_files if _is_risky_path(path)]
    if risky_files:
        events.append(
            FeedbackEvent(
                feedback_id=_stable_id(
                    feedback_id_prefix,
                    session_id,
                    "git_diff",
                    "risk_gate",
                    ",".join(risky_files),
                    evidence,
                ),
                source="git_diff",
                session_id=session_id,
                severity="high",
                category="risk_gate_change",
                summary=f"Diff changes gated or operational files: {', '.join(risky_files[:8])}",
                evidence=[evidence],
                recommended_change={
                    "target": "decision_rule",
                    "title": "Require explicit gate review for risky diff paths",
                },
                metadata={"risky_files": risky_files},
                created_at=utc_now_iso(),
            )
        )

    source_files = [
        path
        for path in changed_files
        if path.endswith(".py") and not path.startswith("tests/") and "/tests/" not in path
    ]
    test_files = [path for path in changed_files if path.startswith("tests/") or "/tests/" in path]
    if source_files and not test_files:
        events.append(
            FeedbackEvent(
                feedback_id=_stable_id(
                    feedback_id_prefix,
                    session_id,
                    "git_diff",
                    "missing_tests",
                    ",".join(source_files),
                    evidence,
                ),
                source="git_diff",
                session_id=session_id,
                severity="medium",
                category="missing_test_coverage",
                summary=f"Source files changed without test files: {', '.join(source_files[:8])}",
                evidence=[evidence],
                recommended_change={
                    "target": "issue",
                    "title": "Review whether changed source files need tests",
                },
                metadata={"source_files_without_tests": source_files},
                created_at=utc_now_iso(),
            )
        )

    return events


def _changed_files(diff_text: str) -> list[str]:
    files: list[str] = []
    for raw_line in diff_text.splitlines():
        line = raw_line.strip()
        path = ""
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = _clean_path(parts[3])
        elif line.startswith("+++ b/") or line.startswith("--- a/"):
            path = _clean_path(line.split(maxsplit=1)[1])
        elif re.match(r"^[A-Z?]{1,2}\s+\S+", line):
            path = _clean_path(line.split(maxsplit=1)[1])
        elif _looks_like_path(line):
            path = _clean_path(line)
        if path and path != "/dev/null" and path not in files:
            files.append(path)
    return files


def _clean_path(path: str) -> str:
    cleaned = path.strip().strip('"')
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    return cleaned


def _looks_like_path(line: str) -> bool:
    if " " in line or "\t" in line:
        return False
    if line.startswith(("+", "-", "@@")):
        return False
    return "/" in line or line.endswith((".py", ".toml", ".yaml", ".yml", ".json", ".md"))


def _is_risky_path(path: str) -> bool:
    return any(re.search(pattern, path) for pattern in RISK_PATH_PATTERNS)


def _scope_severity(changed_files: list[str]) -> str:
    if any(_is_risky_path(path) for path in changed_files):
        return "high"
    if len(changed_files) >= 12:
        return "medium"
    return "low"
