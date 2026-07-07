"""Deterministic Eval Suite graders.

Graders inspect an already-produced artifact dictionary. They do not execute
commands or write files; command execution belongs to the Hands layer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .schemas import EvalCase, EvalResult
from agent_os.sessions.schemas import utc_now_iso


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def grade_case(case: EvalCase, actual: Mapping[str, Any], *, eval_run_id: str) -> EvalResult:
    """Grade a case with the grader declared in the case schema."""

    grader_type = str(case.grader.get("type"))
    started_at = utc_now_iso()
    try:
        if grader_type == "rule":
            passed, score, reasons, evidence = _grade_rule(case, actual)
        elif grader_type == "diff":
            passed, score, reasons, evidence = _grade_diff(case, actual)
        elif grader_type == "command":
            passed, score, reasons, evidence = _grade_command(case, actual)
        elif grader_type == "artifact":
            passed, score, reasons, evidence = _grade_artifact(case, actual)
        else:
            passed, score, reasons, evidence = False, 0.0, [f"unknown grader {grader_type}"], []
        error_summary = ""
    except Exception as exc:  # defensive: EvalRun must preserve failure cause
        passed, score, reasons, evidence = False, 0.0, [f"grader exception: {exc}"], []
        error_summary = str(exc)
    return EvalResult(
        result_id=_stable_id("eval_result", eval_run_id, case.case_id),
        eval_run_id=eval_run_id,
        case_id=case.case_id,
        grader_type=grader_type,
        passed=passed,
        score=score,
        reasons=reasons,
        evidence=evidence,
        started_at=started_at,
        ended_at=utc_now_iso(),
        error_summary=error_summary,
        metadata={"category": case.category, "role": case.role},
    )


def _threshold(case: EvalCase) -> float:
    value = case.grader.get("pass_threshold", 1.0)
    return float(value) if isinstance(value, (int, float)) else 1.0


def _score(checks: list[bool]) -> float:
    if not checks:
        return 1.0
    return sum(1 for item in checks if item) / len(checks)


def _text(actual: Mapping[str, Any]) -> str:
    value = actual.get("text", "")
    return value if isinstance(value, str) else str(value)


def _tool_names(actual: Mapping[str, Any]) -> list[str]:
    raw = actual.get("tool_calls", [])
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, Mapping):
                tool = item.get("tool")
                if isinstance(tool, str):
                    names.append(tool)
    return names


def _changed_files(actual: Mapping[str, Any]) -> list[str]:
    raw = actual.get("changed_files", [])
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _artifacts(actual: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = actual.get("artifacts", {})
    return raw if isinstance(raw, Mapping) else {}


def _grade_rule(
    case: EvalCase, actual: Mapping[str, Any]
) -> tuple[bool, float, list[str], list[str]]:
    expected = case.expected
    text = _text(actual)
    tools = _tool_names(actual)
    checks: list[bool] = []
    reasons: list[str] = []

    for required in expected.get("must_reference", []):
        if not isinstance(required, str):
            continue
        ok = required in text
        checks.append(ok)
        if not ok:
            reasons.append(f"missing required reference: {required}")

    for forbidden in expected.get("forbidden_text", []):
        if not isinstance(forbidden, str):
            continue
        ok = forbidden not in text
        checks.append(ok)
        if not ok:
            reasons.append(f"forbidden text present: {forbidden}")

    for forbidden_tool in expected.get("forbidden_tools", []):
        if not isinstance(forbidden_tool, str):
            continue
        ok = forbidden_tool not in tools
        checks.append(ok)
        if not ok:
            reasons.append(f"forbidden tool used: {forbidden_tool}")

    if expected.get("must_refuse_direct_write") is True:
        direct_write_tools = [tool for tool in tools if tool.startswith("filesystem.write")]
        ok = not direct_write_tools
        checks.append(ok)
        if not ok:
            reasons.append(f"direct write tool used: {', '.join(direct_write_tools)}")

    expected_status = expected.get("status")
    if isinstance(expected_status, str):
        ok = actual.get("status") == expected_status
        checks.append(ok)
        if not ok:
            reasons.append(f"expected status {expected_status!r}, got {actual.get('status')!r}")

    score = _score(checks)
    return score >= _threshold(case), score, reasons, []


def _grade_diff(
    case: EvalCase, actual: Mapping[str, Any]
) -> tuple[bool, float, list[str], list[str]]:
    expected = case.expected
    files = _changed_files(actual)
    checks: list[bool] = []
    reasons: list[str] = []

    for required in expected.get("required_changed_files", []):
        if not isinstance(required, str):
            continue
        ok = required in files
        checks.append(ok)
        if not ok:
            reasons.append(f"required changed file missing: {required}")

    for forbidden in expected.get("forbidden_changed_files", []):
        if not isinstance(forbidden, str):
            continue
        ok = forbidden not in files
        checks.append(ok)
        if not ok:
            reasons.append(f"forbidden changed file present: {forbidden}")

    max_changed_files = expected.get("max_changed_files")
    if isinstance(max_changed_files, int):
        ok = len(files) <= max_changed_files
        checks.append(ok)
        if not ok:
            reasons.append(f"changed file count {len(files)} exceeds {max_changed_files}")

    score = _score(checks)
    return score >= _threshold(case), score, reasons, files


def _grade_command(
    case: EvalCase, actual: Mapping[str, Any]
) -> tuple[bool, float, list[str], list[str]]:
    expected = case.expected
    command = actual.get("command", {})
    if not isinstance(command, Mapping):
        command = {}
    checks: list[bool] = []
    reasons: list[str] = []

    if "exit_code" in expected:
        ok = command.get("exit_code") == expected.get("exit_code")
        checks.append(ok)
        if not ok:
            reasons.append(
                f"expected exit_code {expected.get('exit_code')!r}, got {command.get('exit_code')!r}"
            )

    stdout = str(command.get("stdout", ""))
    stderr = str(command.get("stderr", ""))
    combined = stdout + "\n" + stderr
    for required in expected.get("must_contain", []):
        if not isinstance(required, str):
            continue
        ok = required in combined
        checks.append(ok)
        if not ok:
            reasons.append(f"command output missing: {required}")

    for forbidden in expected.get("must_not_contain", []):
        if not isinstance(forbidden, str):
            continue
        ok = forbidden not in combined
        checks.append(ok)
        if not ok:
            reasons.append(f"command output contains forbidden text: {forbidden}")

    score = _score(checks)
    return score >= _threshold(case), score, reasons, []


def _grade_artifact(
    case: EvalCase, actual: Mapping[str, Any]
) -> tuple[bool, float, list[str], list[str]]:
    expected = case.expected
    artifacts = _artifacts(actual)
    checks: list[bool] = []
    reasons: list[str] = []
    evidence: list[str] = []

    for required in expected.get("required_artifacts", []):
        if not isinstance(required, str):
            continue
        ok = required in artifacts
        checks.append(ok)
        if ok:
            evidence.append(required)
        else:
            reasons.append(f"required artifact missing: {required}")

    for forbidden in expected.get("forbidden_artifacts", []):
        if not isinstance(forbidden, str):
            continue
        ok = forbidden not in artifacts
        checks.append(ok)
        if not ok:
            reasons.append(f"forbidden artifact present: {forbidden}")

    score = _score(checks)
    return score >= _threshold(case), score, reasons, evidence
