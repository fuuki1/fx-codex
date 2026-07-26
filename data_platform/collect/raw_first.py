"""Raw-first ingestion with an atomic, bounded-file publication boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from data_platform.collect.capture_journal import CaptureJournal, CaptureJournalError
from data_platform.collect.contract import CollectedQuote, QuoteContractError
from data_platform.quality.state import QualityState
from data_platform.raw.immutable_store import ImmutableRawStore

FLAG_DUPLICATE = "duplicate_quote"
FLAG_OUT_OF_ORDER = "out_of_order_quote"
FLAG_STALE = "stale_quote"
DEFAULT_STALE_AFTER_SECONDS = 30.0


class RawFirstError(RuntimeError):
    """The raw-first or compatibility evidence contract cannot be honoured."""


@dataclass
class QuarantineRecord:
    raw_sha256: str
    reason: str
    detail: str
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_sha256": self.raw_sha256,
            "reason": self.reason,
            "detail": self.detail,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass
class IngestResult:
    raw_sha256: str
    capture_id: str
    accepted: list[CollectedQuote] = field(default_factory=list)
    quarantined: list[QuarantineRecord] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if not payload:
        return 0
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return payload.count("\n")


class QuoteLog:
    """Canonical journal plus optional legacy JSONL compatibility views.

    Production streaming collection disables ``legacy_jsonl``. The JSONL
    files remain available only to old batch consumers and tests; they are
    never a publication boundary or replay source.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        legacy_jsonl: bool = True,
        verify_journal: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.accepted_path = self.directory / "quotes.jsonl"
        self.quarantine_path = self.directory / "quarantine.jsonl"
        self.legacy_jsonl = legacy_jsonl
        try:
            self.journal = CaptureJournal(
                self.directory / "capture_journal",
                verify=verify_journal,
            )
        except CaptureJournalError as error:
            raise RawFirstError(f"capture journal is invalid: {error}") from error
        # Transitional alias for callers that only inspect entries().
        self.ledger = self.journal
        if self.legacy_jsonl:
            self._validate_legacy_jsonl()

    def publish_legacy(
        self,
        *,
        capture_id: str,
        accepted: Sequence[CollectedQuote],
        quarantined_quotes: Sequence[CollectedQuote],
        quarantine_records: Sequence[QuarantineRecord],
    ) -> None:
        if not self.legacy_jsonl:
            return
        _append_jsonl(
            self.accepted_path,
            ({**quote.to_dict(), "capture_id": capture_id} for quote in accepted),
        )
        rows: list[dict[str, Any]] = [
            {**quote.to_dict(), "capture_id": capture_id} for quote in quarantined_quotes
        ]
        rows.extend({**record.to_dict(), "capture_id": capture_id} for record in quarantine_records)
        _append_jsonl(self.quarantine_path, rows)

    def _validate_legacy_jsonl(self) -> None:
        for path, label in (
            (self.accepted_path, "accepted quote log"),
            (self.quarantine_path, "quarantine log"),
        ):
            if not path.is_file():
                continue
            line_number = 0
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise RawFirstError(f"{label} row {line_number} must be a JSON object")
            except json.JSONDecodeError as error:
                raise RawFirstError(
                    f"{label} is malformed at line {line_number}: {error.msg}"
                ) from error
            except OSError as error:
                raise RawFirstError(f"cannot validate {label}: {error}") from error


Parser = Callable[[bytes], Sequence[CollectedQuote]]


def ingest_payload(
    raw_payload: bytes,
    *,
    parser: Parser,
    log: QuoteLog,
    store: ImmutableRawStore | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    now: Callable[[], datetime] | None = None,
) -> IngestResult:
    """Persist raw bytes first, then atomically publish the terminal outcome.

    ``store`` is a compatibility mirror for low-frequency legacy collectors.
    The canonical streaming path passes ``None`` so message volume creates one
    daily database shard rather than one raw file per provider message.
    """

    clock = now or (lambda: datetime.now(UTC))
    occurred_at = clock()
    try:
        capture = log.journal.begin(
            raw_payload,
            occurred_at=occurred_at,
            metadata={"storage_contract": "daily_sqlite_journal"},
        )
        stored = log.journal.read_raw(capture)
    except CaptureJournalError as error:
        raise RawFirstError(f"raw journal write failed: {error}") from error
    observed = hashlib.sha256(stored).hexdigest()
    if observed != capture.raw_sha256:
        raise RawFirstError(f"raw hash verification failed: {observed} != {capture.raw_sha256}")
    if store is not None:
        ref = store.put(raw_payload)
        if ref.sha256 != capture.raw_sha256 or store.get(ref.sha256) != raw_payload:
            raise RawFirstError("legacy raw mirror verification failed")

    result = IngestResult(raw_sha256=capture.raw_sha256, capture_id=capture.capture_id)
    try:
        quotes = parser(raw_payload)
    except (QuoteContractError, ValueError, KeyError, TypeError) as error:
        record = QuarantineRecord(
            raw_sha256=capture.raw_sha256,
            reason="schema_validation_failed",
            detail=str(error)[:500],
            occurred_at=occurred_at,
        )
        try:
            log.journal.finalize_failure(
                capture,
                occurred_at=occurred_at,
                reason=record.reason,
                detail=record.detail,
                error_type=type(error).__name__,
            )
        except CaptureJournalError as journal_error:
            raise RawFirstError(f"failed to quarantine capture: {journal_error}") from journal_error
        result.quarantined.append(record)
        log.publish_legacy(
            capture_id=capture.capture_id,
            accepted=(),
            quarantined_quotes=(),
            quarantine_records=(record,),
        )
        return result

    valid_quotes: list[CollectedQuote] = []
    preclassified_records: list[QuarantineRecord] = []
    extra_evidence: list[Mapping[str, object]] = []
    for quote in quotes:
        if quote.raw_payload_sha256 == capture.raw_sha256:
            valid_quotes.append(quote)
            continue
        record = QuarantineRecord(
            raw_sha256=capture.raw_sha256,
            reason="raw_hash_mismatch",
            detail=(
                f"quote cites {quote.raw_payload_sha256[:12]}… but payload is "
                f"{capture.raw_sha256[:12]}…"
            ),
            occurred_at=occurred_at,
        )
        preclassified_records.append(record)
        extra_evidence.append(record.to_dict())
    try:
        finalized = log.journal.finalize_quotes(
            capture,
            valid_quotes,
            occurred_at=occurred_at,
            stale_after_seconds=stale_after_seconds,
            extra_quarantine=extra_evidence,
        )
    except CaptureJournalError as error:
        raise RawFirstError(f"capture commit failed: {error}") from error

    result.accepted.extend(finalized.accepted)
    result.quarantined.extend(preclassified_records)
    for quote in finalized.quarantined:
        result.quarantined.append(
            QuarantineRecord(
                raw_sha256=capture.raw_sha256,
                reason=",".join(quote.quality_flags) or str(QualityState.QUARANTINED),
                detail=f"{quote.instrument} quote quarantined",
                occurred_at=occurred_at,
            )
        )
    log.publish_legacy(
        capture_id=capture.capture_id,
        accepted=finalized.accepted,
        quarantined_quotes=finalized.quarantined,
        quarantine_records=preclassified_records,
    )
    return result
