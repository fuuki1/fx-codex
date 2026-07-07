"""Skill lifecycle registry and transition gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .schemas import SkillRecord, SkillTransition
from agent_os.sessions.schemas import AgentOSValidationError, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"shadow", "rejected"},
    "shadow": {"active", "rejected"},
    "active": {"deprecated"},
    "deprecated": {"retired", "active"},
    "retired": set(),
    "rejected": set(),
}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


class SkillRegistry:
    """Filesystem-backed Skill registry.

    The registry stores lifecycle metadata only. It never installs or activates
    executable skills outside the registry file.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def register_candidate(self, record: SkillRecord) -> SkillRecord:
        record = SkillRecord.from_dict(record.to_dict())
        if record.state != "candidate":
            raise AgentOSValidationError("new skills must be registered in candidate state")
        records = self.list_records()
        if self._find(records, record.skill_id, record.version) is not None:
            raise AgentOSValidationError(f"skill {record.skill_id}@{record.version} already exists")
        records.append(record)
        self._save_records(records)
        self._append_event(
            "skill_registered",
            {"skill_id": record.skill_id, "version": record.version, "state": record.state},
        )
        return record

    def transition(
        self,
        *,
        skill_id: str,
        version: str,
        to_state: str,
        actor: str,
        reason: str,
        evidence_paths: list[str],
        eval_run_id: str | None = None,
        shadow_run_id: str | None = None,
        review_note: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[SkillRecord, SkillTransition]:
        if not actor.strip():
            raise AgentOSValidationError("actor is required for skill transitions")
        if not reason.strip():
            raise AgentOSValidationError("reason is required for skill transitions")
        if not evidence_paths:
            raise AgentOSValidationError("evidence_paths are required for skill transitions")

        records = self.list_records()
        index, current = self._find_with_index(records, skill_id, version)
        allowed = ALLOWED_TRANSITIONS.get(current.state, set())
        if to_state not in allowed:
            raise AgentOSValidationError(f"cannot transition {current.state} -> {to_state}")
        self._validate_transition_gate(
            records,
            current=current,
            to_state=to_state,
            eval_run_id=eval_run_id,
            shadow_run_id=shadow_run_id,
        )

        updated = current.with_transition(
            state=to_state,
            actor=actor,
            evidence_paths=evidence_paths,
            eval_run_id=eval_run_id,
            shadow_run_id=shadow_run_id,
            review_note=review_note,
            metadata=metadata,
        )
        transition = SkillTransition(
            transition_id=_stable_id(
                "skill_transition", skill_id, version, current.state, to_state, utc_now_iso()
            ),
            skill_id=skill_id,
            version=version,
            from_state=current.state,
            to_state=to_state,
            actor=actor,
            reason=reason,
            evidence_paths=list(evidence_paths),
            eval_run_id=eval_run_id,
            shadow_run_id=shadow_run_id,
            metadata=metadata or {},
        )
        records[index] = updated
        self._save_records(records)
        self._append_event("skill_transitioned", transition.to_dict())
        return updated, transition

    def get(self, skill_id: str, version: str) -> SkillRecord:
        record = self._find(self.list_records(), skill_id, version)
        if record is None:
            raise AgentOSStorageError(f"skill {skill_id}@{version} not found")
        return record

    def list_records(self) -> list[SkillRecord]:
        if not self.registry_path.exists():
            return []
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {self.registry_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AgentOSStorageError(f"Invalid JSON in {self.registry_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentOSStorageError(f"Expected object in {self.registry_path}")
        raw_records = data.get("skills", [])
        if not isinstance(raw_records, list):
            raise AgentOSStorageError("registry skills must be a list")
        return [SkillRecord.from_dict(item) for item in raw_records if isinstance(item, dict)]

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {self.events_path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(
                    f"Invalid JSONL in {self.events_path}:{line_no}: {exc}"
                ) from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {self.events_path}:{line_no}")
            records.append(item)
        return records

    def _validate_transition_gate(
        self,
        records: list[SkillRecord],
        *,
        current: SkillRecord,
        to_state: str,
        eval_run_id: str | None,
        shadow_run_id: str | None,
    ) -> None:
        if to_state == "active":
            if current.state not in {"shadow", "deprecated"}:
                raise AgentOSValidationError(
                    "skills can only become active from shadow or deprecated"
                )
            if not eval_run_id:
                raise AgentOSValidationError("eval_run_id is required for active skill promotion")
            if not shadow_run_id:
                raise AgentOSValidationError("shadow_run_id is required for active skill promotion")
            active = [
                record
                for record in records
                if record.skill_id == current.skill_id
                and record.version != current.version
                and record.state == "active"
            ]
            if active:
                raise AgentOSValidationError(f"active skill already exists for {current.skill_id}")
        if to_state == "shadow" and current.state != "candidate":
            raise AgentOSValidationError("shadow state can only be reached from candidate")

    def _save_records(self, records: list[SkillRecord]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "skills": [record.to_dict() for record in records],
        }
        try:
            self.registry_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {self.registry_path}: {exc}") from exc

    def _append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        event = {"ts": utc_now_iso(), "event": event_type, "payload": dict(payload)}
        try:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {self.events_path}: {exc}") from exc

    @staticmethod
    def _find(records: list[SkillRecord], skill_id: str, version: str) -> SkillRecord | None:
        for record in records:
            if record.skill_id == skill_id and record.version == version:
                return record
        return None

    @staticmethod
    def _find_with_index(
        records: list[SkillRecord], skill_id: str, version: str
    ) -> tuple[int, SkillRecord]:
        for index, record in enumerate(records):
            if record.skill_id == skill_id and record.version == version:
                return index, record
        raise AgentOSStorageError(f"skill {skill_id}@{version} not found")
