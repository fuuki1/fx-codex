"""Bounded, evidence-pinned liquidity history reads from operational SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from typing import BinaryIO

from . import liquidity, price_history
from .operational_store import OperationalStoreError, open_operational_reader

PRICE_SOURCE_NAME = "prices"
DEFAULT_READER_BUSY_TIMEOUT_MS = 50
INITIAL_WINDOW_DAYS = 32
MAX_WINDOW_DAYS = 512


class OperationalLiquidityError(RuntimeError):
    """The SQLite projection cannot prove parity with its raw price source."""


@dataclass(frozen=True)
class OperationalLiquidityResult:
    rows: tuple[dict[str, object], ...]
    window_days: int
    projected_rows: int
    source_bytes: int


def load_recent_liquidity_rows(
    database_path: str | Path,
    source_path: str | Path,
    *,
    symbols: list[str] | tuple[str, ...],
    as_of: datetime,
    lookback_market_days: int,
    busy_timeout_ms: int = DEFAULT_READER_BUSY_TIMEOUT_MS,
    source_lock_timeout_ms: int = DEFAULT_READER_BUSY_TIMEOUT_MS,
    max_window_days: int = MAX_WINDOW_DAYS,
) -> OperationalLiquidityResult:
    """Load a verified projection, normalizing storage failures for callers."""

    try:
        return _load_recent_liquidity_rows(
            database_path,
            source_path,
            symbols=symbols,
            as_of=as_of,
            lookback_market_days=lookback_market_days,
            busy_timeout_ms=busy_timeout_ms,
            source_lock_timeout_ms=source_lock_timeout_ms,
            max_window_days=max_window_days,
        )
    except OperationalLiquidityError:
        raise
    except (OperationalStoreError, sqlite3.Error) as error:
        raise OperationalLiquidityError(
            f"operational price projection is unavailable: {error}"
        ) from error


def _load_recent_liquidity_rows(
    database_path: str | Path,
    source_path: str | Path,
    *,
    symbols: list[str] | tuple[str, ...],
    as_of: datetime,
    lookback_market_days: int,
    busy_timeout_ms: int,
    source_lock_timeout_ms: int,
    max_window_days: int,
) -> OperationalLiquidityResult:
    """Load exactly the market dates needed by the liquidity policy.

    The read is admitted only when the SQLite cursor covers the complete,
    unchanged raw JSONL inode and no price line was rejected during projection.
    A caller can therefore fall back or fail closed without silently using a
    stale/partial speed cache.
    """

    cutoff = _aware_utc(as_of)
    if lookback_market_days <= 0:
        raise ValueError("lookback_market_days must be positive")
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")
    if source_lock_timeout_ms <= 0:
        raise ValueError("source_lock_timeout_ms must be positive")
    if max_window_days < INITIAL_WINDOW_DAYS:
        raise ValueError(f"max_window_days must be >= {INITIAL_WINDOW_DAYS}")
    normalized_symbols = tuple(
        sorted({str(symbol).upper().replace("/", "") for symbol in symbols if str(symbol)})
    )
    if not normalized_symbols:
        return OperationalLiquidityResult((), 0, 0, 0)

    raw_path = Path(source_path).resolve()
    with _locked_source(raw_path, timeout_ms=source_lock_timeout_ms) as source_handle:
        before = os.fstat(source_handle.fileno())
        with open_operational_reader(database_path, busy_timeout_ms=busy_timeout_ms) as store:
            cursor = store.source_cursor(PRICE_SOURCE_NAME)
            if cursor is None:
                raise OperationalLiquidityError("price source cursor is not bootstrapped")
            _verify_cursor(
                cursor,
                raw_path=raw_path,
                source_stat=before,
                source_handle=source_handle,
            )
            if store.ingest_rejection_count(PRICE_SOURCE_NAME):
                raise OperationalLiquidityError("price projection contains rejected source lines")

            minimum_ns = store.connection.execute(
                f"""
                SELECT MIN(available_time_ns)
                FROM price_points
                WHERE symbol IN ({','.join('?' for _ in normalized_symbols)})
                  AND available_time_ns < ?
                """,
                (*normalized_symbols, _datetime_ns(cutoff)),
            ).fetchone()[0]
            minimum_time = (
                datetime.fromtimestamp(int(minimum_ns) / 1_000_000_000, tz=UTC)
                if minimum_ns is not None
                else None
            )

            window_days = INITIAL_WINDOW_DAYS
            projected: list[dict[str, object]] = []
            eligible_rows: list[tuple[dict[str, object], str, date]] = []
            allowed_dates: dict[str, set[date]] = {}
            while True:
                start = cutoff - timedelta(days=window_days)
                projected = store.price_payloads_between(
                    symbols=normalized_symbols,
                    start=start,
                    end=cutoff,
                )
                eligible_rows = []
                baseline_dates: dict[str, set[date]] = {
                    symbol: set() for symbol in normalized_symbols
                }
                for row in projected:
                    if not price_history.is_pit_eligible_snapshot_entry(row, as_of=cutoff):
                        continue
                    symbol = str(row.get("symbol") or "").upper().replace("/", "")
                    if symbol not in baseline_dates:
                        continue
                    available = datetime.fromisoformat(
                        str(row["available_time"]).replace("Z", "+00:00")
                    ).astimezone(UTC)
                    eligible_rows.append((row, symbol, available.date()))
                    market_date = liquidity.baseline_market_date(
                        symbol,
                        row,
                        decision_time=cutoff,
                    )
                    if market_date is not None:
                        baseline_dates[symbol].add(market_date)
                dates_by_symbol = {
                    symbol: sorted(dates) for symbol, dates in baseline_dates.items()
                }
                complete = minimum_time is None or start <= minimum_time
                enough = all(
                    len(dates_by_symbol[symbol]) >= lookback_market_days
                    for symbol in normalized_symbols
                )
                if enough or complete:
                    allowed_dates = {
                        symbol: set(dates[-lookback_market_days:])
                        for symbol, dates in dates_by_symbol.items()
                    }
                    break
                if window_days >= max_window_days:
                    raise OperationalLiquidityError(
                        "price projection does not cover the configured market-day lookback"
                    )
                window_days = min(max_window_days, window_days * 2)

            rows = tuple(
                row
                for row, symbol, available_date in eligible_rows
                if available_date in allowed_dates[symbol]
            )

        after = os.fstat(source_handle.fileno())
        if _stat_identity(before) != _stat_identity(after):
            raise OperationalLiquidityError("raw price source changed during operational read")
    return OperationalLiquidityResult(
        rows=rows,
        window_days=window_days,
        projected_rows=len(projected),
        source_bytes=before.st_size,
    )


def _verify_cursor(
    cursor,
    *,
    raw_path: Path,
    source_stat: os.stat_result,
    source_handle: BinaryIO,
) -> None:
    if Path(cursor.source_path).resolve() != raw_path:
        raise OperationalLiquidityError("price cursor source path does not match")
    if (cursor.device_id, cursor.inode) != (source_stat.st_dev, source_stat.st_ino):
        raise OperationalLiquidityError("price cursor source identity does not match")
    if cursor.byte_offset != source_stat.st_size:
        raise OperationalLiquidityError("price projection is behind the raw source")
    if cursor.source_size_at_sync != source_stat.st_size:
        raise OperationalLiquidityError("price cursor size evidence does not match")
    if cursor.source_mtime_ns != source_stat.st_mtime_ns:
        raise OperationalLiquidityError("price cursor mtime evidence does not match")
    boundary_length = cursor.byte_offset - cursor.last_line_start_offset
    if boundary_length < 0:
        raise OperationalLiquidityError("price cursor boundary is invalid")
    source_handle.seek(cursor.last_line_start_offset)
    boundary = source_handle.read(boundary_length)
    if len(boundary) != boundary_length:
        raise OperationalLiquidityError("price cursor boundary is unavailable")
    if hashlib.sha256(boundary).hexdigest() != cursor.last_line_sha256:
        raise OperationalLiquidityError("price cursor boundary hash does not match")


@contextmanager
def _locked_source(path: Path, *, timeout_ms: int) -> Iterator[BinaryIO]:
    try:
        handle = path.open("rb")
    except OSError as error:
        raise OperationalLiquidityError(f"raw price source is unavailable: {error}") from error
    deadline = time.monotonic() + timeout_ms / 1_000
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise OperationalLiquidityError("raw price source lock timed out") from error
                time.sleep(0.005)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_ns(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch
    return (
        delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000
    )


__all__ = [
    "OperationalLiquidityError",
    "OperationalLiquidityResult",
    "load_recent_liquidity_rows",
]
