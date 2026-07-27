from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

import pytest

from fx_intel import operational_contracts as contracts
from fx_intel import operational_migration as migration
from fx_intel import operational_store as store
from tools import operational_store_benchmark as benchmark

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.3")


def _decision(identifier: str, minute: int) -> dict[str, object]:
    stamp = NOW + timedelta(minutes=minute)
    return {
        "schema": 3,
        "event_type": "chart_decision",
        "decision_id": identifier,
        "ts": stamp.isoformat(),
        "source": "fx_briefing",
        "mode": "per_timeframe",
        "symbol": "USDJPY",
        "timeframe": "15m",
        "horizon_hours": 0.25,
        "decision": {"direction": "neutral"},
        "prediction_time": stamp.isoformat(),
        "source_cutoff": (stamp - timedelta(seconds=2)).isoformat(),
        "max_feature_available_time": (stamp - timedelta(seconds=1)).isoformat(),
        "pit_eligible": True,
        "pit_contract": contracts.DECISION_JOURNAL_PIT_CONTRACT,
        "producer": contracts.TIMEFRAME_PRODUCER,
        "producer_version": contracts.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context-{identifier}",
        "source_record_ids": [f"source-{identifier}"],
    }


def _price(identifier: str, minute: int) -> dict[str, object]:
    stamp = NOW + timedelta(minutes=minute)
    row: dict[str, object] = {
        "schema_version": 3,
        "ts": stamp.isoformat(),
        "event_time": stamp.isoformat(),
        "available_time": stamp.isoformat(),
        "ingested_time": stamp.isoformat(),
        "source": "oanda",
        "source_record_id": identifier,
        "symbol": "USDJPY",
        "timeframe": "15m",
        "ohlc_scope": "completed_bar",
        "close": 150.0 + minute / 100,
    }
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    row["content_hash"] = hashlib.sha256(encoded).hexdigest()
    return row


def _write(path: Path, rows: list[dict[str, object]]) -> str:
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )
    return store.file_sha256(path)


def test_benchmark_requires_page_parity_and_returns_timings(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write(decisions, [_decision(f"d-{index}", index) for index in range(5)])
    price_hash = _write(prices, [_price(f"p-{index}", index) for index in range(5)])
    database = tmp_path / "candidate.sqlite3"
    with store.open_operational_writer(database, writer_id="migration-test") as opened:
        migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    report = benchmark.run_benchmark(
        db_path=database,
        decision_path=decisions,
        price_path=prices,
        iterations=2,
        page_size=3,
    )

    assert report["page_size"] == 3
    assert report["decision_page"]["jsonl_full_scan"]["median_seconds"] >= 0
    assert report["price_page"]["sqlite_indexed_page"]["median_seconds"] >= 0
