"""Durable lockbox registry with single-use access enforcement.

The registry pins, per experiment, the manifest hash and the lockbox row
commitment at run time. After that:

- re-registering the same experiment with different content is rejected;
- an identical deterministic replay is allowed only while the lockbox is
  unopened (replays cannot change anything by construction);
- access is claimed with a create-exclusive marker before any outcome is
  computed, so the ``single_use`` policy survives crashes (a crash consumes
  the claim, matching the repository's existing claim-store semantics);
- every access attempt is appended to an access ledger with purpose/actor;
- once opened, the experiment is frozen: further runs and further accesses
  are rejected, and any change requires a new experiment_id.

This is local, not independent custody: it cannot prove that a human never
looked at raw outcome data outside the pipeline. That limitation is recorded
in the registry record itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure

CUSTODY_NOTE = "local single-writer registry; not independent custody or an external timestamp"


@dataclass(frozen=True)
class LockboxRecord:
    experiment_id: str
    manifest_sha256: str
    commitment_sha256: str
    inputs_sha256: str
    status: str
    created_at: str
    custody: str = CUSTODY_NOTE


@dataclass(frozen=True)
class LockboxState:
    """Evidence-oriented view used by promotion decisions."""

    registered: bool
    evaluated_once: bool | None
    reused_for_selection: bool | None
    access_count: int
    detail: str


class LockboxRegistry:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    # -- paths ----------------------------------------------------------------

    def _record_path(self, experiment_id: str) -> Path:
        return self.directory / f"{experiment_id}.lockbox.json"

    def _access_ledger_path(self, experiment_id: str) -> Path:
        return self.directory / f"{experiment_id}.access.jsonl"

    def _claim_path(self, experiment_id: str) -> Path:
        return self.directory / f"{experiment_id}.opened"

    # -- registration -----------------------------------------------------------

    def register(
        self,
        *,
        experiment_id: str,
        manifest_sha256: str,
        commitment_sha256: str,
        inputs_sha256: str,
        now: datetime | None = None,
    ) -> LockboxRecord:
        """Create-only registration; identical replay is allowed while unopened."""

        timestamp = _utc_now(now)
        record = LockboxRecord(
            experiment_id=experiment_id,
            manifest_sha256=manifest_sha256,
            commitment_sha256=commitment_sha256,
            inputs_sha256=inputs_sha256,
            status="unopened",
            created_at=timestamp.isoformat(),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._record_path(experiment_id)
        if path.exists():
            existing = self.load(experiment_id)
            if existing.status != "unopened":
                raise TypedFailure(
                    FailureReason.LOCKBOX_VIOLATION,
                    "experiment is frozen after lockbox access; use a new experiment_id",
                    context={"experiment_id": experiment_id, "status": existing.status},
                )
            if (
                existing.manifest_sha256 != manifest_sha256
                or existing.commitment_sha256 != commitment_sha256
                or existing.inputs_sha256 != inputs_sha256
            ):
                raise TypedFailure(
                    FailureReason.LOCKBOX_VIOLATION,
                    "experiment content changed after lockbox registration; "
                    "use a new experiment_id",
                    context={
                        "experiment_id": experiment_id,
                        "registered_manifest": existing.manifest_sha256,
                        "observed_manifest": manifest_sha256,
                    },
                )
            return existing
        payload = json.dumps(_record_dict(record), indent=2, sort_keys=True) + "\n"
        _exclusive_write(path, payload)
        return record

    def load(self, experiment_id: str) -> LockboxRecord:
        path = self._record_path(experiment_id)
        if not path.is_file():
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "lockbox record does not exist for this experiment",
                context={"experiment_id": experiment_id, "path": str(path)},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise TypedFailure(
                FailureReason.INVALID,
                "lockbox record is corrupt",
                context={"experiment_id": experiment_id},
            ) from error
        try:
            return LockboxRecord(
                experiment_id=str(payload["experiment_id"]),
                manifest_sha256=str(payload["manifest_sha256"]),
                commitment_sha256=str(payload["commitment_sha256"]),
                inputs_sha256=str(payload["inputs_sha256"]),
                status=str(payload["status"]),
                created_at=str(payload["created_at"]),
                custody=str(payload.get("custody", CUSTODY_NOTE)),
            )
        except KeyError as error:
            raise TypedFailure(
                FailureReason.INVALID,
                "lockbox record is missing required fields",
                context={"experiment_id": experiment_id, "missing": str(error)},
            ) from error

    # -- access ---------------------------------------------------------------

    def claim_access(
        self,
        *,
        experiment_id: str,
        manifest_sha256: str,
        purpose: str,
        actor: str,
        now: datetime | None = None,
    ) -> None:
        """Claim the single-use access before any outcome is computed."""

        if not purpose.strip() or not actor.strip():
            raise TypedFailure(
                FailureReason.INVALID,
                "lockbox access requires a recorded purpose and actor",
            )
        record = self.load(experiment_id)
        if record.manifest_sha256 != manifest_sha256:
            self._append_access(
                experiment_id,
                outcome="rejected_manifest_mismatch",
                purpose=purpose,
                actor=actor,
                manifest_sha256=manifest_sha256,
                now=now,
            )
            raise TypedFailure(
                FailureReason.LOCKBOX_VIOLATION,
                "manifest changed after lockbox registration",
                context={
                    "experiment_id": experiment_id,
                    "registered": record.manifest_sha256,
                    "observed": manifest_sha256,
                },
            )
        claim = self._claim_path(experiment_id)
        try:
            descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._append_access(
                experiment_id,
                outcome="rejected_already_opened",
                purpose=purpose,
                actor=actor,
                manifest_sha256=manifest_sha256,
                now=now,
            )
            raise TypedFailure(
                FailureReason.LOCKBOX_VIOLATION,
                "lockbox was already opened; single_use policy forbids re-evaluation",
                context={"experiment_id": experiment_id},
            ) from None
        try:
            os.write(
                descriptor,
                json.dumps(
                    {
                        "opened_at": _utc_now(now).isoformat(),
                        "purpose": purpose.strip(),
                        "actor": actor.strip(),
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._append_access(
            experiment_id,
            outcome="claimed",
            purpose=purpose,
            actor=actor,
            manifest_sha256=manifest_sha256,
            now=now,
        )
        self._update_status(experiment_id, "opened")

    def _update_status(self, experiment_id: str, status: str) -> None:
        record = self.load(experiment_id)
        payload = _record_dict(record)
        payload["status"] = status
        path = self._record_path(experiment_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        os.replace(temporary, path)

    def _append_access(
        self,
        experiment_id: str,
        *,
        outcome: str,
        purpose: str,
        actor: str,
        manifest_sha256: str,
        now: datetime | None,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "accessed_at": _utc_now(now).isoformat(),
            "experiment_id": experiment_id,
            "outcome": outcome,
            "purpose": purpose.strip(),
            "actor": actor.strip(),
            "manifest_sha256": manifest_sha256,
        }
        with open(self._access_ledger_path(experiment_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def access_records(self, experiment_id: str) -> list[dict[str, Any]]:
        path = self._access_ledger_path(experiment_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as error:
                raise TypedFailure(
                    FailureReason.INVALID,
                    "lockbox access ledger is corrupt",
                    context={"experiment_id": experiment_id},
                ) from error
            records.append(payload)
        return records

    # -- evidence ---------------------------------------------------------------

    def state(self, experiment_id: str) -> LockboxState:
        """Fail-closed evidence: missing/contradictory state yields None fields."""

        try:
            record = self.load(experiment_id)
        except TypedFailure as failure:
            return LockboxState(
                registered=False,
                evaluated_once=None,
                reused_for_selection=None,
                access_count=0,
                detail=failure.detail,
            )
        try:
            accesses = self.access_records(experiment_id)
        except TypedFailure as failure:
            return LockboxState(
                registered=True,
                evaluated_once=None,
                reused_for_selection=None,
                access_count=0,
                detail=failure.detail,
            )
        claims = [entry for entry in accesses if entry.get("outcome") == "claimed"]
        claim_marker = self._claim_path(experiment_id).exists()
        if record.status == "unopened":
            if claims or claim_marker:
                return LockboxState(
                    registered=True,
                    evaluated_once=None,
                    reused_for_selection=None,
                    access_count=len(claims),
                    detail="registry says unopened but access evidence exists",
                )
            return LockboxState(
                registered=True,
                evaluated_once=False,
                reused_for_selection=False,
                access_count=0,
                detail="lockbox is registered and untouched",
            )
        if record.status == "opened":
            if len(claims) == 1 and claim_marker:
                return LockboxState(
                    registered=True,
                    evaluated_once=True,
                    reused_for_selection=False,
                    access_count=1,
                    detail="lockbox was claimed exactly once with a recorded purpose",
                )
            return LockboxState(
                registered=True,
                evaluated_once=None,
                reused_for_selection=None,
                access_count=len(claims),
                detail="opened status without exactly one recorded claim; ledger incomplete",
            )
        return LockboxState(
            registered=True,
            evaluated_once=None,
            reused_for_selection=None,
            access_count=len(claims),
            detail=f"unknown lockbox status: {record.status}",
        )


def verify_lockbox_file(path: str | Path, *, expected_sha256: str) -> dict[str, Any]:
    """Verify a bundle's lockbox.json against an expected hash before use."""

    file_path = Path(path)
    if not file_path.is_file():
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "bundle lockbox.json is missing",
            context={"path": str(file_path)},
        )
    observed = hashlib.sha256(file_path.read_bytes()).hexdigest()
    if observed != expected_sha256:
        raise TypedFailure(
            FailureReason.HASH_MISMATCH,
            "bundle lockbox.json does not match its recorded hash",
            context={"expected": expected_sha256, "observed": observed},
        )
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypedFailure(FailureReason.INVALID, "lockbox.json must be a JSON object")
    return payload


def _record_dict(record: LockboxRecord) -> dict[str, Any]:
    return {
        "experiment_id": record.experiment_id,
        "manifest_sha256": record.manifest_sha256,
        "commitment_sha256": record.commitment_sha256,
        "inputs_sha256": record.inputs_sha256,
        "status": record.status,
        "created_at": record.created_at,
        "custody": record.custody,
    }


def _exclusive_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, content.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise TypedFailure(FailureReason.INVALID, "timestamps must be timezone-aware")
    return value.astimezone(UTC)
