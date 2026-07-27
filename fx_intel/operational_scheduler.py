"""Create immutable evidence for scheduled operational-store maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
import os
from pathlib import Path
import re
from typing import Literal

from .operational_full_replay import full_replay_with_store, lock_replay_sources
from .operational_shadow_sync import sync_sources, sync_sources_with_store
from .state_io import atomic_write_json_create_only
from .operational_store import file_sha256, open_operational_writer

ScheduledAction = Literal["sync", "full-replay"]
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class OperationalScheduleError(RuntimeError):
    """Raised when a scheduled run cannot preserve its evidence contract."""


class ScheduledRunFailure(OperationalScheduleError):
    """A started scheduled attempt failed after publishing its immutable intent."""

    def __init__(
        self,
        message: str,
        *,
        intent_path: Path,
        failure_path: Path,
    ) -> None:
        super().__init__(message)
        self.intent_path = intent_path
        self.failure_path = failure_path


@dataclass(frozen=True)
class ScheduledRun:
    """Terminal result and immutable report location for one scheduled run."""

    intent_path: Path
    report_path: Path
    payload: dict[str, object]
    exit_code: int


def new_run_id(
    *,
    now: datetime | None = None,
    process_id: int | None = None,
) -> str:
    """Return a filename-safe UTC run id unique to a scheduler process."""
    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise OperationalScheduleError("run-id time must be timezone-aware")
    stamp = stamp.astimezone(UTC)
    pid = os.getpid() if process_id is None else process_id
    if pid < 0:
        raise OperationalScheduleError("process id must be non-negative")
    return f"{stamp:%Y%m%dT%H%M%S%fZ}-p{pid}"


def validate_run_id(value: str) -> str:
    """Reject path separators, dots, whitespace, and unbounded scheduler ids."""
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise OperationalScheduleError(
            "run id must be 1-128 ASCII letters, digits, underscores, or hyphens"
        )
    return value


def run_scheduled(
    *,
    action: ScheduledAction,
    database_path: Path,
    decision_path: Path,
    price_path: Path,
    evidence_dir: Path,
    run_id: str | None = None,
) -> ScheduledRun:
    """Run one fail-closed action with create-only intent and terminal evidence."""
    if action not in ("sync", "full-replay"):
        raise OperationalScheduleError(f"unsupported scheduled action: {action}")
    resolved_run_id = validate_run_id(run_id or new_run_id())
    prefix = "shadow-sync" if action == "sync" else "full-replay"
    intent_path = evidence_dir / f"{prefix}-{resolved_run_id}.intent.json"
    report_path = evidence_dir / f"{prefix}-{resolved_run_id}.result.json"
    failure_path = evidence_dir / f"{prefix}-{resolved_run_id}.failure.json"
    for candidate in (intent_path, report_path, failure_path):
        if candidate.exists():
            raise OperationalScheduleError(f"scheduled evidence already exists: {candidate}")

    started_at = datetime.now(UTC)
    writer_id = f"scheduled-{prefix}-{resolved_run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    intent = {
        "schema_version": 1,
        "operation": "operational_scheduled_intent",
        "action": action,
        "run_id": resolved_run_id,
        "writer_id": writer_id,
        "started_at": started_at.isoformat(),
        "inputs": {
            "database_path": str(Path(database_path).resolve()),
            "decision_path": str(Path(decision_path).resolve()),
            "price_path": str(Path(price_path).resolve()),
        },
    }
    try:
        atomic_write_json_create_only(intent_path, intent)
    except FileExistsError as error:
        raise OperationalScheduleError(f"scheduled intent already exists: {intent_path}") from error

    try:
        payload = _run_action(
            action=action,
            database_path=database_path,
            decision_path=decision_path,
            price_path=price_path,
            writer_id=writer_id,
            started_at=started_at,
        )
        exit_code = _exit_code(action, payload)
        terminal_payload = {
            **payload,
            "scheduler": {
                "run_id": resolved_run_id,
                "action": action,
                "intent_path": str(intent_path.resolve()),
                "intent_sha256": file_sha256(intent_path),
            },
        }
        atomic_write_json_create_only(report_path, terminal_payload)
    except BaseException as error:
        failure = {
            "schema_version": 1,
            "operation": "operational_scheduled_failure",
            "action": action,
            "run_id": resolved_run_id,
            "writer_id": writer_id,
            "started_at": started_at.isoformat(),
            "failed_at": datetime.now(UTC).isoformat(),
            "intent_path": str(intent_path.resolve()),
            "intent_sha256": file_sha256(intent_path),
            "error_type": type(error).__name__,
            "error": str(error),
        }
        try:
            atomic_write_json_create_only(failure_path, failure)
        except BaseException as evidence_error:
            raise OperationalScheduleError(
                "scheduled action failed and terminal failure evidence could not "
                f"be written: action_error={error}; evidence_error={evidence_error}"
            ) from error
        raise ScheduledRunFailure(
            str(error),
            intent_path=intent_path,
            failure_path=failure_path,
        ) from error

    return ScheduledRun(
        intent_path=intent_path,
        report_path=report_path,
        payload=terminal_payload,
        exit_code=exit_code,
    )


def _run_action(
    *,
    action: ScheduledAction,
    database_path: Path,
    decision_path: Path,
    price_path: Path,
    writer_id: str,
    started_at: datetime,
) -> dict[str, object]:
    if action == "sync":
        return sync_sources(
            database_path=database_path,
            decision_path=decision_path,
            price_path=price_path,
            writer_id=writer_id,
        )

    with lock_replay_sources(decision_path, price_path):
        with open_operational_writer(database_path, writer_id=writer_id) as store:
            sync_payload = sync_sources_with_store(
                store,
                decision_path=decision_path,
                price_path=price_path,
                started_at=started_at,
            )
            replay_payload = full_replay_with_store(
                store,
                database_path=database_path,
                decision_path=decision_path,
                price_path=price_path,
                started_at=started_at,
            )
    return {
        **replay_payload,
        "pre_replay_sync": sync_payload,
    }


def _exit_code(action: ScheduledAction, payload: dict[str, object]) -> int:
    if action == "sync":
        return 1 if bool(payload.get("quality_failure")) else 0
    pre_replay_sync = payload.get("pre_replay_sync")
    quality_failure = isinstance(pre_replay_sync, dict) and bool(
        pre_replay_sync.get("quality_failure")
    )
    return 0 if payload.get("verdict") == "full_replay_verified" and not quality_failure else 1


__all__ = [
    "OperationalScheduleError",
    "ScheduledRun",
    "ScheduledRunFailure",
    "new_run_id",
    "run_scheduled",
    "validate_run_id",
]
