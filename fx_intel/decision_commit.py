"""Cross-log commit and receipt records for one decision transaction.

The full audit JSONL and compact learning JSONL are separate append-only files.
A PIT-eligible decision is visible only after the full batch, compact batch,
full-log commit, and compact receipt are durable.  The receipt pins the exact
full wrapper and commit line offsets and hashes, so compact readers need only
two bounded seeks into the large full log per transaction.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, cast, IO

SCHEMA_VERSION = 1
COMMIT_EVENT_TYPE = "decision_cross_log_commit"
RECEIPT_EVENT_TYPE = "decision_cross_log_receipt"
DECISION_BATCH_EVENT_TYPE = "decision_batch"
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
    full_batch_line_start_offset: int,
    full_batch_line_sha256: str,
    compact_batch_sha256: str,
    mode: str,
    committed_at: datetime | None = None,
) -> dict[str, object]:
    """Fsync a prepared compact receipt, then the final full visibility commit."""

    normalized_ids = normalize_decision_ids(decision_ids)
    transaction_id = transaction_id_for(normalized_ids)
    normalized_mode = str(mode)
    compact_path = compact_path_for_mode(path, normalized_mode)
    if (
        isinstance(full_batch_line_start_offset, bool)
        or not isinstance(full_batch_line_start_offset, int)
        or full_batch_line_start_offset < 0
    ):
        raise DecisionCommitError("full_batch_line_start_offset must be non-negative")
    normalized_full_line_sha256 = _required_sha256(
        full_batch_line_sha256,
        "full_batch_line_sha256",
    )
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
    target = commit_path_for(path).resolve()
    compact_path = compact_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    handles: dict[Path, BinaryIO] = {}
    try:
        for candidate in sorted({target, compact_path}):
            handle: BinaryIO = candidate.open("a+b", buffering=0)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            handles[candidate] = handle
        full_handle = handles[target]
        if not _full_batch_handle_reference_matches(
            full_handle,
            line_start_offset=full_batch_line_start_offset,
            line_sha256=normalized_full_line_sha256,
            transaction_id=transaction_id,
            decision_ids=normalized_ids,
            batch_sha256=str(record["full_batch_sha256"]),
            mode=normalized_mode,
        ):
            raise DecisionCommitError("full batch line reference is missing or mismatched")
        commit_payload = _serialized_json_line(record)
        commit_line_start_offset = os.lseek(full_handle.fileno(), 0, os.SEEK_END)
        commit_line_sha256 = hashlib.sha256(commit_payload).hexdigest()
        receipt: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_type": RECEIPT_EVENT_TYPE,
            "decision_transaction_id": transaction_id,
            "decision_ids": normalized_ids,
            "decision_ids_sha256": decision_ids_sha256(normalized_ids),
            "full_batch_sha256": record["full_batch_sha256"],
            "compact_batch_sha256": record["compact_batch_sha256"],
            "mode": normalized_mode,
            "committed_at": record["committed_at"],
            "full_commit_record_sha256": record["commit_record_sha256"],
            "full_batch_line_start_offset": full_batch_line_start_offset,
            "full_batch_line_sha256": normalized_full_line_sha256,
            "full_commit_line_start_offset": commit_line_start_offset,
            "full_commit_line_sha256": commit_line_sha256,
        }
        receipt["receipt_record_sha256"] = canonical_sha256(receipt)
        _append_locked_line(
            handles[compact_path],
            compact_path,
            _serialized_json_line(receipt),
        )
        actual_offset = _append_locked_line(
            full_handle,
            target,
            commit_payload,
        )
        if actual_offset != commit_line_start_offset:
            raise DecisionCommitError("full commit append offset changed while locked")
    finally:
        for handle in reversed(list(handles.values())):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
    return record


def _serialized_json_line(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _append_locked_line(
    handle: BinaryIO,
    path: Path,
    serialized: bytes,
) -> int:
    line_start_offset = os.lseek(handle.fileno(), 0, os.SEEK_END)
    written = handle.write(serialized)
    if written != len(serialized):
        raise OSError(f"short append to {path}: {written}/{len(serialized)}")
    os.fsync(handle.fileno())
    return line_start_offset


def load_commits(
    path: str | Path,
    *,
    verify_compact: bool = True,
) -> dict[str, dict[str, object]]:
    """Load commits backed by exact full wrappers and, optionally, compact batches."""

    target = Path(path)
    full_batches: dict[str, dict[str, object]] = {}
    full_conflicts: set[str] = set()
    candidate_commits: dict[str, dict[str, object]] = {}
    commit_conflicts: set[str] = set()
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
            _accumulate_commit_value(
                raw,
                full_batches=full_batches,
                full_conflicts=full_conflicts,
                candidate_commits=candidate_commits,
                commit_conflicts=commit_conflicts,
            )
    commits = _matching_full_commits(full_batches, candidate_commits)
    if not verify_compact:
        return commits
    verified_by_mode: dict[str, dict[str, dict[str, object]]] = {}
    output: dict[str, dict[str, object]] = {}
    for transaction_id, record in commits.items():
        mode = str(record.get("mode") or "")
        if mode not in verified_by_mode:
            try:
                compact_path = compact_path_for_mode(target, mode)
            except DecisionCommitError:
                verified_by_mode[mode] = {}
            else:
                verified_by_mode[mode] = load_compact_receipts(
                    compact_path,
                    full_path=target,
                )
        receipt = verified_by_mode[mode].get(transaction_id)
        if receipt_matches_commit(receipt, record):
            output[transaction_id] = record
    return output


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


@contextmanager
def verified_compact_reader(
    path: str | Path,
    *,
    full_path: str | Path | None = None,
) -> Iterator[tuple[BinaryIO | None, dict[str, dict[str, object]]]]:
    """Lock compact/full evidence and return a rewound compact handle plus receipts."""

    compact_path = Path(path).resolve()
    resolved_full_path = Path(
        full_path if full_path is not None else commit_path_for(compact_path)
    ).resolve()
    handles: dict[Path, BinaryIO] = {}
    try:
        for candidate in sorted({compact_path, resolved_full_path}):
            try:
                handle: BinaryIO = candidate.open("rb")
            except OSError:
                if candidate == compact_path:
                    yield None, {}
                    return
                continue
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            except BaseException:
                handle.close()
                raise
            handles[candidate] = handle
        compact_handle = handles[compact_path]
        full_handle = handles.get(resolved_full_path)
        receipts = (
            _load_compact_receipts_from_handles(
                compact_handle,
                full_handle,
                expected_mode=_mode_for_compact_path(compact_path),
            )
            if full_handle is not None
            else {}
        )
        compact_handle.seek(0)
        yield compact_handle, receipts
    finally:
        for handle in reversed(list(handles.values())):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def load_compact_receipts(
    path: str | Path,
    *,
    full_path: str | Path | None = None,
) -> dict[str, dict[str, object]]:
    """Load compact receipts proven by local batches and exact full-log lines."""

    with verified_compact_reader(path, full_path=full_path) as (_handle, receipts):
        return receipts


def _load_compact_receipts_from_handles(
    compact_handle: BinaryIO,
    full_handle: BinaryIO,
    *,
    expected_mode: str,
) -> dict[str, dict[str, object]]:
    if not expected_mode:
        return {}
    batches: dict[str, dict[str, object]] = {}
    batch_conflicts: set[str] = set()
    candidate_receipts: dict[str, list[dict[str, object]]] = {}
    pending_id = ""
    pending_rows: list[dict[str, object]] = []
    compact_handle.seek(0)
    for line in compact_handle:
        try:
            raw: Any = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pending_id = ""
            pending_rows = []
            continue
        if not isinstance(raw, dict):
            pending_id = ""
            pending_rows = []
            continue
        if raw.get("event_type") == RECEIPT_EVENT_TYPE:
            receipt = validated_receipt(raw)
            if receipt is not None:
                transaction_id = str(receipt["decision_transaction_id"])
                candidate_receipts.setdefault(transaction_id, []).append(receipt)
            pending_id = ""
            pending_rows = []
            continue
        batch_id = str(raw.get("journal_batch_id") or "")
        if raw.get("event_type") == "journal_batch_commit":
            descriptor = compact_batch_descriptor(
                pending_rows,
                raw,
                expected_mode=expected_mode,
            )
            if descriptor is not None:
                transaction_id = str(descriptor["decision_transaction_id"])
                _accumulate_unique(
                    batches,
                    batch_conflicts,
                    transaction_id,
                    descriptor,
                )
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

    verified: dict[str, dict[str, object]] = {}
    for transaction_id, receipts in candidate_receipts.items():
        batch = batches.get(transaction_id)
        if (
            batch is None
            or not isinstance(batch.get("decision_ids"), Sequence)
            or isinstance(batch.get("decision_ids"), (str, bytes))
        ):
            continue
        batch_decision_ids = cast(Sequence[object], batch["decision_ids"])
        matching = [
            receipt
            for receipt in receipts
            if receipt_matches_compact(
                receipt,
                transaction_id=transaction_id,
                decision_ids=batch_decision_ids,
                batch_sha256=str(batch["compact_batch_sha256"]),
                mode=expected_mode,
            )
            and receipt_matches_full_evidence(receipt, full_handle)
        ]
        if matching:
            verified[transaction_id] = matching[-1]
    return verified


def compact_batch_descriptor(
    rows: Sequence[Mapping[str, object]],
    marker: Mapping[str, object],
    *,
    expected_mode: str,
) -> dict[str, object] | None:
    """Validate one compact row batch and its local fsynced marker."""

    if marker.get("event_type") != "journal_batch_commit" or not rows:
        return None
    batch_id = str(marker.get("journal_batch_id") or "")
    if not batch_id:
        return None
    if not isinstance(marker.get("requires_cross_log_commit"), bool):
        return None
    decision_ids = [row.get("decision_id") for row in rows]
    try:
        normalized_ids = normalize_decision_ids(decision_ids)
        ids_hash = decision_ids_sha256(normalized_ids)
        transaction_id = transaction_id_for(normalized_ids)
    except DecisionCommitError:
        return None
    batch_hash = canonical_sha256(rows)
    if (
        marker.get("journal_batch_size") != len(rows)
        or [row.get("journal_batch_index") for row in rows] != list(range(len(rows)))
        or any(row.get("journal_batch_id") != batch_id for row in rows)
        or marker.get("decision_transaction_id") != transaction_id
        or marker.get("decision_ids") != normalized_ids
        or marker.get("decision_ids_sha256") != ids_hash
        or marker.get("journal_batch_sha256") != batch_hash
        or any(
            row.get("decision_transaction_id") != transaction_id or row.get("mode") != expected_mode
            for row in rows
        )
    ):
        return None
    return {
        "decision_transaction_id": transaction_id,
        "decision_ids": normalized_ids,
        "decision_ids_sha256": ids_hash,
        "compact_batch_sha256": batch_hash,
        "mode": expected_mode,
    }


def validated_receipt(value: object) -> dict[str, object] | None:
    """Validate one self-hashed compact receipt and its embedded commit digest."""

    if not isinstance(value, dict):
        return None
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("event_type") != RECEIPT_EVENT_TYPE
    ):
        return None
    try:
        decision_ids = normalize_decision_ids(value.get("decision_ids", []))
        transaction_id = transaction_id_for(decision_ids)
        full_hash = _required_sha256(value.get("full_batch_sha256"), "full_batch_sha256")
        compact_hash = _required_sha256(
            value.get("compact_batch_sha256"),
            "compact_batch_sha256",
        )
        full_commit_hash = _required_sha256(
            value.get("full_commit_record_sha256"),
            "full_commit_record_sha256",
        )
        _required_sha256(
            value.get("full_batch_line_sha256"),
            "full_batch_line_sha256",
        )
        _required_sha256(
            value.get("full_commit_line_sha256"),
            "full_commit_line_sha256",
        )
        compact_path_for_mode(DEFAULT_COMMIT_FILENAME, str(value.get("mode") or ""))
    except (DecisionCommitError, TypeError):
        return None
    offsets = (
        value.get("full_batch_line_start_offset"),
        value.get("full_commit_line_start_offset"),
    )
    if any(isinstance(offset, bool) or not isinstance(offset, int) for offset in offsets):
        return None
    batch_offset, commit_offset = offsets
    assert isinstance(batch_offset, int)
    assert isinstance(commit_offset, int)
    if batch_offset < 0 or commit_offset <= batch_offset:
        return None
    if (
        value.get("decision_transaction_id") != transaction_id
        or value.get("decision_ids_sha256") != decision_ids_sha256(decision_ids)
        or value.get("full_batch_sha256") != full_hash
        or value.get("compact_batch_sha256") != compact_hash
    ):
        return None
    reconstructed_commit = {
        "schema_version": SCHEMA_VERSION,
        "event_type": COMMIT_EVENT_TYPE,
        "decision_transaction_id": transaction_id,
        "decision_ids": decision_ids,
        "decision_ids_sha256": decision_ids_sha256(decision_ids),
        "full_batch_sha256": full_hash,
        "compact_batch_sha256": compact_hash,
        "mode": value.get("mode"),
        "committed_at": value.get("committed_at"),
    }
    reconstructed_commit["commit_record_sha256"] = canonical_sha256(reconstructed_commit)
    if reconstructed_commit["commit_record_sha256"] != full_commit_hash:
        return None
    stored_hash = value.get("receipt_record_sha256")
    unsigned = {key: item for key, item in value.items() if key != "receipt_record_sha256"}
    if stored_hash != canonical_sha256(unsigned):
        return None
    return value


def receipt_matches_compact(
    receipt: Mapping[str, object] | None,
    *,
    transaction_id: str,
    decision_ids: Sequence[object],
    batch_sha256: str,
    mode: str,
) -> bool:
    validated = validated_receipt(receipt)
    if validated is None:
        return False
    normalized_ids = normalize_decision_ids(decision_ids)
    return (
        transaction_id == transaction_id_for(normalized_ids)
        and validated.get("decision_transaction_id") == transaction_id
        and validated.get("decision_ids") == normalized_ids
        and validated.get("decision_ids_sha256") == decision_ids_sha256(normalized_ids)
        and validated.get("compact_batch_sha256") == batch_sha256
        and validated.get("mode") == mode
    )


def receipt_matches_commit(
    receipt: Mapping[str, object] | None,
    commit: Mapping[str, object],
) -> bool:
    validated = validated_receipt(receipt)
    record = _validated_record(commit)
    if validated is None or record is None:
        return False
    return (
        validated.get("decision_transaction_id") == record["decision_transaction_id"]
        and validated.get("decision_ids") == record["decision_ids"]
        and validated.get("decision_ids_sha256") == record["decision_ids_sha256"]
        and validated.get("full_batch_sha256") == record["full_batch_sha256"]
        and validated.get("compact_batch_sha256") == record["compact_batch_sha256"]
        and validated.get("mode") == record["mode"]
        and validated.get("committed_at") == record["committed_at"]
        and validated.get("full_commit_record_sha256") == record["commit_record_sha256"]
    )


def receipt_matches_full_evidence(
    receipt: Mapping[str, object],
    full_handle: IO[bytes],
) -> bool:
    """Verify the exact wrapper and commit lines named by one receipt."""

    validated = validated_receipt(receipt)
    if validated is None:
        return False
    batch_offset = validated["full_batch_line_start_offset"]
    commit_offset = validated["full_commit_line_start_offset"]
    assert isinstance(batch_offset, int)
    assert isinstance(commit_offset, int)
    batch_line = _read_hashed_json_line(
        full_handle,
        offset=batch_offset,
        expected_sha256=str(validated["full_batch_line_sha256"]),
    )
    commit_line = _read_hashed_json_line(
        full_handle,
        offset=commit_offset,
        expected_sha256=str(validated["full_commit_line_sha256"]),
    )
    full = _full_batch_descriptor(batch_line)
    record = _validated_record(commit_line)
    if full is None or record is None:
        return False
    transaction_id, descriptor = full
    return (
        transaction_id == validated["decision_transaction_id"]
        and descriptor["decision_ids"] == validated["decision_ids"]
        and descriptor["decision_ids_sha256"] == validated["decision_ids_sha256"]
        and descriptor["full_batch_sha256"] == validated["full_batch_sha256"]
        and descriptor["mode"] == validated["mode"]
        and receipt_matches_commit(validated, record)
    )


def _read_hashed_json_line(
    handle: IO[bytes],
    *,
    offset: int,
    expected_sha256: str,
) -> object | None:
    stat = os.fstat(handle.fileno())
    if offset < 0 or offset >= stat.st_size:
        return None
    if offset:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            return None
    handle.seek(offset)
    line = handle.readline()
    if not line.endswith(b"\n") or hashlib.sha256(line).hexdigest() != expected_sha256:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _mode_for_compact_path(path: Path) -> str:
    return next(
        (mode for mode, filename in COMPACT_FILENAMES.items() if filename == path.name),
        "",
    )


def _full_batch_handle_reference_matches(
    handle: IO[bytes],
    *,
    line_start_offset: int,
    line_sha256: str,
    transaction_id: str,
    decision_ids: Sequence[object],
    batch_sha256: str,
    mode: str,
) -> bool:
    raw = _read_hashed_json_line(
        handle,
        offset=line_start_offset,
        expected_sha256=line_sha256,
    )
    full = _full_batch_descriptor(raw)
    if full is None:
        return False
    full_transaction_id, descriptor = full
    return (
        full_transaction_id == transaction_id
        and descriptor["decision_ids"] == normalize_decision_ids(decision_ids)
        and descriptor["decision_ids_sha256"] == decision_ids_sha256(decision_ids)
        and descriptor["full_batch_sha256"] == batch_sha256
        and descriptor["mode"] == mode
    )


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
    "RECEIPT_EVENT_TYPE",
    "append_commit",
    "canonical_sha256",
    "compact_batch_descriptor",
    "compact_path_for_mode",
    "commit_path_for",
    "commits_from_values",
    "decision_ids_sha256",
    "load_compact_receipts",
    "load_commits",
    "matching_commit",
    "normalize_decision_ids",
    "receipt_matches_commit",
    "receipt_matches_compact",
    "receipt_matches_full_evidence",
    "referenced_compact_paths",
    "transaction_id_for",
    "validated_receipt",
    "verified_compact_reader",
]
