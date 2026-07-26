"""Bounded-file, append-only transaction journal for streaming quote capture.

One SQLite shard is created per UTC day.  Evidence tables are insert-only and
hash chained across shards.  A provider message is published in two durable
transactions:

1. ``raw_durable`` stores the exact provider bytes;
2. a terminal entry atomically stores normalized rows and their disposition.

Readers expose accepted rows only when the terminal state is ``committed``.
An interrupted raw transaction is durable but invisible until explicit
recovery appends an ``unavailable`` terminal entry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any

from data_platform.collect.contract import CollectedQuote, QuoteContractError
from data_platform.quality.state import QualityState

JOURNAL_SCHEMA_VERSION = 1
ZERO_SHA256 = "0" * 64
DEFAULT_MAX_SHARD_BYTES = 8 * 1024 * 1024 * 1024
_DATABASE_GLOB = "capture-????-??-??.sqlite3"
_ACCEPTED_STATES = frozenset({QualityState.USABLE, QualityState.DEGRADED})


class CaptureJournalError(RuntimeError):
    """The journal cannot preserve or prove its append-only contract."""


class JournalState(StrEnum):
    RAW_DURABLE = "raw_durable"
    COMMITTED = "committed"
    QUARANTINED = "quarantined"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CaptureRef:
    capture_id: str
    raw_sha256: str
    raw_sequence: int
    raw_entry_sha256: str
    shard_date: str
    size: int


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    capture_id: str
    raw_sha256: str
    state: JournalState
    occurred_at: datetime
    previous_entry_sha256: str
    metadata: dict[str, Any]
    entry_sha256: str
    shard_date: str
    schema_version: int = JOURNAL_SCHEMA_VERSION


@dataclass(frozen=True)
class CommittedQuote:
    commit_sequence: int
    capture_id: str
    row_index: int
    quote: CollectedQuote

    @property
    def replay_position(self) -> tuple[int, int]:
        return (self.commit_sequence, self.row_index)


@dataclass(frozen=True)
class FinalizedQuotes:
    entry: JournalEntry
    accepted: tuple[CollectedQuote, ...]
    quarantined: tuple[CollectedQuote, ...]


@dataclass(frozen=True)
class JournalIdentity:
    root: Path
    genesis_sha256: str
    head_sha256: str
    head_sequence: int
    shard_count: int


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CaptureJournalError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise CaptureJournalError(f"{field_name} must be a lowercase SHA-256")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CaptureJournalError(f"{field_name} must be a lowercase SHA-256")
    return value


def _canonical_value(value: object, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CaptureJournalError(f"{field_name} must contain only finite numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CaptureJournalError(f"{field_name} keys must be strings")
            result[key] = _canonical_value(item, field_name=f"{field_name}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _canonical_value(item, field_name=f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    raise CaptureJournalError(
        f"{field_name} contains unsupported value type {type(value).__name__}"
    )


def _canonical_json(value: object, *, field_name: str) -> str:
    return json.dumps(
        _canonical_value(value, field_name=field_name),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _event_hash(
    *,
    sequence: int,
    capture_id: str,
    raw_sha256: str,
    state: JournalState,
    occurred_at: str,
    previous_entry_sha256: str,
    metadata_json: str,
) -> str:
    payload = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "sequence": sequence,
        "capture_id": capture_id,
        "raw_sha256": raw_sha256,
        "state": str(state),
        "occurred_at": occurred_at,
        "previous_entry_sha256": previous_entry_sha256,
        "metadata": json.loads(metadata_json),
    }
    return hashlib.sha256(_canonical_json(payload, field_name="event").encode("ascii")).hexdigest()


def _row_hash(disposition: str, payload_json: str) -> str:
    return hashlib.sha256(f"{disposition}\0{payload_json}".encode("ascii")).hexdigest()


def _rows_hash(rows: Sequence[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for disposition, payload_json in rows:
        digest.update(bytes.fromhex(_row_hash(disposition, payload_json)))
    return digest.hexdigest()


def _identity(quote: CollectedQuote) -> tuple[str, str]:
    stamp = quote.provider_event_time or quote.received_at
    payload = [
        quote.provider,
        quote.instrument,
        stamp.astimezone(UTC).isoformat(),
        quote.sequence_id,
    ]
    return (
        hashlib.sha256(_canonical_json(payload, field_name="identity").encode("ascii")).hexdigest(),
        stamp.astimezone(UTC).isoformat(),
    )


class CaptureJournal:
    """Daily-sharded canonical capture journal."""

    def __init__(
        self,
        root: str | Path,
        *,
        verify: bool = True,
        max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES,
    ) -> None:
        if (
            isinstance(max_shard_bytes, bool)
            or not isinstance(max_shard_bytes, int)
            or max_shard_bytes < 1
        ):
            raise CaptureJournalError("max_shard_bytes must be a positive integer")
        self.root = Path(root)
        if self.root.is_symlink():
            raise CaptureJournalError(f"capture journal root cannot be a symlink: {self.root}")
        self.max_shard_bytes = max_shard_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        if verify and self.shard_paths():
            self.verify()

    def shard_paths(self) -> tuple[Path, ...]:
        paths = tuple(sorted(self.root.glob(_DATABASE_GLOB)))
        symlinks = [path for path in paths if path.is_symlink()]
        if symlinks:
            raise CaptureJournalError(f"journal shard cannot be a symlink: {symlinks[0]}")
        return paths

    def begin(
        self,
        raw_payload: bytes,
        *,
        occurred_at: datetime,
        metadata: Mapping[str, object] | None = None,
    ) -> CaptureRef:
        if not isinstance(raw_payload, bytes):
            raise CaptureJournalError("raw_payload must be bytes")
        timestamp = _aware_utc(occurred_at, field_name="occurred_at")
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        details = dict(metadata or {})
        details["raw_size_bytes"] = len(raw_payload)
        metadata_json = _canonical_json(details, field_name="metadata")
        shard = self._writable_shard(timestamp)
        self._assert_shard_capacity(shard, incoming_bytes=len(raw_payload))
        with self._connect(shard) as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence, previous_hash, previous_time = self._head(connection)
            if previous_time is not None and timestamp < previous_time:
                raise CaptureJournalError("journal occurred_at moved backwards")
            next_sequence = sequence + 1
            capture_id = hashlib.sha256(
                _canonical_json(
                    {
                        "raw_sha256": raw_digest,
                        "occurred_at": timestamp.isoformat(),
                        "raw_sequence": next_sequence,
                    },
                    field_name="capture",
                ).encode("ascii")
            ).hexdigest()
            entry_hash = _event_hash(
                sequence=next_sequence,
                capture_id=capture_id,
                raw_sha256=raw_digest,
                state=JournalState.RAW_DURABLE,
                occurred_at=timestamp.isoformat(),
                previous_entry_sha256=previous_hash,
                metadata_json=metadata_json,
            )
            connection.execute(
                """
                INSERT INTO events (
                    sequence, capture_id, raw_sha256, state, occurred_at,
                    previous_entry_sha256, metadata_json, entry_sha256, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_sequence,
                    capture_id,
                    raw_digest,
                    str(JournalState.RAW_DURABLE),
                    timestamp.isoformat(),
                    previous_hash,
                    metadata_json,
                    entry_hash,
                    raw_payload,
                ),
            )
            connection.commit()
        return CaptureRef(
            capture_id=capture_id,
            raw_sha256=raw_digest,
            raw_sequence=next_sequence,
            raw_entry_sha256=entry_hash,
            shard_date=self._shard_date(shard),
            size=len(raw_payload),
        )

    def read_raw(self, capture: CaptureRef) -> bytes:
        path = self._path_for_date(capture.shard_date)
        with self._connect(path, readonly=True) as connection:
            row = connection.execute(
                """
                SELECT raw_payload, raw_sha256, entry_sha256
                FROM events
                WHERE sequence = ? AND capture_id = ? AND state = ?
                """,
                (capture.raw_sequence, capture.capture_id, str(JournalState.RAW_DURABLE)),
            ).fetchone()
        if row is None:
            raise CaptureJournalError(f"raw capture is missing: {capture.capture_id}")
        payload = bytes(row["raw_payload"])
        if row["raw_sha256"] != capture.raw_sha256:
            raise CaptureJournalError(f"raw capture digest changed: {capture.capture_id}")
        if row["entry_sha256"] != capture.raw_entry_sha256:
            raise CaptureJournalError(f"raw capture entry hash changed: {capture.capture_id}")
        observed = hashlib.sha256(payload).hexdigest()
        if observed != capture.raw_sha256:
            raise CaptureJournalError(f"raw payload hash mismatch: {capture.capture_id}")
        return payload

    def finalize_quotes(
        self,
        capture: CaptureRef,
        quotes: Sequence[CollectedQuote],
        *,
        occurred_at: datetime,
        stale_after_seconds: float,
        extra_quarantine: Sequence[Mapping[str, object]] = (),
    ) -> FinalizedQuotes:
        timestamp = _aware_utc(occurred_at, field_name="occurred_at")
        if stale_after_seconds < 0:
            raise CaptureJournalError("stale_after_seconds must be non-negative")
        shard = self._writable_shard(timestamp)
        accepted: list[CollectedQuote] = []
        quarantined: list[CollectedQuote] = []
        encoded_rows: list[tuple[str, str, str | None, str | None, str | None, str | None]] = []
        with self._connect(shard) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_unfinalized(connection, capture)
            local_identities: set[str] = set()
            local_last: dict[tuple[str, str], tuple[str, str | None]] = {}
            for quote in quotes:
                if quote.raw_payload_sha256 != capture.raw_sha256:
                    mismatch_evidence = {
                        "raw_sha256": capture.raw_sha256,
                        "reason": "raw_hash_mismatch",
                        "detail": f"quote cites {quote.raw_payload_sha256}",
                        "occurred_at": timestamp.isoformat(),
                    }
                    encoded_rows.append(
                        (
                            "quarantined",
                            _canonical_json(mismatch_evidence, field_name="row"),
                            None,
                            None,
                            None,
                            None,
                        )
                    )
                    continue
                classified = quote
                event = quote.provider_event_time
                if event is not None:
                    lag = (quote.received_at - event).total_seconds()
                    if lag > stale_after_seconds:
                        classified = quote.with_quality(QualityState.QUARANTINED, "stale_quote")
                identity_sha, event_time = _identity(classified)
                key = (classified.provider, classified.instrument)
                if classified.quality_state in _ACCEPTED_STATES:
                    previous = local_last.get(key)
                    if previous is None:
                        state_row = connection.execute(
                            """
                            SELECT last_event_time, last_identity_sha256
                            FROM stream_state
                            WHERE provider = ? AND instrument = ?
                            """,
                            key,
                        ).fetchone()
                        previous = (
                            (
                                str(state_row["last_event_time"]),
                                str(state_row["last_identity_sha256"]),
                            )
                            if state_row is not None
                            else ("", None)
                        )
                    persisted_duplicate = connection.execute(
                        """
                        SELECT 1 FROM quote_rows
                        WHERE disposition = 'accepted' AND identity_sha256 = ?
                        LIMIT 1
                        """,
                        (identity_sha,),
                    ).fetchone()
                    if (
                        identity_sha in local_identities
                        or persisted_duplicate is not None
                        or previous[1] == identity_sha
                    ):
                        classified = classified.with_quality(
                            QualityState.QUARANTINED, "duplicate_quote"
                        )
                    elif previous[0] and event_time < previous[0]:
                        classified = classified.with_quality(
                            QualityState.QUARANTINED, "out_of_order_quote"
                        )
                disposition = (
                    "accepted" if classified.quality_state in _ACCEPTED_STATES else "quarantined"
                )
                if disposition == "accepted":
                    accepted.append(classified)
                    local_identities.add(identity_sha)
                    local_last[key] = (event_time, identity_sha)
                else:
                    quarantined.append(classified)
                payload_json = _canonical_json(classified.to_dict(), field_name="quote")
                encoded_rows.append(
                    (
                        disposition,
                        payload_json,
                        identity_sha,
                        classified.provider,
                        classified.instrument,
                        event_time,
                    )
                )
            for additional_evidence in extra_quarantine:
                encoded_rows.append(
                    (
                        "quarantined",
                        _canonical_json(dict(additional_evidence), field_name="quarantine"),
                        None,
                        None,
                        None,
                        None,
                    )
                )
            quality_flag_counts: dict[str, int] = {}
            for quote in (*accepted, *quarantined):
                for flag in quote.quality_flags:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
            for evidence in extra_quarantine:
                reason = evidence.get("reason")
                if reason:
                    for flag in str(reason).split(","):
                        quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
            terminal = self._insert_terminal(
                connection,
                capture,
                state=JournalState.COMMITTED,
                occurred_at=timestamp,
                rows=encoded_rows,
                metadata={
                    "accepted_count": len(accepted),
                    "quarantined_count": len(encoded_rows) - len(accepted),
                    "quality_flag_counts": quality_flag_counts,
                },
            )
            for (provider, instrument), (
                last_event_time,
                last_identity_sha,
            ) in local_last.items():
                connection.execute(
                    """
                    INSERT INTO stream_state (
                        provider, instrument, last_event_time, last_identity_sha256
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(provider, instrument) DO UPDATE SET
                        last_event_time = excluded.last_event_time,
                        last_identity_sha256 = excluded.last_identity_sha256
                    """,
                    (provider, instrument, last_event_time, last_identity_sha),
                )
            connection.commit()
        return FinalizedQuotes(terminal, tuple(accepted), tuple(quarantined))

    def finalize_failure(
        self,
        capture: CaptureRef,
        *,
        occurred_at: datetime,
        reason: str,
        detail: str,
        error_type: str,
    ) -> JournalEntry:
        timestamp = _aware_utc(occurred_at, field_name="occurred_at")
        evidence = {
            "raw_sha256": capture.raw_sha256,
            "reason": reason,
            "detail": detail[:500],
            "occurred_at": timestamp.isoformat(),
        }
        shard = self._writable_shard(timestamp)
        with self._connect(shard) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_unfinalized(connection, capture)
            entry = self._insert_terminal(
                connection,
                capture,
                state=JournalState.QUARANTINED,
                occurred_at=timestamp,
                rows=[
                    (
                        "quarantined",
                        _canonical_json(evidence, field_name="quarantine"),
                        None,
                        None,
                        None,
                        None,
                    )
                ],
                metadata={
                    "accepted_count": 0,
                    "quarantined_count": 1,
                    "reason": reason,
                    "error_type": error_type,
                },
            )
            connection.commit()
        return entry

    def recover_incomplete(self, *, occurred_at: datetime) -> tuple[str, ...]:
        timestamp = _aware_utc(occurred_at, field_name="occurred_at")
        recovered: list[str] = []
        paths = self.shard_paths()
        candidates: list[CaptureRef] = []
        for path in paths:
            with self._connect(path, readonly=True) as connection:
                rows = connection.execute(
                    """
                    SELECT raw.*
                    FROM events AS raw
                    WHERE raw.state = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM events AS terminal
                          WHERE terminal.capture_id = raw.capture_id
                            AND terminal.state != ?
                      )
                    ORDER BY raw.sequence
                    """,
                    (
                        str(JournalState.RAW_DURABLE),
                        str(JournalState.RAW_DURABLE),
                    ),
                ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                candidates.append(
                    CaptureRef(
                        capture_id=str(row["capture_id"]),
                        raw_sha256=str(row["raw_sha256"]),
                        raw_sequence=int(row["sequence"]),
                        raw_entry_sha256=str(row["entry_sha256"]),
                        shard_date=self._shard_date(path),
                        size=int(metadata["raw_size_bytes"]),
                    )
                )
        for capture in candidates:
            if self._terminal_exists(capture.capture_id):
                continue
            shard = self._writable_shard(timestamp)
            with self._connect(shard) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_unfinalized(connection, capture)
                self._insert_terminal(
                    connection,
                    capture,
                    state=JournalState.UNAVAILABLE,
                    occurred_at=timestamp,
                    rows=[],
                    metadata={
                        "accepted_count": 0,
                        "quarantined_count": 0,
                        "reason": "recovered_incomplete_capture",
                    },
                )
                connection.commit()
            recovered.append(capture.capture_id)
        return tuple(recovered)

    def entries(self, *, start_sequence: int = 0) -> tuple[JournalEntry, ...]:
        return tuple(self.iter_entries(start_sequence=start_sequence))

    def iter_entries(self, *, start_sequence: int = 0) -> Iterator[JournalEntry]:
        """Stream journal entry metadata without retaining the full history."""

        for path in self.shard_paths():
            with self._connect(path, readonly=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM events WHERE sequence >= ? ORDER BY sequence",
                    (start_sequence,),
                )
                for row in rows:
                    yield self._entry_from_row(row, self._shard_date(path))

    def read_quotes(
        self,
        *,
        start_sequence: int = 0,
        expected_entry_sha256: str | None = None,
    ) -> tuple[CommittedQuote, ...]:
        """Read verified committed rows into a tuple for bounded replay windows."""

        return tuple(
            self.iter_verified_quotes(
                start_sequence=start_sequence,
                expected_entry_sha256=expected_entry_sha256,
            )
        )

    def iter_verified_quotes(
        self,
        *,
        start_sequence: int = 0,
        expected_entry_sha256: str | None = None,
    ) -> Iterator[CommittedQuote]:
        """Stream verified committed rows from a durable replay checkpoint.

        Origin replay verifies the complete chain. Incremental replay anchors
        the first event to the commit-entry hash stored in the bridge
        checkpoint, then verifies every subsequent event and terminal binding.
        """

        if (
            isinstance(start_sequence, bool)
            or not isinstance(start_sequence, int)
            or start_sequence < 0
        ):
            raise CaptureJournalError("start_sequence must be a non-negative integer")
        if start_sequence == 0:
            if expected_entry_sha256 is not None:
                raise CaptureJournalError("origin replay must not provide an entry hash")
            expected_sequence = 1
            expected_previous = ZERO_SHA256
        else:
            expected_sequence = start_sequence
            expected_previous = ""
            _sha256(expected_entry_sha256, field_name="expected_entry_sha256")

        found_anchor = start_sequence == 0
        previous_time: datetime | None = None
        for path in self.shard_paths():
            with self._connect(path, readonly=True) as connection:
                events = connection.execute(
                    "SELECT * FROM events WHERE sequence >= ? ORDER BY sequence",
                    (start_sequence,),
                )
                for event in events:
                    sequence = int(event["sequence"])
                    if sequence != expected_sequence:
                        raise CaptureJournalError(
                            f"journal sequence gap: expected {expected_sequence}"
                        )
                    try:
                        state = JournalState(event["state"])
                        occurred_at = _aware_utc(
                            datetime.fromisoformat(event["occurred_at"]),
                            field_name="occurred_at",
                        )
                        metadata = json.loads(event["metadata_json"])
                    except (ValueError, TypeError, json.JSONDecodeError) as error:
                        raise CaptureJournalError(
                            f"journal event schema invalid at sequence {sequence}"
                        ) from error
                    metadata_json = _canonical_json(metadata, field_name="metadata")
                    if metadata_json != event["metadata_json"]:
                        raise CaptureJournalError(
                            f"journal metadata is not canonical at sequence {sequence}"
                        )
                    event_previous = _sha256(
                        event["previous_entry_sha256"],
                        field_name="previous_entry_sha256",
                    )
                    event_hash = _event_hash(
                        sequence=sequence,
                        capture_id=_sha256(event["capture_id"], field_name="capture_id"),
                        raw_sha256=_sha256(event["raw_sha256"], field_name="raw_sha256"),
                        state=state,
                        occurred_at=occurred_at.isoformat(),
                        previous_entry_sha256=event_previous,
                        metadata_json=metadata_json,
                    )
                    if event["entry_sha256"] != event_hash:
                        raise CaptureJournalError(
                            f"journal entry hash mismatch at sequence {sequence}"
                        )
                    if not found_anchor:
                        if event_hash != expected_entry_sha256:
                            raise CaptureJournalError(
                                "incremental replay checkpoint entry hash changed"
                            )
                        found_anchor = True
                    elif event_previous != expected_previous:
                        raise CaptureJournalError(
                            f"journal hash chain mismatch at sequence {sequence}"
                        )
                    if previous_time is not None and occurred_at < previous_time:
                        raise CaptureJournalError("journal occurred_at moved backwards")
                    if state is JournalState.RAW_DURABLE:
                        if (
                            hashlib.sha256(bytes(event["raw_payload"])).hexdigest()
                            != event["raw_sha256"]
                        ):
                            raise CaptureJournalError(
                                f"raw payload hash mismatch at sequence {sequence}"
                            )
                    else:
                        if event["raw_payload"] is not None:
                            raise CaptureJournalError(
                                f"terminal event contains raw payload at sequence {sequence}"
                            )
                        self._verify_terminal(connection, event, metadata)
                    if state is JournalState.COMMITTED:
                        yield from self._committed_rows(connection, event)
                    expected_previous = event_hash
                    previous_time = occurred_at
                    expected_sequence += 1
        if not found_anchor:
            raise CaptureJournalError("incremental replay checkpoint is missing")

    def identity(self) -> JournalIdentity:
        paths = self.shard_paths()
        if not paths:
            raise CaptureJournalError("capture journal has no durable entries")
        with self._connect(paths[0], readonly=True) as connection:
            first = connection.execute(
                "SELECT entry_sha256 FROM events ORDER BY sequence LIMIT 1"
            ).fetchone()
        with self._connect(paths[-1], readonly=True) as connection:
            last = connection.execute(
                "SELECT sequence, entry_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        if first is None or last is None:
            raise CaptureJournalError("capture journal has no durable entries")
        return JournalIdentity(
            root=self.root.resolve(),
            genesis_sha256=_sha256(first["entry_sha256"], field_name="genesis_sha256"),
            head_sha256=_sha256(last["entry_sha256"], field_name="head_sha256"),
            head_sequence=int(last["sequence"]),
            shard_count=len(paths),
        )

    def verified_quote_count(self) -> int:
        """Fully verify evidence and stream semantic quote validation in bounded memory."""

        self.verify()
        count = 0
        for path in self.shard_paths():
            with self._connect(path, readonly=True) as connection:
                rows = connection.execute(
                    """
                    SELECT quote.payload_json
                    FROM quote_rows AS quote
                    JOIN events AS terminal
                      ON terminal.capture_id = quote.capture_id
                     AND terminal.state = ?
                    WHERE quote.disposition = 'accepted'
                    ORDER BY terminal.sequence, quote.row_index
                    """,
                    (str(JournalState.COMMITTED),),
                )
                for row in rows:
                    try:
                        payload = json.loads(row["payload_json"])
                        quote = CollectedQuote.from_dict(payload)
                    except (
                        json.JSONDecodeError,
                        QuoteContractError,
                        TypeError,
                        ValueError,
                    ) as error:
                        raise CaptureJournalError("committed quote is invalid") from error
                    if _canonical_json(quote.to_dict(), field_name="quote") != row["payload_json"]:
                        raise CaptureJournalError("committed quote is not canonical")
                    if quote.quality_state not in _ACCEPTED_STATES:
                        raise CaptureJournalError(
                            "committed accepted quote has invalid quality state"
                        )
                    count += 1
        return count

    def genesis_sha256(self) -> str:
        paths = self.shard_paths()
        if not paths:
            raise CaptureJournalError("capture journal has no durable entries")
        with self._connect(paths[0], readonly=True) as connection:
            row = connection.execute(
                "SELECT entry_sha256 FROM events ORDER BY sequence LIMIT 1"
            ).fetchone()
        if row is None:
            raise CaptureJournalError("capture journal has no durable entries")
        return _sha256(row["entry_sha256"], field_name="genesis_sha256")

    def verify(self) -> None:
        paths = self.shard_paths()
        expected_sequence = 1
        expected_previous = ZERO_SHA256
        previous_time: datetime | None = None
        for path in paths:
            shard_date = self._shard_date(path)
            with self._connect(path, readonly=True) as connection:
                meta = connection.execute("SELECT * FROM shard_meta WHERE singleton = 1").fetchone()
                if meta is None:
                    raise CaptureJournalError(f"shard metadata is missing: {path}")
                if int(meta["schema_version"]) != JOURNAL_SCHEMA_VERSION:
                    raise CaptureJournalError(f"unsupported journal schema: {path}")
                if meta["shard_date"] != shard_date:
                    raise CaptureJournalError(f"journal shard date mismatch: {path}")
                if int(meta["previous_sequence"]) != expected_sequence - 1:
                    raise CaptureJournalError(f"journal shard sequence boundary mismatch: {path}")
                if meta["previous_entry_sha256"] != expected_previous:
                    raise CaptureJournalError(f"journal shard hash boundary mismatch: {path}")
                rows = connection.execute("SELECT * FROM events ORDER BY sequence")
                for row in rows:
                    if int(row["sequence"]) != expected_sequence:
                        raise CaptureJournalError(
                            f"journal sequence gap: expected {expected_sequence}"
                        )
                    if row["previous_entry_sha256"] != expected_previous:
                        raise CaptureJournalError(
                            f"journal hash chain mismatch at sequence {expected_sequence}"
                        )
                    try:
                        state = JournalState(row["state"])
                        occurred_at = _aware_utc(
                            datetime.fromisoformat(row["occurred_at"]),
                            field_name="occurred_at",
                        )
                        metadata = json.loads(row["metadata_json"])
                    except (ValueError, TypeError, json.JSONDecodeError) as error:
                        raise CaptureJournalError(
                            f"journal event schema invalid at sequence {expected_sequence}"
                        ) from error
                    canonical_metadata = _canonical_json(metadata, field_name="metadata")
                    if canonical_metadata != row["metadata_json"]:
                        raise CaptureJournalError(
                            f"journal metadata is not canonical at sequence {expected_sequence}"
                        )
                    if previous_time is not None and occurred_at < previous_time:
                        raise CaptureJournalError("journal occurred_at moved backwards")
                    expected_hash = _event_hash(
                        sequence=expected_sequence,
                        capture_id=_sha256(row["capture_id"], field_name="capture_id"),
                        raw_sha256=_sha256(row["raw_sha256"], field_name="raw_sha256"),
                        state=state,
                        occurred_at=occurred_at.isoformat(),
                        previous_entry_sha256=expected_previous,
                        metadata_json=canonical_metadata,
                    )
                    if row["entry_sha256"] != expected_hash:
                        raise CaptureJournalError(
                            f"journal entry hash mismatch at sequence {expected_sequence}"
                        )
                    if state is JournalState.RAW_DURABLE:
                        raw_payload = bytes(row["raw_payload"])
                        if hashlib.sha256(raw_payload).hexdigest() != row["raw_sha256"]:
                            raise CaptureJournalError(
                                f"raw payload hash mismatch at sequence {expected_sequence}"
                            )
                    else:
                        if row["raw_payload"] is not None:
                            raise CaptureJournalError(
                                f"terminal event contains raw payload at sequence {expected_sequence}"
                            )
                        self._verify_terminal(connection, row, metadata)
                    expected_previous = expected_hash
                    previous_time = occurred_at
                    expected_sequence += 1

    def verify_tail(self) -> None:
        """Verify the active shard and publication tail without full replay."""

        paths = self.shard_paths()
        if not paths:
            raise CaptureJournalError("capture journal has no shards")
        path = paths[-1]
        with self._connect(path, readonly=True) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            if quick is None or quick[0] != "ok":
                raise CaptureJournalError(f"SQLite quick_check failed: {path}")
            rows = connection.execute(
                "SELECT * FROM events ORDER BY sequence DESC LIMIT 2"
            ).fetchall()
            if not rows:
                raise CaptureJournalError(f"active journal shard has no events: {path}")
            rows = list(reversed(rows))
            for index, row in enumerate(rows):
                try:
                    state = JournalState(row["state"])
                    occurred_at = _aware_utc(
                        datetime.fromisoformat(row["occurred_at"]),
                        field_name="occurred_at",
                    )
                    metadata = json.loads(row["metadata_json"])
                except (ValueError, TypeError, json.JSONDecodeError) as error:
                    raise CaptureJournalError("journal tail schema is invalid") from error
                metadata_json = _canonical_json(metadata, field_name="metadata")
                expected_hash = _event_hash(
                    sequence=int(row["sequence"]),
                    capture_id=_sha256(row["capture_id"], field_name="capture_id"),
                    raw_sha256=_sha256(row["raw_sha256"], field_name="raw_sha256"),
                    state=state,
                    occurred_at=occurred_at.isoformat(),
                    previous_entry_sha256=_sha256(
                        row["previous_entry_sha256"],
                        field_name="previous_entry_sha256",
                    ),
                    metadata_json=metadata_json,
                )
                if row["entry_sha256"] != expected_hash:
                    raise CaptureJournalError("journal tail entry hash mismatch")
                if index and row["previous_entry_sha256"] != rows[index - 1]["entry_sha256"]:
                    raise CaptureJournalError("journal tail hash chain mismatch")
                if state is JournalState.RAW_DURABLE:
                    if hashlib.sha256(bytes(row["raw_payload"])).hexdigest() != row["raw_sha256"]:
                        raise CaptureJournalError("journal tail raw hash mismatch")
                else:
                    self._verify_terminal(connection, row, metadata)

    def checkpoint(self) -> None:
        paths = self.shard_paths()
        for path in paths:
            self._checkpoint_path(path)

    def checkpoint_shard(self, shard_date: str) -> Path:
        """Checkpoint one existing UTC-day shard and return its canonical path."""

        path = self._path_for_date(shard_date)
        if path not in self.shard_paths():
            raise CaptureJournalError(f"journal shard does not exist: {path}")
        self._checkpoint_path(path)
        return path

    def _verify_terminal(
        self,
        connection: sqlite3.Connection,
        event: sqlite3.Row,
        metadata: Mapping[str, object],
    ) -> None:
        raw_date = str(metadata.get("raw_shard_date", ""))
        raw_sequence = metadata.get("raw_entry_sequence")
        raw_hash = metadata.get("raw_entry_sha256")
        if not raw_date or isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
            raise CaptureJournalError(f"terminal raw binding is invalid: {event['capture_id']}")
        raw_path = self._path_for_date(raw_date)
        if not raw_path.is_file():
            raise CaptureJournalError(f"terminal raw shard is missing: {raw_path}")
        with self._connect(raw_path, readonly=True) as raw_connection:
            raw = raw_connection.execute(
                """
                SELECT capture_id, raw_sha256, state, entry_sha256
                FROM events WHERE sequence = ?
                """,
                (raw_sequence,),
            ).fetchone()
        if (
            raw is None
            or raw["capture_id"] != event["capture_id"]
            or raw["raw_sha256"] != event["raw_sha256"]
            or raw["state"] != str(JournalState.RAW_DURABLE)
            or raw["entry_sha256"] != raw_hash
        ):
            raise CaptureJournalError(f"terminal raw binding changed: {event['capture_id']}")
        rows = connection.execute(
            """
            SELECT row_index, disposition, payload_json, payload_sha256
            FROM quote_rows WHERE capture_id = ? ORDER BY row_index
            """,
            (event["capture_id"],),
        ).fetchall()
        encoded: list[tuple[str, str]] = []
        for expected_index, row in enumerate(rows):
            if int(row["row_index"]) != expected_index:
                raise CaptureJournalError(f"capture row index gap: {event['capture_id']}")
            if row["payload_sha256"] != _row_hash(row["disposition"], row["payload_json"]):
                raise CaptureJournalError(f"capture row hash mismatch: {event['capture_id']}")
            encoded.append((str(row["disposition"]), str(row["payload_json"])))
        if metadata.get("rows_sha256") != _rows_hash(encoded):
            raise CaptureJournalError(f"capture rows hash mismatch: {event['capture_id']}")
        accepted = sum(disposition == "accepted" for disposition, _payload in encoded)
        if metadata.get("accepted_count") != accepted:
            raise CaptureJournalError(f"capture accepted count mismatch: {event['capture_id']}")
        if metadata.get("quarantined_count") != len(encoded) - accepted:
            raise CaptureJournalError(f"capture quarantine count mismatch: {event['capture_id']}")

    @staticmethod
    def _committed_rows(
        connection: sqlite3.Connection,
        commit: sqlite3.Row,
    ) -> list[CommittedQuote]:
        records: list[CommittedQuote] = []
        rows = connection.execute(
            """
            SELECT row_index, payload_json
            FROM quote_rows
            WHERE capture_id = ? AND disposition = 'accepted'
            ORDER BY row_index
            """,
            (commit["capture_id"],),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                quote = CollectedQuote.from_dict(payload)
            except (json.JSONDecodeError, QuoteContractError, TypeError, ValueError) as error:
                raise CaptureJournalError(
                    f"committed quote is invalid for {commit['capture_id']}"
                ) from error
            if _canonical_json(quote.to_dict(), field_name="quote") != row["payload_json"]:
                raise CaptureJournalError(
                    f"committed quote is not canonical for {commit['capture_id']}"
                )
            if quote.quality_state not in _ACCEPTED_STATES:
                raise CaptureJournalError(
                    f"committed accepted quote has invalid quality state: {commit['capture_id']}"
                )
            records.append(
                CommittedQuote(
                    commit_sequence=int(commit["sequence"]),
                    capture_id=str(commit["capture_id"]),
                    row_index=int(row["row_index"]),
                    quote=quote,
                )
            )
        return records

    def _insert_terminal(
        self,
        connection: sqlite3.Connection,
        capture: CaptureRef,
        *,
        state: JournalState,
        occurred_at: datetime,
        rows: Sequence[tuple[str, str, str | None, str | None, str | None, str | None]],
        metadata: Mapping[str, object],
    ) -> JournalEntry:
        sequence, previous_hash, previous_time = self._head(connection)
        if previous_time is not None and occurred_at < previous_time:
            raise CaptureJournalError("journal occurred_at moved backwards")
        encoded_pairs = [(row[0], row[1]) for row in rows]
        details = {
            **dict(metadata),
            "raw_entry_sequence": capture.raw_sequence,
            "raw_entry_sha256": capture.raw_entry_sha256,
            "raw_shard_date": capture.shard_date,
            "rows_sha256": _rows_hash(encoded_pairs),
        }
        metadata_json = _canonical_json(details, field_name="metadata")
        next_sequence = sequence + 1
        entry_hash = _event_hash(
            sequence=next_sequence,
            capture_id=capture.capture_id,
            raw_sha256=capture.raw_sha256,
            state=state,
            occurred_at=occurred_at.isoformat(),
            previous_entry_sha256=previous_hash,
            metadata_json=metadata_json,
        )
        for row_index, (
            disposition,
            payload_json,
            identity_sha,
            provider,
            instrument,
            event_time,
        ) in enumerate(rows):
            connection.execute(
                """
                INSERT INTO quote_rows (
                    capture_id, row_index, disposition, payload_json,
                    payload_sha256, identity_sha256, provider, instrument, event_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture.capture_id,
                    row_index,
                    disposition,
                    payload_json,
                    _row_hash(disposition, payload_json),
                    identity_sha,
                    provider,
                    instrument,
                    event_time,
                ),
            )
        connection.execute(
            """
            INSERT INTO events (
                sequence, capture_id, raw_sha256, state, occurred_at,
                previous_entry_sha256, metadata_json, entry_sha256, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                next_sequence,
                capture.capture_id,
                capture.raw_sha256,
                str(state),
                occurred_at.isoformat(),
                previous_hash,
                metadata_json,
                entry_hash,
            ),
        )
        return JournalEntry(
            sequence=next_sequence,
            capture_id=capture.capture_id,
            raw_sha256=capture.raw_sha256,
            state=state,
            occurred_at=occurred_at,
            previous_entry_sha256=previous_hash,
            metadata=json.loads(metadata_json),
            entry_sha256=entry_hash,
            shard_date=self._shard_date(self._writable_shard(occurred_at)),
        )

    def _assert_unfinalized(self, connection: sqlite3.Connection, capture: CaptureRef) -> None:
        paths = self.shard_paths()
        # The normal streaming path begins and terminates in the active shard;
        # avoid opening every historical day for every provider message. A
        # capture crossing UTC rotation or restart takes the slower full scan.
        candidates = (
            (paths[-1],) if paths and capture.shard_date == self._shard_date(paths[-1]) else paths
        )
        for path in candidates:
            query = """
                SELECT state FROM events
                WHERE capture_id = ? AND state != ?
                LIMIT 1
            """
            parameters = (capture.capture_id, str(JournalState.RAW_DURABLE))
            if paths and path == paths[-1]:
                found = connection.execute(query, parameters).fetchone()
            else:
                with self._connect(path, readonly=True) as candidate:
                    found = candidate.execute(query, parameters).fetchone()
            if found is not None:
                raise CaptureJournalError(f"capture is already terminal: {capture.capture_id}")
        raw = self.read_raw(capture)
        if hashlib.sha256(raw).hexdigest() != capture.raw_sha256:
            raise CaptureJournalError(f"raw capture verification failed: {capture.capture_id}")

    def _terminal_exists(self, capture_id: str) -> bool:
        for path in reversed(self.shard_paths()):
            with self._connect(path, readonly=True) as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM events
                    WHERE capture_id = ? AND state != ?
                    LIMIT 1
                    """,
                    (capture_id, str(JournalState.RAW_DURABLE)),
                ).fetchone()
            if row is not None:
                return True
        return False

    def _writable_shard(self, occurred_at: datetime) -> Path:
        date = _aware_utc(occurred_at, field_name="occurred_at").date().isoformat()
        paths = self.shard_paths()
        if paths:
            latest_date = self._shard_date(paths[-1])
            if date < latest_date:
                raise CaptureJournalError(
                    f"journal shard date moved backwards: {date} < {latest_date}"
                )
            if date > latest_date:
                self._checkpoint_path(paths[-1])
        path = self._path_for_date(date)
        if not path.exists():
            self._initialize_shard(path, date)
        return path

    def _initialize_shard(self, path: Path, shard_date: str) -> None:
        previous_sequence = 0
        previous_hash = ZERO_SHA256
        previous_state: list[sqlite3.Row] = []
        paths = self.shard_paths()
        if paths:
            previous = paths[-1]
            with self._connect(previous, readonly=True) as connection:
                row = connection.execute(
                    "SELECT sequence, entry_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    previous_sequence = int(row["sequence"])
                    previous_hash = str(row["entry_sha256"])
                previous_state = connection.execute(
                    "SELECT * FROM stream_state ORDER BY provider, instrument"
                ).fetchall()
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        with self._connect(path, initialize=False) as connection:
            self._create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO shard_meta (
                    singleton, schema_version, shard_date, previous_sequence,
                    previous_entry_sha256, created_at
                ) VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    JOURNAL_SCHEMA_VERSION,
                    shard_date,
                    previous_sequence,
                    previous_hash,
                    datetime.now(UTC).isoformat(),
                ),
            )
            for row in previous_state:
                connection.execute(
                    """
                    INSERT INTO stream_state (
                        provider, instrument, last_event_time, last_identity_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["provider"],
                        row["instrument"],
                        row["last_event_time"],
                        row["last_identity_sha256"],
                    ),
                )
            connection.commit()
        if not existed:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE shard_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                shard_date TEXT NOT NULL,
                previous_sequence INTEGER NOT NULL,
                previous_entry_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                capture_id TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('raw_durable', 'committed', 'quarantined', 'unavailable')
                ),
                occurred_at TEXT NOT NULL,
                previous_entry_sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                entry_sha256 TEXT NOT NULL UNIQUE,
                raw_payload BLOB
            );
            CREATE INDEX events_capture_idx ON events(capture_id);
            CREATE TABLE quote_rows (
                capture_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('accepted', 'quarantined')
                ),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                identity_sha256 TEXT,
                provider TEXT,
                instrument TEXT,
                event_time TEXT,
                PRIMARY KEY (capture_id, row_index)
            );
            CREATE INDEX quote_rows_identity_idx
                ON quote_rows(identity_sha256)
                WHERE disposition = 'accepted';
            CREATE TABLE stream_state (
                provider TEXT NOT NULL,
                instrument TEXT NOT NULL,
                last_event_time TEXT NOT NULL,
                last_identity_sha256 TEXT NOT NULL,
                PRIMARY KEY (provider, instrument)
            );
            CREATE TRIGGER shard_meta_no_update
            BEFORE UPDATE ON shard_meta BEGIN
                SELECT RAISE(ABORT, 'append-only shard metadata');
            END;
            CREATE TRIGGER shard_meta_no_delete
            BEFORE DELETE ON shard_meta BEGIN
                SELECT RAISE(ABORT, 'append-only shard metadata');
            END;
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events BEGIN
                SELECT RAISE(ABORT, 'append-only events');
            END;
            CREATE TRIGGER events_no_delete
            BEFORE DELETE ON events BEGIN
                SELECT RAISE(ABORT, 'append-only events');
            END;
            CREATE TRIGGER quote_rows_no_update
            BEFORE UPDATE ON quote_rows BEGIN
                SELECT RAISE(ABORT, 'append-only quote rows');
            END;
            CREATE TRIGGER quote_rows_no_delete
            BEFORE DELETE ON quote_rows BEGIN
                SELECT RAISE(ABORT, 'append-only quote rows');
            END;
            """)

    @contextmanager
    def _connect(
        self,
        path: Path,
        *,
        readonly: bool = False,
        initialize: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        try:
            if readonly:
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
            else:
                connection = sqlite3.connect(path, timeout=30.0)
        except sqlite3.Error as error:
            raise CaptureJournalError(f"cannot open journal shard {path}: {error}") from error
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            if not readonly:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA wal_autocheckpoint=1000")
            if initialize and not readonly:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
                ).fetchone()
                if exists is None:
                    raise CaptureJournalError(f"journal shard is uninitialized: {path}")
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise CaptureJournalError(f"SQLite journal failure for {path}: {error}") from error
        finally:
            connection.close()

    @staticmethod
    def _head(
        connection: sqlite3.Connection,
    ) -> tuple[int, str, datetime | None]:
        row = connection.execute(
            "SELECT sequence, entry_sha256, occurred_at FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return (
                int(row["sequence"]),
                str(row["entry_sha256"]),
                datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC),
            )
        meta = connection.execute(
            "SELECT previous_sequence, previous_entry_sha256 FROM shard_meta WHERE singleton = 1"
        ).fetchone()
        if meta is None:
            raise CaptureJournalError("journal shard metadata is missing")
        return int(meta["previous_sequence"]), str(meta["previous_entry_sha256"]), None

    @staticmethod
    def _entry_from_row(row: sqlite3.Row, shard_date: str) -> JournalEntry:
        return JournalEntry(
            sequence=int(row["sequence"]),
            capture_id=str(row["capture_id"]),
            raw_sha256=str(row["raw_sha256"]),
            state=JournalState(row["state"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC),
            previous_entry_sha256=str(row["previous_entry_sha256"]),
            metadata=json.loads(row["metadata_json"]),
            entry_sha256=str(row["entry_sha256"]),
            shard_date=shard_date,
        )

    def _path_for_date(self, shard_date: str) -> Path:
        return self.root / f"capture-{shard_date}.sqlite3"

    @staticmethod
    def _shard_date(path: Path) -> str:
        return path.name.removeprefix("capture-").removesuffix(".sqlite3")

    def _checkpoint_path(self, path: Path) -> None:
        with self._connect(path) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or int(result[0]) != 0:
                raise CaptureJournalError(f"journal WAL checkpoint is busy: {path}")

    def _assert_shard_capacity(self, path: Path, *, incoming_bytes: int) -> None:
        companions = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        observed = sum(candidate.stat().st_size for candidate in companions if candidate.exists())
        # Reserve a conservative page allowance for the raw event and indexes.
        projected = observed + incoming_bytes + 64 * 1024
        if projected > self.max_shard_bytes:
            raise CaptureJournalError(
                "active journal shard capacity exceeded: "
                f"projected={projected}, max={self.max_shard_bytes}"
            )


class CommittedCaptureReader:
    """Compatibility-named reader for the canonical journal boundary."""

    def __init__(self, journal_root: str | Path) -> None:
        self.journal = CaptureJournal(journal_root)

    def read_quotes(
        self,
        *,
        start_sequence: int = 0,
        expected_entry_sha256: str | None = None,
    ) -> tuple[CommittedQuote, ...]:
        return self.journal.read_quotes(
            start_sequence=start_sequence,
            expected_entry_sha256=expected_entry_sha256,
        )
