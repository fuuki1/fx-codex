"""Regression: ``run_replay_cycle`` must not reference an undefined name.

``run_replay_cycle`` accepts ``admission_rejection_reasons`` and records it in
its result as ``live_admission_reasons_observed``. The local that carries it was
missing, so building the result payload raised ``NameError`` on every call that
got that far.

The gap survived because no tracked test exercised ``run_replay_cycle`` at all:
the replay suite lives in an untracked file, so CI never ran this path. This
module is deliberately self-contained -- it builds its own decision and tape --
so the guard keeps working regardless of that file's fate.

An empty portfolio is not enough to reproduce: with no tape window the function
returns early, before the payload is built. One queued decision and two ticks
are the minimum that reaches it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from data_platform.collect.contract import CollectedQuote
from fx_intel.virtual_portfolio import (
    VirtualPortfolioPolicy,
    open_virtual_portfolio_writer,
)
from fx_intel.operational_contracts import (
    DECISION_JOURNAL_PIT_CONTRACT,
    FUSION_PRODUCER,
    FUSION_PRODUCER_VERSION,
)
from fx_intel.virtual_portfolio_replay import run_replay_cycle

START = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def _decision(*, symbol: str = "USDJPY", timeframe: str = "1h") -> dict[str, object]:
    context_hash = hashlib.sha256(b"point-in-time-context").hexdigest()
    stop = 149.52 if symbol == "USDJPY" else 1.095
    target = 151.02 if symbol == "USDJPY" else 1.115
    reference_atr = 0.2 if symbol == "USDJPY" else 0.0052
    target_rr = 2.0 if symbol == "USDJPY" else (1.115 - 1.1002) / 0.0052
    return {
        "decision_id": "replay-decision-1",
        "ts": START.isoformat(),
        "prediction_time": START.isoformat(),
        "source_cutoff": (START - timedelta(seconds=2)).isoformat(),
        "max_feature_available_time": (START - timedelta(seconds=1)).isoformat(),
        "input_context_id": f"context:sha256:{context_hash}",
        "source_record_ids": ["context-row-1"],
        "pit_eligible": True,
        "pit_contract": DECISION_JOURNAL_PIT_CONTRACT,
        "producer": FUSION_PRODUCER,
        "producer_version": FUSION_PRODUCER_VERSION,
        "mode": "fusion",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": "long",
        "conviction": 80,
        "stop": stop,
        "target1": target,
        "price_frame": {
            "stop_atr_multiple": 2.5 if symbol == "USDJPY" else 1.0,
            "target_rr": target_rr,
            "reference_atr": reference_atr,
        },
        "reason": "PIT replay fixture",
        "features": {"rsi": 55.0, "regime": "trend"},
        "components": {"technical": 0.7},
        "warnings": [],
    }


def _tick(
    moment: datetime,
    bid: float,
    ask: float,
    *,
    received_at: datetime,
    instrument: str = "USDJPY",
) -> dict:
    identity = f"{instrument}:{moment.isoformat()}:{bid}:{ask}".encode()
    quote = CollectedQuote(
        provider="dukascopy",
        account_environment="datafeed",
        instrument=instrument,
        provider_event_time=moment,
        received_at=received_at,
        bid=bid,
        ask=ask,
        bid_size=1.0,
        ask_size=1.0,
        tradable=False,
        sequence_id=None,
        connection_id="historical-fixture",
        writer_id="fixture",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(identity).hexdigest(),
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    )
    return quote.to_dict()


def _write_rows(path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_replay_cycle_records_caller_supplied_admission_reasons(tmp_path) -> None:
    reason = "live heartbeat stale: snapshot"
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(START + timedelta(seconds=1), 150.00, 150.02, received_at=as_of),
            _tick(START + timedelta(minutes=5), 150.10, 150.12, received_at=as_of),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(_decision(), observed_at=START, source="fixture", batch_id="batch-1")
        result = run_replay_cycle(
            store,
            quote_log=quotes,
            as_of=as_of,
            admission_rejection_reasons=[reason],
        )

    # Recorded verbatim, and only recorded: replay is a historical
    # reconciliation, so these reasons never gate what it admits.
    assert result["live_admission_reasons_observed"] == [reason]
    assert result["opened"] == 1


def test_replay_cycle_defaults_admission_reasons_to_empty(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(START + timedelta(seconds=1), 150.00, 150.02, received_at=as_of),
            _tick(START + timedelta(minutes=5), 150.10, 150.12, received_at=as_of),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(_decision(), observed_at=START, source="fixture", batch_id="batch-1")
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)

    assert result["live_admission_reasons_observed"] == []
