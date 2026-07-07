"""Shadow Mode comparator and side-effect policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .schemas import PROHIBITED_SIDE_EFFECTS, ShadowProposal, ShadowReport
from agent_os.sessions.schemas import ToolCall


def compare_shadow(
    *,
    shadow_run_id: str,
    proposal: ShadowProposal,
    baseline_tool_calls: Sequence[ToolCall] | None = None,
    baseline_scores: Mapping[str, float] | None = None,
) -> ShadowReport:
    """Compare a no-side-effect shadow proposal with baseline session behavior."""

    blocked_reasons = _blocked_reasons(proposal)
    policy_compliance = 0.0 if blocked_reasons else 1.0
    tool_risk = _tool_risk(proposal)
    task_success_estimate = _task_success_estimate(proposal)
    diff_size_delta = _diff_size_delta(proposal, baseline_scores or {})
    scores = {
        "policy_compliance": policy_compliance,
        "task_success_estimate": task_success_estimate,
        "tool_risk": tool_risk,
        "diff_size_delta": diff_size_delta,
    }
    notable = _notable_differences(proposal, baseline_tool_calls or [], diff_size_delta)
    if blocked_reasons:
        outcome = "blocked"
        recommendation = "reject_until_policy_clean"
    else:
        outcome = _outcome(scores)
        recommendation = _recommendation(outcome, task_success_estimate)
    return ShadowReport(
        shadow_run_id=shadow_run_id,
        base_session_id=proposal.base_session_id,
        candidate=proposal.candidate,
        outcome=outcome,
        scores=scores,
        notable_differences=notable,
        promotion_recommendation=recommendation,
        blocked_reasons=blocked_reasons,
        metadata={
            "eval_run_id": proposal.eval_run_id,
            "proposed_tool_count": len(proposal.proposed_tool_calls),
        },
    )


def _blocked_reasons(proposal: ShadowProposal) -> list[str]:
    reasons: list[str] = []
    for index, tool_call in enumerate(proposal.proposed_tool_calls):
        side_effect = _side_effect(tool_call)
        tool_name = str(tool_call.get("tool", f"tool[{index}]"))
        if side_effect in PROHIBITED_SIDE_EFFECTS:
            reasons.append(f"prohibited side effect {side_effect} in {tool_name}")
        if tool_call.get("execute") is True:
            reasons.append(f"shadow proposal attempts execution in {tool_name}")
    return reasons


def _tool_risk(proposal: ShadowProposal) -> float:
    if not proposal.proposed_tool_calls:
        return 0.0
    risky = sum(
        1 for call in proposal.proposed_tool_calls if _side_effect(call) in PROHIBITED_SIDE_EFFECTS
    )
    execute = sum(1 for call in proposal.proposed_tool_calls if call.get("execute") is True)
    return min(1.0, (risky + execute) / len(proposal.proposed_tool_calls))


def _side_effect(tool_call: Mapping[str, Any]) -> str:
    value = tool_call.get("side_effect", "proposal")
    return value if isinstance(value, str) else "proposal"


def _task_success_estimate(proposal: ShadowProposal) -> float:
    score = 0.4
    if proposal.plan.strip():
        score += 0.2
    if proposal.synthetic_diff.strip():
        score += 0.2
    if proposal.eval_run_id:
        score += 0.2
    return min(1.0, score)


def _diff_size_delta(proposal: ShadowProposal, baseline_scores: Mapping[str, float]) -> float:
    baseline_lines = float(baseline_scores.get("diff_lines", 0.0))
    shadow_lines = len([line for line in proposal.synthetic_diff.splitlines() if line.strip()])
    if baseline_lines <= 0:
        return 0.0
    return round((shadow_lines - baseline_lines) / baseline_lines, 4)


def _notable_differences(
    proposal: ShadowProposal, baseline_tool_calls: Sequence[ToolCall], diff_size_delta: float
) -> list[str]:
    notable: list[str] = []
    baseline_tools = {call.tool for call in baseline_tool_calls}
    proposed_tools = {
        str(call.get("tool"))
        for call in proposal.proposed_tool_calls
        if isinstance(call.get("tool"), str)
    }
    added_tools = sorted(proposed_tools - baseline_tools)
    if added_tools:
        notable.append("shadow proposes additional tools: " + ", ".join(added_tools))
    if diff_size_delta < 0:
        notable.append(f"shadow synthetic diff is smaller by {abs(diff_size_delta):.1%}")
    elif diff_size_delta > 0:
        notable.append(f"shadow synthetic diff is larger by {diff_size_delta:.1%}")
    if proposal.eval_run_id:
        notable.append(f"shadow references eval run {proposal.eval_run_id}")
    return notable


def _outcome(scores: Mapping[str, float]) -> str:
    if scores["policy_compliance"] < 1.0:
        return "blocked"
    if scores["task_success_estimate"] >= 0.8 and scores["tool_risk"] == 0:
        return "better"
    if scores["tool_risk"] > 0:
        return "worse"
    return "same"


def _recommendation(outcome: str, task_success_estimate: float) -> str:
    if outcome == "better" and task_success_estimate >= 0.8:
        return "keep_shadow_until_20_cases"
    if outcome == "same":
        return "continue_shadow"
    if outcome == "worse":
        return "revise_candidate"
    return "reject_until_policy_clean"
