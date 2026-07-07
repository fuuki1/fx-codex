"""Command line interface for Agent OS session, feedback, and eval recording."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .agents.store import AgentSpecRegistry, HandoffStore, WorkPlanStore
from .candidates.store import CandidateArtifactStore
from .evals.runner import EvalStore, load_actuals, load_eval_suite, run_eval_suite
from .feedback.collectors import (
    collect_git_diff_summary,
    collect_github_review,
    collect_trader_events_jsonl,
    persist_collected_feedback,
)
from .feedback.store import FeedbackStore
from .sessions.schemas import DecisionRecord, ToolCall, utc_now_iso
from .sessions.store import AgentSessionStore
from .shadow.runner import ShadowStore, load_shadow_proposal, run_shadow
from .skills.registry import SkillRegistry
from .skills.schemas import SkillRecord

DEFAULT_STORE_ROOT = Path("runs/agent_sessions")
DEFAULT_EVAL_ROOT = Path("runs/eval_runs")
DEFAULT_EVAL_SUITE = Path("agent_os/evals/suite.yaml")
DEFAULT_SHADOW_ROOT = Path("runs/shadow_runs")
DEFAULT_SKILL_ROOT = Path("runs/skill_registry")
DEFAULT_AGENT_SPEC_ROOT = Path("agent_os/agents/specs")


def _json_arg(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-os", description="Agent OS session recorder")
    parser.add_argument("--store-root", default=str(DEFAULT_STORE_ROOT), help="session store root")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create a session")
    start.add_argument("--role", required=True)
    start.add_argument("--request", required=True)
    start.add_argument("--repo", default=".")
    start.add_argument("--metadata-json", type=_json_arg, default={})
    start.add_argument("--session-id")

    finish = sub.add_parser("finish", help="finish a session")
    finish.add_argument("session_id")
    finish.add_argument(
        "--status", required=True, choices=["completed", "failed", "blocked", "archived"]
    )
    finish.add_argument("--reason", default="")

    show = sub.add_parser("show", help="print session.json")
    show.add_argument("session_id")

    tool = sub.add_parser("record-tool", help="append a tool audit record")
    tool.add_argument("session_id")
    tool.add_argument("--tool-call-id", required=True)
    tool.add_argument("--tool", required=True)
    tool.add_argument("--cwd", default=".")
    tool.add_argument("--input-json", type=_json_arg, default={})
    tool.add_argument("--started-at", default="")
    tool.add_argument("--ended-at", default="")
    tool.add_argument("--exit-code", type=int)
    tool.add_argument("--stdout-summary", default="")
    tool.add_argument("--stderr-summary", default="")
    tool.add_argument("--changed-file", action="append", default=[])
    tool.add_argument("--approval-id")
    tool.add_argument(
        "--status", choices=["success", "failed", "blocked", "skipped"], default="success"
    )
    tool.add_argument("--error-summary", default="")
    tool.add_argument("--metadata-json", type=_json_arg, default={})

    decision = sub.add_parser("record-decision", help="append a decision record")
    decision.add_argument("session_id")
    decision.add_argument("--decision-id", required=True)
    decision.add_argument("--actor", required=True)
    decision.add_argument("--action", required=True)
    decision.add_argument(
        "--policy-result", required=True, choices=["allow", "deny", "approval_required"]
    )
    decision.add_argument("--rationale", required=True)
    decision.add_argument("--evidence", action="append", default=[])
    decision.add_argument("--metadata-json", type=_json_arg, default={})

    agent_parser = sub.add_parser("agent", help="agent spec and handoff commands")
    agent_parser.add_argument("--spec-root", default=str(DEFAULT_AGENT_SPEC_ROOT))
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("spec-list", help="list available agent specs")
    agent_spec_show = agent_sub.add_parser("spec-show", help="show an agent spec")
    agent_spec_show.add_argument("--role", required=True)
    handoff_create = agent_sub.add_parser("handoff-create", help="record a role handoff contract")
    handoff_create.add_argument("--session-id", required=True)
    handoff_create.add_argument("--from-role", required=True)
    handoff_create.add_argument("--to-role", required=True)
    handoff_create.add_argument("--approval-role", required=True)
    handoff_create.add_argument("--task", required=True)
    handoff_create.add_argument("--context-summary", required=True)
    handoff_create.add_argument("--acceptance", action="append", required=True)
    handoff_create.add_argument("--required-artifact", action="append", default=[])
    handoff_create.add_argument("--blocked-action", action="append", default=[])
    handoff_create.add_argument("--handoff-id")
    handoff_create.add_argument("--metadata-json", type=_json_arg, default={})
    handoff_transition = agent_sub.add_parser(
        "handoff-transition", help="transition a handoff status"
    )
    handoff_transition.add_argument("--session-id", required=True)
    handoff_transition.add_argument("--handoff-id", required=True)
    handoff_transition.add_argument("--to-status", required=True)
    handoff_transition.add_argument("--actor", required=True)
    handoff_transition.add_argument("--reason", required=True)
    handoff_transition.add_argument("--evidence", action="append", default=[])
    handoff_transition.add_argument("--metadata-json", type=_json_arg, default={})
    handoff_list = agent_sub.add_parser("handoff-list", help="list session handoffs")
    handoff_list.add_argument("--session-id", required=True)
    plan_create = agent_sub.add_parser("work-plan-create", help="record a multi-handoff work plan")
    plan_create.add_argument("--session-id", required=True)
    plan_create.add_argument("--objective", required=True)
    plan_create.add_argument("--owner-role", required=True)
    plan_create.add_argument("--handoff-id", action="append", required=True)
    plan_create.add_argument("--completion", action="append", required=True)
    plan_create.add_argument("--risk-level", default="medium")
    plan_create.add_argument("--plan-id")
    plan_create.add_argument("--metadata-json", type=_json_arg, default={})
    plan_transition = agent_sub.add_parser("work-plan-transition", help="transition a work plan")
    plan_transition.add_argument("--session-id", required=True)
    plan_transition.add_argument("--plan-id", required=True)
    plan_transition.add_argument("--to-status", required=True)
    plan_transition.add_argument("--actor", required=True)
    plan_transition.add_argument("--reason", required=True)
    plan_transition.add_argument("--evidence", action="append", default=[])
    plan_transition.add_argument("--metadata-json", type=_json_arg, default={})
    plan_list = agent_sub.add_parser("work-plan-list", help="list session work plans")
    plan_list.add_argument("--session-id", required=True)

    eval_parser = sub.add_parser("eval", help="eval suite commands")
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run", help="run a deterministic eval suite")
    eval_run.add_argument("--suite", default=str(DEFAULT_EVAL_SUITE))
    eval_run.add_argument("--actuals", required=True, help="JSON object keyed by case_id")
    eval_run.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    eval_run.add_argument("--eval-run-id")
    eval_run.add_argument("--metadata-json", type=_json_arg, default={})

    shadow_parser = sub.add_parser("shadow", help="shadow mode commands")
    shadow_sub = shadow_parser.add_subparsers(dest="shadow_command", required=True)
    shadow_run = shadow_sub.add_parser("run", help="run a no-side-effect shadow comparison")
    shadow_run.add_argument("--proposal", required=True)
    shadow_run.add_argument("--shadow-root", default=str(DEFAULT_SHADOW_ROOT))
    shadow_run.add_argument("--shadow-run-id")
    shadow_run.add_argument("--baseline-scores-json", type=_json_arg, default={})
    shadow_run.add_argument("--metadata-json", type=_json_arg, default={})

    feedback_parser = sub.add_parser("feedback", help="feedback collector commands")
    feedback_sub = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_collect = feedback_sub.add_parser(
        "collect", help="collect feedback events and candidates from a saved artifact"
    )
    feedback_collect.add_argument("--session-id", required=True)
    feedback_collect.add_argument(
        "--source",
        required=True,
        choices=["git-diff", "github-review", "trader-events"],
    )
    feedback_collect.add_argument("--input", required=True, help="collector input file")
    feedback_collect.add_argument("--metadata-json", type=_json_arg, default={})

    candidate_parser = sub.add_parser("candidate", help="candidate artifact commands")
    candidate_sub = candidate_parser.add_subparsers(dest="candidate_command", required=True)
    candidate_materialize = candidate_sub.add_parser(
        "materialize", help="materialize feedback candidates for review"
    )
    candidate_materialize.add_argument("--session-id", required=True)
    candidate_materialize.add_argument("--skill-version", default="0.1.0")
    candidate_materialize.add_argument("--metadata-json", type=_json_arg, default={})

    skill_parser = sub.add_parser("skill", help="skill lifecycle commands")
    skill_parser.add_argument("--skill-root", default=str(DEFAULT_SKILL_ROOT))
    skill_sub = skill_parser.add_subparsers(dest="skill_command", required=True)

    skill_register = skill_sub.add_parser("register", help="register a candidate skill")
    skill_register.add_argument("--skill-id", required=True)
    skill_register.add_argument("--version", required=True)
    skill_register.add_argument("--title", required=True)
    skill_register.add_argument("--summary", required=True)
    skill_register.add_argument("--owner", required=True)
    skill_register.add_argument("--feedback-id", action="append", default=[])
    skill_register.add_argument("--evidence", action="append", default=[])
    skill_register.add_argument("--body-path")
    skill_register.add_argument("--previous-version")
    skill_register.add_argument("--metadata-json", type=_json_arg, default={})

    skill_transition = skill_sub.add_parser("transition", help="transition a skill lifecycle state")
    skill_transition.add_argument("--skill-id", required=True)
    skill_transition.add_argument("--version", required=True)
    skill_transition.add_argument("--to-state", required=True)
    skill_transition.add_argument("--actor", required=True)
    skill_transition.add_argument("--reason", required=True)
    skill_transition.add_argument("--evidence", action="append", default=[])
    skill_transition.add_argument("--eval-run-id")
    skill_transition.add_argument("--shadow-run-id")
    skill_transition.add_argument("--review-note", default="")
    skill_transition.add_argument("--metadata-json", type=_json_arg, default={})

    skill_show = skill_sub.add_parser("show", help="show a skill record")
    skill_show.add_argument("--skill-id", required=True)
    skill_show.add_argument("--version", required=True)

    skill_sub.add_parser("list", help="list skill records")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = AgentSessionStore(args.store_root)

    if args.command == "start":
        session = store.create_session(
            role=args.role,
            user_request=args.request,
            repo=args.repo,
            metadata=args.metadata_json,
            session_id=args.session_id,
        )
        _print_json(session.to_dict())
        return 0

    if args.command == "finish":
        session = store.finish_session(args.session_id, args.status, reason=args.reason)
        _print_json(session.to_dict())
        return 0

    if args.command == "show":
        _print_json(store.load_session(args.session_id).to_dict())
        return 0

    if args.command == "record-tool":
        call = ToolCall(
            tool_call_id=args.tool_call_id,
            session_id=args.session_id,
            tool=args.tool,
            started_at=args.started_at or utc_now_iso(),
            ended_at=args.ended_at or utc_now_iso(),
            cwd=str(Path(args.cwd).expanduser().resolve()),
            input_redacted=args.input_json,
            exit_code=args.exit_code,
            stdout_summary=args.stdout_summary,
            stderr_summary=args.stderr_summary,
            changed_files=args.changed_file,
            approval_id=args.approval_id,
            status=args.status,
            error_summary=args.error_summary,
            metadata=args.metadata_json,
        )
        store.record_tool_call(call)
        _print_json(call.to_dict())
        return 0

    if args.command == "record-decision":
        decision = DecisionRecord(
            decision_id=args.decision_id,
            session_id=args.session_id,
            ts=utc_now_iso(),
            actor=args.actor,
            action=args.action,
            policy_result=args.policy_result,
            rationale=args.rationale,
            evidence_paths=args.evidence,
            metadata=args.metadata_json,
        )
        store.record_decision(decision)
        _print_json(decision.to_dict())
        return 0

    if args.command == "agent":
        specs = AgentSpecRegistry(args.spec_root)
        handoffs = HandoffStore(store, spec_registry=specs)
        work_plans = WorkPlanStore(store, spec_registry=specs)
        if args.agent_command == "spec-list":
            _print_json({"agent_specs": [spec.to_dict() for spec in specs.list_specs()]})
            return 0
        if args.agent_command == "spec-show":
            _print_json(specs.get(args.role).to_dict())
            return 0
        if args.agent_command == "handoff-create":
            from_spec = specs.get(args.from_role)
            to_spec = specs.get(args.to_role)
            approval_spec = specs.get(args.approval_role)
            if to_spec.role not in from_spec.handoff_targets:
                parser.error(f"{from_spec.role} cannot hand off to {to_spec.role}")
            contract = handoffs.create_handoff(
                session_id=args.session_id,
                from_role=from_spec.role,
                to_role=to_spec.role,
                approval_role=approval_spec.role,
                task=args.task,
                context_summary=args.context_summary,
                acceptance_criteria=args.acceptance,
                required_artifacts=args.required_artifact,
                blocked_actions=args.blocked_action,
                handoff_id=args.handoff_id,
                metadata=args.metadata_json,
            )
            _print_json(contract.to_dict())
            return 0
        if args.agent_command == "handoff-transition":
            updated = handoffs.transition_handoff(
                session_id=args.session_id,
                handoff_id=args.handoff_id,
                to_status=args.to_status,
                actor=args.actor,
                reason=args.reason,
                evidence_paths=args.evidence,
                metadata=args.metadata_json,
            )
            _print_json(updated.to_dict())
            return 0
        if args.agent_command == "handoff-list":
            _print_json(
                {
                    "handoffs": [
                        contract.to_dict() for contract in handoffs.read_handoffs(args.session_id)
                    ]
                }
            )
            return 0
        if args.agent_command == "work-plan-create":
            plan = work_plans.create_plan(
                session_id=args.session_id,
                objective=args.objective,
                owner_role=args.owner_role,
                handoff_ids=args.handoff_id,
                completion_criteria=args.completion,
                risk_level=args.risk_level,
                plan_id=args.plan_id,
                metadata=args.metadata_json,
            )
            _print_json(plan.to_dict())
            return 0
        if args.agent_command == "work-plan-transition":
            plan = work_plans.transition_plan(
                session_id=args.session_id,
                plan_id=args.plan_id,
                to_status=args.to_status,
                actor=args.actor,
                reason=args.reason,
                evidence_paths=args.evidence,
                metadata=args.metadata_json,
            )
            _print_json(plan.to_dict())
            return 0
        if args.agent_command == "work-plan-list":
            _print_json(
                {"work_plans": [plan.to_dict() for plan in work_plans.read_plans(args.session_id)]}
            )
            return 0

    if args.command == "eval" and args.eval_command == "run":
        suite = load_eval_suite(args.suite)
        actuals = load_actuals(args.actuals)
        run, _results = run_eval_suite(
            suite=suite,
            actuals_by_case=actuals,
            store=EvalStore(args.eval_root),
            eval_run_id=args.eval_run_id,
            metadata=args.metadata_json,
        )
        _print_json(run.to_dict())
        return 0

    if args.command == "shadow" and args.shadow_command == "run":
        proposal = load_shadow_proposal(args.proposal)
        _run, report = run_shadow(
            proposal=proposal,
            shadow_store=ShadowStore(args.shadow_root),
            session_store=store,
            shadow_run_id=args.shadow_run_id,
            baseline_scores=args.baseline_scores_json,
            metadata=args.metadata_json,
        )
        _print_json(report.to_dict())
        return 0

    if args.command == "feedback" and args.feedback_command == "collect":
        input_path = Path(args.input)
        input_text = input_path.read_text(encoding="utf-8")
        if args.source == "git-diff":
            events = collect_git_diff_summary(
                session_id=args.session_id,
                diff_text=input_text,
                evidence_path=input_path,
            )
        elif args.source == "github-review":
            events = collect_github_review(
                session_id=args.session_id,
                review_text=input_text,
                evidence_path=input_path,
            )
        elif args.source == "trader-events":
            events = collect_trader_events_jsonl(
                session_id=args.session_id,
                jsonl_text=input_text,
                evidence_path=input_path,
            )
        else:
            parser.error(f"unsupported feedback source {args.source}")
        collection_result = persist_collected_feedback(
            FeedbackStore(store),
            session_id=args.session_id,
            source=args.source,
            events=events,
            metadata=args.metadata_json,
        )
        _print_json(collection_result.to_dict())
        return 0

    if args.command == "candidate" and args.candidate_command == "materialize":
        candidates = FeedbackStore(store).read_candidates(args.session_id)
        materialization = CandidateArtifactStore(store).materialize_feedback_candidates(
            session_id=args.session_id,
            candidates=candidates,
            skill_version=args.skill_version,
            metadata=args.metadata_json,
        )
        _print_json(materialization.to_dict())
        return 0

    if args.command == "skill":
        registry = SkillRegistry(args.skill_root)
        if args.skill_command == "register":
            record = SkillRecord(
                skill_id=args.skill_id,
                version=args.version,
                state="candidate",
                title=args.title,
                summary=args.summary,
                owner=args.owner,
                created_from_feedback_ids=args.feedback_id,
                evidence_paths=args.evidence,
                previous_version=args.previous_version,
                body_path=args.body_path,
                metadata=args.metadata_json,
            )
            _print_json(registry.register_candidate(record).to_dict())
            return 0
        if args.skill_command == "transition":
            record, transition = registry.transition(
                skill_id=args.skill_id,
                version=args.version,
                to_state=args.to_state,
                actor=args.actor,
                reason=args.reason,
                evidence_paths=args.evidence,
                eval_run_id=args.eval_run_id,
                shadow_run_id=args.shadow_run_id,
                review_note=args.review_note,
                metadata=args.metadata_json,
            )
            _print_json({"skill": record.to_dict(), "transition": transition.to_dict()})
            return 0
        if args.skill_command == "show":
            _print_json(registry.get(args.skill_id, args.version).to_dict())
            return 0
        if args.skill_command == "list":
            _print_json({"skills": [record.to_dict() for record in registry.list_records()]})
            return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
