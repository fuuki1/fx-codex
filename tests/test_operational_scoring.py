"""operational storeの満期予測→正準採点→immutable outcome接続テスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

import pytest

from fx_intel import operational_store as store
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.operational_scoring import score_due_predictions

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
DUE = NOW + timedelta(hours=1)
SCORED_AT = DUE + timedelta(minutes=1)
CHECKPOINT = "prices:2026-07-23T09:00:00Z"


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.3")


def _decision_event() -> dict[str, object]:
    decision = {
        "direction": "long",
        "close": 100.0,
        "stop": 99.1,
        "target1": 101.1,
        "target2": 102.1,
        "entry_bid": 99.9,
        "entry_ask": 100.1,
        "quote_observed_at": NOW.isoformat(),
        "quote_available_at": NOW.isoformat(),
        "quote_source": "fixture_quotes",
        "quote_source_record_id": "entry-1",
        "planned_risk_distance": 1.0,
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "cost_model_id": DEFAULT_COST_MODEL_ID,
        "cost_model_version": DEFAULT_COST_MODEL_VERSION,
        "cost_status": DEFAULT_COST_STATUS,
        "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
        "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
        "slippage_r": 0.0,
        "commission_r": 0.0,
        "financing_r": 0.0,
        "cost_quality_flags": [],
    }
    return {
        "decision_id": "decision-1",
        "ts": NOW.isoformat(),
        "symbol": "USDJPY",
        "mode": "per_timeframe",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "decision": decision,
    }


def _prediction() -> store.PredictionRecord:
    return store.PredictionRecord(
        prediction_id="decision-1",
        decision_id="decision-1",
        symbol="USDJPY",
        timeframe="1h",
        prediction_time=NOW,
        available_time=NOW - timedelta(seconds=1),
        due_time=DUE,
        source="fx_briefing",
        payload=_decision_event(),
        ingested_time=NOW,
    )


def _bar(index: int) -> store.PricePointRecord:
    start = NOW + timedelta(minutes=index * 5)
    end = start + timedelta(minutes=5)
    payload: dict[str, object] = {
        "ts": end.isoformat(),
        "event_time": end.isoformat(),
        "available_time": end.isoformat(),
        "ingested_time": end.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "bar_start": start.isoformat(),
        "bar_end": end.isoformat(),
        "bar_granularity": "M5",
        "complete": True,
        "source": "fixture_quotes",
        "source_record_id": f"bar-{index}",
        "ohlc_scope": "completed_bid_ask_bar",
        "data_quality_flags": [],
        "path_quality": 1.0,
        "bid_open": 100.4,
        "bid_high": 100.6,
        "bid_low": 99.8,
        "bid_close": 100.5,
        "ask_open": 100.6,
        "ask_high": 100.8,
        "ask_low": 100.0,
        "ask_close": 100.7,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["content_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return store.PricePointRecord(
        source_record_id=f"bar-{index}",
        symbol="USDJPY",
        timeframe="1h",
        event_time=end,
        available_time=end,
        ingested_time=end,
        source="fixture_quotes",
        revision_id="initial",
        open=100.5,
        high=100.7,
        low=99.9,
        close=100.6,
        bid=100.5,
        ask=100.7,
        ohlc_scope="completed_bid_ask_bar",
        payload=payload,
    )


def test_matured_prediction_is_scored_and_closed(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="scoring-test") as opened:
        opened.record_prediction(_prediction())
        for index in range(12):
            opened.record_price_point(_bar(index))

        report = score_due_predictions(opened, as_of=SCORED_AT, source_checkpoint=CHECKPOINT)

        assert report.due == 1
        assert report.scored == 1
        assert report.deferred == 0
        assert report.inserted == 1

        row = opened.connection.execute(
            "SELECT status, realized_net_r, label_version FROM outcomes"
        ).fetchone()
        assert row["status"] == "scored"
        assert row["realized_net_r"] == pytest.approx(0.4)
        assert row["label_version"] == NET_LABEL_VERSION

        # The prediction must no longer be pending, or it would be rescored forever.
        status = opened.connection.execute(
            "SELECT status FROM predictions WHERE prediction_id = 'decision-1'"
        ).fetchone()[0]
        assert status == "scored"


def test_rescoring_is_idempotent_and_does_not_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="scoring-test") as opened:
        opened.record_prediction(_prediction())
        for index in range(12):
            opened.record_price_point(_bar(index))

        first = score_due_predictions(opened, as_of=SCORED_AT, source_checkpoint=CHECKPOINT)
        second = score_due_predictions(opened, as_of=SCORED_AT, source_checkpoint=CHECKPOINT)

        assert first.inserted == 1
        # The prediction is closed, so the second sweep finds nothing due at all.
        assert second.due == 0
        assert second.inserted == 0
        assert opened.connection.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1


def test_missing_price_path_defers_instead_of_freezing_an_outcome(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="scoring-test") as opened:
        opened.record_prediction(_prediction())

        report = score_due_predictions(opened, as_of=SCORED_AT, source_checkpoint=CHECKPOINT)

        assert report.due == 1
        assert report.deferred == 1
        assert report.scored == 0
        assert report.inserted == 0
        assert report.reasons["price_path_not_ingested"] == 1
        # Nothing was written, so a late path can still score this prediction.
        assert opened.connection.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
        status = opened.connection.execute(
            "SELECT status FROM predictions WHERE prediction_id = 'decision-1'"
        ).fetchone()[0]
        assert status == "pending"


def test_immature_prediction_is_not_scored_early(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="scoring-test") as opened:
        opened.record_prediction(_prediction())
        for index in range(12):
            opened.record_price_point(_bar(index))

        report = score_due_predictions(
            opened,
            as_of=DUE - timedelta(minutes=1),
            source_checkpoint=CHECKPOINT,
        )

        assert report.due == 0
        assert report.inserted == 0
