"""Shadow Mode loading, execution, and persistence."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .comparator import compare_shadow
from .schemas import ShadowProposal, ShadowReport, ShadowRun
from agent_os.sessions.schemas import AgentOSValidationError, ToolCall, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


def load_shadow_proposal(path: str | Path) -> ShadowProposal:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentOSStorageError(f"Unable to read shadow proposal {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentOSStorageError(f"Invalid shadow proposal JSON in {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentOSStorageError(f"Expected object in shadow proposal {target}")
    return ShadowProposal.from_dict(data)


class ShadowStore:
    """Filesystem store for Shadow Mode artifacts."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def new_shadow_run_id(candidate: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("_")[:48] or "candidate"
        return f"shadow_{timestamp}_{slug}"

    def run_dir(self, shadow_run_id: str) -> Path:
        self._validate_shadow_run_id(shadow_run_id)
        return self.root / shadow_run_id

    def create_run(
        self,
        proposal: ShadowProposal,
        *,
        shadow_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ShadowRun:
        run = ShadowRun(
            shadow_run_id=shadow_run_id or self.new_shadow_run_id(proposal.candidate),
            base_session_id=proposal.base_session_id,
            candidate=proposal.candidate,
            status="running",
            started_at=utc_now_iso(),
            metadata=metadata or {},
        )
        self._ensure_run_dir(run.shadow_run_id)
        self.save_run(run)
        self.save_proposal(run.shadow_run_id, proposal)
        self.append_event(
            run.shadow_run_id,
            "shadow_run_created",
            {"base_session_id": proposal.base_session_id, "candidate": proposal.candidate},
        )
        return run

    def save_run(self, run: ShadowRun) -> None:
        run = ShadowRun.from_dict(run.to_dict())
        self._write_json(run.shadow_run_id, "shadow_run.json", run.to_dict())

    def load_run(self, shadow_run_id: str) -> ShadowRun:
        return ShadowRun.from_dict(self._read_json(shadow_run_id, "shadow_run.json"))

    def save_proposal(self, shadow_run_id: str, proposal: ShadowProposal) -> None:
        proposal = ShadowProposal.from_dict(proposal.to_dict())
        self._write_json(shadow_run_id, "shadow_proposal.json", proposal.to_dict())

    def load_proposal(self, shadow_run_id: str) -> ShadowProposal:
        return ShadowProposal.from_dict(self._read_json(shadow_run_id, "shadow_proposal.json"))

    def save_report(self, report: ShadowReport) -> None:
        report = ShadowReport.from_dict(report.to_dict())
        self._write_json(report.shadow_run_id, "shadow_report.json", report.to_dict())
        self.append_event(
            report.shadow_run_id,
            "shadow_report_recorded",
            {
                "outcome": report.outcome,
                "policy_compliance": report.scores.get("policy_compliance"),
                "tool_risk": report.scores.get("tool_risk"),
            },
        )

    def load_report(self, shadow_run_id: str) -> ShadowReport:
        return ShadowReport.from_dict(self._read_json(shadow_run_id, "shadow_report.json"))

    def finish_run(self, run: ShadowRun, report: ShadowReport) -> ShadowRun:
        status = "blocked" if report.outcome == "blocked" else "completed"
        finished = run.with_status(status)
        self.save_run(finished)
        self.append_event(
            run.shadow_run_id,
            "shadow_run_finished",
            {"status": finished.status, "outcome": report.outcome},
        )
        return finished

    def read_events(self, shadow_run_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(shadow_run_id, "events.jsonl", missing_ok=True)

    def append_event(self, shadow_run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(
            shadow_run_id,
            "events.jsonl",
            {"ts": utc_now_iso(), "event": event_type, "payload": dict(payload)},
        )

    def _ensure_run_dir(self, shadow_run_id: str) -> Path:
        path = self.run_dir(shadow_run_id)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to create {path}: {exc}") from exc
        return path

    def _write_json(self, shadow_run_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_run_dir(shadow_run_id)
        path = self.run_dir(shadow_run_id) / name
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _read_json(self, shadow_run_id: str, name: str) -> dict[str, Any]:
        path = self.run_dir(shadow_run_id) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AgentOSStorageError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentOSStorageError(f"Expected JSON object in {path}")
        return data

    def _append_jsonl(self, shadow_run_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_run_dir(shadow_run_id)
        path = self.run_dir(shadow_run_id) / name
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {path}: {exc}") from exc

    def _read_jsonl(
        self, shadow_run_id: str, name: str, *, missing_ok: bool
    ) -> list[dict[str, Any]]:
        path = self.run_dir(shadow_run_id) / name
        if missing_ok and not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {path}:{line_no}")
            records.append(item)
        return records

    @staticmethod
    def _validate_shadow_run_id(shadow_run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", shadow_run_id):
            raise AgentOSValidationError("shadow_run_id contains unsafe characters")


def run_shadow(
    *,
    proposal: ShadowProposal,
    shadow_store: ShadowStore,
    session_store: AgentSessionStore,
    shadow_run_id: str | None = None,
    baseline_scores: dict[str, float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[ShadowRun, ShadowReport]:
    """Run a side-effect-free shadow comparison."""

    # Ensure the real session exists before comparing against it.
    session_store.load_session(proposal.base_session_id)
    baseline_tool_calls: list[ToolCall] = session_store.read_tool_calls(proposal.base_session_id)
    run = shadow_store.create_run(proposal, shadow_run_id=shadow_run_id, metadata=metadata)
    report = compare_shadow(
        shadow_run_id=run.shadow_run_id,
        proposal=proposal,
        baseline_tool_calls=baseline_tool_calls,
        baseline_scores=baseline_scores or {},
    )
    shadow_store.save_report(report)
    finished = shadow_store.finish_run(run, report)
    return finished, report
