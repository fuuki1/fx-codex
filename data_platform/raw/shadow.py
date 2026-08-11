"""Disabled-by-default, bounded asynchronous raw-ledger shadow adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
import time
from typing import Literal

from data_platform.contracts.raw_event import RawEvent
from data_platform.raw.event_ledger import RawEventLedger

RAW_LEDGER_FLAG = "FX_RAW_LEDGER_ENABLED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def raw_ledger_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return false unless the feature flag is explicitly enabled."""

    source = os.environ if environment is None else environment
    return source.get(RAW_LEDGER_FLAG, "").strip().lower() in _TRUE_VALUES


CaptureStatus = Literal["disabled", "queued", "failed"]


@dataclass(frozen=True)
class ShadowCaptureResult:
    status: CaptureStatus
    event_id: str
    detail: str | None = None


@dataclass(frozen=True)
class _QuarantineWork:
    event_id: str
    annotation_id: str
    reason: str
    annotated_at: datetime
    provenance: Mapping[str, str]


@dataclass(frozen=True)
class _FailureWork:
    event_id: str
    detail: str


class RawLedgerShadowWriter:
    """Queue raw evidence without placing ledger I/O on the caller's hot path.

    The daemon worker and bounded non-blocking queue make lock/disk stalls
    unable to block the existing analysis caller. Queue overflow is explicit
    failure evidence, never an invisible fallback.
    """

    def __init__(
        self,
        ledger: RawEventLedger,
        *,
        enabled: bool = False,
        failure_log: str | Path | None = None,
        error_logger: Callable[[str], None] | None = None,
        max_queue_size: int = 256,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        self.ledger = ledger
        self.enabled = enabled
        self.failure_log = None if failure_log is None else Path(failure_log)
        self.error_logger = error_logger
        self._queue: Queue[RawEvent | _QuarantineWork] = Queue(maxsize=max_queue_size)
        self._failure_queue: Queue[_FailureWork] = Queue(maxsize=max_queue_size)
        self._metrics_lock = Lock()
        self._worker: Thread | None = None
        self._failure_worker: Thread | None = None
        self._metrics = {
            "attempted": 0,
            "queued": 0,
            "stored": 0,
            "idempotent": 0,
            "failed": 0,
            "queue_overflow": 0,
            "raw_attempted": 0,
            "raw_queued": 0,
            "raw_stored": 0,
            "raw_idempotent": 0,
            "raw_failed": 0,
            "raw_queue_overflow": 0,
            "annotations_attempted": 0,
            "annotations_queued": 0,
            "annotations_stored": 0,
            "annotations_idempotent": 0,
            "annotations_failed": 0,
            "annotations_queue_overflow": 0,
            "failure_reports_queued": 0,
            "failure_report_queue_overflow": 0,
            "failure_report_errors": 0,
            "failure_log_write_errors": 0,
        }

    @classmethod
    def from_environment(
        cls,
        ledger: RawEventLedger,
        *,
        environment: Mapping[str, str] | None = None,
        failure_log: str | Path | None = None,
        error_logger: Callable[[str], None] | None = None,
        max_queue_size: int = 256,
    ) -> RawLedgerShadowWriter:
        return cls(
            ledger,
            enabled=raw_ledger_enabled(environment),
            failure_log=failure_log,
            error_logger=error_logger,
            max_queue_size=max_queue_size,
        )

    def capture(self, event: RawEvent) -> ShadowCaptureResult:
        if not self.enabled:
            return ShadowCaptureResult(status="disabled", event_id=event.event_id)
        self._increment("attempted")
        self._increment("raw_attempted")
        self._ensure_worker()
        try:
            self._queue.put_nowait(event)
        except Full:
            self._increment("failed")
            self._increment("queue_overflow")
            self._increment("raw_failed")
            self._increment("raw_queue_overflow")
            detail = "RawLedgerShadowQueueFull: raw ledger shadow queue is full"
            self._queue_failure_report(event.event_id, detail)
            return ShadowCaptureResult(status="failed", event_id=event.event_id, detail=detail)
        self._increment("queued")
        self._increment("raw_queued")
        return ShadowCaptureResult(status="queued", event_id=event.event_id)

    def annotate_quarantine(
        self,
        event_id: str,
        *,
        annotation_id: str,
        reason: str,
        annotated_at: datetime,
        provenance: Mapping[str, str],
    ) -> ShadowCaptureResult:
        """Queue quality evidence behind its raw event on the same FIFO worker."""

        if not self.enabled:
            return ShadowCaptureResult(status="disabled", event_id=event_id)
        work = _QuarantineWork(
            event_id=event_id,
            annotation_id=annotation_id,
            reason=reason,
            annotated_at=annotated_at,
            provenance=dict(provenance),
        )
        self._increment("attempted")
        self._increment("annotations_attempted")
        self._ensure_worker()
        try:
            self._queue.put_nowait(work)
        except Full:
            self._increment("failed")
            self._increment("queue_overflow")
            self._increment("annotations_failed")
            self._increment("annotations_queue_overflow")
            detail = "RawLedgerShadowQueueFull: raw ledger shadow queue is full"
            self._queue_failure_report(event_id, detail)
            return ShadowCaptureResult(
                status="failed",
                event_id=event_id,
                detail=detail,
            )
        self._increment("queued")
        self._increment("annotations_queued")
        return ShadowCaptureResult(status="queued", event_id=event_id)

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        """Wait for tests/controlled shutdown; callers never need this on the hot path."""

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        while self._queue.unfinished_tasks or self._failure_queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def metrics(self) -> dict[str, int | bool | float]:
        with self._metrics_lock:
            worker_alive = self._worker is not None and self._worker.is_alive()
            failure_worker_alive = (
                self._failure_worker is not None and self._failure_worker.is_alive()
            )
            return {
                "enabled": self.enabled,
                "worker_alive": worker_alive,
                "queue_backlog": self._queue.qsize(),
                "unfinished_tasks": self._queue.unfinished_tasks,
                "failure_worker_alive": failure_worker_alive,
                "failure_queue_backlog": self._failure_queue.qsize(),
                "failure_unfinished_tasks": self._failure_queue.unfinished_tasks,
                "queue_overflow_ratio": (
                    0.0
                    if self._metrics["attempted"] == 0
                    else self._metrics["queue_overflow"] / self._metrics["attempted"]
                ),
                "raw_failure_ratio": (
                    0.0
                    if self._metrics["raw_attempted"] == 0
                    else self._metrics["raw_failed"] / self._metrics["raw_attempted"]
                ),
                "raw_queue_overflow_ratio": (
                    0.0
                    if self._metrics["raw_attempted"] == 0
                    else self._metrics["raw_queue_overflow"] / self._metrics["raw_attempted"]
                ),
                "annotation_failure_ratio": (
                    0.0
                    if self._metrics["annotations_attempted"] == 0
                    else self._metrics["annotations_failed"]
                    / self._metrics["annotations_attempted"]
                ),
                "annotation_queue_overflow_ratio": (
                    0.0
                    if self._metrics["annotations_attempted"] == 0
                    else self._metrics["annotations_queue_overflow"]
                    / self._metrics["annotations_attempted"]
                ),
                **self._metrics,
            }

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._metrics_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = Thread(
                    target=self._run_worker,
                    name="raw-ledger-shadow",
                    daemon=True,
                )
                self._worker.start()

    def _ensure_failure_worker(self) -> None:
        if self._failure_worker is not None and self._failure_worker.is_alive():
            return
        with self._metrics_lock:
            if self._failure_worker is None or not self._failure_worker.is_alive():
                self._failure_worker = Thread(
                    target=self._run_failure_worker,
                    name="raw-ledger-shadow-failures",
                    daemon=True,
                )
                self._failure_worker.start()

    def _queue_failure_report(self, event_id: str, detail: str) -> None:
        if self.failure_log is None and self.error_logger is None:
            return
        self._ensure_failure_worker()
        try:
            self._failure_queue.put_nowait(_FailureWork(event_id=event_id, detail=detail))
        except Full:
            self._increment("failure_report_queue_overflow")
            return
        self._increment("failure_reports_queued")

    def _run_failure_worker(self) -> None:
        while True:
            work = self._failure_queue.get()
            try:
                if not self._report_failure(work.event_id, work.detail):
                    self._increment("failure_report_errors")
            except BaseException:
                self._increment("failure_report_errors")
            finally:
                self._failure_queue.task_done()

    def _run_worker(self) -> None:
        while True:
            work = self._queue.get()
            try:
                if isinstance(work, RawEvent):
                    status = self.ledger.append(work).status
                    self._increment(f"raw_{status}")
                else:
                    status = self.ledger.annotate_quarantine(
                        work.event_id,
                        annotation_id=work.annotation_id,
                        reason=work.reason,
                        annotated_at=work.annotated_at,
                        provenance=work.provenance,
                    )
                    self._increment(f"annotations_{status}")
            except BaseException as error:
                self._increment("failed")
                self._increment(
                    "raw_failed" if isinstance(work, RawEvent) else "annotations_failed"
                )
                try:
                    detail = f"{type(error).__name__}: {error}"[:500]
                    self._report_failure(work.event_id, detail)
                except BaseException:
                    # Diagnostic paths are never allowed to kill the worker.
                    pass
            else:
                self._increment(status)
            finally:
                self._queue.task_done()

    def _increment(self, name: str) -> None:
        with self._metrics_lock:
            self._metrics[name] += 1

    def _report_failure(self, event_id: str, detail: str) -> bool:
        reported = True
        if self.error_logger is not None:
            try:
                self.error_logger(detail)
            except Exception:
                reported = False
        if self.failure_log is not None and not self._write_failure(event_id, detail):
            reported = False
        return reported

    def _write_failure(self, event_id: str, detail: str) -> bool:
        if self.failure_log is None:
            return True
        try:
            self.failure_log.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "event_id_sha256": hashlib_sha256(event_id),
                "failed_at": datetime.now(UTC).isoformat(),
                "error_type": detail.split(":", 1)[0],
            }
            descriptor = os.open(
                self.failure_log,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, UnicodeError, ValueError):
            self._increment("failure_log_write_errors")
            return False
        return True


def hashlib_sha256(value: str) -> str:
    """Hash identifiers before diagnostic logging to avoid source-ID disclosure."""

    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
