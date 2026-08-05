"""Incremental, read-only index over the append-only historical quote tape.

The JSONL file remains the canonical immutable input.  This module stores only
byte ranges and point-in-time lookup metadata in SQLite so consumers can seek
to relevant rows without rescanning the complete file every five minutes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from data_platform.collect.contract import CollectedQuote, QualityState, QuoteContractError

QUOTE_INDEX_CONTRACT = "fx-quote-tape-offset-index-v1"
QUOTE_INDEX_SCHEMA_VERSION = 1
DEFAULT_MAX_INDEX_AGE = timedelta(minutes=15)


class QuoteIndexError(RuntimeError):
    """The index cannot safely represent the canonical quote tape."""


@dataclass(frozen=True)
class IndexedQuotePayloads:
    payloads: tuple[Mapping[str, Any], ...]
    watermark: Mapping[str, datetime]
    source_path: str
    indexed_size_bytes: int
    source_size_bytes: int
    index_lag_bytes: int
    rejected_rows: int
    index_path: str
    indexed_at: datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS source_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_path TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    source_size_at_sync INTEGER NOT NULL CHECK (source_size_at_sync >= byte_offset),
    last_line_start_offset INTEGER NOT NULL CHECK (last_line_start_offset >= 0),
    last_line_end_offset INTEGER NOT NULL CHECK (last_line_end_offset >= last_line_start_offset),
    last_line_sha256 TEXT NOT NULL,
    complete_lines INTEGER NOT NULL CHECK (complete_lines >= 0),
    rejected_lines INTEGER NOT NULL CHECK (rejected_lines >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_offsets (
    start_offset INTEGER PRIMARY KEY CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    instrument TEXT NOT NULL,
    event_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_line_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quote_offsets_window
    ON quote_offsets(instrument, event_time, received_at);
CREATE INDEX IF NOT EXISTS quote_offsets_watermark
    ON quote_offsets(instrument, received_at, event_time);

CREATE TABLE IF NOT EXISTS rejected_offsets (
    start_offset INTEGER PRIMARY KEY CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    instrument TEXT,
    reason TEXT NOT NULL,
    raw_line_sha256 TEXT NOT NULL
);
"""


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QuoteIndexError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware(value, "timestamp").isoformat()


def _parse_timestamp(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise QuoteIndexError(f"{name} is not a valid ISO timestamp") from error
    return _aware(parsed, name)


def _normalize_symbol(value: object) -> str:
    return str(value or "").upper().replace("/", "").replace("_", "")


def _connect_writer(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_SCHEMA)
    connection.execute(f"PRAGMA user_version={QUOTE_INDEX_SCHEMA_VERSION}")
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('contract', ?)",
        (QUOTE_INDEX_CONTRACT,),
    )
    contract = connection.execute("SELECT value FROM metadata WHERE key = 'contract'").fetchone()
    if contract is None or str(contract["value"]) != QUOTE_INDEX_CONTRACT:
        connection.close()
        raise QuoteIndexError("quote index contract mismatch")
    return connection


def _connect_reader(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise QuoteIndexError(f"quote index does not exist: {database}")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != QUOTE_INDEX_SCHEMA_VERSION:
        connection.close()
        raise QuoteIndexError(f"unsupported quote index schema: {version}")
    contract = connection.execute("SELECT value FROM metadata WHERE key = 'contract'").fetchone()
    if contract is None or str(contract["value"]) != QUOTE_INDEX_CONTRACT:
        connection.close()
        raise QuoteIndexError("quote index contract mismatch")
    return connection


def _source_state(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("""
        SELECT source_path, device_id, inode, byte_offset, source_size_at_sync,
               last_line_start_offset, last_line_end_offset, last_line_sha256,
               complete_lines, rejected_lines, updated_at
        FROM source_state
        WHERE singleton = 1
        """).fetchone()


def _verify_source_identity(source: Path, state: sqlite3.Row) -> None:
    stat = source.stat()
    expected = (int(state["device_id"]), int(state["inode"]))
    observed = (int(stat.st_dev), int(stat.st_ino))
    if observed != expected:
        raise QuoteIndexError(f"quote source identity changed: {expected} != {observed}")
    byte_offset = int(state["byte_offset"])
    if stat.st_size < byte_offset:
        raise QuoteIndexError(
            f"quote source was truncated: {stat.st_size} < indexed offset {byte_offset}"
        )
    line_end = int(state["last_line_end_offset"])
    if line_end == 0:
        return
    line_start = int(state["last_line_start_offset"])
    with source.open("rb") as handle:
        handle.seek(line_start)
        raw = handle.read(line_end - line_start)
    observed_sha = hashlib.sha256(raw).hexdigest()
    if observed_sha != str(state["last_line_sha256"]):
        raise QuoteIndexError("indexed quote source prefix was rewritten")


def _classify_line(
    raw: bytes,
) -> tuple[tuple[str, str, str] | None, str | None, str | None, str]:
    raw_sha = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, "malformed_json", raw_sha
    if not isinstance(payload, Mapping):
        return None, None, "non_object_json", raw_sha
    instrument = _normalize_symbol(payload.get("instrument")) or None
    try:
        quote = CollectedQuote.from_dict(payload)
    except (KeyError, TypeError, ValueError, QuoteContractError):
        return None, instrument, "quote_contract_invalid", raw_sha
    if (
        quote.provider != "dukascopy"
        or quote.account_environment != "datafeed"
        or quote.source_endpoint_class != "historical_datafeed"
        or quote.collection_mode != "historical_download"
        or quote.quality_state is not QualityState.USABLE
        or quote.provider_event_time is None
        or quote.provider_event_time > quote.received_at
    ):
        return None, instrument, "replay_contract_ineligible", raw_sha
    return (
        (
            _normalize_symbol(quote.instrument),
            quote.provider_event_time.isoformat(),
            quote.received_at.isoformat(),
        ),
        instrument,
        None,
        raw_sha,
    )


def sync_quote_index(
    source_path: str | Path,
    database_path: str | Path,
    *,
    now: datetime | None = None,
    batch_size: int = 10_000,
) -> dict[str, object]:
    """Index only complete bytes appended since the prior committed checkpoint."""

    if batch_size <= 0:
        raise QuoteIndexError("batch_size must be positive")
    source = Path(source_path).expanduser().resolve()
    database = Path(database_path).expanduser().resolve()
    if not source.is_file():
        raise QuoteIndexError(f"quote source does not exist: {source}")
    observed_at = _aware(now or datetime.now(UTC), "sync time")
    source_stat = source.stat()
    connection = _connect_writer(database)
    try:
        state = _source_state(connection)
        if state is None:
            with connection:
                connection.execute(
                    """
                    INSERT INTO source_state(
                        singleton, source_path, device_id, inode, byte_offset,
                        source_size_at_sync, last_line_start_offset,
                        last_line_end_offset, last_line_sha256, complete_lines,
                        rejected_lines, updated_at
                    ) VALUES (1, ?, ?, ?, 0, ?, 0, 0, '', 0, 0, ?)
                    """,
                    (
                        str(source),
                        int(source_stat.st_dev),
                        int(source_stat.st_ino),
                        int(source_stat.st_size),
                        observed_at.isoformat(),
                    ),
                )
            state = _source_state(connection)
            assert state is not None
        elif str(state["source_path"]) != str(source):
            raise QuoteIndexError(f"quote source path changed: {state['source_path']} != {source}")
        _verify_source_identity(source, state)

        start_offset = int(state["byte_offset"])
        offset = start_offset
        complete_lines = int(state["complete_lines"])
        rejected_lines = int(state["rejected_lines"])
        inserted = rejected = batches = 0
        last_line_start = int(state["last_line_start_offset"])
        last_line_end = int(state["last_line_end_offset"])
        last_line_sha = str(state["last_line_sha256"])
        snapshot_size = source.stat().st_size

        with source.open("rb") as handle:
            handle.seek(offset)
            while offset < snapshot_size:
                rows: list[tuple[int, int, str, str, str, str]] = []
                failures: list[tuple[int, int, str | None, str, str]] = []
                batch_last_start = last_line_start
                batch_last_end = last_line_end
                batch_last_sha = last_line_sha
                for _ in range(batch_size):
                    line_start = handle.tell()
                    if line_start >= snapshot_size:
                        break
                    raw = handle.readline(snapshot_size - line_start)
                    if not raw.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    line_end = handle.tell()
                    classified, instrument, reason, raw_sha = _classify_line(raw)
                    if classified is None:
                        failures.append(
                            (
                                line_start,
                                line_end,
                                instrument,
                                reason or "unknown_rejection",
                                raw_sha,
                            )
                        )
                    else:
                        symbol, event_time, received_at = classified
                        rows.append(
                            (
                                line_start,
                                line_end,
                                symbol,
                                event_time,
                                received_at,
                                raw_sha,
                            )
                        )
                    batch_last_start = line_start
                    batch_last_end = line_end
                    batch_last_sha = raw_sha
                new_offset = handle.tell()
                if new_offset == offset:
                    break
                with connection:
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO quote_offsets(
                            start_offset, end_offset, instrument, event_time,
                            received_at, raw_line_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO rejected_offsets(
                            start_offset, end_offset, instrument, reason,
                            raw_line_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        failures,
                    )
                    complete_lines += len(rows) + len(failures)
                    rejected_lines += len(failures)
                    connection.execute(
                        """
                        UPDATE source_state
                        SET byte_offset = ?, source_size_at_sync = ?,
                            last_line_start_offset = ?, last_line_end_offset = ?,
                            last_line_sha256 = ?, complete_lines = ?,
                            rejected_lines = ?, updated_at = ?
                        WHERE singleton = 1
                        """,
                        (
                            new_offset,
                            snapshot_size,
                            batch_last_start,
                            batch_last_end,
                            batch_last_sha,
                            complete_lines,
                            rejected_lines,
                            observed_at.isoformat(),
                        ),
                    )
                inserted += len(rows)
                rejected += len(failures)
                batches += 1
                offset = new_offset
                last_line_start = batch_last_start
                last_line_end = batch_last_end
                last_line_sha = batch_last_sha

        if offset == start_offset:
            with connection:
                connection.execute(
                    """
                    UPDATE source_state
                    SET source_size_at_sync = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (snapshot_size, observed_at.isoformat()),
                )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "ok": integrity == "ok",
            "contract": QUOTE_INDEX_CONTRACT,
            "source_path": str(source),
            "database_path": str(database),
            "started_offset": start_offset,
            "indexed_offset": offset,
            "source_size_bytes": snapshot_size,
            "partial_tail_bytes": snapshot_size - offset,
            "scanned_bytes": offset - start_offset,
            "indexed_rows": inserted,
            "rejected_rows": rejected,
            "batches": batches,
            "updated_at": observed_at.isoformat(),
            "integrity_check": integrity,
        }
    finally:
        connection.close()


def load_indexed_quote_payloads(
    source_path: str | Path,
    database_path: str | Path,
    *,
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
    as_of: datetime,
    max_index_age: timedelta = DEFAULT_MAX_INDEX_AGE,
) -> IndexedQuotePayloads:
    """Read verified raw rows selected by the SQLite byte-offset index."""

    source = Path(source_path).expanduser().resolve()
    database = Path(database_path).expanduser().resolve()
    wanted = {_normalize_symbol(symbol) for symbol in symbols}
    wanted.discard("")
    if not wanted:
        raise QuoteIndexError("at least one quote symbol is required")
    start_utc = _aware(start, "query start")
    end_utc = _aware(end, "query end")
    as_of_utc = _aware(as_of, "query as_of")
    if end_utc < start_utc:
        raise QuoteIndexError("quote index query window is reversed")
    if max_index_age.total_seconds() <= 0:
        raise QuoteIndexError("max_index_age must be positive")

    connection = _connect_reader(database)
    try:
        state = _source_state(connection)
        if state is None:
            raise QuoteIndexError("quote index has not been initialized")
        if str(state["source_path"]) != str(source):
            raise QuoteIndexError("quote index source path mismatch")
        _verify_source_identity(source, state)
        indexed_at = _parse_timestamp(state["updated_at"], "index updated_at")
        if as_of_utc - indexed_at > max_index_age:
            raise QuoteIndexError(
                f"quote index is stale by {(as_of_utc - indexed_at).total_seconds():.1f}s"
            )
        indexed_size = int(state["byte_offset"])
        source_size = source.stat().st_size
        if source_size != indexed_size:
            raise QuoteIndexError(
                f"quote index is behind source by {source_size - indexed_size} bytes"
            )
        placeholders = ",".join("?" for _ in sorted(wanted))
        parameters: list[object] = [
            *sorted(wanted),
            _timestamp(start_utc),
            _timestamp(end_utc),
            _timestamp(as_of_utc),
        ]
        rows = connection.execute(
            f"""
            SELECT start_offset, end_offset, raw_line_sha256
            FROM quote_offsets
            WHERE instrument IN ({placeholders})
              AND event_time >= ?
              AND event_time <= ?
              AND received_at <= ?
            ORDER BY start_offset
            """,
            parameters,
        ).fetchall()
        watermark_rows = connection.execute(
            f"""
            SELECT instrument, MAX(event_time) AS event_time
            FROM quote_offsets
            WHERE instrument IN ({placeholders})
              AND received_at <= ?
            GROUP BY instrument
            """,
            [*sorted(wanted), _timestamp(as_of_utc)],
        ).fetchall()
        rejected = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM rejected_offsets
                WHERE instrument IS NULL OR instrument IN ({placeholders})
                """,
                sorted(wanted),
            ).fetchone()[0]
        )
        payloads: list[Mapping[str, Any]] = []
        with source.open("rb") as handle:
            for row in rows:
                line_start = int(row["start_offset"])
                line_end = int(row["end_offset"])
                handle.seek(line_start)
                raw = handle.read(line_end - line_start)
                if hashlib.sha256(raw).hexdigest() != str(row["raw_line_sha256"]):
                    raise QuoteIndexError("indexed quote row hash mismatch")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise QuoteIndexError("indexed quote row is no longer valid JSON") from error
                if not isinstance(payload, Mapping):
                    raise QuoteIndexError("indexed quote row is no longer an object")
                payloads.append(payload)
        watermark = {
            str(row["instrument"]): _parse_timestamp(row["event_time"], "quote watermark")
            for row in watermark_rows
            if row["event_time"] is not None
        }
        return IndexedQuotePayloads(
            payloads=tuple(payloads),
            watermark=watermark,
            source_path=str(source),
            indexed_size_bytes=indexed_size,
            source_size_bytes=source_size,
            index_lag_bytes=max(0, source_size - indexed_size),
            rejected_rows=rejected,
            index_path=str(database),
            indexed_at=indexed_at,
        )
    finally:
        connection.close()
