"""Append-only wall-clock and monotonic timing for analysis pipelines.

The UTC timestamps make stages comparable across processes.  Durations use a
monotonic clock so NTP or wall-clock adjustments cannot produce a negative or
misleading elapsed time.  Timing is observability-only: a record-write failure
is exposed on the recorder but never changes a research pipeline's outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

CONTRACT = "fx-codex-stage-timing-v1"


class StageOutcome:
    """Allow a caught, fail-closed error to remain visible in timing evidence."""

    def __init__(self) -> None:
        self.status = "ok"
        self.error_type: str | None = None

    def mark_handled_error(self, error: BaseException | str) -> None:
        self.status = "handled_error"
        if self.error_type is None:
            self.error_type = (
                type(error).__name__ if isinstance(error, BaseException) else str(error)
            )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stage timing datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


class StageTimingRecorder:
    """Record completed or failed stages to one append-only JSONL stream."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        pipeline: str,
        trace_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.pipeline = pipeline
        self.trace_id = trace_id or uuid4().hex
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._metadata = dict(metadata or {})
        self._sequence = 0
        self.records: list[dict[str, object]] = []
        self.write_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def bind(self, **metadata: object) -> None:
        """Attach correlation metadata to this and all later stage records."""

        self._metadata.update(metadata)

    @contextmanager
    def stage(self, name: str, **detail: object) -> Iterator[StageOutcome]:
        """Measure one stage and persist its result even when the stage raises."""

        if not name:
            raise ValueError("stage name must not be empty")
        started_at = self._aware_utc(self._clock())
        started_ns = self._monotonic_ns()
        outcome = StageOutcome()
        try:
            yield outcome
        except BaseException as error:
            outcome.status = "error"
            outcome.error_type = type(error).__name__
            raise
        finally:
            finished_ns = self._monotonic_ns()
            finished_at = self._aware_utc(self._clock())
            self._sequence += 1
            record: dict[str, object] = {
                "contract": CONTRACT,
                "event_type": "stage_timing",
                "pipeline": self.pipeline,
                "trace_id": self.trace_id,
                "sequence": self._sequence,
                "stage": name,
                "status": outcome.status,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "elapsed_ms": round(max(0, finished_ns - started_ns) / 1_000_000, 3),
                "pid": os.getpid(),
                "metadata": _json_value(self._metadata),
                "detail": _json_value(detail),
            }
            if outcome.error_type is not None:
                record["error_type"] = outcome.error_type
            self.records.append(record)
            self._append(record)

    def _append(self, record: Mapping[str, object]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode(
                "utf-8"
            )
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._set_write_error("timing log is busy; stage record was dropped")
                    return
                written = 0
                while written < len(encoded):
                    count = os.write(descriptor, encoded[written:])
                    if count <= 0:
                        raise OSError("stage timing write made no progress")
                    written += count
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        except (OSError, TypeError, ValueError) as error:
            self._set_write_error(f"{type(error).__name__}: {error}")

    def _set_write_error(self, message: str) -> None:
        if self.write_error is not None:
            return
        self.write_error = message
        try:
            os.write(2, f"stage timing unavailable: {message}\n".encode())
        except OSError:
            pass

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stage timing clock must return an aware datetime")
        return value.astimezone(UTC)


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """Return a compact in-process summary without claiming sample statistics."""

    return {
        "contract": CONTRACT,
        "stage_count": len(records),
        "elapsed_ms_by_stage": {
            str(record.get("stage") or "unknown"): record.get("elapsed_ms") for record in records
        },
        "failed_stages": [
            str(record.get("stage") or "unknown")
            for record in records
            if record.get("status") != "ok"
        ],
    }
