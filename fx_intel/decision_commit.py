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
import logging
import os
from pathlib import Path
from typing import Any, cast

from .state_io import atomic_write_json

SCHEMA_VERSION = 1
COMMIT_EVENT_TYPE = "decision_cross_log_commit"
DECISION_BATCH_EVENT_TYPE = "decision_batch"
DEFAULT_COMMIT_FILENAME = "briefing_decisions.jsonl"
COMMIT_INDEX_SCHEMA_VERSION = 1
COMMIT_INDEX_KIND = "decision-commit-index"
DEFAULT_COMMIT_INDEX_FILENAME = "briefing_decision_commit_index.json"
COMPACT_FILENAMES = {
    "fusion": "briefing_journal.jsonl",
    "per_timeframe": "briefing_tf_journal.jsonl",
}
_LOGGER = logging.getLogger(__name__)


class DecisionCommitError(RuntimeError):
    """A decision transaction cannot be committed or verified."""


def commit_path_for(log_path: str | Path) -> Path:
    """Return the canonical full decision log containing commit markers."""

    target = Path(log_path)
    if target.name in {"briefing_journal.jsonl", "briefing_tf_journal.jsonl"}:
        return target.parent / DEFAULT_COMMIT_FILENAME
    return target


def commit_index_path_for(log_path: str | Path) -> Path:
    """Return the small derived index used by compact journal readers."""

    target = commit_path_for(log_path)
    if target.name == DEFAULT_COMMIT_FILENAME:
        return target.parent / DEFAULT_COMMIT_INDEX_FILENAME
    return target.parent / f".{target.name}.commit-index.json"


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
    try:
        _refresh_commit_index(target, expected_transaction_id=transaction_id)
    except Exception:
        _LOGGER.exception(
            "decision commit index refresh failed; full-log fallback remains authoritative"
        )
    return record


def load_commits(
    path: str | Path,
    *,
    verify_compact: bool = True,
) -> dict[str, dict[str, object]]:
    """Load commits backed by exact full wrappers and, optionally, compact batches."""

    target = Path(path)
    if not target.is_file():
        return {}
    indexed = _load_indexed_commits(target)
    if indexed is None:
        commits, _, _, _, _ = _stream_full_commit_state(target)
    else:
        commits, _, _, _, _ = indexed
    return _verify_compact_commits(target, commits) if verify_compact else commits


def _stream_full_commit_state(
    target: Path,
) -> tuple[dict[str, dict[str, object]], os.stat_result, int, int, str]:
    full_batches: dict[str, dict[str, object]] = {}
    full_conflicts: set[str] = set()
    candidate_commits: dict[str, dict[str, object]] = {}
    commit_conflicts: set[str] = set()
    try:
        handle = target.open("rb")
    except OSError:
        raise DecisionCommitError(f"decision log is unavailable: {target}")
    byte_offset = 0
    last_line_start = 0
    last_line_sha256 = hashlib.sha256(b"").hexdigest()
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        before = os.fstat(handle.fileno())
        for line in handle:
            line_start = byte_offset
            if not line.endswith(b"\n"):
                break
            byte_offset += len(line)
            try:
                raw: Any = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raw = None
            if raw is not None:
                _accumulate_commit_value(
                    raw,
                    full_batches=full_batches,
                    full_conflicts=full_conflicts,
                    candidate_commits=candidate_commits,
                    commit_conflicts=commit_conflicts,
                )
            last_line_start = line_start
            last_line_sha256 = hashlib.sha256(line).hexdigest()
        after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DecisionCommitError(f"decision log changed while indexing: {target}")
    commits = _matching_full_commits(full_batches, candidate_commits)
    return commits, after, byte_offset, last_line_start, last_line_sha256


def _verify_compact_commits(
    target: Path,
    commits: Mapping[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
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


def _load_indexed_commits(
    target: Path,
) -> tuple[dict[str, dict[str, object]], os.stat_result, int, int, str] | None:
    loaded = _read_commit_index(commit_index_path_for(target), target=target)
    if loaded is None:
        return None
    payload, cached_commits = loaded
    try:
        handle = target.open("rb")
    except OSError:
        return None
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        stat = os.fstat(handle.fileno())
        byte_offset = cast(int, payload["byte_offset"])
        if (
            stat.st_dev != cast(int, payload["device_id"])
            or stat.st_ino != cast(int, payload["inode"])
            or stat.st_size < byte_offset
            or (
                stat.st_size == byte_offset
                and stat.st_mtime_ns != cast(int, payload["source_mtime_ns"])
            )
        ):
            return None
        if not _index_boundary_matches(handle, payload):
            return None
        handle.seek(byte_offset)
        suffix = handle.read()
        after = os.fstat(handle.fileno())
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return None
        if suffix and not suffix.endswith(b"\n"):
            return None

    suffix_values: list[object] = []
    for line in suffix.splitlines():
        try:
            suffix_values.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            suffix_values.append(None)
    if _suffix_conflicts_with_cache(suffix_values, cached_commits):
        return None
    suffix_commits = commits_from_values(suffix_values)
    commits = dict(cached_commits)
    for transaction_id, record in suffix_commits.items():
        previous = commits.get(transaction_id)
        if previous is not None and previous != record:
            return None
        commits[transaction_id] = record

    end_offset = byte_offset + len(suffix)
    if suffix:
        relative_start = suffix.rfind(b"\n", 0, len(suffix) - 1) + 1
        last_line_start = byte_offset + relative_start
        last_line_sha256 = hashlib.sha256(suffix[relative_start:]).hexdigest()
    else:
        last_line_start = cast(int, payload["last_line_start_offset"])
        last_line_sha256 = str(payload["last_line_sha256"])
    return commits, after, end_offset, last_line_start, last_line_sha256


def _read_commit_index(
    index_path: Path,
    *,
    target: Path,
) -> tuple[dict[str, object], dict[str, dict[str, object]]] | None:
    try:
        with index_path.open(encoding="utf-8") as handle:
            payload: Any = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema_version") != COMMIT_INDEX_SCHEMA_VERSION
        or payload.get("index_kind") != COMMIT_INDEX_KIND
        or Path(str(payload.get("source_path") or "")).resolve() != target.resolve()
    ):
        return None
    stored_hash = payload.get("index_record_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "index_record_sha256"}
    if stored_hash != canonical_sha256(unsigned):
        return None
    integer_fields = (
        "device_id",
        "inode",
        "byte_offset",
        "last_line_start_offset",
        "source_mtime_ns",
    )
    if any(
        isinstance(payload.get(field), bool)
        or not isinstance(payload.get(field), int)
        or int(payload[field]) < 0
        for field in integer_fields
    ):
        return None
    if int(payload["last_line_start_offset"]) > int(payload["byte_offset"]):
        return None
    try:
        _required_sha256(payload.get("last_line_sha256"), "last_line_sha256")
    except DecisionCommitError:
        return None
    raw_commits = payload.get("commits")
    if not isinstance(raw_commits, list) or payload.get("commit_count") != len(raw_commits):
        return None
    commits: dict[str, dict[str, object]] = {}
    conflicts: set[str] = set()
    for raw in raw_commits:
        record = _validated_record(raw)
        if record is None:
            return None
        transaction_id = str(record["decision_transaction_id"])
        _accumulate_unique(commits, conflicts, transaction_id, record)
    if conflicts or len(commits) != len(raw_commits):
        return None
    return payload, commits


def _index_boundary_matches(handle: Any, payload: Mapping[str, object]) -> bool:
    byte_offset = cast(int, payload["byte_offset"])
    last_line_start = cast(int, payload["last_line_start_offset"])
    if byte_offset == 0:
        return (
            last_line_start == 0
            and payload.get("last_line_sha256") == hashlib.sha256(b"").hexdigest()
        )
    length = byte_offset - last_line_start
    if length <= 0:
        return False
    handle.seek(last_line_start)
    line = handle.read(length)
    return (
        len(line) == length
        and line.endswith(b"\n")
        and hashlib.sha256(line).hexdigest() == payload.get("last_line_sha256")
    )


def _suffix_conflicts_with_cache(
    values: Sequence[object],
    cached_commits: Mapping[str, Mapping[str, object]],
) -> bool:
    for raw in values:
        full = _full_batch_descriptor(raw)
        if full is not None:
            transaction_id, descriptor = full
            cached = cached_commits.get(transaction_id)
            if cached is not None and not (
                cached.get("decision_ids") == descriptor["decision_ids"]
                and cached.get("decision_ids_sha256") == descriptor["decision_ids_sha256"]
                and cached.get("full_batch_sha256") == descriptor["full_batch_sha256"]
                and cached.get("mode") == descriptor["mode"]
            ):
                return True
        record = _validated_record(raw)
        if record is not None:
            transaction_id = str(record["decision_transaction_id"])
            cached = cached_commits.get(transaction_id)
            if cached is not None and cached != record:
                return True
    return False


def _refresh_commit_index(
    target: Path,
    *,
    expected_transaction_id: str,
) -> None:
    index_path = commit_index_path_for(target)
    lock_path = Path(f"{index_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        indexed = _load_indexed_commits(target)
        if indexed is None:
            indexed = _stream_full_commit_state(target)
        commits, stat, byte_offset, last_line_start, last_line_sha256 = indexed
        if expected_transaction_id not in commits:
            raise DecisionCommitError(
                "new commit is not backed by an exact canonical full decision wrapper"
            )
        payload: dict[str, object] = {
            "schema_version": COMMIT_INDEX_SCHEMA_VERSION,
            "index_kind": COMMIT_INDEX_KIND,
            "source_path": str(target.resolve()),
            "device_id": stat.st_dev,
            "inode": stat.st_ino,
            "byte_offset": byte_offset,
            "last_line_start_offset": last_line_start,
            "last_line_sha256": last_line_sha256,
            "source_mtime_ns": stat.st_mtime_ns,
            "commit_count": len(commits),
            "commits": [commits[key] for key in sorted(commits)],
        }
        payload["index_record_sha256"] = canonical_sha256(payload)
        atomic_write_json(index_path, payload)
        directory_fd = os.open(index_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def commits_from_values(values: Sequence[object]) -> dict[str, dict[str, object]]:
    """Validate commit markers and their full wrappers in one stable prefix."""

    full_batches: dict[str, dict[str, object]] = {}
    full_conflicts: set[str] = set()
    candidate_commits: dict[str, dict[str, object]] = {}
    commit_conflicts: set[str] = set()
    for raw in values:
        _accumulate_commit_value(
            raw,
            full_batches=full_batches,
            full_conflicts=full_conflicts,
            candidate_commits=candidate_commits,
            commit_conflicts=commit_conflicts,
        )
    return _matching_full_commits(full_batches, candidate_commits)


def _accumulate_commit_value(
    raw: object,
    *,
    full_batches: dict[str, dict[str, object]],
    full_conflicts: set[str],
    candidate_commits: dict[str, dict[str, object]],
    commit_conflicts: set[str],
) -> None:
    full = _full_batch_descriptor(raw)
    if full is not None:
        transaction_id, descriptor = full
        _accumulate_unique(
            full_batches,
            full_conflicts,
            transaction_id,
            descriptor,
        )
    record = _validated_record(raw)
    if record is not None:
        transaction_id = str(record["decision_transaction_id"])
        _accumulate_unique(
            candidate_commits,
            commit_conflicts,
            transaction_id,
            record,
        )


def _full_batch_descriptor(
    raw: object,
) -> tuple[str, dict[str, object]] | None:
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("event_type") != DECISION_BATCH_EVENT_TYPE
    ):
        return None
    events = raw.get("events")
    if (
        not isinstance(events, list)
        or not events
        or not all(isinstance(event, dict) for event in events)
    ):
        return None
    try:
        decision_ids = normalize_decision_ids([event.get("decision_id") for event in events])
        transaction_id = transaction_id_for(decision_ids)
    except DecisionCommitError:
        return None
    modes = {str(event.get("mode") or "") for event in events}
    if len(modes) != 1:
        return None
    mode = next(iter(modes))
    try:
        compact_path_for_mode(DEFAULT_COMMIT_FILENAME, mode)
    except DecisionCommitError:
        return None
    descriptor: dict[str, object] = {
        "decision_ids": decision_ids,
        "decision_ids_sha256": decision_ids_sha256(decision_ids),
        "full_batch_sha256": canonical_sha256(events),
        "mode": mode,
    }
    if (
        raw.get("decision_transaction_id") != transaction_id
        or raw.get("decision_ids") != descriptor["decision_ids"]
        or raw.get("decision_ids_sha256") != descriptor["decision_ids_sha256"]
        or raw.get("decision_batch_sha256") != descriptor["full_batch_sha256"]
    ):
        return None
    return transaction_id, descriptor


def _accumulate_unique(
    records: dict[str, dict[str, object]],
    conflicts: set[str],
    transaction_id: str,
    value: dict[str, object],
) -> None:
    previous = records.get(transaction_id)
    if previous is not None and previous != value:
        conflicts.add(transaction_id)
        records.pop(transaction_id, None)
    elif transaction_id not in conflicts:
        records[transaction_id] = value


def _matching_full_commits(
    full_batches: Mapping[str, Mapping[str, object]],
    candidate_commits: Mapping[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for transaction_id, record in candidate_commits.items():
        full_batch = full_batches.get(transaction_id)
        if full_batch is not None and (
            record.get("decision_ids") == full_batch["decision_ids"]
            and record.get("decision_ids_sha256") == full_batch["decision_ids_sha256"]
            and record.get("full_batch_sha256") == full_batch["full_batch_sha256"]
            and record.get("mode") == full_batch["mode"]
        ):
            output[transaction_id] = record
    return output


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
    "COMMIT_INDEX_KIND",
    "COMPACT_FILENAMES",
    "DEFAULT_COMMIT_FILENAME",
    "DEFAULT_COMMIT_INDEX_FILENAME",
    "DecisionCommitError",
    "append_commit",
    "canonical_sha256",
    "compact_path_for_mode",
    "commit_index_path_for",
    "commit_path_for",
    "commits_from_values",
    "decision_ids_sha256",
    "load_commits",
    "matching_commit",
    "normalize_decision_ids",
    "referenced_compact_paths",
    "transaction_id_for",
]
