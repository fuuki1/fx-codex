"""Locked, content-addressed, idempotent JSONL append primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

RowIdentity = Callable[[Mapping[str, object]], str | None]
RowDigest = Callable[[Mapping[str, object]], str]

TIMESTAMP_FIELDS: tuple[str, ...] = (
    "ts",
    "event_time",
    "available_time",
    "ingested_time",
    "published_time",
    "revision_time",
    "source_time",
)


class AppendOnlyWriteError(RuntimeError):
    """Raised when append-only identity or integrity cannot be preserved."""


class AppendOnlyReadError(RuntimeError):
    """Raised when an append-only journal cannot be verified without guessing."""


class SidecarLockError(RuntimeError):
    """Raised when a writer/migration sidecar lock is already held."""


def canonical_row_hash(row: Mapping[str, object]) -> str:
    payload = {str(key): value for key, value in row.items() if key != "content_hash"}
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AppendOnlyWriteError("JSONL row is not canonically serializable") from error
    return hashlib.sha256(encoded).hexdigest()


def append_jsonl_idempotent(
    path: str | Path,
    rows: Iterable[Mapping[str, object]],
    *,
    identity: RowIdentity,
    row_digest: RowDigest = canonical_row_hash,
    tolerate_legacy_conflicts: bool = False,
    allow_legacy_existing: bool = False,
) -> int:
    """Append rows under ``flock``; replay is a no-op and conflicts fail closed."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC) + timedelta(seconds=5)
    prepared: list[dict[str, Any]] = []
    for row_number, raw in enumerate(rows, start=1):
        row = _verified_row(dict(raw))
        try:
            _validated_row_timestamps(row, target, row_number, cutoff=cutoff)
        except AppendOnlyReadError as error:
            raise AppendOnlyWriteError(
                f"invalid pending JSONL row {row_number}: {error}"
            ) from error
        prepared.append(row)
    appended = 0
    with exclusive_sidecar_lock(target):
        with target.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                known: dict[str, str] = {}
                authoritative: dict[str, bool] = {}
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise AppendOnlyWriteError(
                            f"malformed JSONL at {target}:{line_number}"
                        ) from error
                    if not isinstance(parsed, dict):
                        raise AppendOnlyWriteError(
                            f"non-object JSONL row at {target}:{line_number}"
                        )
                    if parsed.get("content_hash") is None and not allow_legacy_existing:
                        raise AppendOnlyWriteError(
                            f"legacy unhashed row requires migration at {target}:{line_number}"
                        )
                    existing = _verified_row(parsed, add_hash=False)
                    try:
                        _validated_row_timestamps(
                            existing,
                            target,
                            line_number,
                            cutoff=cutoff,
                        )
                    except AppendOnlyReadError as error:
                        raise AppendOnlyWriteError(
                            f"invalid existing JSONL row at {target}:{line_number}: {error}"
                        ) from error
                    key = identity(existing)
                    if not key:
                        continue
                    digest = row_digest(existing)
                    prior = known.get(key)
                    if prior is not None and prior == digest:
                        raise AppendOnlyWriteError(f"duplicate existing rows for identity {key}")
                    if prior is not None and prior != digest:
                        is_authoritative = bool(
                            parsed.get("content_hash") or parsed.get("decision_id")
                        )
                        if (
                            tolerate_legacy_conflicts
                            and not authoritative.get(key, False)
                            and not is_authoritative
                        ):
                            known[key] = "legacy-conflict"
                            continue
                        raise AppendOnlyWriteError(f"conflicting existing rows for identity {key}")
                    known[key] = digest
                    authoritative[key] = bool(
                        authoritative.get(key, False)
                        or parsed.get("content_hash")
                        or parsed.get("decision_id")
                    )

                pending: list[dict[str, Any]] = []
                for row in prepared:
                    key = identity(row)
                    if not key:
                        raise AppendOnlyWriteError("pending JSONL row has no stable identity")
                    digest = row_digest(row)
                    prior = known.get(key)
                    if prior is not None:
                        if prior != digest:
                            raise AppendOnlyWriteError(f"conflicting append for identity {key}")
                        continue
                    known[key] = digest
                    authoritative[key] = True
                    pending.append(row)

                handle.seek(0, os.SEEK_END)
                for row in pending:
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                    )
                if pending:
                    handle.flush()
                    os.fsync(handle.fileno())
                appended = len(pending)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return appended


def read_jsonl_strict(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    allow_legacy_unhashed: bool = False,
    identity: RowIdentity | None = None,
) -> Iterator[dict[str, Any]]:
    """Read JSONL without silently hiding corruption, ambiguous time, or future rows.

    Schema-v2 rows (and every row that declares ``content_hash``) must match their
    canonical digest.  Explicit legacy rows may remain readable for backward
    compatibility, but they still need an aware timestamp and are never upgraded or
    rewritten by this reader.  Promotion code can set ``allow_legacy_unhashed=False``.
    """

    cutoff = _aware_utc(as_of, "as_of") if as_of is not None else None
    target = Path(path)
    if not target.exists():
        return
    with exclusive_sidecar_lock(target):
        try:
            handle = target.open(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            raise AppendOnlyReadError(f"cannot read append-only journal: {target}") from error

        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                seen_identities: dict[str, int] = {}
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise AppendOnlyReadError(
                            f"malformed JSONL at {target}:{line_number}"
                        ) from error
                    if not isinstance(parsed, dict):
                        raise AppendOnlyReadError(f"non-object JSONL row at {target}:{line_number}")

                    schema = parsed.get("schema_version", parsed.get("schema"))
                    supplied = parsed.get("content_hash")
                    requires_hash = supplied is not None or (
                        isinstance(schema, int) and not isinstance(schema, bool) and schema >= 2
                    )
                    if requires_hash:
                        if not isinstance(supplied, str):
                            raise AppendOnlyReadError(
                                f"schema-v2 row lacks content_hash at {target}:{line_number}"
                            )
                        try:
                            expected = canonical_row_hash(parsed)
                        except AppendOnlyWriteError as error:
                            raise AppendOnlyReadError(
                                f"unhashable JSONL row at {target}:{line_number}"
                            ) from error
                        if supplied != expected:
                            raise AppendOnlyReadError(
                                f"content_hash mismatch at {target}:{line_number}"
                            )
                    elif not allow_legacy_unhashed:
                        raise AppendOnlyReadError(
                            f"legacy unhashed row is inadmissible at {target}:{line_number}"
                        )

                    _validated_row_timestamps(parsed, target, line_number, cutoff=cutoff)
                    resolved_identity = (identity or _default_read_identity)(parsed)
                    if resolved_identity:
                        prior_line = seen_identities.get(resolved_identity)
                        if prior_line is not None:
                            raise AppendOnlyReadError(
                                "duplicate append-only identity "
                                f"{resolved_identity} at {target}:{prior_line},{line_number}"
                            )
                        seen_identities[resolved_identity] = line_number
                    yield parsed
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_sidecar_lock(
    path: str | Path,
    *,
    blocking: bool = True,
) -> Iterator[Path]:
    """Serialize writers and migrations independently of a replaceable data inode."""

    target = Path(path)
    lock_path = target.with_name(f"{target.name}.writer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as error:
            raise SidecarLockError(f"sidecar lock is held: {lock_path}") from error
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _verified_row(row: dict[str, Any], *, add_hash: bool = True) -> dict[str, Any]:
    expected = canonical_row_hash(row)
    supplied = row.get("content_hash")
    if supplied is not None and supplied != expected:
        raise AppendOnlyWriteError("JSONL row content_hash does not match its payload")
    if add_hash:
        row["content_hash"] = expected
    return row


def _default_read_identity(row: Mapping[str, object]) -> str | None:
    """Recognize repository-wide immutable IDs without guessing source semantics."""

    for field in ("decision_id", "local_record_id"):
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return None


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AppendOnlyReadError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _validated_row_timestamps(
    row: Mapping[str, object],
    path: Path,
    line_number: int,
    *,
    cutoff: datetime | None,
) -> dict[str, datetime]:
    """Validate every declared clock, not only the first timestamp we recognize.

    A malicious or corrupt row must not hide a future/naive ``source_time`` behind a
    valid ``available_time``.  For append-only observations, ``available_time`` is
    the normalized first-use boundary and therefore cannot precede any other
    declared causal timestamp in the same row.
    """

    parsed: dict[str, datetime] = {}
    for field in TIMESTAMP_FIELDS:
        raw = row.get(field)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise AppendOnlyReadError(f"{field} timestamp invalid at {path}:{line_number}")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as error:
            raise AppendOnlyReadError(
                f"{field} timestamp invalid at {path}:{line_number}"
            ) from error
        if value.tzinfo is None or value.utcoffset() is None:
            raise AppendOnlyReadError(f"{field} timestamp is naive at {path}:{line_number}")
        normalized = value.astimezone(UTC)
        if cutoff is not None and normalized > cutoff:
            raise AppendOnlyReadError(f"future row: {field} beyond as_of at {path}:{line_number}")
        parsed[field] = normalized

    if not any(field in parsed for field in ("available_time", "event_time", "ts")):
        raise AppendOnlyReadError(f"row timestamp missing at {path}:{line_number}")

    available = parsed.get("available_time")
    if available is not None:
        for field in (
            "event_time",
            "ingested_time",
            "published_time",
            "revision_time",
            "source_time",
        ):
            boundary_value = parsed.get(field)
            if boundary_value is not None and boundary_value > available:
                raise AppendOnlyReadError(
                    f"{field} is later than available_time at {path}:{line_number}"
                )

    published = parsed.get("published_time")
    revision = parsed.get("revision_time")
    if published is not None and revision is not None and revision < published:
        raise AppendOnlyReadError(f"revision_time precedes published_time at {path}:{line_number}")
    return parsed
