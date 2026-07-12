"""Durable, append-only, tamper-evident trial ledger.

Every attempted trial — succeeded, failed or aborted — is appended as one
JSONL line that embeds the SHA-256 of the previous line, forming a hash chain.
A sidecar head file pins the chain tip so that tail truncation is detected.
There is deliberately no update or delete API; the only mutation is append,
and a duplicate ``trial_id`` is rejected. Editing, reordering, deleting or
truncating the file breaks verification with a typed failure, which downstream
promotion logic treats as "no performance claim possible".

This ledger records search breadth honestly: multiple-testing corrections
(DSR/PBO) must receive the number of distinct candidates recorded here, never
a caller-supplied smaller number.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure

LEDGER_SCHEMA_VERSION = 1
TRIAL_STATUSES = frozenset({"succeeded", "failed", "aborted"})
_GENESIS = "0" * 64


@dataclass(frozen=True)
class TrialLedgerEntry:
    trial_id: str
    experiment_id: str
    parent_trial_id: str | None
    started_at: str
    finished_at: str
    status: str
    git_commit: str
    manifest_hash: str
    dataset_hash: str
    feature_hash: str
    label_hash: str
    split_hash: str
    model_family: str
    hyperparameters: Mapping[str, Any]
    seed: int
    metrics: Mapping[str, Any] | None
    cost_metrics: Mapping[str, Any] | None
    failure_reason: Mapping[str, Any] | None
    selected: bool
    lockbox_accessed: bool = False
    promotion_claimed: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trial_id.strip():
            raise TypedFailure(FailureReason.INVALID, "trial_id is required")
        if not self.experiment_id.strip():
            raise TypedFailure(FailureReason.INVALID, "experiment_id is required")
        if self.status not in TRIAL_STATUSES:
            raise TypedFailure(
                FailureReason.INVALID,
                "trial status must be succeeded, failed or aborted",
                context={"observed": self.status},
            )
        if self.status == "failed" and self.failure_reason is None:
            raise TypedFailure(
                FailureReason.INCOMPLETE,
                "failed trials must record a failure_reason",
                context={"trial_id": self.trial_id},
            )
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise TypedFailure(
                    FailureReason.INVALID,
                    f"{name} must be a timezone-aware ISO-8601 string",
                    context={"trial_id": self.trial_id, "observed": value},
                )


@dataclass(frozen=True)
class LedgerAudit:
    path: str
    entry_count: int
    experiment_ids: tuple[str, ...]
    head_sha256: str


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _record_sha256(sequence: int, previous_sha256: str, entry: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({"sequence": sequence, "previous_sha256": previous_sha256, "entry": entry})
    ).hexdigest()


class TrialLedger:
    """Append-only JSONL ledger with a hash chain and a pinned head."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")

    # -- writing ------------------------------------------------------------

    def append(self, entry: TrialLedgerEntry) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._verified_records(handle.read())
                if any(record["entry"]["trial_id"] == entry.trial_id for record in records):
                    raise TypedFailure(
                        FailureReason.INVALID,
                        "trial_id already exists; ledger entries cannot be overwritten",
                        context={"trial_id": entry.trial_id},
                    )
                sequence = len(records)
                previous = records[-1]["sha256"] if records else _GENESIS
                payload = _json_ready(asdict(entry))
                sha256 = _record_sha256(sequence, previous, payload)
                record = {
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "sequence": sequence,
                    "previous_sha256": previous,
                    "entry": payload,
                    "sha256": sha256,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                self._write_head(sha256, sequence)
                return sha256
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_head(self, sha256: str, sequence: int) -> None:
        temporary = self.head_path.with_name(f".{self.head_path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"sha256": sha256, "sequence": sequence}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.head_path)

    # -- reading ------------------------------------------------------------

    def verify(self) -> LedgerAudit:
        if not self.path.exists():
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "trial ledger file does not exist",
                context={"path": str(self.path)},
            )
        records = self._verified_records(self.path.read_text(encoding="utf-8"))
        if not records:
            raise TypedFailure(
                FailureReason.INCOMPLETE,
                "trial ledger is empty",
                context={"path": str(self.path)},
            )
        head = self._read_head()
        tip = records[-1]
        if head["sha256"] != tip["sha256"] or head["sequence"] != tip["sequence"]:
            raise TypedFailure(
                FailureReason.LINEAGE_BROKEN,
                "ledger head does not match the last record; truncation or rollback detected",
                context={"head": head, "tip_sequence": tip["sequence"]},
            )
        experiment_ids = tuple(sorted({record["entry"]["experiment_id"] for record in records}))
        return LedgerAudit(
            path=str(self.path),
            entry_count=len(records),
            experiment_ids=experiment_ids,
            head_sha256=tip["sha256"],
        )

    def entries(self) -> list[dict[str, Any]]:
        self.verify()
        records = self._verified_records(self.path.read_text(encoding="utf-8"))
        return [dict(record["entry"]) for record in records]

    def entries_for_experiment(self, experiment_id: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries() if entry["experiment_id"] == experiment_id]

    def distinct_candidate_count(self, experiment_id: str) -> int:
        """Search breadth for one experiment: distinct recorded candidates."""

        candidates = {
            str(entry.get("extra", {}).get("candidate_id", entry["trial_id"]))
            for entry in self.entries_for_experiment(experiment_id)
        }
        return len(candidates)

    # -- internals ----------------------------------------------------------

    def _read_head(self) -> dict[str, Any]:
        if not self.head_path.exists():
            raise TypedFailure(
                FailureReason.LINEAGE_BROKEN,
                "ledger head sidecar is missing for a non-empty ledger",
                context={"path": str(self.head_path)},
            )
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise TypedFailure(
                FailureReason.LINEAGE_BROKEN,
                "ledger head sidecar is corrupt",
                context={"path": str(self.head_path)},
            ) from error
        if not isinstance(head, dict) or "sha256" not in head or "sequence" not in head:
            raise TypedFailure(
                FailureReason.LINEAGE_BROKEN,
                "ledger head sidecar has an unexpected shape",
                context={"path": str(self.head_path)},
            )
        return head

    def _verified_records(self, content: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        previous = _GENESIS
        for line_number, line in enumerate(content.splitlines()):
            if not line.strip():
                raise TypedFailure(
                    FailureReason.HASH_MISMATCH,
                    "ledger contains a blank line",
                    context={"line": line_number},
                )
            try:
                record = json.loads(line)
            except ValueError as error:
                raise TypedFailure(
                    FailureReason.HASH_MISMATCH,
                    "ledger line is not valid JSON",
                    context={"line": line_number},
                ) from error
            expected_keys = {"schema_version", "sequence", "previous_sha256", "entry", "sha256"}
            if not isinstance(record, dict) or set(record) != expected_keys:
                raise TypedFailure(
                    FailureReason.HASH_MISMATCH,
                    "ledger record has an unexpected shape",
                    context={"line": line_number},
                )
            if record["schema_version"] != LEDGER_SCHEMA_VERSION:
                raise TypedFailure(
                    FailureReason.INVALID,
                    "ledger schema version mismatch",
                    context={"line": line_number, "observed": record["schema_version"]},
                )
            if record["sequence"] != len(records):
                raise TypedFailure(
                    FailureReason.LINEAGE_BROKEN,
                    "ledger sequence is not contiguous; deletion or reordering detected",
                    context={"line": line_number, "observed": record["sequence"]},
                )
            if record["previous_sha256"] != previous:
                raise TypedFailure(
                    FailureReason.LINEAGE_BROKEN,
                    "ledger chain linkage is broken",
                    context={"line": line_number},
                )
            recomputed = _record_sha256(
                record["sequence"], record["previous_sha256"], record["entry"]
            )
            if recomputed != record["sha256"]:
                raise TypedFailure(
                    FailureReason.HASH_MISMATCH,
                    "ledger record hash does not match its content; edit detected",
                    context={"line": line_number},
                )
            records.append(record)
            previous = record["sha256"]
        return records


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value
