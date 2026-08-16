from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import json
import os
from pathlib import Path

import pytest

from fx_intel import liquidity, operational_liquidity, operational_migration, operational_store
from fx_intel.price_history import snapshot_entries

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operational_store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(operational_store.sqlite3, "sqlite_version", "3.53.3")


def _market_stamps(count: int) -> list[datetime]:
    stamps: list[datetime] = []
    cursor = AS_OF - timedelta(days=1)
    while len(stamps) < count:
        if cursor.weekday() < 5:
            stamps.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(stamps)


def _build_projection(tmp_path: Path) -> tuple[Path, Path, list[dict[str, object]]]:
    source = tmp_path / "prices.jsonl"
    rows: list[dict[str, object]] = []
    for index, stamp in enumerate(_market_stamps(25)):
        rows.extend(
            snapshot_entries(
                {
                    "USDJPY": {
                        "15m": {
                            "close": 150.0,
                            "bid": 149.99,
                            "ask": 150.01 + index / 100_000,
                        }
                    },
                    "EURUSD": {
                        "15m": {
                            "close": 1.1,
                            "bid": 1.0999,
                            "ask": 1.1001 + index / 1_000_000,
                        }
                    },
                },
                now=stamp,
                run_id=f"run-{index}",
                writer_id="test",
            )
        )
    encoded = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for row in rows
    )
    source.write_bytes(encoded)
    source_stat = source.stat()
    last_line = encoded.splitlines(keepends=True)[-1]
    last_line_start = len(encoded) - len(last_line)
    database = tmp_path / "operational.sqlite3"
    with operational_store.open_operational_writer(database, writer_id="test") as store:
        store.record_price_points(
            [operational_migration.price_record_from_row(row) for row in rows]
        )
        store.bootstrap_source_cursor(
            source_name="prices",
            source_kind="prices",
            source_path=str(source.resolve()),
            device_id=source_stat.st_dev,
            inode=source_stat.st_ino,
            byte_offset=len(encoded),
            last_line_start_offset=last_line_start,
            last_line_sha256=hashlib.sha256(last_line).hexdigest(),
            source_mtime_ns=source_stat.st_mtime_ns,
            source_sha256=hashlib.sha256(encoded).hexdigest(),
            row_count=len(rows),
            captured_at=AS_OF,
        )
    return database, source, rows


def test_operational_liquidity_returns_exact_last_market_dates(tmp_path: Path) -> None:
    database, source, rows = _build_projection(tmp_path)

    result = operational_liquidity.load_recent_liquidity_rows(
        database,
        source,
        symbols=("USDJPY", "EURUSD"),
        as_of=AS_OF,
        lookback_market_days=20,
    )

    expected_dates = set(_market_stamps(20))
    assert len(result.rows) == 40
    assert {datetime.fromisoformat(str(row["available_time"])) for row in result.rows} == (
        expected_dates
    )
    assert {str(row["source_record_id"]) for row in result.rows} <= {
        str(row["source_record_id"]) for row in rows
    }
    assert result.window_days >= operational_liquidity.INITIAL_WINDOW_DAYS

    quote = liquidity.scanner_quote(
        "USDJPY",
        bid=149.99,
        ask=150.01,
        observed_at=AS_OF,
    )
    assert quote is not None
    full_snapshot = liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=AS_OF,
        quote=quote,
        price_rows=rows,
        session_bucket="new_york",
    )
    operational_snapshot = liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=AS_OF,
        quote=quote,
        price_rows=result.rows,
        session_bucket="new_york",
    )
    assert operational_snapshot.to_dict() == full_snapshot.to_dict()


def test_operational_liquidity_rejects_projection_behind_source(tmp_path: Path) -> None:
    database, source, _rows = _build_projection(tmp_path)
    with source.open("ab") as handle:
        handle.write(b"{}\n")

    with pytest.raises(
        operational_liquidity.OperationalLiquidityError,
        match="projection is behind",
    ):
        operational_liquidity.load_recent_liquidity_rows(
            database,
            source,
            symbols=("USDJPY", "EURUSD"),
            as_of=AS_OF,
            lookback_market_days=20,
        )


def test_operational_liquidity_rejects_same_size_mtime_restored_tamper(
    tmp_path: Path,
) -> None:
    database, source, _rows = _build_projection(tmp_path)
    original_stat = source.stat()
    payload = bytearray(source.read_bytes())
    payload[-8] = ord("x") if payload[-8] != ord("x") else ord("y")
    source.write_bytes(payload)
    source.touch()
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(
        operational_liquidity.OperationalLiquidityError,
        match="boundary hash",
    ):
        operational_liquidity.load_recent_liquidity_rows(
            database,
            source,
            symbols=("USDJPY", "EURUSD"),
            as_of=AS_OF,
            lookback_market_days=20,
        )


def test_operational_liquidity_normalizes_corrupt_database_failure(tmp_path: Path) -> None:
    _database, source, _rows = _build_projection(tmp_path)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")

    with pytest.raises(
        operational_liquidity.OperationalLiquidityError,
        match="projection is unavailable",
    ):
        operational_liquidity.load_recent_liquidity_rows(
            corrupt,
            source,
            symbols=("USDJPY", "EURUSD"),
            as_of=AS_OF,
            lookback_market_days=20,
        )
