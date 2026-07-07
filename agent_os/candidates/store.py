"""Materialize reviewable Memory and Skill candidates from feedback records."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .schemas import CandidateMaterialization, MEMORY_TARGETS, MemoryCandidate
from agent_os.feedback.schemas import FeedbackCandidate
from agent_os.sessions.schemas import AgentOSValidationError, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError, AgentSessionStore


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return slug[:64] or "candidate"


class CandidateArtifactStore:
    """Filesystem-backed candidate artifact store.

    This store only writes review artifacts inside an existing session directory.
    It does not update durable Memory, install Skills, or change lifecycle state.
    """

    def __init__(self, session_store: AgentSessionStore):
        self.session_store = session_store

    def materialize_feedback_candidates(
        self,
        *,
        session_id: str,
        candidates: list[FeedbackCandidate],
        skill_version: str = "0.1.0",
        metadata: dict[str, Any] | None = None,
    ) -> CandidateMaterialization:
        self._ensure_session_exists(session_id)
        self._validate_skill_version(skill_version)
        normalized = [FeedbackCandidate.from_dict(candidate.to_dict()) for candidate in candidates]
        for candidate in normalized:
            if candidate.session_id != session_id:
                raise AgentOSValidationError(
                    f"candidate {candidate.candidate_id} belongs to {candidate.session_id}"
                )

        memory_ids: list[str] = []
        skill_paths: list[str] = []
        skipped: list[dict[str, Any]] = []
        existing_memory_ids = {
            candidate.candidate_id for candidate in self.read_memory_candidates(session_id)
        }

        for candidate in normalized:
            if candidate.status != "candidate":
                skipped.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "reason": f"status {candidate.status} is not materializable",
                    }
                )
                continue
            if candidate.target in MEMORY_TARGETS:
                if candidate.candidate_id in existing_memory_ids:
                    skipped.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "reason": "memory candidate already materialized",
                        }
                    )
                    continue
                memory = self._memory_candidate_from_feedback(candidate)
                self._append_jsonl(session_id, "memory_candidates.jsonl", memory.to_dict())
                existing_memory_ids.add(memory.candidate_id)
                memory_ids.append(memory.candidate_id)
                self.session_store.append_event(
                    session_id,
                    "memory_candidate_materialized",
                    {"candidate_id": memory.candidate_id, "target": memory.target},
                )
                continue
            if candidate.target == "skill":
                relative_path = self._materialize_skill_candidate(
                    session_id,
                    candidate,
                    skill_version=skill_version,
                    skipped=skipped,
                )
                if relative_path:
                    skill_paths.append(relative_path)
                continue
            skipped.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reason": f"target {candidate.target} is not a memory or skill artifact",
                }
            )

        for item in skipped:
            self.session_store.append_event(session_id, "candidate_materialization_skipped", item)

        result = CandidateMaterialization(
            materialization_id=_stable_id(
                "candidate_materialization",
                session_id,
                utc_now_iso(),
                *[candidate.candidate_id for candidate in normalized],
            ),
            session_id=session_id,
            memory_candidate_ids=memory_ids,
            skill_candidate_paths=skill_paths,
            skipped_candidates=skipped,
            metadata=metadata or {},
        )
        self._append_jsonl(session_id, "candidate_materializations.jsonl", result.to_dict())
        self.session_store.append_event(
            session_id,
            "candidate_materialization_finished",
            {
                "materialization_id": result.materialization_id,
                "memory_candidates": len(memory_ids),
                "skill_candidates": len(skill_paths),
                "skipped_candidates": len(skipped),
            },
        )
        return result

    def read_memory_candidates(self, session_id: str) -> list[MemoryCandidate]:
        return [
            MemoryCandidate.from_dict(item)
            for item in self._read_jsonl(session_id, "memory_candidates.jsonl", missing_ok=True)
        ]

    def read_materializations(self, session_id: str) -> list[CandidateMaterialization]:
        return [
            CandidateMaterialization.from_dict(item)
            for item in self._read_jsonl(
                session_id, "candidate_materializations.jsonl", missing_ok=True
            )
        ]

    def _memory_candidate_from_feedback(self, candidate: FeedbackCandidate) -> MemoryCandidate:
        body = "\n\n".join(
            [
                candidate.title,
                f"Rationale: {candidate.rationale}",
                "Evidence:\n" + "\n".join(f"- {item}" for item in candidate.evidence),
            ]
        )
        return MemoryCandidate(
            candidate_id=candidate.candidate_id,
            session_id=candidate.session_id,
            target=candidate.target,
            title=candidate.title,
            body=body,
            source_feedback_ids=list(candidate.source_feedback_ids),
            evidence=list(candidate.evidence),
            metadata=dict(candidate.metadata),
        )

    def _materialize_skill_candidate(
        self,
        session_id: str,
        candidate: FeedbackCandidate,
        *,
        skill_version: str,
        skipped: list[dict[str, Any]],
    ) -> str:
        skill_id = _slug(candidate.title)
        relative_path = Path("skill_candidates") / skill_id / "SKILL.md"
        target = self.session_store.session_dir(session_id) / relative_path
        body = self._skill_markdown(candidate, skill_id=skill_id, skill_version=skill_version)
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise AgentOSStorageError(f"Unable to read {target}: {exc}") from exc
            if existing != body:
                skipped.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "reason": f"skill candidate already exists at {relative_path}",
                    }
                )
                return ""
            skipped.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reason": f"skill candidate already materialized at {relative_path}",
                }
            )
            return ""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {target}: {exc}") from exc
        self.session_store.append_event(
            session_id,
            "skill_candidate_materialized",
            {
                "candidate_id": candidate.candidate_id,
                "skill_id": skill_id,
                "version": skill_version,
                "path": str(relative_path),
            },
        )
        return str(relative_path)

    @staticmethod
    def _skill_markdown(candidate: FeedbackCandidate, *, skill_id: str, skill_version: str) -> str:
        evidence = "\n".join(f"- {item}" for item in candidate.evidence) or "- none recorded"
        feedback_ids = "\n".join(f"- {item}" for item in candidate.source_feedback_ids)
        return (
            "---\n"
            f"skill_id: {skill_id}\n"
            f"version: {skill_version}\n"
            "state: candidate\n"
            "owner: skill_maintainer\n"
            "---\n\n"
            f"# {candidate.title}\n\n"
            "## Summary\n\n"
            f"{candidate.rationale}\n\n"
            "## Source Feedback\n\n"
            f"{feedback_ids}\n\n"
            "## Evidence\n\n"
            f"{evidence}\n\n"
            "## Review Notes\n\n"
            "- Keep this candidate inactive until eval and shadow evidence pass.\n"
        )

    def _append_jsonl(self, session_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / name
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {path}: {exc}") from exc

    def _read_jsonl(self, session_id: str, name: str, *, missing_ok: bool) -> list[dict[str, Any]]:
        self._ensure_session_exists(session_id)
        path = self.session_store.session_dir(session_id) / name
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

    def _ensure_session_exists(self, session_id: str) -> None:
        try:
            self.session_store.load_session(session_id)
        except (AgentOSStorageError, AgentOSValidationError) as exc:
            raise AgentOSStorageError(
                f"Cannot materialize candidates for missing or invalid session {session_id!r}: {exc}"
            ) from exc

    @staticmethod
    def _validate_skill_version(version: str) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+([.-][A-Za-z0-9]+)?", version):
            raise AgentOSValidationError("skill_version must be semver-like, for example 0.1.0")
