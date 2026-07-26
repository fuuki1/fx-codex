"""Materialize committed bid/ask journal rows into a transactional shadow store.

Only hash-bound ``committed`` captures are visible. Canonical snapshot rows and
the first unconsumed ``(commit_sequence, row_index)`` are committed together in
one SQLite WAL transaction. A crash therefore exposes either both the rows and
their checkpoint or neither. The legacy accepted JSONL and its byte offsets are
never read here.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
from typing import Any

from data_platform.collect.capture_journal import (
    CaptureJournalError,
    CommittedCaptureReader,
    CommittedQuote,
    JournalState,
)
from data_platform.collect.contract import CollectedQuote
from data_platform.contracts.market_quote import MarketQuote
from data_platform.materialize.bid_ask_bars import BAR_INTERVALS, BidAskBar, materialize_bars
from data_platform.quality.state import QualityState
from fx_intel.universe import normalize_symbol

BRIDGE_STATE_SCHEMA = 3
SNAPSHOT_SCHEMA_VERSION = 3
SNAPSHOT_STORE_SCHEMA_VERSION = 1
DEFAULT_OUTPUT_TIMEFRAMES = ("15m", "1h", "4h", "1d")
_REPLAY_ROW_BITS = 32


class BidAskBridgeError(RuntimeError):
    """The bridge cannot advance without violating replay or PIT integrity."""


@dataclass(frozen=True, order=True)
class ReplayPosition:
    """Stable inclusive replay cursor in ledger commit order."""

    commit_sequence: int
    row_index: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("commit_sequence", self.commit_sequence),
            ("row_index", self.row_index),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BidAskBridgeError(f"{field_name} must be a non-negative integer")

    @classmethod
    def origin(cls) -> ReplayPosition:
        return cls(commit_sequence=0, row_index=0)

    @classmethod
    def from_dict(cls, value: object) -> ReplayPosition:
        if not isinstance(value, dict) or set(value) != {"commit_sequence", "row_index"}:
            raise BidAskBridgeError("bridge state next_position has an invalid schema")
        return cls(
            commit_sequence=value["commit_sequence"],
            row_index=value["row_index"],
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "commit_sequence": self.commit_sequence,
            "row_index": self.row_index,
        }


@dataclass(frozen=True)
class BridgeResult:
    input_rows: int
    completed_bars: int
    output_rows: int
    appended_rows: int
    start_position: ReplayPosition
    next_position: ReplayPosition
    last_materialized_close: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "completed_bars": self.completed_bars,
            "output_rows": self.output_rows,
            "appended_rows": self.appended_rows,
            "start_position": self.start_position.to_dict(),
            "next_position": self.next_position.to_dict(),
            "last_materialized_close": self.last_materialized_close,
        }


@dataclass(frozen=True)
class _LocatedQuote:
    position: ReplayPosition
    capture_id: str
    commit_entry_sha256: str
    collected: CollectedQuote
    market: MarketQuote


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise BidAskBridgeError("canonical snapshot contains non-JSON data") from error


def _create_store_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(f"""
        CREATE TABLE bridge_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            next_commit_sequence INTEGER NOT NULL CHECK (next_commit_sequence >= 0),
            next_row_index INTEGER NOT NULL CHECK (next_row_index >= 0),
            checkpoint_commit_entry_sha256 TEXT,
            last_materialized_close TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE snapshots (
            event_time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            capture_slot TEXT NOT NULL,
            available_time TEXT NOT NULL,
            ingested_time TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (event_time, symbol, timeframe)
        );
        CREATE INDEX snapshots_capture_slot_idx
            ON snapshots(capture_slot, symbol, timeframe);
        CREATE TRIGGER snapshots_no_update
        BEFORE UPDATE ON snapshots BEGIN
            SELECT RAISE(ABORT, 'append-only canonical snapshots');
        END;
        CREATE TRIGGER snapshots_no_delete
        BEFORE DELETE ON snapshots BEGIN
            SELECT RAISE(ABORT, 'append-only canonical snapshots');
        END;
        PRAGMA user_version={SNAPSHOT_STORE_SCHEMA_VERSION};
        """)


_REQUIRED_STORE_OBJECTS = {
    ("table", "bridge_state"),
    ("table", "snapshots"),
    ("index", "snapshots_capture_slot_idx"),
    ("trigger", "snapshots_no_update"),
    ("trigger", "snapshots_no_delete"),
}


def _validate_store_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SNAPSHOT_STORE_SCHEMA_VERSION:
        raise BidAskBridgeError("canonical snapshot store schema version is invalid")
    observed = {
        (str(row["type"]), str(row["name"]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name IN ('bridge_state', 'snapshots', 'snapshots_capture_slot_idx', "
            "'snapshots_no_update', 'snapshots_no_delete')"
        )
    }
    missing = sorted(_REQUIRED_STORE_OBJECTS - observed)
    if missing:
        raise BidAskBridgeError(f"canonical snapshot store schema objects are missing: {missing}")
    integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if integrity != "ok":
        raise BidAskBridgeError("canonical snapshot store quick_check failed")


@contextmanager
def _connect_store(
    path: Path,
    *,
    readonly: bool,
    create: bool = False,
) -> Iterator[sqlite3.Connection]:
    if readonly and not path.is_file():
        raise BidAskBridgeError(f"canonical snapshot store is missing: {path}")
    if path.is_symlink():
        raise BidAskBridgeError(f"canonical snapshot store cannot be a symlink: {path}")
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    try:
        connection = (
            sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
            if readonly
            else sqlite3.connect(path, timeout=30.0)
        )
    except sqlite3.Error as error:
        raise BidAskBridgeError(f"cannot open canonical snapshot store: {error}") from error
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if not readonly:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA wal_autocheckpoint=1000")
        initialized = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'"
        ).fetchone()
        if initialized is None:
            if readonly or existed or not create:
                raise BidAskBridgeError("canonical snapshot store is uninitialized")
            _create_store_schema(connection)
            connection.commit()
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _validate_store_schema(connection)
        yield connection
    except sqlite3.Error as error:
        raise BidAskBridgeError(f"canonical snapshot SQLite failure: {error}") from error
    finally:
        connection.close()


def _state_from_connection(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM bridge_state WHERE singleton = 1").fetchone()
    if row is None:
        return None
    try:
        config = json.loads(str(row["config_json"]))
    except json.JSONDecodeError as error:
        raise BidAskBridgeError("canonical snapshot bridge config is malformed") from error
    if not isinstance(config, dict):
        raise BidAskBridgeError("canonical snapshot bridge config is invalid")
    value = {
        "schema_version": int(row["schema_version"]),
        **config,
        "next_position": {
            "commit_sequence": int(row["next_commit_sequence"]),
            "row_index": int(row["next_row_index"]),
        },
        "checkpoint_commit_entry_sha256": row["checkpoint_commit_entry_sha256"],
        "last_materialized_close": row["last_materialized_close"],
        "updated_at": str(row["updated_at"]),
    }
    if value["schema_version"] != BRIDGE_STATE_SCHEMA:
        raise BidAskBridgeError("canonical snapshot bridge state schema is invalid")
    return value


def _load_store_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with _connect_store(path, readonly=True) as connection:
        return _state_from_connection(connection)


def _validate_configuration(
    state: dict[str, Any] | None,
    *,
    ingest_log_directory: Path,
    journal_root: Path,
    journal_genesis_sha256: str,
    commit_entry_hashes: Mapping[int, str],
    records: Sequence[CommittedQuote],
    instruments: tuple[str, ...],
    timeframes: tuple[str, ...],
    output_path: Path,
) -> ReplayPosition:
    if state is None:
        return ReplayPosition.origin()
    expected = {
        "ingest_log_directory": str(ingest_log_directory.resolve()),
        "journal_root": str(journal_root.resolve()),
        "journal_genesis_sha256": journal_genesis_sha256,
        "instruments": list(instruments),
        "timeframes": list(timeframes),
        "output_path": str(output_path.resolve()),
    }
    mismatches = [key for key, value in expected.items() if state.get(key) != value]
    if mismatches:
        raise BidAskBridgeError(
            "bridge source/config changed; explicit audited checkpoint migration required: "
            + ", ".join(mismatches)
        )
    position = ReplayPosition.from_dict(state.get("next_position"))
    if position == ReplayPosition.origin():
        if state.get("checkpoint_commit_entry_sha256") is not None:
            raise BidAskBridgeError("origin checkpoint must not bind a commit entry hash")
        return position
    capture_records = [
        record for record in records if record.commit_sequence == position.commit_sequence
    ]
    if not capture_records or position.row_index > len(capture_records):
        raise BidAskBridgeError(
            "bridge checkpoint is not present in committed journal history; "
            "explicit audited checkpoint migration required"
        )
    expected_commit_hash = commit_entry_hashes.get(position.commit_sequence)
    if (
        expected_commit_hash is None
        or state.get("checkpoint_commit_entry_sha256") != expected_commit_hash
    ):
        raise BidAskBridgeError(
            "bridge checkpoint commit hash changed; explicit audited checkpoint migration required"
        )
    return position


def _checkpoint_commit_hash(
    position: ReplayPosition, commit_entry_hashes: Mapping[int, str]
) -> str | None:
    if position == ReplayPosition.origin():
        return None
    digest = commit_entry_hashes.get(position.commit_sequence)
    if digest is None:
        raise BidAskBridgeError("next replay position has no committed journal entry")
    return digest


def _locate_quote(
    record: CommittedQuote,
    commit_entry_hashes: Mapping[int, str],
) -> _LocatedQuote:
    if record.row_index >= 1 << _REPLAY_ROW_BITS:
        raise BidAskBridgeError("committed capture exceeds durable replay row-index capacity")
    fallback_sequence = (record.commit_sequence << _REPLAY_ROW_BITS) | record.row_index
    try:
        market = record.quote.to_market_quote(fallback_sequence_id=fallback_sequence)
    except ValueError as error:
        raise BidAskBridgeError(
            f"cannot bridge quote at {record.replay_position}: {error}"
        ) from error
    return _LocatedQuote(
        position=ReplayPosition(record.commit_sequence, record.row_index),
        capture_id=record.capture_id,
        commit_entry_sha256=commit_entry_hashes[record.commit_sequence],
        collected=record.quote,
        market=market,
    )


def _bucket_close(timestamp: datetime) -> datetime:
    span = BAR_INTERVALS["1m"]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (timestamp.astimezone(UTC) - epoch) // span
    return epoch + (elapsed + 1) * span


def _symbol(instrument: str) -> str:
    return normalize_symbol(instrument)


def _bar_snapshot(bar: BidAskBar, located: Sequence[_LocatedQuote]) -> dict[str, object]:
    matching = [item for item in located if item.market.instrument == bar.instrument]
    window = [
        item for item in matching if bar.open_time <= item.market.source_timestamp < bar.close_time
    ]
    flags = ["provider_sampling_cadence_unmeasured"]
    if bar.quote_count == 1:
        flags.append("single_quote_bar")
    if any(item.collected.quality_state is QualityState.DEGRADED for item in window):
        flags.append("contains_degraded_quote")
    for item in window:
        flags.extend(item.collected.quality_flags)
    providers = sorted({item.collected.provider for item in window})
    environments = sorted({item.collected.account_environment for item in window})
    collection_modes = sorted({item.collected.collection_mode for item in window})
    endpoint_classes = sorted({item.collected.source_endpoint_class for item in window})
    quality_states = sorted({str(item.collected.quality_state) for item in window})
    if providers != ["oanda"]:
        raise BidAskBridgeError("canonical bridge accepts only provider=oanda")
    if collection_modes != ["live_stream"]:
        raise BidAskBridgeError("canonical bridge accepts only collection_mode=live_stream")
    if endpoint_classes != ["streaming_pricing"]:
        raise BidAskBridgeError(
            "canonical bridge accepts only source_endpoint_class=streaming_pricing"
        )
    lineage = [
        {
            "commit_sequence": item.position.commit_sequence,
            "row_index": item.position.row_index,
            "capture_id": item.capture_id,
            "raw_payload_sha256": item.collected.raw_payload_sha256,
            "commit_entry_sha256": item.commit_entry_sha256,
        }
        for item in window
    ]
    lineage_sha256 = hashlib.sha256(_canonical_json(lineage).encode("utf-8")).hexdigest()

    def single_or_mixed(values: list[str]) -> str:
        return values[0] if len(values) == 1 else "mixed"

    return {
        "open": bar.mid_open,
        "high": bar.mid_high,
        "low": bar.mid_low,
        "close": bar.mid_close,
        "bid": bar.bid_close,
        "ask": bar.ask_close,
        "spread": bar.ask_close - bar.bid_close,
        "bid_open": bar.bid_open,
        "bid_high": bar.bid_high,
        "bid_low": bar.bid_low,
        "ask_open": bar.ask_open,
        "ask_high": bar.ask_high,
        "ask_low": bar.ask_low,
        "spread_mean": bar.spread_mean,
        "spread_median": bar.spread_median,
        "spread_p95": bar.spread_p95,
        "spread_max": bar.spread_max,
        "quote_count": bar.quote_count,
        "stale_seconds": bar.stale_seconds,
        "source_coverage": bar.source_coverage,
        "coverage_measurement": (
            "expected_quote_interval"
            if bar.source_coverage is not None
            else "unmeasured_provider_sampling_cadence"
        ),
        "quote_provider": single_or_mixed(providers),
        "account_environment": single_or_mixed(environments),
        "collection_mode": single_or_mixed(collection_modes),
        "source_endpoint_class": single_or_mixed(endpoint_classes),
        "source_quality_states": quality_states,
        "all_quotes_tradable": bool(window) and all(item.collected.tradable for item in window),
        "source_received_at_max": max(item.collected.received_at for item in window).isoformat(),
        "source_payload_count": len({item.collected.raw_payload_sha256 for item in window}),
        "source_lineage_sha256": lineage_sha256,
        "source_first_commit_sequence": min(item.position.commit_sequence for item in window),
        "source_last_commit_sequence": max(item.position.commit_sequence for item in window),
        "open_time": bar.open_time.isoformat(),
        "event_time": bar.close_time.isoformat(),
        "source_record_id": (f"oanda-pricing:{bar.instrument}:1m:{bar.close_time.isoformat()}"),
        "ohlc_scope": "completed_bid_ask_bar",
        "data_quality_flags": list(dict.fromkeys(flags)),
    }


def _content_hash(row: Mapping[str, object]) -> str:
    payload = {
        key: value
        for key, value in row.items()
        if key != "content_hash" and not str(key).startswith("_")
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_snapshot_row(
    *,
    symbol: str,
    timeframe: str,
    payload: Mapping[str, object],
    materialized_at: datetime,
    run_id: str,
    writer_id: str,
    journal_genesis_sha256: str,
) -> dict[str, object]:
    event_time = datetime.fromisoformat(str(payload["event_time"]))
    open_time = datetime.fromisoformat(str(payload["open_time"]))
    if (
        event_time.tzinfo is None
        or event_time.utcoffset() is None
        or open_time.tzinfo is None
        or open_time.utcoffset() is None
    ):
        raise BidAskBridgeError("completed bar timestamps must be timezone-aware")
    event_time = event_time.astimezone(UTC)
    open_time = open_time.astimezone(UTC)
    if open_time >= event_time:
        raise BidAskBridgeError("completed bar open_time must precede event_time")
    if event_time > materialized_at:
        raise BidAskBridgeError("completed bar event_time cannot be in the future")

    row: dict[str, object] = {
        key: value
        for key, value in payload.items()
        if value is not None and key not in {"event_time", "open_time"}
    }
    event_stamp = event_time.isoformat()
    materialized_stamp = materialized_at.isoformat()
    row.update(
        {
            "ts": event_stamp,
            "event_time": event_stamp,
            "open_time": open_time.isoformat(),
            "available_time": materialized_stamp,
            "ingested_time": materialized_stamp,
            "capture_slot": event_stamp,
            "symbol": symbol,
            "timeframe": timeframe,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": "oanda_pricing_stream_bid_ask",
            "local_record_id": f"{symbol}:{timeframe}:{event_stamp}",
            "run_id": run_id,
            "writer_id": writer_id,
            "source_journal_genesis_sha256": journal_genesis_sha256,
        }
    )
    required = (
        "close",
        "bid",
        "ask",
        "source_record_id",
        "ohlc_scope",
        "data_quality_flags",
        "quote_provider",
        "account_environment",
        "collection_mode",
        "source_endpoint_class",
        "source_quality_states",
        "all_quotes_tradable",
        "quote_count",
        "coverage_measurement",
        "stale_seconds",
        "source_lineage_sha256",
        "source_journal_genesis_sha256",
        "source_first_commit_sequence",
        "source_last_commit_sequence",
    )
    missing = [key for key in required if key not in row]
    if missing:
        raise BidAskBridgeError(f"completed bar is missing provenance: {missing}")
    row["content_hash"] = _content_hash(row)
    return row


def _snapshot_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    try:
        event_time = datetime.fromisoformat(str(row["event_time"]))
    except (KeyError, TypeError, ValueError) as error:
        raise BidAskBridgeError("canonical snapshot has an invalid event_time") from error
    if event_time.tzinfo is None or event_time.utcoffset() is None:
        raise BidAskBridgeError("canonical snapshot event_time must be timezone-aware")
    symbol = str(row.get("symbol") or "").strip()
    timeframe = str(row.get("timeframe") or "").strip()
    if not symbol or not timeframe:
        raise BidAskBridgeError("canonical snapshot is missing symbol/timeframe")
    return event_time.astimezone(UTC).isoformat(), symbol, timeframe


def _validate_snapshot_row(row: Mapping[str, object]) -> None:
    if row.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise BidAskBridgeError("canonical snapshot has an unsupported schema_version")
    if row.get("content_hash") != _content_hash(row):
        raise BidAskBridgeError("canonical snapshot content_hash mismatch")
    event_stamp, symbol, timeframe = _snapshot_key(row)
    required = (
        "ts",
        "available_time",
        "ingested_time",
        "capture_slot",
        "source",
        "run_id",
        "writer_id",
        "source_record_id",
        "ohlc_scope",
        "data_quality_flags",
        "quote_provider",
        "account_environment",
        "collection_mode",
        "source_endpoint_class",
        "source_quality_states",
        "all_quotes_tradable",
        "quote_count",
        "coverage_measurement",
        "stale_seconds",
        "source_lineage_sha256",
        "source_journal_genesis_sha256",
        "source_first_commit_sequence",
        "source_last_commit_sequence",
    )
    missing = [key for key in required if row.get(key) in (None, "")]
    if missing:
        raise BidAskBridgeError(f"canonical snapshot is missing provenance: {missing}")
    if row.get("source") != "oanda_pricing_stream_bid_ask":
        raise BidAskBridgeError("canonical snapshot source is invalid")
    if row.get("ohlc_scope") != "completed_bid_ask_bar":
        raise BidAskBridgeError("canonical snapshot scope is invalid")
    if row.get("quote_provider") != "oanda":
        raise BidAskBridgeError("canonical snapshot provider is invalid")
    if row.get("collection_mode") != "live_stream":
        raise BidAskBridgeError("canonical snapshot collection mode is invalid")
    if row.get("source_endpoint_class") != "streaming_pricing":
        raise BidAskBridgeError("canonical snapshot endpoint class is invalid")
    for field_name in ("source_lineage_sha256", "source_journal_genesis_sha256"):
        digest = row.get(field_name)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BidAskBridgeError(f"canonical snapshot {field_name} is invalid")
    if row.get("ts") != event_stamp or row.get("capture_slot") != event_stamp:
        raise BidAskBridgeError("canonical snapshot time identity is inconsistent")
    if normalize_symbol(symbol) != symbol or timeframe not in DEFAULT_OUTPUT_TIMEFRAMES:
        raise BidAskBridgeError("canonical snapshot symbol/timeframe is invalid")

    parsed_times: dict[str, datetime] = {}
    for field_name in ("event_time", "open_time", "available_time", "ingested_time"):
        try:
            parsed = datetime.fromisoformat(str(row[field_name]))
        except (KeyError, TypeError, ValueError) as error:
            raise BidAskBridgeError(f"canonical snapshot {field_name} is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BidAskBridgeError(f"canonical snapshot {field_name} must be timezone-aware")
        parsed_times[field_name] = parsed.astimezone(UTC)
    if not (
        parsed_times["open_time"]
        < parsed_times["event_time"]
        <= parsed_times["available_time"]
        <= parsed_times["ingested_time"]
    ):
        raise BidAskBridgeError("canonical snapshot timestamps violate PIT order")

    numbers: dict[str, float] = {}
    for field_name in ("close", "bid", "ask", "stale_seconds"):
        value = row.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BidAskBridgeError(f"canonical snapshot {field_name} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise BidAskBridgeError(f"canonical snapshot {field_name} must be finite")
        numbers[field_name] = number
    if numbers["close"] <= 0 or numbers["bid"] <= 0 or numbers["bid"] >= numbers["ask"]:
        raise BidAskBridgeError("canonical snapshot contains an invalid bid/ask/close")
    if numbers["stale_seconds"] < 0:
        raise BidAskBridgeError("canonical snapshot stale_seconds cannot be negative")
    quote_count = row.get("quote_count")
    if isinstance(quote_count, bool) or not isinstance(quote_count, int) or quote_count < 1:
        raise BidAskBridgeError("canonical snapshot quote_count must be positive")
    if not isinstance(row.get("all_quotes_tradable"), bool):
        raise BidAskBridgeError("canonical snapshot all_quotes_tradable must be boolean")
    first_sequence = row.get("source_first_commit_sequence")
    last_sequence = row.get("source_last_commit_sequence")
    if (
        isinstance(first_sequence, bool)
        or not isinstance(first_sequence, int)
        or isinstance(last_sequence, bool)
        or not isinstance(last_sequence, int)
        or first_sequence < 1
        or last_sequence < first_sequence
    ):
        raise BidAskBridgeError("canonical snapshot source commit sequence range is invalid")
    for field_name in ("data_quality_flags", "source_quality_states"):
        values = row.get(field_name)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise BidAskBridgeError(f"canonical snapshot {field_name} is invalid")


def _same_snapshot(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    comparable = (
        "open",
        "high",
        "low",
        "close",
        "bid",
        "ask",
        "spread",
        "bid_open",
        "bid_high",
        "bid_low",
        "ask_open",
        "ask_high",
        "ask_low",
        "open_time",
        "event_time",
        "spread_mean",
        "spread_median",
        "spread_p95",
        "spread_max",
        "stale_seconds",
        "source_coverage",
        "quote_count",
        "quote_provider",
        "account_environment",
        "collection_mode",
        "source_endpoint_class",
        "coverage_measurement",
        "source_quality_states",
        "all_quotes_tradable",
        "source_received_at_max",
        "source_payload_count",
        "source_record_id",
        "data_quality_flags",
        "source",
        "schema_version",
        "ohlc_scope",
        "source_lineage_sha256",
        "source_journal_genesis_sha256",
        "source_first_commit_sequence",
        "source_last_commit_sequence",
    )
    return all(left.get(key) == right.get(key) for key in comparable)


def _decode_snapshot_row(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as error:
        raise BidAskBridgeError("canonical snapshot payload_json is malformed") from error
    if not isinstance(payload, dict):
        raise BidAskBridgeError("canonical snapshot payload_json is not an object")
    _validate_snapshot_row(payload)
    indexed = {
        "event_time": str(row["event_time"]),
        "symbol": str(row["symbol"]),
        "timeframe": str(row["timeframe"]),
        "capture_slot": str(row["capture_slot"]),
        "available_time": str(row["available_time"]),
        "ingested_time": str(row["ingested_time"]),
        "schema_version": int(row["schema_version"]),
        "content_hash": str(row["content_hash"]),
    }
    if any(payload.get(key) != value for key, value in indexed.items()):
        raise BidAskBridgeError("canonical snapshot indexed columns disagree with payload")
    return {str(key): value for key, value in payload.items()}


def read_snapshot_rows(path: str | Path) -> tuple[dict[str, object], ...]:
    """Read and validate all canonical rows for offline audit/tests."""

    with _connect_store(Path(path), readonly=True) as connection:
        rows = connection.execute("SELECT * FROM snapshots ORDER BY event_time, symbol, timeframe")
        return tuple(_decode_snapshot_row(row) for row in rows)


def read_latest_snapshot_slot(
    path: str | Path,
) -> tuple[datetime, tuple[dict[str, object], ...]]:
    """Read and validate every row in the latest logical event-time slot."""

    with _connect_store(Path(path), readonly=True) as connection:
        latest = connection.execute("SELECT MAX(event_time) FROM snapshots").fetchone()[0]
        if latest is None:
            raise BidAskBridgeError("canonical snapshot store has no rows")
        try:
            latest_time = datetime.fromisoformat(str(latest))
        except ValueError as error:
            raise BidAskBridgeError("canonical snapshot latest event_time is invalid") from error
        if latest_time.tzinfo is None or latest_time.utcoffset() is None:
            raise BidAskBridgeError("canonical snapshot latest event_time is naive")
        rows = tuple(
            _decode_snapshot_row(row)
            for row in connection.execute(
                "SELECT * FROM snapshots WHERE event_time = ? ORDER BY symbol, timeframe",
                (str(latest),),
            )
        )
    if not rows:
        raise BidAskBridgeError("canonical snapshot latest slot is empty")
    return latest_time.astimezone(UTC), rows


def _transaction_test_hook(stage: str) -> None:
    """Fault-injection boundary used only by the synthetic crash probe."""

    del stage


def _write_store_transaction(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    prior_state: dict[str, Any] | None,
    next_state: dict[str, object],
) -> int:
    with _connect_store(path, readonly=False, create=True) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_state = _state_from_connection(connection)
            if current_state != prior_state:
                raise BidAskBridgeError(
                    "canonical bridge state changed concurrently; refusing stale checkpoint advance"
                )
            if current_state is None:
                row_count = int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
                if row_count:
                    raise BidAskBridgeError(
                        "canonical snapshot rows exist without their transactional checkpoint"
                    )

            appended = 0
            for raw in rows:
                row = dict(raw)
                _validate_snapshot_row(row)
                event_time, symbol, timeframe = _snapshot_key(row)
                existing = connection.execute(
                    "SELECT * FROM snapshots "
                    "WHERE event_time = ? AND symbol = ? AND timeframe = ?",
                    (event_time, symbol, timeframe),
                ).fetchone()
                if existing is not None:
                    prior = _decode_snapshot_row(existing)
                    if not _same_snapshot(prior, row):
                        raise BidAskBridgeError(
                            "conflicting canonical snapshot for natural key "
                            f"{(event_time, symbol, timeframe)}"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO snapshots (
                        event_time, symbol, timeframe, capture_slot,
                        available_time, ingested_time, schema_version,
                        content_hash, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_time,
                        symbol,
                        timeframe,
                        row["capture_slot"],
                        row["available_time"],
                        row["ingested_time"],
                        row["schema_version"],
                        row["content_hash"],
                        _canonical_json(row),
                    ),
                )
                appended += 1

            _transaction_test_hook("after_snapshot_inserts")
            config = {
                key: value
                for key, value in next_state.items()
                if key
                not in {
                    "schema_version",
                    "next_position",
                    "checkpoint_commit_entry_sha256",
                    "last_materialized_close",
                    "updated_at",
                }
            }
            position = ReplayPosition.from_dict(next_state["next_position"])
            connection.execute(
                """
                INSERT INTO bridge_state (
                    singleton, schema_version, config_json,
                    next_commit_sequence, next_row_index,
                    checkpoint_commit_entry_sha256,
                    last_materialized_close, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    config_json = excluded.config_json,
                    next_commit_sequence = excluded.next_commit_sequence,
                    next_row_index = excluded.next_row_index,
                    checkpoint_commit_entry_sha256 =
                        excluded.checkpoint_commit_entry_sha256,
                    last_materialized_close = excluded.last_materialized_close,
                    updated_at = excluded.updated_at
                """,
                (
                    BRIDGE_STATE_SCHEMA,
                    _canonical_json(config),
                    position.commit_sequence,
                    position.row_index,
                    next_state["checkpoint_commit_entry_sha256"],
                    next_state["last_materialized_close"],
                    next_state["updated_at"],
                ),
            )
            _transaction_test_hook("before_commit")
            connection.commit()
            return appended
        except BaseException:
            connection.rollback()
            raise


def materialize_increment(
    *,
    ingest_log_dir: str | Path,
    output_path: str | Path,
    instruments: Sequence[str],
    timeframes: Sequence[str] = DEFAULT_OUTPUT_TIMEFRAMES,
    as_of: datetime | None = None,
    close_delay: timedelta = timedelta(seconds=30),
    write: bool = True,
) -> BridgeResult:
    """Materialize all newly completed one-minute quote bars.

    Each completed one-minute observation is exposed under every decision
    timeframe so outcome scoring has a dense, common post-decision path.  This
    does not claim that the observation itself is a 15m/1h/4h/1d candle.
    """

    now = as_of or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise BidAskBridgeError("as_of must be timezone-aware")
    now = now.astimezone(UTC)
    if close_delay < timedelta(0):
        raise BidAskBridgeError("close_delay must be non-negative")
    try:
        selected = tuple(dict.fromkeys(normalize_symbol(str(value)) for value in instruments))
    except ValueError as error:
        raise BidAskBridgeError(str(error)) from error
    output_timeframes = tuple(dict.fromkeys(str(value).strip() for value in timeframes))
    if not selected or any(not value for value in selected):
        raise BidAskBridgeError("at least one non-empty instrument is required")
    if not output_timeframes or any(
        value not in DEFAULT_OUTPUT_TIMEFRAMES for value in output_timeframes
    ):
        raise BidAskBridgeError(f"timeframes must be a subset of {DEFAULT_OUTPUT_TIMEFRAMES}")

    source = Path(ingest_log_dir)
    destination = Path(output_path)
    journal_root = source / "capture_journal"
    if not source.is_dir() or not journal_root.is_dir():
        raise BidAskBridgeError(f"ingest log directory is invalid: {source}")
    state = _load_store_state(destination)
    requested_position = (
        ReplayPosition.origin()
        if state is None
        else ReplayPosition.from_dict(state.get("next_position"))
    )
    expected_entry_sha256 = None if state is None else state.get("checkpoint_commit_entry_sha256")
    if expected_entry_sha256 is not None and not isinstance(expected_entry_sha256, str):
        raise BidAskBridgeError("bridge checkpoint entry hash has an invalid type")
    # read_quotes verifies the origin chain or the stored hash anchor itself;
    # avoid a second full-history constructor verification every minute.
    reader = CommittedCaptureReader(journal_root, verify=False)
    try:
        committed_records = reader.read_quotes(
            start_sequence=requested_position.commit_sequence,
            expected_entry_sha256=expected_entry_sha256,
        )
        journal_entries = reader.journal.entries(
            start_sequence=requested_position.commit_sequence,
        )
        journal_genesis = reader.journal.genesis_sha256()
    except CaptureJournalError as error:
        raise BidAskBridgeError(f"committed quote source invalid: {error}") from error
    if not journal_entries:
        raise BidAskBridgeError("capture journal has no durable entries")
    commit_entry_hashes = {
        entry.sequence: entry.entry_sha256
        for entry in journal_entries
        if entry.state is JournalState.COMMITTED
    }
    start_position = _validate_configuration(
        state,
        ingest_log_directory=source,
        journal_root=journal_root,
        journal_genesis_sha256=journal_genesis,
        commit_entry_hashes=commit_entry_hashes,
        records=committed_records,
        instruments=selected,
        timeframes=output_timeframes,
        output_path=destination,
    )
    unread = [
        record
        for record in committed_records
        if ReplayPosition(record.commit_sequence, record.row_index) >= start_position
    ]
    located = [_locate_quote(record, commit_entry_hashes) for record in unread]
    selected_rows = [item for item in located if _symbol(item.market.instrument) in selected]
    cutoff = now - close_delay
    pending_positions = [
        item.position
        for item in selected_rows
        if _bucket_close(item.market.source_timestamp) > cutoff
    ]
    complete_end = (
        ReplayPosition(unread[-1].commit_sequence, unread[-1].row_index + 1)
        if unread
        else start_position
    )
    next_position = min(pending_positions) if pending_positions else complete_end

    bars: list[BidAskBar] = []
    for instrument in sorted({item.market.instrument for item in selected_rows}):
        quotes = [item.market for item in selected_rows if item.market.instrument == instrument]
        bars.extend(bar for bar in materialize_bars(quotes, "1m") if bar.close_time <= cutoff)
    bars.sort(key=lambda bar: (bar.close_time, bar.instrument))

    rows: list[dict[str, object]] = []
    writer_id = f"{socket.gethostname()}:bid-ask-materializer-v2"
    for bar in bars:
        payload = _bar_snapshot(bar, selected_rows)
        symbol = _symbol(bar.instrument)
        run_id = f"bid-ask-bridge:{bar.close_time.isoformat()}"
        rows.extend(
            _canonical_snapshot_row(
                symbol=symbol,
                timeframe=timeframe,
                payload=payload,
                materialized_at=now,
                run_id=run_id,
                writer_id=writer_id,
                journal_genesis_sha256=journal_genesis,
            )
            for timeframe in output_timeframes
        )

    appended = 0
    last_close = (
        bars[-1].close_time.isoformat() if bars else state and state.get("last_materialized_close")
    )
    if write:
        appended = _write_store_transaction(
            destination,
            rows,
            prior_state=state,
            next_state={
                "schema_version": BRIDGE_STATE_SCHEMA,
                "ingest_log_directory": str(source.resolve()),
                "journal_root": str(journal_root.resolve()),
                "journal_genesis_sha256": journal_genesis,
                "instruments": list(selected),
                "timeframes": list(output_timeframes),
                "next_position": next_position.to_dict(),
                "checkpoint_commit_entry_sha256": _checkpoint_commit_hash(
                    next_position, commit_entry_hashes
                ),
                "last_materialized_close": last_close,
                "updated_at": now.isoformat(),
                "output_path": str(destination.resolve()),
            },
        )
    return BridgeResult(
        input_rows=len(unread),
        completed_bars=len(bars),
        output_rows=len(rows),
        appended_rows=appended,
        start_position=start_position,
        next_position=next_position,
        last_materialized_close=None if last_close is None else str(last_close),
    )
