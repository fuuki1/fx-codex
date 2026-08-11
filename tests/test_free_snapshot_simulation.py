from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from data_platform.collect.contract import CollectedQuote
from fx_intel.free_snapshot_simulation import (
    ConservativeSnapshotSpreadPolicy,
    reconcile_modeled_snapshot_quotes,
    run_free_snapshot_cycle,
)
from fx_intel.price_history import _content_hash
from fx_intel.virtual_portfolio import (
    MODELED_SNAPSHOT_EXECUTION_MODE,
    VirtualPortfolioPolicy,
    open_virtual_portfolio_writer,
    portfolio_snapshot,
)
from tests.test_virtual_portfolio import START, _decision


def _snapshot(moment, *, mid: float, identity: str) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 3,
        "ts": moment.isoformat(),
        "available_time": moment.isoformat(),
        "ingested_time": moment.isoformat(),
        "capture_slot": moment.replace(second=0, microsecond=0).isoformat(),
        "symbol": "USDJPY",
        "timeframe": "15m",
        "open": mid,
        "high": mid,
        "low": mid,
        "close": mid,
        "source": "tradingview_ta_scanner",
        "local_record_id": identity,
        "run_id": f"run-{identity}",
        "writer_id": "fixture",
        "ohlc_scope": "forming_bar_snapshot",
        "data_quality_flags": [
            "forming_bar_ohlc_not_post_prediction_interval",
            "bid_ask_unavailable",
            "event_time_unavailable",
            "source_record_id_unavailable",
        ],
    }
    row["content_hash"] = _content_hash(row)
    return row


def _write_rows(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _free_decision(policy: VirtualPortfolioPolicy) -> dict[str, object]:
    decision = _decision(policy)
    decision["timeframe"] = "15m"
    return decision


def test_free_snapshot_cycle_opens_marks_closes_and_never_enters_learning(tmp_path) -> None:
    database = tmp_path / "portfolio.sqlite3"
    snapshots = tmp_path / "snapshots.jsonl"
    entry_time = START + timedelta(seconds=1)
    mark_time = START + timedelta(minutes=5)
    exit_time = START + timedelta(minutes=10)
    rows = [
        _snapshot(entry_time, mid=150.0, identity="entry"),
        _snapshot(mark_time, mid=150.2, identity="mark"),
        _snapshot(exit_time, mid=151.1, identity="exit"),
    ]
    _write_rows(snapshots, rows[:1])

    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        policy = VirtualPortfolioPolicy()
        store.initialize_portfolio(policy=policy, created_at=START)
        store.queue_decision(
            _free_decision(policy),
            observed_at=START,
            source="fixture",
            batch_id="free-snapshot",
        )
        opened = run_free_snapshot_cycle(
            store,
            snapshot_log=snapshots,
            as_of=entry_time,
        )
        assert opened["opened"] == 1
        trade = store.open_trade_details()[0]
        assert trade["entry_bid"] == 149.99
        assert trade["entry_ask"] == 150.01
        assert trade["execution_assessment"]["execution_mode"] == (MODELED_SNAPSHOT_EXECUTION_MODE)
        assert trade["execution_assessment"]["learning_eligible"] is False

        _write_rows(snapshots, rows)
        settled = run_free_snapshot_cycle(
            store,
            snapshot_log=snapshots,
            as_of=exit_time + timedelta(seconds=1),
        )
        assert settled["marked"] == 2
        assert settled["closed"] == 1
        assert store.export_learning_rows(as_of=exit_time + timedelta(seconds=1))["rows"] == []
        snapshot = store.snapshot(now=exit_time + timedelta(seconds=1))
        assert snapshot["closed_trade_count"] == 1
        assert snapshot["free_snapshot_simulation"]["modeled_trade_count"] == 1
        close = store.connection.execute(
            "SELECT learning_eligible, attribution_json FROM simulated_trade_closes"
        ).fetchone()
        assert close is not None
        assert int(close["learning_eligible"]) == 0
        attribution = json.loads(str(close["attribution_json"]))
        assert attribution["execution_observation_mode"] == MODELED_SNAPSHOT_EXECUTION_MODE
        assert "modeled_snapshot_fill" in attribution["label_quality"]["exclusion_reasons"]
    reopened = portfolio_snapshot(database, now=exit_time + timedelta(seconds=1))
    assert reopened["configured"] is True
    assert reopened["free_snapshot_simulation"]["modeled_trade_count"] == 1


def test_delayed_bid_ask_reconciliation_is_append_only(tmp_path) -> None:
    database = tmp_path / "portfolio.sqlite3"
    snapshots = tmp_path / "snapshots.jsonl"
    quotes = tmp_path / "quotes.jsonl"
    entry_time = START + timedelta(seconds=1)
    _write_rows(snapshots, [_snapshot(entry_time, mid=150.0, identity="entry")])

    actual_time = entry_time + timedelta(milliseconds=100)
    received_at = START + timedelta(hours=2)
    raw_hash = hashlib.sha256(b"measured-quote").hexdigest()
    actual = CollectedQuote(
        provider="dukascopy",
        account_environment="datafeed",
        instrument="USDJPY",
        provider_event_time=actual_time,
        received_at=received_at,
        bid=149.995,
        ask=150.005,
        bid_size=1.0,
        ask_size=1.0,
        tradable=False,
        sequence_id=None,
        connection_id="fixture",
        writer_id="fixture",
        revision_id=None,
        raw_payload_sha256=raw_hash,
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    )
    _write_rows(quotes, [actual.to_dict()])

    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        policy = VirtualPortfolioPolicy()
        store.initialize_portfolio(policy=policy, created_at=START)
        store.queue_decision(
            _free_decision(policy),
            observed_at=START,
            source="fixture",
            batch_id="free-snapshot-reconciliation",
        )
        run_free_snapshot_cycle(store, snapshot_log=snapshots, as_of=entry_time)
        result = reconcile_modeled_snapshot_quotes(
            store,
            quote_log=quotes,
            as_of=received_at,
        )
        assert result["reconciled"] == 1
        row = store.connection.execute(
            "SELECT leg, modeled_bid, actual_bid, evidence_json "
            "FROM simulated_trade_quote_reconciliations"
        ).fetchone()
        assert row is not None
        assert row["leg"] == "entry"
        assert float(row["modeled_bid"]) == 149.99
        assert float(row["actual_bid"]) == 149.995
        evidence = json.loads(str(row["evidence_json"]))
        assert evidence["modeled_fill_rewritten"] is False
        assert evidence["learning_eligible"] is False
    reopened = portfolio_snapshot(database, now=received_at)
    assert reopened["configured"] is True
    assert reopened["free_snapshot_simulation"]["reconciled_bid_ask_legs"] == 1


def test_spread_policy_is_explicit_and_conservative() -> None:
    policy = ConservativeSnapshotSpreadPolicy()
    assert policy.spread("USDJPY") == 0.02
    assert policy.spread("EURUSD") == 0.0002
    assert policy.spread("GBPUSD") == 0.00025
    assert len(policy.sha256) == 64
