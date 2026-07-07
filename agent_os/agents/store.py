"""Agent spec registry and session-scoped handoff persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .schemas import AgentSpec, AgentWorkPlan, HandoffContract
from agent_os.sessions.schemas import AgentOSValidationError, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def load_agent_spec(path: str | Path) -> AgentSpec:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentOSStorageError(f"Unable to read agent spec {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentOSStorageError(f"Invalid JSON in agent spec {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentOSStorageError(f"Expected object in agent spec {target}")
    return AgentSpec.from_dict(data)


class AgentSpecRegistry:
    """Read-only registry for static AgentSpec files."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def list_specs(self) -> list[AgentSpec]:
        if not self.root.exists():
            return []
        specs = [load_agent_spec(path) for path in sorted(self.root.glob("*.json"))]
        roles = [spec.role for spec in specs]
        duplicates = sorted({role for role in roles if roles.count(role) > 1})
        if duplicates:
            raise AgentOSValidationError(f"duplicate agent specs for roles: {duplicates}")
        return specs

    def get(self, role: str) -> AgentSpec:
        for spec in self.list_specs():
            if spec.role == role:
                return spec
        raise AgentOSStorageError(f"agent spec {role} not found")

    def validate_handoff(self, contract: HandoffContract) -> None:
        from_spec = self.get(contract.from_role)
        self.get(contract.to_role)
        self.get(contract.approval_role)
        if contract.to_role not in from_spec.handoff_targets:
            raise AgentOSValidationError(
                f"{contract.from_role} cannot hand off to {contract.to_role}"
            )


class HandoffStore:
    """Persist handoff contracts inside an existing AgentSession directory."""

    def __init__(
        self, session_store: AgentSessionStore, spec_registry: AgentSpecRegistry | None = None
    ):
        self.session_store = session_store
        self.spec_registry = spec_registry

    def create_handoff(
        self,
        *,
        session_id: str,
        from_role: str,
        to_role: str,
        approval_role: str,
        task: str,
        context_summary: str,
        acceptance_criteria: list[str],
        required_artifacts: list[str] | None = None,
        blocked_actions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        handoff_id: str | None = None,
    ) -> HandoffContract:
        self._ensure_session_exists(session_id)
        contract = HandoffContract.from_dict(
            HandoffContract(
                handoff_id=handoff_id
                or _stable_id("handoff", session_id, from_role, to_role, task, utc_now_iso()),
                session_id=session_id,
                from_role=from_role,
                to_role=to_role,
                approval_role=approval_role,
                task=task,
                context_summary=context_summary,
                acceptance_criteria=acceptance_criteria,
                required_artifacts=required_artifacts or [],
                blocked_actions=blocked_actions or [],
                metadata=metadata or {},
            ).to_dict()
        )
        if self.spec_registry is not None:
            self.spec_registry.validate_handoff(contract)
        handoffs = self.read_handoffs(session_id)
        if any(item.handoff_id == contract.handoff_id for item in handoffs):
            raise AgentOSValidationError(f"handoff {contract.handoff_id} already exists")
        handoffs.append(contract)
        self._write_handoffs(session_id, handoffs)
        self.session_store.append_event(
            session_id,
            "handoff_recorded",
            {
                "handoff_id": contract.handoff_id,
                "from_role": contract.from_role,
                "to_role": contract.to_role,
                "approval_role": contract.approval_role,
            },
        )
        return contract

    def transition_handoff(
        self,
        *,
        session_id: str,
        handoff_id: str,
        to_status: str,
        actor: str,
        reason: str,
        evidence_paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HandoffContract:
        handoffs = self.read_handoffs(session_id)
        for index, contract in enumerate(handoffs):
            if contract.handoff_id == handoff_id:
                self._validate_transition_actor(contract, to_status=to_status, actor=actor)
                updated = contract.with_status(
                    status=to_status,
                    actor=actor,
                    reason=reason,
                    evidence_paths=evidence_paths or [],
                    metadata=metadata,
                )
                handoffs[index] = updated
                self._write_handoffs(session_id, handoffs)
                self.session_store.append_event(
                    session_id,
                    "handoff_transitioned",
                    {
                        "handoff_id": handoff_id,
                        "from_status": contract.status,
                        "to_status": updated.status,
                        "actor": actor,
                    },
                )
                return updated
        raise AgentOSStorageError(f"handoff {handoff_id} not found")

    @staticmethod
    def _validate_transition_actor(
        contract: HandoffContract, *, to_status: str, actor: str
    ) -> None:
        if to_status == "accepted" and actor != contract.to_role:
            raise AgentOSValidationError("handoff acceptance must be recorded by to_role")
        if to_status == "completed" and actor != contract.approval_role:
            raise AgentOSValidationError("handoff completion must be recorded by approval_role")
        if to_status in {"rejected", "blocked"} and actor not in {
            contract.from_role,
            contract.to_role,
            contract.approval_role,
        }:
            raise AgentOSValidationError(
                "handoff rejection or block must be recorded by a contract role"
            )

    def read_handoffs(self, session_id: str) -> list[HandoffContract]:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / "handoffs.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        records: list[HandoffContract] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {path}:{line_no}")
            records.append(HandoffContract.from_dict(item))
        return records

    def _write_handoffs(self, session_id: str, handoffs: list[HandoffContract]) -> None:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / "handoffs.jsonl"
        try:
            with path.open("w", encoding="utf-8") as handle:
                for contract in handoffs:
                    handle.write(
                        json.dumps(contract.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                    )
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _ensure_session_exists(self, session_id: str) -> None:
        try:
            self.session_store.load_session(session_id)
        except (AgentOSStorageError, AgentOSValidationError) as exc:
            raise AgentOSStorageError(
                f"Cannot store handoff for missing or invalid session {session_id!r}: {exc}"
            ) from exc


class WorkPlanStore:
    """Persist work plans that group multiple handoff contracts."""

    def __init__(
        self, session_store: AgentSessionStore, spec_registry: AgentSpecRegistry | None = None
    ):
        self.session_store = session_store
        self.spec_registry = spec_registry

    def create_plan(
        self,
        *,
        session_id: str,
        objective: str,
        owner_role: str,
        handoff_ids: list[str],
        completion_criteria: list[str],
        risk_level: str = "medium",
        metadata: dict[str, Any] | None = None,
        plan_id: str | None = None,
    ) -> AgentWorkPlan:
        self._ensure_session_exists(session_id)
        if self.spec_registry is not None:
            self.spec_registry.get(owner_role)
        self._validate_handoff_ids(session_id, handoff_ids)
        plan = AgentWorkPlan.from_dict(
            AgentWorkPlan(
                plan_id=plan_id
                or _stable_id("work_plan", session_id, owner_role, objective, utc_now_iso()),
                session_id=session_id,
                objective=objective,
                owner_role=owner_role,
                handoff_ids=handoff_ids,
                completion_criteria=completion_criteria,
                risk_level=risk_level,
                metadata=metadata or {},
            ).to_dict()
        )
        plans = self.read_plans(session_id)
        if any(item.plan_id == plan.plan_id for item in plans):
            raise AgentOSValidationError(f"work plan {plan.plan_id} already exists")
        plans.append(plan)
        self._write_plans(session_id, plans)
        self.session_store.append_event(
            session_id,
            "work_plan_recorded",
            {
                "plan_id": plan.plan_id,
                "owner_role": plan.owner_role,
                "handoffs": len(plan.handoff_ids),
                "risk_level": plan.risk_level,
            },
        )
        return plan

    def transition_plan(
        self,
        *,
        session_id: str,
        plan_id: str,
        to_status: str,
        actor: str,
        reason: str,
        evidence_paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentWorkPlan:
        plans = self.read_plans(session_id)
        for index, plan in enumerate(plans):
            if plan.plan_id == plan_id:
                if to_status == "completed":
                    self._validate_all_handoffs_completed(session_id, plan.handoff_ids)
                updated = plan.with_status(
                    status=to_status,
                    actor=actor,
                    reason=reason,
                    evidence_paths=evidence_paths or [],
                    metadata=metadata,
                )
                plans[index] = updated
                self._write_plans(session_id, plans)
                self.session_store.append_event(
                    session_id,
                    "work_plan_transitioned",
                    {
                        "plan_id": plan_id,
                        "from_status": plan.status,
                        "to_status": updated.status,
                        "actor": actor,
                    },
                )
                return updated
        raise AgentOSStorageError(f"work plan {plan_id} not found")

    def read_plans(self, session_id: str) -> list[AgentWorkPlan]:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / "work_plans.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        records: list[AgentWorkPlan] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {path}:{line_no}")
            records.append(AgentWorkPlan.from_dict(item))
        return records

    def _validate_handoff_ids(self, session_id: str, handoff_ids: list[str]) -> None:
        if not handoff_ids:
            raise AgentOSValidationError("handoff_ids must be non-empty")
        existing = {
            handoff.handoff_id
            for handoff in HandoffStore(self.session_store).read_handoffs(session_id)
        }
        missing = [handoff_id for handoff_id in handoff_ids if handoff_id not in existing]
        if missing:
            raise AgentOSValidationError(f"work plan references missing handoffs: {missing}")

    def _validate_all_handoffs_completed(self, session_id: str, handoff_ids: list[str]) -> None:
        handoffs = {
            handoff.handoff_id: handoff
            for handoff in HandoffStore(self.session_store).read_handoffs(session_id)
        }
        incomplete = [
            handoff_id
            for handoff_id in handoff_ids
            if handoffs.get(handoff_id) is None or handoffs[handoff_id].status != "completed"
        ]
        if incomplete:
            raise AgentOSValidationError(f"work plan has incomplete handoffs: {incomplete}")

    def _write_plans(self, session_id: str, plans: list[AgentWorkPlan]) -> None:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / "work_plans.jsonl"
        try:
            with path.open("w", encoding="utf-8") as handle:
                for plan in plans:
                    handle.write(
                        json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                    )
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _ensure_session_exists(self, session_id: str) -> None:
        try:
            self.session_store.load_session(session_id)
        except (AgentOSStorageError, AgentOSValidationError) as exc:
            raise AgentOSStorageError(
                f"Cannot store work plan for missing or invalid session {session_id!r}: {exc}"
            ) from exc
