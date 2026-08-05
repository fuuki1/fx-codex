"""Append-only, hash-verified persistence for canonical FX outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .evaluation_labels import (
    KNOWN_NET_LABEL_PROVENANCES,
    KNOWN_NET_LABEL_VERSIONS,
    canonical_net_label_contract_flags,
)
from .trade_outcome import (
    CanonicalTradeOutcome,
    assert_canonical_outcome_idempotent,
)

STORE_SCHEMA_VERSION = 2
EXPORT_SCHEMA_VERSION = 2


class CanonicalOutcomeStoreCorruption(RuntimeError):
    """The persisted canonical store cannot be verified safely."""


class CanonicalOutcomeStoreUnavailable(RuntimeError):
    """The canonical store does not exist yet."""


@dataclass(frozen=True)
class CanonicalOutcomeStoreAudit:
    path: str
    entry_count: int
    store_sha256: str
    canonical_export_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "entry_count": self.entry_count,
            "store_sha256": self.store_sha256,
            "canonical_export_sha256": self.canonical_export_sha256,
        }


def append_canonical_outcomes(
    path: str | Path,
    outcomes: Sequence[CanonicalTradeOutcome],
) -> int:
    """Append unseen outcomes under ``flock``; replay is idempotent."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing = _parse_store_text(handle.read(), target)
            by_key = {outcome.outcome_key: outcome for outcome in existing}
            pending: list[CanonicalTradeOutcome] = []
            for outcome in outcomes:
                prior = by_key.get(outcome.outcome_key)
                if prior is not None:
                    assert_canonical_outcome_idempotent(prior, outcome)
                    continue
                _validate_outcome(outcome, target)
                by_key[outcome.outcome_key] = outcome
                pending.append(outcome)

            handle.seek(0, os.SEEK_END)
            for outcome in pending:
                handle.write(
                    json.dumps(
                        _store_record(outcome),
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


def load_canonical_outcomes(path: str | Path) -> list[CanonicalTradeOutcome]:
    """Load only after schema, key, hash, and uniqueness verification."""

    _audit, outcomes = _read_verified(Path(path))
    return outcomes


def verify_canonical_outcome_store(path: str | Path) -> CanonicalOutcomeStoreAudit:
    """Return physical-store and canonical-export hashes after verification."""

    audit, _outcomes = _read_verified(Path(path))
    return audit


def export_canonical_outcomes(
    path: str | Path,
    export_path: str | Path,
) -> CanonicalOutcomeStoreAudit:
    """Atomically export one verified snapshot with a reproducible content hash."""

    audit, outcomes = _read_verified(Path(path))
    ordered = sorted(outcomes, key=lambda outcome: outcome.outcome_key)
    serialized = [outcome.to_dict() for outcome in ordered]
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "entry_count": len(serialized),
        "canonical_outcomes_sha256": audit.canonical_export_sha256,
        "outcomes": serialized,
    }
    target = Path(export_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return audit


def _read_verified(
    target: Path,
) -> tuple[CanonicalOutcomeStoreAudit, list[CanonicalTradeOutcome]]:
    if not target.exists():
        raise CanonicalOutcomeStoreUnavailable(f"canonical outcome store is missing: {target}")
    with target.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            text = handle.read()
            outcomes = _parse_store_text(text, target)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return _audit(target, text.encode("utf-8"), outcomes), outcomes


def _parse_store_text(text: str, target: Path) -> list[CanonicalTradeOutcome]:
    outcomes: list[CanonicalTradeOutcome] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise CanonicalOutcomeStoreCorruption(
                f"malformed canonical JSONL at {target}:{line_number}"
            ) from error
        if not isinstance(raw, Mapping):
            raise CanonicalOutcomeStoreCorruption(
                f"non-object canonical row at {target}:{line_number}"
            )
        outcome = _outcome_from_record(raw, target, line_number)
        if outcome.outcome_key in seen:
            raise CanonicalOutcomeStoreCorruption(
                f"duplicate canonical outcome key {outcome.outcome_key} "
                f"at {target}:{line_number}"
            )
        seen.add(outcome.outcome_key)
        outcomes.append(outcome)
    return outcomes


def _outcome_from_record(
    raw: Mapping[str, object],
    target: Path,
    line_number: int,
) -> CanonicalTradeOutcome:
    if raw.get("schema_version") != STORE_SCHEMA_VERSION:
        raise CanonicalOutcomeStoreCorruption(
            f"unknown canonical store schema at {target}:{line_number}"
        )
    key = raw.get("outcome_key")
    payload = raw.get("outcome")
    stored_hash = raw.get("outcome_sha256")
    if (
        not isinstance(key, list)
        or len(key) != 2
        or not all(isinstance(value, str) and value for value in key)
        or not isinstance(payload, Mapping)
        or not isinstance(stored_hash, str)
    ):
        raise CanonicalOutcomeStoreCorruption(
            f"invalid canonical record envelope at {target}:{line_number}"
        )
    expected_hash = _sha256(payload)
    if stored_hash != expected_hash:
        raise CanonicalOutcomeStoreCorruption(
            f"canonical outcome hash mismatch at {target}:{line_number}"
        )
    prepared: dict[str, Any] = dict(payload)
    flags = prepared.get("quality_flags")
    if isinstance(flags, list):
        prepared["quality_flags"] = tuple(str(flag) for flag in flags)
    cost_flags = prepared.get("cost_quality_flags")
    if isinstance(cost_flags, list):
        prepared["cost_quality_flags"] = tuple(str(flag) for flag in cost_flags)
    try:
        outcome = CanonicalTradeOutcome(**prepared)
    except (TypeError, ValueError) as error:
        raise CanonicalOutcomeStoreCorruption(
            f"invalid canonical outcome schema at {target}:{line_number}"
        ) from error
    _validate_outcome(outcome, target, line_number)
    if list(outcome.outcome_key) != key:
        raise CanonicalOutcomeStoreCorruption(
            f"canonical envelope key mismatch at {target}:{line_number}"
        )
    return outcome


def _validate_outcome(
    outcome: CanonicalTradeOutcome,
    target: Path,
    line_number: int | None = None,
) -> None:
    location = f"{target}:{line_number}" if line_number is not None else str(target)
    if not outcome.decision_id.strip() or not outcome.label_version.strip():
        raise CanonicalOutcomeStoreCorruption(
            f"canonical outcome is missing its natural key at {location}"
        )
    if outcome.net_label_eligible and (
        outcome.label_version not in KNOWN_NET_LABEL_VERSIONS
        or outcome.label_provenance not in KNOWN_NET_LABEL_PROVENANCES
        or bool(canonical_net_label_contract_flags(outcome.to_dict()))
    ):
        raise CanonicalOutcomeStoreCorruption(
            f"eligible canonical outcome is incomplete at {location}"
        )


def _store_record(outcome: CanonicalTradeOutcome) -> dict[str, object]:
    payload = outcome.to_dict()
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "outcome_key": list(outcome.outcome_key),
        "outcome": payload,
        "outcome_sha256": _sha256(payload),
    }


def _audit(
    target: Path,
    physical: bytes,
    outcomes: Sequence[CanonicalTradeOutcome],
) -> CanonicalOutcomeStoreAudit:
    ordered = sorted(outcomes, key=lambda outcome: outcome.outcome_key)
    canonical_payload = [outcome.to_dict() for outcome in ordered]
    return CanonicalOutcomeStoreAudit(
        path=str(target),
        entry_count=len(outcomes),
        store_sha256=hashlib.sha256(physical).hexdigest(),
        canonical_export_sha256=_sha256(canonical_payload),
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
