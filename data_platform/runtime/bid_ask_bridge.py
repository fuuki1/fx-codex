"""Materialize committed bid/ask journal rows into completed one-minute bars.

Only hash-bound ``committed`` captures are visible. The checkpoint stores the
first unconsumed ``(commit_sequence, row_index)``; the first still-open minute
is replayed on the next invocation. Output is appended idempotently before the
checkpoint advances, so a crash can replay data but cannot silently skip it.
The legacy accepted JSONL and its byte offsets are never read here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
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
    collected: CollectedQuote
    market: MarketQuote


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BidAskBridgeError(f"cannot read bridge state {path}: {error}") from error
    if not isinstance(value, dict):
        raise BidAskBridgeError(f"unsupported bridge state at {path}")
    if value.get("schema_version") in (1, 2):
        raise BidAskBridgeError(
            "legacy bridge state requires explicit audited checkpoint migration"
        )
    if value.get("schema_version") != BRIDGE_STATE_SCHEMA:
        raise BidAskBridgeError(f"unsupported bridge state at {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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


def _locate_quote(record: CommittedQuote) -> _LocatedQuote:
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
        "open_time": bar.open_time.isoformat(),
        "event_time": bar.close_time.isoformat(),
        "source_record_id": (f"oanda-pricing:{bar.instrument}:1m:{bar.close_time.isoformat()}"),
        "ohlc_scope": "closed_bar_after_prediction",
        "data_quality_flags": list(dict.fromkeys(flags)),
    }


def _content_hash(row: Mapping[str, object]) -> str:
    payload = {
        key: value
        for key, value in row.items()
        if key != "content_hash" and not str(key).startswith("_")
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_snapshot_row(
    *,
    symbol: str,
    timeframe: str,
    payload: Mapping[str, object],
    materialized_at: datetime,
    run_id: str,
    writer_id: str,
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
    )
    missing = [key for key in required if row.get(key) in (None, "")]
    if missing:
        raise BidAskBridgeError(f"canonical snapshot is missing provenance: {missing}")
    if row.get("source") != "oanda_pricing_stream_bid_ask":
        raise BidAskBridgeError("canonical snapshot source is invalid")
    if row.get("ohlc_scope") != "closed_bar_after_prediction":
        raise BidAskBridgeError("canonical snapshot scope is invalid")
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
    )
    return all(left.get(key) == right.get(key) for key in comparable)


def _append_snapshot_rows(path: Path, rows: Sequence[Mapping[str, object]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing: dict[tuple[str, str, str], dict[str, object]] = {}
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as error:
                    raise BidAskBridgeError(
                        f"malformed canonical JSONL at {path}:{line_number}"
                    ) from error
                if not isinstance(parsed, dict):
                    raise BidAskBridgeError(
                        f"non-object canonical JSONL row at {path}:{line_number}"
                    )
                _validate_snapshot_row(parsed)
                key = _snapshot_key(parsed)
                prior = existing.get(key)
                if prior is not None and not _same_snapshot(prior, parsed):
                    raise BidAskBridgeError(
                        f"conflicting existing canonical snapshots for natural key {key}"
                    )
                existing[key] = parsed

            pending: list[dict[str, object]] = []
            for raw in rows:
                row = dict(raw)
                _validate_snapshot_row(row)
                key = _snapshot_key(row)
                prior = existing.get(key)
                if prior is not None:
                    if not _same_snapshot(prior, row):
                        raise BidAskBridgeError(
                            f"conflicting canonical snapshot for natural key {key}"
                        )
                    continue
                existing[key] = row
                pending.append(row)

            handle.seek(0, os.SEEK_END)
            for row in pending:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if pending:
                handle.flush()
                os.fsync(handle.fileno())
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return len(pending)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def materialize_increment(
    *,
    ingest_log_dir: str | Path,
    state_path: str | Path,
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
    checkpoint = Path(state_path)
    destination = Path(output_path)
    journal_root = source / "capture_journal"
    if not source.is_dir() or not journal_root.is_dir():
        raise BidAskBridgeError(f"ingest log directory is invalid: {source}")
    state = _load_state(checkpoint)
    requested_position = (
        ReplayPosition.origin()
        if state is None
        else ReplayPosition.from_dict(state.get("next_position"))
    )
    expected_entry_sha256 = None if state is None else state.get("checkpoint_commit_entry_sha256")
    if expected_entry_sha256 is not None and not isinstance(expected_entry_sha256, str):
        raise BidAskBridgeError("bridge checkpoint entry hash has an invalid type")
    reader = CommittedCaptureReader(journal_root)
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
    located = [_locate_quote(record) for record in unread]
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
            )
            for timeframe in output_timeframes
        )

    appended = 0
    if write:
        appended = _append_snapshot_rows(destination, rows)
        last_close = (
            bars[-1].close_time.isoformat()
            if bars
            else state and state.get("last_materialized_close")
        )
        _atomic_write_json(
            checkpoint,
            {
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
    else:
        last_close = bars[-1].close_time.isoformat() if bars else None
    return BridgeResult(
        input_rows=len(unread),
        completed_bars=len(bars),
        output_rows=len(rows),
        appended_rows=appended,
        start_position=start_position,
        next_position=next_position,
        last_materialized_close=None if last_close is None else str(last_close),
    )
