"""Raw-first ingest pipeline for collected quotes.

Mandatory processing order (never reordered, never skipped):

    provider raw payload
      -> immutable raw store (content-addressed, BEFORE any parsing)
      -> hash verification (stored bytes re-read and re-hashed)
      -> schema validation / parsing
      -> normalized ``CollectedQuote``
      -> quality assignment (duplicate / out-of-order / stale flags)
      -> append-only JSONL quote log (accepted) or quarantine log (rejected)

A parse or contract failure never discards the raw payload — the raw ref is
already durable and the failure is written to the quarantine log with its
reason. Nothing is repaired: no forward-fill, no averaging, no zero-fill.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from data_platform.collect.contract import CollectedQuote, QuoteContractError
from data_platform.quality.state import QualityState
from data_platform.raw.immutable_store import ImmutableRawStore

FLAG_DUPLICATE = "duplicate_quote"
FLAG_OUT_OF_ORDER = "out_of_order_quote"
FLAG_STALE = "stale_quote"
DEFAULT_STALE_AFTER_SECONDS = 30.0


class RawFirstError(RuntimeError):
    """Raised when the raw store or append-only log invariants cannot be honoured."""


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
    accepted: list[CollectedQuote] = field(default_factory=list)
    quarantined: list[QuarantineRecord] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Durable append: newline-delimited JSON, fsynced, never rewritten."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if not payload:
        return 0
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return payload.count("\n")


class QuoteLog:
    """Append-only quote logs with duplicate and ordering detection.

    Bootstrap streams the file line-by-line so memory use remains bounded. A
    malformed accepted-log row is a fail-closed integrity error; silently
    skipping it could allow a duplicate or out-of-order quote to be reaccepted.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.accepted_path = self.directory / "quotes.jsonl"
        self.quarantine_path = self.directory / "quarantine.jsonl"
        self._seen: set[tuple[str, str, str, int | None]] = set()
        self._last_event: dict[tuple[str, str], datetime] = {}
        self._bootstrap()

    def _bootstrap(self) -> None:
        if not self.accepted_path.is_file():
            return
        with self.accepted_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise RawFirstError(
                        f"accepted quote log is malformed at line {line_number}: {error.msg}"
                    ) from error
                if not isinstance(row, dict):
                    raise RawFirstError(
                        f"accepted quote log row {line_number} must be a JSON object"
                    )
                try:
                    identity = self._identity_from_dict(row)
                    key = (str(row["provider"]), str(row["instrument"]))
                    event = row.get("provider_event_time") or row.get("received_at")
                    stamp = datetime.fromisoformat(str(event))
                except (KeyError, TypeError, ValueError) as error:
                    raise RawFirstError(
                        f"accepted quote log row {line_number} violates the quote schema"
                    ) from error
                self._seen.add(identity)
                previous = self._last_event.get(key)
                if previous is None or stamp > previous:
                    self._last_event[key] = stamp

    @staticmethod
    def _identity_from_dict(row: dict[str, Any]) -> tuple[str, str, str, int | None]:
        return (
            str(row["provider"]),
            str(row["instrument"]),
            str(row.get("provider_event_time") or row.get("received_at")),
            row.get("sequence_id"),
        )

    @staticmethod
    def _identity(quote: CollectedQuote) -> tuple[str, str, str, int | None]:
        stamp = quote.provider_event_time or quote.received_at
        return (quote.provider, quote.instrument, stamp.isoformat(), quote.sequence_id)

    def classify(self, quote: CollectedQuote) -> CollectedQuote:
        identity = self._identity(quote)
        if identity in self._seen:
            return quote.with_quality(QualityState.QUARANTINED, FLAG_DUPLICATE)
        key = (quote.provider, quote.instrument)
        stamp = quote.provider_event_time or quote.received_at
        last = self._last_event.get(key)
        if last is not None and stamp < last:
            return quote.with_quality(QualityState.DEGRADED, FLAG_OUT_OF_ORDER)
        return quote

    def record(self, quote: CollectedQuote) -> None:
        identity = self._identity(quote)
        if quote.quality_state in (QualityState.USABLE, QualityState.DEGRADED):
            self._seen.add(identity)
            key = (quote.provider, quote.instrument)
            stamp = quote.provider_event_time or quote.received_at
            last = self._last_event.get(key)
            if last is None or stamp > last:
                self._last_event[key] = stamp
            _append_jsonl(self.accepted_path, [quote.to_dict()])
        else:
            _append_jsonl(self.quarantine_path, [quote.to_dict()])

    def quarantine(self, record: QuarantineRecord) -> None:
        _append_jsonl(self.quarantine_path, [record.to_dict()])


Parser = Callable[[bytes], Sequence[CollectedQuote]]


def ingest_payload(
    raw_payload: bytes,
    *,
    parser: Parser,
    store: ImmutableRawStore,
    log: QuoteLog,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    now: Callable[[], datetime] | None = None,
) -> IngestResult:
    clock = now or (lambda: datetime.now(UTC))
    ref = store.put(raw_payload)
    stored = store.get(ref.sha256)
    observed = hashlib.sha256(stored).hexdigest()
    if observed != ref.sha256:
        raise RawFirstError(f"raw hash verification failed: {observed} != {ref.sha256}")
    result = IngestResult(raw_sha256=ref.sha256)
    try:
        quotes = parser(raw_payload)
    except (QuoteContractError, ValueError, KeyError, TypeError) as error:
        record = QuarantineRecord(
            raw_sha256=ref.sha256,
            reason="schema_validation_failed",
            detail=str(error)[:500],
            occurred_at=clock(),
        )
        log.quarantine(record)
        result.quarantined.append(record)
        return result
    for quote in quotes:
        if quote.raw_payload_sha256 != ref.sha256:
            record = QuarantineRecord(
                raw_sha256=ref.sha256,
                reason="raw_hash_mismatch",
                detail=(
                    f"quote cites {quote.raw_payload_sha256[:12]}… but payload is "
                    f"{ref.sha256[:12]}…"
                ),
                occurred_at=clock(),
            )
            log.quarantine(record)
            result.quarantined.append(record)
            continue
        classified = log.classify(quote)
        event = classified.provider_event_time
        if event is not None:
            lag = (classified.received_at - event).total_seconds()
            if lag > stale_after_seconds:
                classified = classified.with_quality(QualityState.QUARANTINED, FLAG_STALE)
        log.record(classified)
        if classified.quality_state in (QualityState.USABLE, QualityState.DEGRADED):
            result.accepted.append(classified)
        else:
            result.quarantined.append(
                QuarantineRecord(
                    raw_sha256=ref.sha256,
                    reason=",".join(classified.quality_flags) or str(classified.quality_state),
                    detail=f"{classified.instrument} quote quarantined",
                    occurred_at=clock(),
                )
            )
    return result
