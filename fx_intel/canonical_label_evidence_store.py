"""Append-only, hash-verified persistence for canonical label evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .canonical_label_evidence import (
    CanonicalLabelEvidence,
    InvalidCanonicalLabelEvidence,
    validate_canonical_label_evidence,
)

STORE_SCHEMA_VERSION = 1


class CanonicalLabelEvidenceStoreCorruption(RuntimeError):
    """The evidence store cannot be verified safely."""


class CanonicalLabelEvidenceStoreUnavailable(RuntimeError):
    """The evidence store does not exist yet."""


class CanonicalLabelEvidenceConflict(ValueError):
    """The same evidence natural key has different content."""


@dataclass(frozen=True)
class CanonicalLabelEvidenceStoreAudit:
    path: str
    entry_count: int
    store_sha256: str
    canonical_evidence_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "entry_count": self.entry_count,
            "store_sha256": self.store_sha256,
            "canonical_evidence_sha256": self.canonical_evidence_sha256,
        }


def append_canonical_label_evidence(
    path: str | Path,
    evidence_rows: Sequence[CanonicalLabelEvidence],
) -> int:
    """Append unseen evidence under ``flock``; identical replay is a no-op."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            try:
                existing = _parse_store_text(handle.read(), target)
            except UnicodeError as error:
                raise CanonicalLabelEvidenceStoreCorruption(
                    f"invalid UTF-8 in canonical label evidence store: {target}"
                ) from error
            by_key = {row.evidence_key: row for row in existing}
            pending: list[CanonicalLabelEvidence] = []
            for row in evidence_rows:
                prior = by_key.get(row.evidence_key)
                if prior is not None:
                    if prior != row:
                        raise CanonicalLabelEvidenceConflict(
                            "conflicting canonical label evidence for "
                            f"{row.decision_id} + {row.label_version} + "
                            f"{row.evidence_version}"
                        )
                    continue
                _validate(row, target)
                by_key[row.evidence_key] = row
                pending.append(row)

            handle.seek(0, os.SEEK_END)
            for row in pending:
                handle.write(
                    json.dumps(
                        _store_record(row),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
            if pending:
                handle.flush()
                os.fsync(handle.fileno())
            return len(pending)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_canonical_label_evidence(path: str | Path) -> list[CanonicalLabelEvidence]:
    """Load evidence only after schema, key, hash, and uniqueness checks."""

    _audit_result, rows = _read_verified(Path(path))
    return rows


def verify_canonical_label_evidence_store(
    path: str | Path,
) -> CanonicalLabelEvidenceStoreAudit:
    """Return physical and canonical hashes after complete verification."""

    audit, _rows = _read_verified(Path(path))
    return audit


def _read_verified(
    target: Path,
) -> tuple[CanonicalLabelEvidenceStoreAudit, list[CanonicalLabelEvidence]]:
    if not target.exists():
        raise CanonicalLabelEvidenceStoreUnavailable(
            f"canonical label evidence store is missing: {target}"
        )
    try:
        with target.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                text = handle.read()
                rows = _parse_store_text(text, target)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except UnicodeError as error:
        raise CanonicalLabelEvidenceStoreCorruption(
            f"invalid UTF-8 in canonical label evidence store: {target}"
        ) from error
    return _audit(target, text.encode("utf-8"), rows), rows


def _parse_store_text(text: str, target: Path) -> list[CanonicalLabelEvidence]:
    rows: list[CanonicalLabelEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise CanonicalLabelEvidenceStoreCorruption(
                f"malformed evidence JSONL at {target}:{line_number}"
            ) from error
        if not isinstance(raw, Mapping):
            raise CanonicalLabelEvidenceStoreCorruption(
                f"non-object evidence row at {target}:{line_number}"
            )
        row = _evidence_from_record(raw, target, line_number)
        if row.evidence_key in seen:
            raise CanonicalLabelEvidenceStoreCorruption(
                f"duplicate evidence key {row.evidence_key} at {target}:{line_number}"
            )
        seen.add(row.evidence_key)
        rows.append(row)
    return rows


def _evidence_from_record(
    raw: Mapping[str, object],
    target: Path,
    line_number: int,
) -> CanonicalLabelEvidence:
    if raw.get("schema_version") != STORE_SCHEMA_VERSION:
        raise CanonicalLabelEvidenceStoreCorruption(
            f"unknown evidence store schema at {target}:{line_number}"
        )
    key = raw.get("evidence_key")
    payload = raw.get("evidence")
    stored_hash = raw.get("evidence_sha256")
    if (
        not isinstance(key, list)
        or len(key) != 3
        or not all(isinstance(value, str) and value for value in key)
        or not isinstance(payload, Mapping)
        or not isinstance(stored_hash, str)
    ):
        raise CanonicalLabelEvidenceStoreCorruption(
            f"invalid evidence envelope at {target}:{line_number}"
        )
    if stored_hash != _sha256(payload):
        raise CanonicalLabelEvidenceStoreCorruption(
            f"evidence hash mismatch at {target}:{line_number}"
        )
    prepared: dict[str, Any] = dict(payload)
    flags = prepared.get("quality_flags")
    if isinstance(flags, list):
        prepared["quality_flags"] = tuple(str(flag) for flag in flags)
    try:
        row = CanonicalLabelEvidence(**prepared)
    except (TypeError, ValueError) as error:
        raise CanonicalLabelEvidenceStoreCorruption(
            f"invalid evidence schema at {target}:{line_number}"
        ) from error
    _validate(row, target, line_number)
    if list(row.evidence_key) != key:
        raise CanonicalLabelEvidenceStoreCorruption(
            f"evidence envelope key mismatch at {target}:{line_number}"
        )
    return row


def _validate(
    row: CanonicalLabelEvidence,
    target: Path,
    line_number: int | None = None,
) -> None:
    try:
        validate_canonical_label_evidence(row)
    except InvalidCanonicalLabelEvidence as error:
        location = f"{target}:{line_number}" if line_number is not None else str(target)
        raise CanonicalLabelEvidenceStoreCorruption(
            f"invalid canonical label evidence at {location}: {error}"
        ) from error


def _store_record(row: CanonicalLabelEvidence) -> dict[str, object]:
    payload = row.to_dict()
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "evidence_key": list(row.evidence_key),
        "evidence": payload,
        "evidence_sha256": _sha256(payload),
    }


def _audit(
    target: Path,
    physical: bytes,
    rows: Sequence[CanonicalLabelEvidence],
) -> CanonicalLabelEvidenceStoreAudit:
    canonical = [row.to_dict() for row in sorted(rows, key=lambda item: item.evidence_key)]
    return CanonicalLabelEvidenceStoreAudit(
        path=str(target),
        entry_count=len(rows),
        store_sha256=hashlib.sha256(physical).hexdigest(),
        canonical_evidence_sha256=_sha256(canonical),
    )


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


__all__ = [
    "CanonicalLabelEvidenceConflict",
    "CanonicalLabelEvidenceStoreAudit",
    "CanonicalLabelEvidenceStoreCorruption",
    "CanonicalLabelEvidenceStoreUnavailable",
    "append_canonical_label_evidence",
    "load_canonical_label_evidence",
    "verify_canonical_label_evidence_store",
]
