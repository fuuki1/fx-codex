"""Cross-log commit records for one full/compact decision transaction.

The full audit JSONL and the compact learning JSONL are separate append-only
files.  A PIT-eligible decision is visible only after both local batches are
durable and one matching marker has been fsynced to the full decision log.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
COMMIT_EVENT_TYPE = "decision_cross_log_commit"
DEFAULT_COMMIT_FILENAME = "briefing_decisions.jsonl"
COMPACT_FILENAMES = {
    "fusion": "briefing_journal.jsonl",
    "per_timeframe": "briefing_tf_journal.jsonl",
}


class DecisionCommitError(RuntimeError):
    """A decision transaction cannot be committed or verified."""


def commit_path_for(log_path: str | Path) -> Path:
    """Return the canonical full decision log containing commit markers."""

    target = Path(log_path)
    if target.name in {"briefing_journal.jsonl", "briefing_tf_journal.jsonl"}:
        return target.parent / DEFAULT_COMMIT_FILENAME
    return target


def compact_path_for_mode(commit_path: str | Path, mode: str) -> Path:
    """Return the compact journal named by one supported transaction mode."""

    compact_name = COMPACT_FILENAMES.get(str(mode))
    if compact_name is None:
        raise DecisionCommitError(f"unsupported decision transaction mode: {mode}")
    return commit_path_for(commit_path).parent / compact_name


def referenced_compact_paths(commit_path: str | Path) -> tuple[Path, ...]:
    """List compact journals referenced by valid markers in the full log."""

    target = commit_path_for(commit_path)
    commits = load_commits(target, verify_compact=False)
    modes = sorted({str(record.get("mode") or "") for record in commits.values()})
    return tuple(compact_path_for_mode(target, mode) for mode in modes)


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_decision_ids(values: Sequence[object]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise DecisionCommitError("decision IDs must be a sequence")
    decision_ids = [str(value or "").strip() for value in values]
    if not decision_ids or any(not value for value in decision_ids):
        raise DecisionCommitError("decision IDs must be non-empty")
    if len(set(decision_ids)) != len(decision_ids):
        raise DecisionCommitError("decision IDs must be unique within one transaction")
    return decision_ids


def decision_ids_sha256(values: Sequence[object]) -> str:
    return canonical_sha256(normalize_decision_ids(values))


def transaction_id_for(values: Sequence[object]) -> str:
    return decision_ids_sha256(values)


def append_commit(
    path: str | Path,
    *,
    decision_ids: Sequence[object],
    full_batch_sha256: str,
    compact_batch_sha256: str,
    mode: str,
    committed_at: datetime | None = None,
) -> dict[str, object]:
    """Fsync one visibility record after both append-only batches are durable."""

    normalized_ids = normalize_decision_ids(decision_ids)
    transaction_id = transaction_id_for(normalized_ids)
    normalized_mode = str(mode)
    compact_path_for_mode(path, normalized_mode)
    timestamp = (committed_at or datetime.now(UTC)).astimezone(UTC)
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": COMMIT_EVENT_TYPE,
        "decision_transaction_id": transaction_id,
        "decision_ids": normalized_ids,
        "decision_ids_sha256": decision_ids_sha256(normalized_ids),
        "full_batch_sha256": _required_sha256(full_batch_sha256, "full_batch_sha256"),
        "compact_batch_sha256": _required_sha256(
            compact_batch_sha256,
            "compact_batch_sha256",
        ),
        "mode": normalized_mode,
        "committed_at": timestamp.isoformat(),
    }
    record["commit_record_sha256"] = canonical_sha256(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def load_commits(
    path: str | Path,
    *,
    verify_compact: bool = True,
) -> dict[str, dict[str, object]]:
    """Load self-consistent records whose compact batch still matches exactly."""

    target = Path(path)
    commits: dict[str, dict[str, object]] = {}
    conflicts: set[str] = set()
    try:
        handle = target.open(encoding="utf-8")
    except OSError:
        return {}
    with handle:
        for line in handle:
            try:
                raw: Any = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            record = _validated_record(raw)
            if record is None:
                continue
            transaction_id = str(record["decision_transaction_id"])
            previous = commits.get(transaction_id)
            if previous is not None and previous != record:
                conflicts.add(transaction_id)
                commits.pop(transaction_id, None)
            elif transaction_id not in conflicts:
                commits[transaction_id] = record
    if not verify_compact:
        return commits
    verified_by_mode: dict[str, set[tuple[str, str, str]]] = {}
    output: dict[str, dict[str, object]] = {}
    for transaction_id, record in commits.items():
        mode = str(record.get("mode") or "")
        if mode not in verified_by_mode:
            try:
                compact_path = compact_path_for_mode(target, mode)
            except DecisionCommitError:
                verified_by_mode[mode] = set()
            else:
                verified_by_mode[mode] = _verified_compact_batches(compact_path)
        key = (
            transaction_id,
            str(record.get("decision_ids_sha256") or ""),
            str(record.get("compact_batch_sha256") or ""),
        )
        if key in verified_by_mode[mode]:
            output[transaction_id] = record
    return output


def commits_from_values(values: Sequence[object]) -> dict[str, dict[str, object]]:
    """Validate commit markers already captured in one stable raw prefix."""

    commits: dict[str, dict[str, object]] = {}
    conflicts: set[str] = set()
    for raw in values:
        record = _validated_record(raw)
        if record is None:
            continue
        transaction_id = str(record["decision_transaction_id"])
        previous = commits.get(transaction_id)
        if previous is not None and previous != record:
            conflicts.add(transaction_id)
            commits.pop(transaction_id, None)
        elif transaction_id not in conflicts:
            commits[transaction_id] = record
    return commits


def _verified_compact_batches(path: Path) -> set[tuple[str, str, str]]:
    verified: set[tuple[str, str, str]] = set()
    expected_mode = next(
        (mode for mode, filename in COMPACT_FILENAMES.items() if filename == path.name),
        "",
    )
    if not expected_mode:
        return verified
    pending_id = ""
    pending_rows: list[dict[str, object]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return verified
    with handle:
        for line in handle:
            try:
                raw: Any = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            batch_id = str(raw.get("journal_batch_id") or "")
            if raw.get("event_type") == "journal_batch_commit":
                indices = [row.get("journal_batch_index") for row in pending_rows]
                decision_ids = [row.get("decision_id") for row in pending_rows]
                try:
                    decision_ids_hash = decision_ids_sha256(decision_ids)
                    expected_transaction_id = transaction_id_for(decision_ids)
                except DecisionCommitError:
                    decision_ids_hash = ""
                    expected_transaction_id = ""
                batch_hash = canonical_sha256(pending_rows)
                transaction_id = str(raw.get("decision_transaction_id") or "")
                if (
                    batch_id
                    and batch_id == pending_id
                    and raw.get("journal_batch_size") == len(pending_rows)
                    and indices == list(range(len(pending_rows)))
                    and raw.get("decision_ids") == decision_ids
                    and raw.get("decision_ids_sha256") == decision_ids_hash
                    and raw.get("journal_batch_sha256") == batch_hash
                    and transaction_id == expected_transaction_id
                    and raw.get("requires_cross_log_commit") is True
                    and all(
                        row.get("decision_transaction_id") == transaction_id
                        and row.get("mode") == expected_mode
                        for row in pending_rows
                    )
                ):
                    verified.add((transaction_id, decision_ids_hash, batch_hash))
                pending_id = ""
                pending_rows = []
                continue
            if batch_id:
                if batch_id != pending_id:
                    pending_id = batch_id
                    pending_rows = []
                pending_rows.append(raw)
                continue
            pending_id = ""
            pending_rows = []
    return verified


def matching_commit(
    commits: Mapping[str, Mapping[str, object]],
    *,
    transaction_id: str,
    decision_ids: Sequence[object],
    batch_kind: str,
    batch_sha256: str,
    mode: str,
) -> bool:
    """Verify exact transaction membership, mode, and one batch digest."""

    if batch_kind not in {"full", "compact"}:
        raise ValueError("batch_kind must be full or compact")
    normalized_mode = str(mode)
    compact_path_for_mode(DEFAULT_COMMIT_FILENAME, normalized_mode)
    normalized_ids = normalize_decision_ids(decision_ids)
    expected_transaction = transaction_id_for(normalized_ids)
    if transaction_id != expected_transaction:
        return False
    record = commits.get(transaction_id)
    if not isinstance(record, Mapping):
        return False
    return (
        record.get("decision_ids") == normalized_ids
        and record.get("decision_ids_sha256") == decision_ids_sha256(normalized_ids)
        and record.get("mode") == normalized_mode
        and record.get(f"{batch_kind}_batch_sha256") == batch_sha256
    )


def _validated_record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("event_type") != COMMIT_EVENT_TYPE
    ):
        return None
    try:
        decision_ids = normalize_decision_ids(value.get("decision_ids", []))
        transaction_id = transaction_id_for(decision_ids)
        full_hash = _required_sha256(value.get("full_batch_sha256"), "full_batch_sha256")
        compact_hash = _required_sha256(value.get("compact_batch_sha256"), "compact_batch_sha256")
        compact_path_for_mode(DEFAULT_COMMIT_FILENAME, str(value.get("mode") or ""))
    except (DecisionCommitError, TypeError):
        return None
    if (
        value.get("decision_transaction_id") != transaction_id
        or value.get("decision_ids_sha256") != decision_ids_sha256(decision_ids)
        or value.get("full_batch_sha256") != full_hash
        or value.get("compact_batch_sha256") != compact_hash
    ):
        return None
    stored_hash = value.get("commit_record_sha256")
    unsigned = {key: item for key, item in value.items() if key != "commit_record_sha256"}
    if stored_hash != canonical_sha256(unsigned):
        return None
    return value


def _required_sha256(value: object, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64:
        raise DecisionCommitError(f"{name} must be SHA-256")
    try:
        bytes.fromhex(normalized)
    except ValueError as error:
        raise DecisionCommitError(f"{name} must be SHA-256") from error
    return normalized


__all__ = [
    "COMMIT_EVENT_TYPE",
    "COMPACT_FILENAMES",
    "DEFAULT_COMMIT_FILENAME",
    "DecisionCommitError",
    "append_commit",
    "canonical_sha256",
    "compact_path_for_mode",
    "commit_path_for",
    "commits_from_values",
    "decision_ids_sha256",
    "load_commits",
    "matching_commit",
    "normalize_decision_ids",
    "referenced_compact_paths",
    "transaction_id_for",
]
