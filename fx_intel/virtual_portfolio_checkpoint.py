"""Create and verify externally retainable V2 event-chain checkpoints.

The checkpoint is research-ledger evidence only. It snapshots SQLite through
the read-only backup API, never contacts a broker, and never mutates portfolio
state. A different-host copy plus a caller-supplied out-of-band digest is
reported as an observation, never as independently authenticated custody.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from typing import Any

from fx_intel.virtual_portfolio_v2 import (
    GENESIS_EVENT_SHA256,
    V2_SCHEMA_VERSION,
    audit_v2,
    event_head,
)

CHECKPOINT_CONTRACT = "virtual-portfolio-v2-checkpoint-v1"
CHECKPOINT_VERIFICATION_CONTRACT = "virtual-portfolio-v2-checkpoint-verification-v1"


class VirtualPortfolioCheckpointError(RuntimeError):
    """Checkpoint creation or verification failed closed."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def checkpoint_file_sha256(path: Path) -> str:
    """Return the raw-byte SHA-256 used for out-of-band checkpoint transfer."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VirtualPortfolioCheckpointError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _read_checkpoint(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VirtualPortfolioCheckpointError("checkpoint is unreadable or invalid JSON") from error
    if not isinstance(raw, dict):
        raise VirtualPortfolioCheckpointError("checkpoint must be a JSON object")
    recorded_sha = raw.get("checkpoint_sha256")
    identity = {key: value for key, value in raw.items() if key != "checkpoint_sha256"}
    return raw, isinstance(recorded_sha, str) and recorded_sha == _sha256_json(identity)


def _open_snapshot(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def create_v2_checkpoint(
    database: Path,
    output: Path,
    *,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Create one immutable checkpoint from a transactionally consistent backup."""

    source = database.expanduser().resolve()
    target = output.expanduser().resolve()
    if not source.is_file():
        raise VirtualPortfolioCheckpointError(f"database does not exist: {source}")
    if target.exists():
        raise VirtualPortfolioCheckpointError(f"checkpoint already exists: {target}")
    moment = _aware_utc(created_at or datetime.now(UTC), "checkpoint created_at")
    hostname = str(socket.gethostname()).strip()
    if not hostname:
        raise VirtualPortfolioCheckpointError("source_host is required")
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".virtual-portfolio-checkpoint-",
        dir=target.parent,
    ) as temporary:
        snapshot_path = Path(temporary) / "portfolio.sqlite3"
        source_connection = _open_snapshot(source)
        snapshot_connection = sqlite3.connect(snapshot_path)
        try:
            source_connection.backup(snapshot_connection)
            snapshot_connection.commit()
        finally:
            snapshot_connection.close()
            source_connection.close()

        snapshot = _open_snapshot(snapshot_path)
        try:
            integrity = str(snapshot.execute("PRAGMA integrity_check").fetchone()[0])
            user_version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
            audit = audit_v2(snapshot)
            event_count, event_head_sha256 = event_head(snapshot)
        finally:
            snapshot.close()
        if integrity != "ok":
            raise VirtualPortfolioCheckpointError(f"snapshot integrity check failed: {integrity}")
        if audit.get("passed") is not True:
            raise VirtualPortfolioCheckpointError(f"V2 audit failed: {audit.get('errors')}")
        snapshot_sha256 = checkpoint_file_sha256(snapshot_path)
        snapshot_size = snapshot_path.stat().st_size

    identity: dict[str, object] = {
        "contract": CHECKPOINT_CONTRACT,
        "checkpoint_id": (
            f"v2-checkpoint-{event_count}-"
            f"{event_head_sha256[:16]}-{moment.strftime('%Y%m%dT%H%M%S%fZ')}"
        ),
        "created_at": moment.isoformat(),
        "source": {
            "host": hostname,
            "database_path": str(source),
            "sqlite_user_version": user_version,
            "v2_schema_version": V2_SCHEMA_VERSION,
        },
        "database_snapshot": {
            "sha256": snapshot_sha256,
            "size_bytes": snapshot_size,
            "integrity_check": integrity,
        },
        "event_chain": {
            "event_count": event_count,
            "event_head_sha256": event_head_sha256,
            "genesis_event_sha256": GENESIS_EVENT_SHA256,
        },
        "audit": {
            "contract": audit.get("contract"),
            "passed": True,
            "error_count": 0,
            "journal_transaction_count": audit.get("journal_transaction_count"),
            "valuation_count": audit.get("valuation_count"),
        },
        "custody": {
            "origin_scope": "source_host_local",
            "external_retention_claimed_at_creation": False,
            "limitation": (
                "External custody exists only after identical checkpoint bytes are "
                "retained and verified on a different host or authority."
            ),
        },
        "broker_connected": False,
        "execution_mode": "offline_simulation",
    }
    payload = {**identity, "checkpoint_sha256": _sha256_json(identity)}
    encoded = (_json(payload) + "\n").encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError as error:
        raise VirtualPortfolioCheckpointError(f"checkpoint already exists: {target}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return payload


def verify_v2_checkpoint(
    database: Path,
    checkpoint: Path,
    *,
    expected_checkpoint_file_sha256: str,
) -> dict[str, object]:
    """Verify an out-of-band digest and the retained event-chain prefix."""

    source = database.expanduser().resolve()
    checkpoint_path = checkpoint.expanduser().resolve()
    expected_digest = str(expected_checkpoint_file_sha256).strip().lower()
    if len(expected_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_digest
    ):
        raise VirtualPortfolioCheckpointError(
            "expected checkpoint file SHA-256 must be 64 lowercase hex characters"
        )
    actual_digest = checkpoint_file_sha256(checkpoint_path)
    out_of_band_digest_match = actual_digest == expected_digest
    raw, checkpoint_hash_valid = _read_checkpoint(checkpoint_path)
    checkpoint_contract_valid = raw.get("contract") == CHECKPOINT_CONTRACT
    chain = raw.get("event_chain")
    chain = chain if isinstance(chain, dict) else {}
    source_meta = raw.get("source")
    source_meta = source_meta if isinstance(source_meta, dict) else {}
    try:
        checkpoint_count = int(chain["event_count"])
        checkpoint_head = str(chain["event_head_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise VirtualPortfolioCheckpointError(
            "checkpoint event-chain identity is invalid"
        ) from error

    connection = _open_snapshot(source)
    try:
        connection.execute("BEGIN")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        audit = audit_v2(connection)
        current_count, current_head = event_head(connection)
        retained = (
            connection.execute(
                """
                SELECT event_sha256
                FROM portfolio_event_log
                WHERE event_seq = ?
                """,
                (checkpoint_count,),
            ).fetchone()
            if checkpoint_count > 0
            else None
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    retained_head = (
        str(retained["event_sha256"])
        if retained is not None
        else GENESIS_EVENT_SHA256 if checkpoint_count == 0 else None
    )
    prefix_preserved = current_count >= checkpoint_count and retained_head == checkpoint_head
    host = str(socket.gethostname()).strip()
    source_host = str(source_meta.get("host") or "").strip()
    different_host_copy_observed = bool(host and source_host and host != source_host)
    passed = (
        checkpoint_contract_valid
        and checkpoint_hash_valid
        and out_of_band_digest_match
        and integrity == "ok"
        and audit.get("passed") is True
        and prefix_preserved
    )
    return {
        "contract": CHECKPOINT_VERIFICATION_CONTRACT,
        "verified_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "checkpoint_contract_valid": checkpoint_contract_valid,
        "checkpoint_hash_valid": checkpoint_hash_valid,
        "checkpoint_id": raw.get("checkpoint_id"),
        "checkpoint_sha256": raw.get("checkpoint_sha256"),
        "checkpoint_file_sha256_actual": actual_digest,
        "checkpoint_file_sha256_expected": expected_digest,
        "out_of_band_digest_match": out_of_band_digest_match,
        "checkpoint_event_count": checkpoint_count,
        "checkpoint_event_head_sha256": checkpoint_head,
        "current_event_count": current_count,
        "current_event_head_sha256": current_head,
        "retained_prefix_head_sha256": retained_head,
        "prefix_preserved": prefix_preserved,
        "database_integrity_check": integrity,
        "v2_audit_passed": audit.get("passed") is True,
        "v2_audit_errors": audit.get("errors"),
        "source_host": source_host,
        "verification_host": host,
        "different_host_copy_observed": different_host_copy_observed,
        "independent_custody_verified": False,
        "custody_assurance": (
            "different_host_copy_with_out_of_band_digest_observed"
            if different_host_copy_observed and out_of_band_digest_match
            else "independent_custody_not_verified"
        ),
        "broker_connected": False,
        "execution_mode": "offline_simulation",
    }


__all__ = [
    "CHECKPOINT_CONTRACT",
    "CHECKPOINT_VERIFICATION_CONTRACT",
    "VirtualPortfolioCheckpointError",
    "checkpoint_file_sha256",
    "create_v2_checkpoint",
    "verify_v2_checkpoint",
]
