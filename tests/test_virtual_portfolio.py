from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import random
import sqlite3
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from fx_intel.canonical_outcome_store import load_canonical_outcomes
from fx_intel.canonical_label_evidence_store import load_canonical_label_evidence
from fx_intel.evaluation_labels import canonical_net_label_contract_flags
from fx_intel.operational_contracts import (
    DECISION_JOURNAL_PIT_CONTRACT,
    FUSION_PRODUCER,
    FUSION_PRODUCER_VERSION,
)
from fx_intel.virtual_portfolio import (
    SCHEMA_VERSION,
    ImmutableVirtualRecordConflict,
    InvalidVirtualRecord,
    PositionMarkRequest,
    QuoteSnapshot,
    TradeCloseRequest,
    VirtualPortfolioError,
    VirtualPortfolioPolicy,
    _OPEN_INSERT_KEYS,
    _classify_label_quality,
    _record_hash,
    classify_sizing_invariants,
    five_x_virtual_portfolio_policy,
    open_virtual_portfolio_reader,
    open_virtual_portfolio_writer,
    portfolio_snapshot,
    summarize_net_r_robust,
)
from tools.virtual_portfolio import _set_five_x_limits
from tools.virtual_portfolio_canonical_outcomes import connect as connect_canonical_outcomes
from tools.virtual_portfolio_daily_report import build_daily_report

START = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def _frozen_datetime(moment: datetime):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return moment.replace(tzinfo=None)
            return moment.astimezone(tz)

    return FrozenDateTime


def _decision(
    policy: VirtualPortfolioPolicy,
    *,
    decision_id: str = "decision-1",
    prediction_time: datetime = START,
    timeframe: str = "1h",
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "ts": prediction_time.isoformat(),
        "prediction_time": prediction_time.isoformat(),
        "source_cutoff": (prediction_time - timedelta(seconds=2)).isoformat(),
        "max_feature_available_time": (prediction_time - timedelta(seconds=1)).isoformat(),
        "input_context_id": f"context-{decision_id}",
        "source_record_ids": [f"source-{decision_id}"],
        "pit_eligible": True,
        "pit_contract": DECISION_JOURNAL_PIT_CONTRACT,
        "producer": FUSION_PRODUCER,
        "producer_version": FUSION_PRODUCER_VERSION,
        "mode": "fusion",
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "direction": "long",
        "conviction": 80,
        "stop": 149.52,
        "target1": 151.02,
        "reason": "PITで観測した上昇仮説",
        "features": {"rsi": 55.0, "regime": "trend"},
        "components": {"technical": 0.7, "news": 0.1},
        "warnings": [],
        "data_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "policy_sha256": policy.policy_sha256,
    }


def _quote(
    *,
    moment: datetime = START,
    bid: float = 150.0,
    ask: float = 150.02,
    record_id: str = "quote-1",
) -> QuoteSnapshot:
    return QuoteSnapshot(
        source_record_id=record_id,
        source="test-executable-quotes",
        revision_id="original",
        event_time=moment - timedelta(seconds=2),
        available_time=moment - timedelta(seconds=1),
        ingested_time=moment,
        bid=bid,
        ask=ask,
    )


def _initialized(tmp_path):
    path = tmp_path / "virtual.sqlite3"
    policy = VirtualPortfolioPolicy()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert store.initialize_portfolio(policy=policy, created_at=START)
    return path, policy


def _cycle_args(tmp_path, database):
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"events": []}), encoding="utf-8")
    return SimpleNamespace(
        db=database,
        as_run_db=tmp_path / "as_run.sqlite3",
        counterfactual_db=tmp_path / "counterfactual.sqlite3",
        writer_id="test",
        decisions=decisions,
        closes=tmp_path / "missing-closes.json",
        snapshot_prices=tmp_path / "missing-snapshots.jsonl",
        quotes=tmp_path / "missing-quotes.jsonl",
        quote_index=None,
        heartbeat_config=None,
        close_session=False,
        audit_run_root=tmp_path / "runs",
        no_audit_artifact=True,
        stage_timing_log=tmp_path / "stage_timings.jsonl",
    )


def test_initialize_is_idempotent_and_reader_is_query_only(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert not store.initialize_portfolio(
            policy=policy,
            created_at=START + timedelta(minutes=1),
        )
        assert store.cash_balance_jpy() == 1_000_000
        assert int(store.connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 30_000

    with open_virtual_portfolio_reader(path) as store:
        snapshot = store.snapshot(now=START)
        assert snapshot["balance_jpy"] == 1_000_000
        assert snapshot["broker_connected"] is False
        assert int(store.connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 5_000
        with pytest.raises(sqlite3.OperationalError, match="readonly|query only"):
            store.connection.execute(
                "INSERT INTO ledger_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad", "trade_closed", 0, 0, None, "{}", "bad", "bad"),
            )
        with pytest.raises(VirtualPortfolioError, match="mutation requires"):
            store.record_session_event(
                session_date_jst="2026-07-29",
                event_type="opened",
                event_time=START,
            )


def test_initialized_ledger_rejects_a_second_writer_identity(tmp_path) -> None:
    path, _ = _initialized(tmp_path)

    with pytest.raises(VirtualPortfolioError, match="not authorized"):
        with open_virtual_portfolio_writer(path, writer_id="second-service"):
            pass


def test_open_transaction_rolls_back_decision_anchor_and_capacity_on_insert_failure(
    tmp_path,
) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.connection.execute("""
            CREATE TRIGGER injected_open_failure
            BEFORE INSERT ON simulated_trade_opens
            BEGIN
                SELECT RAISE(ABORT, 'injected open failure');
            END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected open failure"):
            store.process_decision(
                _decision(policy, decision_id="atomic-open"),
                quote=_quote(record_id="atomic-open-quote"),
                observed_at=START,
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                price_anchor=_anchor_payload(
                    anchor_event_time=(START - timedelta(seconds=2)).isoformat(),
                    anchor_lag_seconds=0.0,
                    target="151.02",
                    target_rr="2.0",
                ),
            )
        counts = {
            table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "decision_records",
                "decision_price_anchors",
                "simulated_trade_opens",
            )
        }

    assert counts == {
        "decision_records": 0,
        "decision_price_anchors": 0,
        "simulated_trade_opens": 0,
    }


def test_terminal_checklist_block_cannot_open_even_with_valid_quote(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    decision = _decision(policy, decision_id="terminal-block")
    decision["checklist"] = {
        "steps": [
            {
                "order": 5,
                "key": "spread",
                "status": "block",
                "note": "spread exceeds risk distance",
            }
        ]
    }
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.process_decision(
            decision,
            quote=_quote(record_id="terminal-block-quote"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert result["disposition"] == "abstained"
    assert any("decision_pipeline:spread" in reason for reason in result["rejection_reasons"])


def test_zero_width_stop_is_terminal_before_open(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    decision = _decision(policy, decision_id="zero-stop")
    decision["stop"] = 150.02
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.process_decision(
            decision,
            quote=_quote(record_id="zero-stop-quote"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        open_count = int(
            store.connection.execute("SELECT COUNT(*) FROM simulated_trade_opens").fetchone()[0]
        )

    assert result["disposition"] == "abstained"
    assert open_count == 0


def test_jpy_conversion_and_sizing_properties_over_rate_grid(tmp_path) -> None:
    """Property test: conversion changes units, never the bounded JPY risk."""

    for index, conversion_rate in enumerate((80.0, 100.0, 125.0, 150.0, 200.0)):
        path, policy = _initialized(tmp_path / str(index))
        decision = _decision(policy, decision_id=f"eur-{index}")
        decision.update(symbol="EURUSD", stop=1.0950, target1=1.1100)
        with open_virtual_portfolio_writer(path, writer_id="test") as store:
            result = store.process_decision(
                decision,
                quote=_quote(bid=1.1000, ask=1.1002, record_id=f"eur-quote-{index}"),
                observed_at=START,
                quote_to_jpy_rate=conversion_rate,
                quote_conversion_source="USDJPY canonical tick",
            )
            row = store.connection.execute(
                "SELECT units, planned_risk_jpy, quote_to_jpy_rate FROM simulated_trade_opens"
            ).fetchone()
        assert result["disposition"] == "opened"
        assert 0 < int(row["planned_risk_jpy"]) <= 5_000
        assert float(row["quote_to_jpy_rate"]) == conversion_rate
        expected = int(5_000 / ((1.1002 - 1.0950) * conversion_rate))
        assert int(row["units"]) == expected


def test_sizing_invariant_properties_over_valid_microstructure_grid() -> None:
    """Property test over reproducible random valid spreads, stops, and rewards."""

    generator = random.Random(20260811)
    for _ in range(250):
        entry = Decimal("100") + Decimal(str(generator.uniform(0, 100)))
        spread = Decimal(str(generator.uniform(0.0001, 0.02)))
        risk = spread * Decimal(str(generator.uniform(2.01, 20)))
        reward_to_risk = Decimal(str(generator.uniform(0.25, 9.5)))
        planned_risk = Decimal("5000")
        units = (planned_risk / risk).quantize(Decimal("1"))
        notional = abs(units) * entry
        equity = max(Decimal("1000000"), notional / Decimal("50"))
        assessment = classify_sizing_invariants(
            executable=entry,
            stop=entry - risk,
            target=entry + risk * reward_to_risk,
            bid=entry - spread,
            ask=entry,
            units=units,
            conversion_rate=Decimal("1"),
            planned_risk_jpy=units * risk,
            equity_jpy=equity,
        )
        assert assessment["canonical"] is True
        assert assessment["denominator_unstable"] is False


def test_five_x_policy_is_append_only_point_in_time_and_keeps_per_trade_risk(
    tmp_path,
) -> None:
    path, base_policy = _initialized(tmp_path)
    changed_at = START + timedelta(minutes=2)
    five_x_policy = five_x_virtual_portfolio_policy()

    assert five_x_policy.daily_loss_limit_bps == base_policy.daily_loss_limit_bps * 5
    assert five_x_policy.weekly_loss_limit_bps == base_policy.weekly_loss_limit_bps * 5
    assert five_x_policy.monthly_loss_limit_bps == base_policy.monthly_loss_limit_bps * 5
    assert five_x_policy.hard_drawdown_bps == base_policy.hard_drawdown_bps * 5
    assert five_x_policy.max_open_positions == base_policy.max_open_positions * 5
    assert five_x_policy.risk_per_trade_bps == base_policy.risk_per_trade_bps
    assert five_x_policy.daily_target_jpy == base_policy.daily_target_jpy

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(base_policy, decision_id="pre-transition-loss"),
            quote=_quote(record_id="pre-transition-entry"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(opened["trade_id"]),
                close_time=START + timedelta(minutes=1),
                quote=_quote(
                    moment=START + timedelta(minutes=1),
                    bid=148.48,
                    ask=148.50,
                    record_id="pre-transition-exit",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="stop_gap",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
        before = store.snapshot(now=START + timedelta(minutes=1))
        transition = store.activate_five_x_limits(changed_at=changed_at)
        retry = store.activate_five_x_limits(changed_at=changed_at)
        after = store.snapshot(now=changed_at)
        base_row = store.connection.execute("SELECT policy_sha256 FROM portfolio_config").fetchone()
        update_count = int(
            store.connection.execute("SELECT COUNT(*) FROM portfolio_policy_updates").fetchone()[0]
        )

        assert (
            store.policy(
                as_of=changed_at - timedelta(microseconds=1),
                known_at=changed_at - timedelta(microseconds=1),
            ).policy_sha256
            == base_policy.policy_sha256
        )
        assert (
            store.policy(
                as_of=changed_at,
                known_at=changed_at,
            ).policy_sha256
            == five_x_policy.policy_sha256
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute("UPDATE portfolio_policy_updates SET reason_text = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute("DELETE FROM portfolio_policy_updates")

    assert before["risk_gate"]["can_open"] is False
    assert before["risk_gate"]["reason"] == "日次損失上限に到達"
    assert before["risk_gate"]["daily_loss_limit_jpy"] == 15_000
    assert after["risk_gate"]["can_open"] is True
    assert after["risk_gate"]["daily_loss_limit_jpy"] == 75_000
    assert after["risk_gate"]["weekly_loss_limit_jpy"] == 150_000
    assert after["risk_gate"]["monthly_loss_limit_jpy"] == 300_000
    assert after["hard_drawdown_limit_jpy"] == 500_000
    assert after["horizon_allocation"]["capacity_limit"] == 10
    assert after["policy_provenance"]["source"] == "portfolio_policy_updates"
    assert after["policy_provenance"]["append_only"] is True
    assert transition["inserted"] is True
    assert retry["inserted"] is False
    assert str(base_row["policy_sha256"]) == base_policy.policy_sha256
    assert update_count == 1


def test_writer_migrates_v6_store_to_append_only_policy_history(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with sqlite3.connect(path) as connection:
        for trigger_name in (
            "portfolio_policy_updates_no_update",
            "portfolio_policy_updates_no_delete",
        ):
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE portfolio_policy_updates")
        connection.execute("PRAGMA user_version=6")
        connection.commit()

    with pytest.raises(VirtualPortfolioError, match="version=6"):
        with open_virtual_portfolio_reader(path):
            pass

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert int(store.connection.execute("PRAGMA user_version").fetchone()[0]) == (
            SCHEMA_VERSION
        )
        assert store.policy().policy_sha256 == policy.policy_sha256
        assert (
            int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM portfolio_policy_updates"
                ).fetchone()[0]
            )
            == 0
        )
        trigger_count = int(store.connection.execute("""
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'trigger'
                  AND name IN (
                    'portfolio_policy_updates_no_update',
                    'portfolio_policy_updates_no_delete'
                  )
                """).fetchone()[0])

    assert trigger_count == 2


def test_set_five_x_limits_cli_operation_initializes_new_database_idempotently(
    tmp_path,
) -> None:
    path = tmp_path / "new-portfolio.sqlite3"

    first = _set_five_x_limits(path, "five-x-cli-test")
    second = _set_five_x_limits(path, "five-x-cli-test")

    assert first["initialized"] is True
    assert first["transition"]["inserted"] is True
    assert second["initialized"] is False
    assert second["transition"]["inserted"] is False
    assert first["after"]["daily_loss_limit_bps"] == 750
    assert first["after"]["weekly_loss_limit_bps"] == 1_500
    assert first["after"]["monthly_loss_limit_bps"] == 3_000
    assert first["after"]["hard_drawdown_bps"] == 5_000
    assert first["after"]["max_open_positions"] == 10
    assert first["after"]["risk_per_trade_bps"] == 50
    with open_virtual_portfolio_reader(path) as store:
        assert (
            int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM portfolio_policy_updates"
                ).fetchone()[0]
            )
            == 1
        )


def test_writer_refuses_to_migrate_a_database_owned_by_another_application(
    tmp_path,
) -> None:
    path = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE foreign_data(value TEXT)")
        connection.execute("INSERT INTO foreign_data VALUES ('preserve-me')")
        connection.execute("PRAGMA application_id=123456")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(VirtualPortfolioError, match="owned by another application"):
        with open_virtual_portfolio_writer(path, writer_id="must-refuse"):
            pass

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM foreign_data").fetchone()[0] == "preserve-me"
        assert int(connection.execute("PRAGMA application_id").fetchone()[0]) == 123456
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
    finally:
        connection.close()


def test_process_decision_rejects_a_caller_supplied_risk_snapshot(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    decision = _decision(policy, decision_id="forged-risk")
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        forged = store.prediction_portfolio_risk_snapshot(as_of=START)
        forged["cash_balance_jpy"] = 999_000_000
        decision["portfolio_risk_snapshot"] = forged

        with pytest.raises(
            ImmutableVirtualRecordConflict,
            match="authoritative ledger snapshot",
        ):
            store.process_decision(decision, observed_at=START)

        assert store.pending_decisions() == []


def test_writer_migrates_v3_store_to_current_additive_tables(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.process_decision(
            _decision(policy, decision_id="pre-v4-decision", timeframe="1d"),
            observed_at=START,
        )
        assert result["disposition"] == "abstained"
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE decision_horizon_assignments")
        connection.execute("DROP TABLE session_close_completions")
        connection.execute("DROP TABLE simulated_trade_close_observations")
        connection.execute("DROP TABLE simulated_trade_open_observations")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(VirtualPortfolioError, match="version=3"):
        with open_virtual_portfolio_reader(path):
            pass

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert not store.initialize_portfolio(policy=policy, created_at=START)
        assert store.pending_decisions() == []
        version = int(store.connection.execute("PRAGMA user_version").fetchone()[0])
        decision_count = int(
            store.connection.execute("SELECT COUNT(*) FROM decision_records").fetchone()[0]
        )
        assignment_count = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_horizon_assignments"
            ).fetchone()[0]
        )

    assert version == SCHEMA_VERSION
    assert decision_count == 1
    assert assignment_count == 0
    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "session_close_completions" in tables
    assert "decision_horizon_assignments" in tables
    assert "simulated_trade_open_observations" in tables
    assert "simulated_trade_close_observations" in tables


def test_writer_migrates_original_v1_store_to_current_additive_tables(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    original_v1_tables = {
        "decision_records",
        "learning_exports",
        "ledger_events",
        "portfolio_config",
        "portfolio_marks",
        "session_events",
        "simulated_trade_closes",
        "simulated_trade_opens",
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table in sorted(tables - original_v1_tables):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("PRAGMA user_version=1")
        connection.commit()

    with pytest.raises(VirtualPortfolioError, match="version=1"):
        with open_virtual_portfolio_reader(path):
            pass

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        version = int(store.connection.execute("PRAGMA user_version").fetchone()[0])
        before = store.audit_virtual_portfolio_v2()
        migration = store.migrate_virtual_portfolio_v2_accounting(
            recorded_at=START + timedelta(minutes=1)
        )
        after = store.audit_virtual_portfolio_v2()

    assert version == SCHEMA_VERSION
    assert before["passed"] is False
    assert migration["inserted"] == 1
    assert migration["remaining"] == 0
    assert after["passed"] is True


def test_writer_migrates_v4_open_and_close_knowledge_clocks(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="pre-v6-trade"),
            quote=_quote(record_id="pre-v6-open"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(opened["trade_id"]),
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="pre-v6-close",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE simulated_trade_close_observations")
        connection.execute("DROP TABLE simulated_trade_open_observations")
        connection.execute("PRAGMA user_version=4")
        connection.commit()

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        version = int(store.connection.execute("PRAGMA user_version").fetchone()[0])
        open_observation = store.connection.execute(
            "SELECT * FROM simulated_trade_open_observations"
        ).fetchone()
        close_observation = store.connection.execute(
            "SELECT * FROM simulated_trade_close_observations"
        ).fetchone()
        snapshot = store.snapshot(now=close_time)

    assert version == SCHEMA_VERSION
    assert open_observation is not None
    assert int(open_observation["open_known_at_ns"]) == int(START.timestamp() * 1_000_000_000)
    assert close_observation is not None
    assert int(close_observation["close_known_at_ns"]) == int(
        close_time.timestamp() * 1_000_000_000
    )
    assert snapshot["reconciliation"]["passed"] is True
    assert snapshot["open_position_count"] == 0


def test_schema_v6_observation_hashes_and_append_only_triggers_fail_closed(
    tmp_path,
) -> None:
    missing_trigger_path, _ = _initialized(tmp_path / "missing-trigger")
    with sqlite3.connect(missing_trigger_path) as connection:
        connection.execute("DROP TRIGGER simulated_trade_open_observations_no_update")
        connection.commit()
    with pytest.raises(VirtualPortfolioError, match="trigger is missing"):
        with open_virtual_portfolio_reader(missing_trigger_path):
            pass

    tampered_path, policy = _initialized(tmp_path / "tampered")
    with open_virtual_portfolio_writer(tampered_path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="observation-tamper"),
            quote=_quote(record_id="observation-tamper"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        assert opened["disposition"] == "opened"
    with sqlite3.connect(tampered_path) as connection:
        connection.execute("DROP TRIGGER simulated_trade_open_observations_no_update")
        connection.execute(
            """
            UPDATE simulated_trade_open_observations
            SET record_sha256 = ?
            WHERE trade_id = ?
            """,
            ("0" * 64, str(opened["trade_id"])),
        )
        connection.commit()
    with pytest.raises(VirtualPortfolioError, match="record hash mismatch"):
        with open_virtual_portfolio_reader(tampered_path):
            pass
    with pytest.raises(VirtualPortfolioError, match="record hash mismatch"):
        with open_virtual_portfolio_writer(tampered_path, writer_id="test"):
            pass

    audit_path, audit_policy = _initialized(tmp_path / "audit-tampered")
    with open_virtual_portfolio_writer(audit_path, writer_id="test") as store:
        audited_open = store.process_decision(
            _decision(audit_policy, decision_id="audit-observation-tamper"),
            quote=_quote(record_id="audit-observation-tamper"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        store.connection.execute("DROP TRIGGER simulated_trade_open_observations_no_update")
        store.connection.execute(
            """
            UPDATE simulated_trade_open_observations
            SET record_sha256 = ?
            WHERE trade_id = ?
            """,
            ("0" * 64, str(audited_open["trade_id"])),
        )
        with pytest.raises(VirtualPortfolioError, match="record hash mismatch"):
            store.audit_virtual_portfolio_v2()


def test_session_close_request_is_completed_only_by_its_request_id(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    cutoff = START + timedelta(hours=8)
    session_date = "2026-07-29"
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert store.record_session_event(
            session_date_jst=session_date,
            event_type="closed",
            event_time=cutoff - timedelta(minutes=1),
        )
        assert store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=cutoff,
            cutoff_time=cutoff,
            detail={"source": "test"},
        )
        request = store.pending_session_close_request(as_of=cutoff)
        assert request is not None
        assert request["cutoff_time"] == cutoff.isoformat()
        assert store.session_close_request(session_date_jst=session_date) == request
        assert not store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=cutoff,
            cutoff_time=cutoff,
            detail={"source": "test"},
        )
        with pytest.raises(ImmutableVirtualRecordConflict):
            store.record_session_close_request(
                session_date_jst=session_date,
                requested_at=cutoff + timedelta(seconds=1),
                cutoff_time=cutoff,
                detail={"source": "changed"},
            )
        # A legacy session-level closed event predating this request cannot
        # suppress or satisfy the later request.
        assert store.pending_session_close_request(as_of=cutoff) == request
        completed_at = cutoff + timedelta(minutes=1)
        assert store.record_session_close_completion(
            request_id=str(request["request_id"]),
            completed_at=completed_at,
            detail={"source": "test-completion"},
        )
        assert store.pending_session_close_request(as_of=completed_at) is None
        completion = store.session_close_completion(request_id=str(request["request_id"]))
        assert completion is not None
        assert completion["request_id"] == request["request_id"]
        assert not store.record_session_close_completion(
            request_id=str(request["request_id"]),
            completed_at=completed_at,
            detail={"source": "test-completion"},
        )
        with pytest.raises(ImmutableVirtualRecordConflict):
            store.record_session_close_completion(
                request_id=str(request["request_id"]),
                completed_at=completed_at + timedelta(seconds=1),
                detail={"source": "changed"},
            )


def test_horizon_allocation_uses_capacity_only_for_15m_and_1h(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        for index, timeframe in enumerate(("4h", "1d"), start=1):
            result = store.process_decision(
                _decision(
                    policy,
                    decision_id=f"observer-{index}",
                    timeframe=timeframe,
                ),
                observed_at=START,
            )
            assert result["disposition"] == "abstained"
            assert result["horizon_allocation"]["allocation_class"] == "observation_only"
            assert "実行可能bid/askが無い" not in result["rejection_reasons"]

        for index, timeframe in enumerate(("15m", "1h"), start=1):
            result = store.process_decision(
                _decision(
                    policy,
                    decision_id=f"capacity-{index}",
                    timeframe=timeframe,
                ),
                quote=_quote(record_id=f"capacity-quote-{index}"),
                observed_at=START,
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
            assert result["disposition"] == "opened"
            assert result["horizon_allocation"]["allocation_class"] == ("intraday_capacity")

        snapshot = store.snapshot(now=START)

    assert snapshot["open_position_count"] == 2
    assert snapshot["horizon_allocation"]["intraday_open_positions"] == 2
    assert snapshot["horizon_allocation"]["counts"]["observation_only"] == 2
    assert snapshot["horizon_allocation"]["counts"]["intraday_capacity"] == 2
    assert snapshot["daily_profit_task"] == {
        "contract": "virtual-daily-profit-task-v1",
        "required": True,
        "session_date_jst": "2026-07-29",
        "deadline_jst": "2026-07-30T00:00:00+09:00",
        "target_jpy": 70_000,
        "realized_net_pnl_jpy": 0,
        "remaining_jpy": 70_000,
        "progress_ratio": 0.0,
        "status": "in_progress",
        "completion_basis": "canonical_realized_net_pnl",
        "optimization_input": False,
        "training_label": False,
        "achievement_guaranteed": False,
        "safety_constraints_override_allowed": False,
    }
    assert snapshot["data_quality_state"]["daily_target_task_completed"] is False
    assert snapshot["research_kpis"]["daily_profit_reference"]["required"] is True
    assert snapshot["research_kpis"]["daily_profit_reference"]["optimization_input"] is False


def test_session_close_completion_refuses_remaining_open_position(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    cutoff = START + timedelta(minutes=1)
    session_date = START.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="open-before-close"),
            quote=_quote(record_id="open-before-close-quote"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        assert opened["disposition"] == "opened"
        store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=cutoff,
            cutoff_time=cutoff,
            detail={"source": "test"},
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        with pytest.raises(InvalidVirtualRecord, match="zero open"):
            store.record_session_close_completion(
                request_id=str(request["request_id"]),
                completed_at=cutoff,
                detail={"source": "should-fail"},
            )


def test_session_close_completion_cannot_precede_request_time(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    cutoff = START + timedelta(minutes=1)
    requested_at = START + timedelta(minutes=2)
    session_date = requested_at.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=requested_at,
            cutoff_time=cutoff,
            detail={"source": "test"},
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        assert store.pending_session_close_request(as_of=cutoff) is None
        with pytest.raises(InvalidVirtualRecord, match="precede request"):
            store.record_session_close_completion(
                request_id=str(request["request_id"]),
                completed_at=cutoff + timedelta(seconds=30),
                detail={"source": "too-early"},
            )


def test_cycle_keeps_request_pending_when_reconciliation_fails(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, _ = _initialized(tmp_path)
    cycle_time = START + timedelta(minutes=2)
    session_date = cycle_time.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=START + timedelta(minutes=1),
            cutoff_time=START + timedelta(minutes=1),
            detail={"source": "test"},
        )
        with store.connection:
            store.connection.execute(
                """
                INSERT INTO ledger_events(
                    event_id, event_type, effective_time_ns, amount_jpy, trade_id,
                    detail_json, record_sha256, writer_id
                ) VALUES (?, 'trade_closed', ?, 1, NULL, '{}', ?, ?)
                """,
                (
                    "fault-unreconciled-ledger",
                    int(START.timestamp() * 1_000_000_000),
                    "f" * 64,
                    "fault-injection",
                ),
            )
    monkeypatch.setattr(cli, "datetime", _frozen_datetime(cycle_time))

    result = cli._cycle(_cycle_args(tmp_path, path))

    assert result["session_close_status"] == "failed_closed_reconciliation"
    assert result["ok"] is False
    assert result["stage_timing"]["failed_stages"] == [
        "canonical_replay_cycle",
        "counterfactual_cycle",
    ]
    assert {
        "latest_snapshot_read",
        "required_heartbeat_evaluation",
        "canonical_writer_acquire",
        "canonical_decision_queue",
        "canonical_replay_cycle",
        "canonical_writer_release",
    } <= set(result["stage_timing"]["elapsed_ms_by_stage"])
    with open_virtual_portfolio_reader(path) as store:
        assert store.pending_session_close_request(as_of=cycle_time) is not None
        assert (
            store.connection.execute("SELECT COUNT(*) FROM session_close_completions").fetchone()[0]
            == 0
        )


def test_cycle_timing_marks_handled_replay_failure(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, _ = _initialized(tmp_path)
    monkeypatch.setattr(cli, "run_replay_cycle", mock.Mock(side_effect=RuntimeError("replay")))

    result = cli._cycle(_cycle_args(tmp_path, path))

    assert result["replay"]["status"] == "failed_closed"
    assert "canonical_replay_cycle" in result["stage_timing"]["failed_stages"]


def test_cycle_queues_canonical_decisions_before_counterfactual_work(tmp_path) -> None:
    from tools import virtual_portfolio as cli

    path, _ = _initialized(tmp_path)
    args = _cycle_args(tmp_path, path)

    cli._cycle(args)

    timing_rows = [
        json.loads(line)
        for line in args.stage_timing_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    stages = [str(row["stage"]) for row in timing_rows]
    assert stages.index("latest_snapshot_read") < stages.index("canonical_decision_queue")
    assert stages.index("canonical_decision_queue") < stages.index("counterfactual_cycle")
    assert stages.index("canonical_writer_release") < stages.index("counterfactual_cycle")


def test_counterfactual_exception_preserves_canonical_cycle(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, policy = _initialized(tmp_path)
    args = _cycle_args(tmp_path, path)
    decision = _decision(policy, decision_id="counterfactual-failure")
    args.decisions.write_text(
        json.dumps(
            {
                "generated_at": START.isoformat(),
                "events": [{"decision": decision}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_counterfactual_cycle",
        mock.Mock(side_effect=RuntimeError("counterfactual unavailable")),
    )
    monkeypatch.setattr(
        cli,
        "run_replay_cycle",
        mock.Mock(
            return_value={
                "status": "ok",
                "opened": 0,
                "abstained": 0,
                "closed": 0,
                "waiting": 1,
                "conflicts": [],
            }
        ),
    )

    result = cli._cycle(args)

    assert result["ok"] is True
    assert result["queued_intakes"] == 1
    assert result["counterfactual"]["status"] == "failed_closed"
    assert result["counterfactual"]["conflicts"] == [
        {"record_id": "counterfactual", "error": "counterfactual unavailable"}
    ]
    assert result["stage_timing"]["failed_stages"] == ["counterfactual_cycle"]
    with open_virtual_portfolio_reader(path) as store:
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_intakes WHERE decision_id = ?",
                ("counterfactual-failure",),
            ).fetchone()[0]
            == 1
        )


def test_scheduled_cycle_skips_optional_counterfactual_work(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, _ = _initialized(tmp_path)
    args = _cycle_args(tmp_path, path)
    args.skip_counterfactual = True
    counterfactual = mock.Mock(side_effect=AssertionError("must be isolated"))
    monkeypatch.setattr(cli, "_counterfactual_cycle", counterfactual)

    result = cli._cycle(args)

    assert result["ok"] is True
    assert result["counterfactual"]["status"] == "scheduled_separately"
    assert "counterfactual_cycle" not in result["stage_timing"]["elapsed_ms_by_stage"]
    counterfactual.assert_not_called()


def test_cycle_timing_marks_replay_partial_failure_return(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, _ = _initialized(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_replay_cycle",
        mock.Mock(
            return_value={
                "status": "partial_failure",
                "opened": 0,
                "abstained": 0,
                "closed": 0,
                "conflicts": [{"record_id": "x", "error": "bad row"}],
            }
        ),
    )

    result = cli._cycle(_cycle_args(tmp_path, path))

    assert result["replay"]["status"] == "partial_failure"
    assert "canonical_replay_cycle" in result["stage_timing"]["failed_stages"]


def test_cycle_does_not_hide_completed_session_with_open_position(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, policy = _initialized(tmp_path)
    requested_at = START + timedelta(minutes=1)
    cycle_time = START + timedelta(minutes=2)
    session_date = cycle_time.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="fault-open"),
            quote=_quote(record_id="fault-open-quote"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        assert opened["disposition"] == "opened"
        store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=requested_at,
            cutoff_time=requested_at,
            detail={"source": "test"},
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        with store.connection:
            store.connection.execute(
                """
                INSERT INTO session_close_completions(
                    completion_id, request_id, session_id, session_date_jst,
                    completed_at_ns, detail_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    "fault-completion-with-open",
                    str(request["request_id"]),
                    str(request["session_id"]),
                    session_date,
                    int(cycle_time.timestamp() * 1_000_000_000),
                    "e" * 64,
                    "fault-injection",
                ),
            )
    monkeypatch.setattr(cli, "datetime", _frozen_datetime(cycle_time))

    result = cli._cycle(_cycle_args(tmp_path, path))

    assert result["session_close_status"] == ("failed_closed_inconsistent_request_state")
    assert result["ok"] is False
    assert result["portfolio"]["session_close_lifecycle"]["status"] == (
        "inconsistent_completed_with_open_positions"
    )


def test_unsupported_horizon_and_post_cutoff_decision_fail_closed(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    session_date = START.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        unsupported = store.process_decision(
            _decision(policy, decision_id="unsupported", timeframe="fusion"),
            observed_at=START,
        )
        assert unsupported["disposition"] == "abstained"
        assert unsupported["horizon_allocation"]["allocation_class"] == "unsupported"
        assert store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=START,
            cutoff_time=START,
            detail={"source": "test"},
        )
        after_cutoff = store.process_decision(
            _decision(policy, decision_id="post-cutoff", timeframe="1h"),
            quote=_quote(record_id="post-cutoff-quote"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        completed_at = START + timedelta(minutes=1)
        store.record_session_close_completion(
            request_id=str(request["request_id"]),
            completed_at=completed_at,
            detail={"source": "test"},
        )
        pre_cutoff_time = START - timedelta(minutes=1)
        delayed_quote = QuoteSnapshot(
            source_record_id="late-pre-cutoff-quote",
            source="test-executable-quotes",
            revision_id="original",
            event_time=pre_cutoff_time,
            available_time=completed_at,
            ingested_time=completed_at,
            bid=150.0,
            ask=150.02,
        )
        late_pre_cutoff = store.process_decision(
            _decision(
                policy,
                decision_id="late-pre-cutoff",
                prediction_time=pre_cutoff_time,
                timeframe="1h",
            ),
            quote=delayed_quote,
            observed_at=pre_cutoff_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
            historical_replay=True,
            replay_as_of=completed_at,
        )
        historical_snapshot = store.snapshot(now=START - timedelta(minutes=2))

    assert after_cutoff["disposition"] == "abstained"
    assert "日次決済締切後のため新規仮想建てを停止" in after_cutoff["rejection_reasons"]
    assert late_pre_cutoff["disposition"] == "abstained"
    assert "日次決済完了済みsessionのため新規仮想建てを停止" in late_pre_cutoff["rejection_reasons"]
    assert historical_snapshot["session_close_lifecycle"]["status"] == "not_requested"


def test_completed_session_reopens_for_strictly_post_completion_decisions(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    session_date = START.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    completed_at = START + timedelta(minutes=1)
    reopened_at = completed_at + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=START,
            cutoff_time=START,
            detail={"source": "test"},
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        assert store.record_session_close_completion(
            request_id=str(request["request_id"]),
            completed_at=completed_at,
            detail={"source": "test"},
        )
        at_completion = store.process_decision(
            _decision(
                policy,
                decision_id="at-completion",
                prediction_time=completed_at,
                timeframe="1h",
            ),
            quote=_quote(
                moment=completed_at,
                record_id="at-completion-quote",
            ),
            observed_at=completed_at,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        reopened = store.process_decision(
            _decision(
                policy,
                decision_id="post-completion",
                prediction_time=reopened_at,
                timeframe="1h",
            ),
            quote=_quote(
                moment=reopened_at,
                record_id="post-completion-quote",
            ),
            observed_at=reopened_at,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        snapshot = store.snapshot(now=reopened_at)
        close_time = reopened_at + timedelta(minutes=1)
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(reopened["trade_id"]),
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="post-completion-close",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
        daily_report = build_daily_report(
            store,
            session_date_jst=session_date,
            generated_at=close_time,
        )

    assert at_completion["disposition"] == "abstained"
    assert "日次決済完了済みsessionのため新規仮想建てを停止" in at_completion["rejection_reasons"]
    assert reopened["disposition"] == "opened"
    assert snapshot["risk_gate"]["can_open"] is True
    assert snapshot["session_close_lifecycle"]["status"] == "completed"
    assert snapshot["session_close_lifecycle"]["sealed_through"] == completed_at.isoformat()
    assert snapshot["session_close_lifecycle"]["post_completion_open_position_count"] == 1
    assert snapshot["data_quality_state"]["session_close_lifecycle_ok"] is True
    assert daily_report["trade_count"] == 0
    assert daily_report["portfolio"]["today_pnl_jpy"] == 0
    assert daily_report["portfolio"]["balance_jpy"] == policy.initial_capital_jpy


def test_delayed_post_completion_open_is_not_visible_before_replay_as_of(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    session_date = START.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    completed_at = START + timedelta(minutes=1)
    event_time = START + timedelta(minutes=2)
    replay_as_of = START + timedelta(minutes=3)
    delayed_quote = QuoteSnapshot(
        source_record_id="delayed-post-completion-quote",
        source="test-executable-quotes",
        revision_id="original",
        event_time=event_time,
        available_time=replay_as_of,
        ingested_time=replay_as_of,
        bid=150.0,
        ask=150.02,
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        assert store.record_session_close_request(
            session_date_jst=session_date,
            requested_at=START,
            cutoff_time=START,
            detail={"source": "test"},
        )
        request = store.session_close_request(session_date_jst=session_date)
        assert request is not None
        assert store.record_session_close_completion(
            request_id=str(request["request_id"]),
            completed_at=completed_at,
            detail={"source": "test"},
        )
        reopened = store.process_decision(
            _decision(
                policy,
                decision_id="delayed-post-completion",
                prediction_time=event_time,
                timeframe="1h",
            ),
            quote=delayed_quote,
            observed_at=event_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
            historical_replay=True,
            replay_as_of=replay_as_of,
            price_anchor=_anchor_payload(
                anchor_event_time=event_time.isoformat(),
                anchor_lag_seconds=0.0,
                target="151.02",
                target_rr="2.0",
            ),
        )
        impossible_close_time = event_time + timedelta(seconds=30)
        with pytest.raises(
            InvalidVirtualRecord,
            match="cannot precede open known_at",
        ):
            store.close_trade(
                TradeCloseRequest(
                    trade_id=str(reopened["trade_id"]),
                    close_time=impossible_close_time,
                    quote=QuoteSnapshot(
                        source_record_id="close-before-open-known",
                        source="test-executable-quotes",
                        revision_id="original",
                        event_time=impossible_close_time,
                        available_time=impossible_close_time,
                        ingested_time=impossible_close_time,
                        bid=150.50,
                        ask=150.52,
                    ),
                    quote_to_jpy_rate=1,
                    quote_conversion_source="JPY quote currency",
                    close_reason="time_exit",
                    cost_model_id="explicit-test-costs-v1",
                    cost_model_version="1",
                    cost_source="test-fixture",
                    costs_complete=True,
                    historical_replay=True,
                    observed_at=impossible_close_time,
                )
            )
        before_known = store.snapshot(now=event_time)
        when_known = store.snapshot(now=replay_as_of)

    assert reopened["disposition"] == "opened"
    assert before_known["open_position_count"] == 0
    assert before_known["trades"] == []
    assert before_known["risk_gate"]["can_open"] is True
    assert before_known["session_close_lifecycle"]["post_completion_open_position_count"] == 0
    assert when_known["open_position_count"] == 1
    assert when_known["open_positions"][0]["open_known_at"] == replay_as_of.isoformat()
    assert when_known["open_positions"][0]["open_known_at_basis"] == ("historical_replay_as_of")
    assert when_known["session_close_lifecycle"]["post_completion_open_position_count"] == 1


def test_delayed_close_is_not_visible_before_its_observation_time(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    close_time = START + timedelta(minutes=1)
    observed_at = START + timedelta(minutes=2)
    delayed_quote = QuoteSnapshot(
        source_record_id="delayed-close-quote",
        source="test-executable-quotes",
        revision_id="original",
        event_time=close_time,
        available_time=observed_at,
        ingested_time=observed_at,
        bid=150.50,
        ask=150.52,
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="delayed-close"),
            quote=_quote(record_id="delayed-close-open"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        closed = store.close_trade(
            TradeCloseRequest(
                trade_id=str(opened["trade_id"]),
                close_time=close_time,
                quote=delayed_quote,
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
                historical_replay=True,
                observed_at=observed_at,
            )
        )
        before_known = store.snapshot(now=close_time)
        when_known = store.snapshot(now=observed_at)

    assert closed["inserted"] is True
    assert before_known["open_position_count"] == 1
    assert before_known["closed_trade_count"] == 0
    assert before_known["balance_jpy"] == policy.initial_capital_jpy
    assert before_known["trades"][0]["status"] == "open"
    assert before_known["reconciliation"]["passed"] is True
    assert when_known["open_position_count"] == 0
    assert when_known["closed_trade_count"] == 1
    assert when_known["balance_jpy"] == policy.initial_capital_jpy + int(closed["net_pnl_jpy"])
    assert when_known["trades"][0]["status"] == "closed"
    assert when_known["trades"][0]["close_known_at"] == observed_at.isoformat()
    assert when_known["trades"][0]["close_known_at_basis"] == ("historical_close_observed_at")


def test_position_mark_updates_equity_without_changing_cash(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    mark_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy),
            quote=_quote(),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        request = PositionMarkRequest(
            trade_id=str(opened["trade_id"]),
            mark_time=mark_time,
            quote=_quote(
                moment=mark_time,
                bid=150.50,
                ask=150.52,
                record_id="mark-1",
            ),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        marked = store.record_position_mark(request)
        assert marked["inserted"] is True
        assert store.record_position_mark(request)["inserted"] is False
        delayed_retry = PositionMarkRequest(
            trade_id=request.trade_id,
            mark_time=request.mark_time,
            quote=request.quote,
            quote_to_jpy_rate=request.quote_to_jpy_rate,
            quote_conversion_source=request.quote_conversion_source,
            observed_at=mark_time + timedelta(minutes=5),
        )
        assert store.record_position_mark(delayed_retry)["inserted"] is False
        snapshot = store.snapshot(now=mark_time)

        assert snapshot["cash_jpy"] == 1_000_000
        assert snapshot["balance_jpy"] == 1_000_000
        assert snapshot["unrealized_pnl_jpy"] == 4_800
        assert snapshot["equity_jpy"] == 1_004_800
        assert snapshot["equity_drawdown_jpy"] == 0
        assert snapshot["equity_drawdown_pct"] == 0
        assert snapshot["risk_gate"]["remaining_equity_drawdown_budget_jpy"] == 100_000
        assert snapshot["equity_valuation_status"] == ("delayed_executable_marks_complete")
        assert snapshot["data_quality_state"]["position_marks_complete"] is True
        assert snapshot["open_positions"][0]["mark_spread_quote_cost_jpy"] == 200
        assert snapshot["trades"][0]["valuation_status"] == "delayed_executable_mark"
        assert snapshot["trades"][0]["unrealized_pnl_jpy"] == 4_800
        with pytest.raises(ImmutableVirtualRecordConflict):
            store.record_position_mark(
                PositionMarkRequest(
                    trade_id=str(opened["trade_id"]),
                    mark_time=mark_time,
                    quote=_quote(
                        moment=mark_time,
                        bid=150.40,
                        ask=150.42,
                        record_id="mark-conflict",
                    ),
                    quote_to_jpy_rate=1,
                    quote_conversion_source="JPY quote currency",
                )
            )


def test_historical_snapshot_excludes_future_records(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    future = START + timedelta(days=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(
                policy,
                decision_id="future-decision",
                prediction_time=future,
            ),
            quote=_quote(moment=future, record_id="future-quote"),
            observed_at=future,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        assert opened["disposition"] == "opened"
        historical = store.snapshot(now=START)
        current = store.snapshot(now=future)

    assert historical["open_position_count"] == 0
    assert historical["recent_decisions"] == []
    assert historical["trades"] == []
    assert current["open_position_count"] == 1
    assert len(current["recent_decisions"]) == 1


def test_missing_execution_evidence_records_abstention(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    decision = _decision(policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.process_decision(decision, observed_at=START)
        assert result["disposition"] == "abstained"
        assert "実行可能bid/askが無い" in result["rejection_reasons"]
        snapshot = store.snapshot(now=START)

    assert snapshot["open_position_count"] == 0
    assert snapshot["recent_decisions"][0]["pit_eligible"] is True
    assert snapshot["recent_decisions"][0]["disposition"] == "abstained"


def test_open_close_reconciles_net_pnl_and_failure_evidence(tmp_path, monkeypatch) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy),
            quote=_quote(),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        assert opened["disposition"] == "opened"
        trade_id = str(opened["trade_id"])
        assert store.snapshot(now=START)["open_position_count"] == 1
        # P0-D: a learning label requires an observed path. Without a mark the
        # close is recorded but excluded from learning as `sparse_path_marks`.
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=trade_id,
                mark_time=START + timedelta(seconds=30),
                quote=_quote(
                    moment=START + timedelta(seconds=30),
                    bid=150.20,
                    ask=150.22,
                    record_id="mark-reconcile-1",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
        )
        closed = store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=START + timedelta(minutes=1),
                quote=_quote(
                    moment=START + timedelta(minutes=1),
                    bid=150.50,
                    ask=150.52,
                    record_id="quote-2",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
                slippage_cost_jpy=100,
                commission_jpy=50,
                financing_jpy=25,
                conversion_cost_jpy=25,
            )
        )
        snapshot = store.snapshot(now=START + timedelta(minutes=1))
        learning = store.export_learning_rows(
            as_of=START + timedelta(minutes=1),
            record_export=True,
        )

    attribution = closed["attribution"]
    assert attribution["identity"] == (
        "net_pnl_jpy = gross_market_pnl_jpy - spread_quote_cost_jpy "
        "- slippage_cost_jpy - commission_jpy - financing_jpy "
        "- conversion_cost_jpy"
    )
    assert attribution["gross_market_pnl_jpy"] == 5_000
    assert attribution["spread_quote_cost_jpy"] == 200
    assert attribution["slippage_cost_jpy"] == 100
    assert attribution["commission_jpy"] == 50
    assert attribution["financing_jpy"] == 25
    assert attribution["conversion_cost_jpy"] == 25
    assert attribution["net_pnl_jpy"] == 4_600
    assert attribution["cost_contract"] == {
        "cost_model_id": "explicit-test-costs-v1",
        "cost_model_version": "1",
        "cost_source": "test-fixture",
        "costs_complete": True,
    }
    assert attribution["execution_observation_mode"] == "contemporaneous_quote"
    assert attribution["replay_evidence"] == {}
    counterfactuals = attribution["counterfactuals"]
    assert counterfactuals["causal_claim"] is False
    assert counterfactuals["no_trade"]["net_pnl_jpy"] == 0
    assert counterfactuals["opposite_direction"]["net_pnl_jpy"] < 0
    assert counterfactuals["decision_mid_fill"]["net_pnl_jpy"] == 4_800
    assert counterfactuals["fixed_time_exit"]["status"] == "available"
    assert counterfactuals["alternative_stop_target"]["status"] == "unavailable"
    assert closed["failures"] == []
    assert snapshot["balance_jpy"] == 1_004_600
    assert snapshot["today_pnl_jpy"] == 4_600
    assert snapshot["open_position_count"] == 0
    assert snapshot["trades"][0]["net_pnl_jpy"] == 4_600
    assert snapshot["reconciliation"]["passed"] is True
    assert snapshot["cumulative_net_pnl_jpy"] == 4_600
    assert snapshot["remaining_daily_target_jpy"] == 65_400
    assert snapshot["data_quality_state"]["daily_stop_active"] is False
    assert snapshot["data_quality_state"]["risk_block_active"] is False
    assert snapshot["balance_curve"][-1]["balance_jpy"] == 1_004_600
    assert learning["row_count"] == 1
    assert learning["promotion_eligible"] is False
    assert learning["rows"][0]["research_only"] is True
    canonical = learning["rows"][0]["canonical_outcome"]
    assert canonical_net_label_contract_flags(canonical) == ()
    assert canonical["entry_bid"] == 150.0
    assert canonical["entry_ask"] == 150.02
    assert canonical["exit_bid"] == 150.50
    assert canonical["exit_ask"] == 150.52
    assert canonical["conversion_r"] == 25 / 5_000
    assert learning["canonical_net_r"]["net_label_coverage"] == 1.0

    canonical_store = tmp_path / "canonical_outcomes.jsonl"
    evidence_store = tmp_path / "canonical_label_evidence.jsonl"
    first_connection = connect_canonical_outcomes(
        database=path,
        store_path=canonical_store,
        as_of=START + timedelta(minutes=1),
        label_evidence_store_path=evidence_store,
    )
    second_connection = connect_canonical_outcomes(
        database=path,
        store_path=canonical_store,
        as_of=START + timedelta(minutes=1),
        label_evidence_store_path=evidence_store,
    )
    assert first_connection["coverage"] == 1.0
    assert first_connection["appended_rows"] == 1
    assert second_connection["appended_rows"] == 0
    assert len(load_canonical_outcomes(canonical_store)) == 1
    assert first_connection["label_evidence"]["coverage"] == 1.0
    assert first_connection["label_evidence"]["appended_rows"] == 1
    assert first_connection["label_evidence"]["phase2_verified_path_rows"] == 0
    assert second_connection["label_evidence"]["appended_rows"] == 0
    stored_evidence = load_canonical_label_evidence(evidence_store)
    assert len(stored_evidence) == 1
    assert stored_evidence[0].label_quality == "incomplete"

    from tools import virtual_portfolio_canonical_outcomes as connector_module

    def _unexpected_evidence_builder(*_args, **_kwargs):
        raise AssertionError("outcome-only connector must not build sidecar evidence")

    monkeypatch.setattr(
        connector_module,
        "canonical_label_evidence_from_virtual_row",
        _unexpected_evidence_builder,
    )
    outcome_only = connect_canonical_outcomes(
        database=path,
        store_path=tmp_path / "outcome_only.jsonl",
        as_of=START + timedelta(minutes=1),
        label_evidence_store_path=None,
    )
    assert outcome_only["coverage"] == 1.0
    assert "label_evidence" not in outcome_only
    connected_snapshot = portfolio_snapshot(path, now=START + timedelta(minutes=1))
    connected_summary = connected_snapshot["learning"]["canonical_net_r"]
    assert connected_summary["status"] == "connected"
    assert connected_summary["connected_rows"] == 1
    assert connected_summary["net_label_coverage"] == 1.0

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER simulated_trade_closes_no_update")
        connection.execute(
            "UPDATE simulated_trade_closes SET exit_bid = exit_bid - 0.01 WHERE trade_id = ?",
            (trade_id,),
        )
    with open_virtual_portfolio_reader(path) as store:
        with pytest.raises(VirtualPortfolioError, match="close record hash mismatch"):
            store.export_learning_rows(as_of=START + timedelta(minutes=1))


@pytest.mark.parametrize("collision", ["outcome", "evidence"])
def test_canonical_connector_rejects_database_output_collision_without_mutation(
    tmp_path,
    collision,
) -> None:
    database = tmp_path / "virtual.sqlite3"
    outcome_store = database if collision == "outcome" else tmp_path / "outcomes.jsonl"
    evidence_store = database if collision == "evidence" else tmp_path / "evidence.jsonl"
    original = b"not-opened-or-mutated\n"
    database.write_bytes(original)

    with pytest.raises(ValueError, match="must differ|must be distinct"):
        connect_canonical_outcomes(
            database=database,
            store_path=outcome_store,
            label_evidence_store_path=evidence_store,
            as_of=START,
        )

    assert database.read_bytes() == original


def test_required_daily_profit_task_does_not_block_new_capacity(tmp_path) -> None:
    path = tmp_path / "virtual.sqlite3"
    policy = VirtualPortfolioPolicy(daily_target_jpy=1_000)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.initialize_portfolio(policy=policy, created_at=START)
        first = store.process_decision(
            _decision(policy, decision_id="reference-win"),
            quote=_quote(record_id="reference-entry"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(first["trade_id"]),
                close_time=START + timedelta(minutes=1),
                quote=_quote(
                    moment=START + timedelta(minutes=1),
                    bid=150.50,
                    ask=150.52,
                    record_id="reference-exit",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
        snapshot = store.snapshot(now=START + timedelta(minutes=1))
        second = store.process_decision(
            _decision(
                policy,
                decision_id="after-reference",
                prediction_time=START + timedelta(minutes=2),
            ),
            quote=_quote(
                moment=START + timedelta(minutes=2),
                record_id="after-reference-entry",
            ),
            observed_at=START + timedelta(minutes=2),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert snapshot["target_met"] is True
    assert snapshot["daily_profit_task"]["required"] is True
    assert snapshot["daily_profit_task"]["status"] == "completed"
    assert snapshot["daily_profit_task"]["remaining_jpy"] == 0
    assert snapshot["data_quality_state"]["daily_target_stop_active"] is False
    assert snapshot["data_quality_state"]["daily_target_reference_met"] is True
    assert snapshot["data_quality_state"]["daily_target_task_completed"] is True
    assert snapshot["risk_gate"]["can_open"] is True
    assert second["disposition"] == "opened"


def test_legacy_unassigned_close_is_excluded_from_intraday_learning_and_kpi(
    tmp_path,
) -> None:
    path, policy = _initialized(tmp_path)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy, decision_id="legacy-close"),
            quote=_quote(record_id="legacy-entry"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        # P0-D: observe the path so this close stays learning-eligible; the
        # exclusion under test is the missing horizon assignment, not sparsity.
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=str(opened["trade_id"]),
                mark_time=START + timedelta(seconds=30),
                quote=_quote(
                    moment=START + timedelta(seconds=30),
                    bid=150.20,
                    ask=150.22,
                    record_id="legacy-mark",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(opened["trade_id"]),
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="legacy-exit",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE decision_horizon_assignments")
        connection.execute("DROP TABLE session_close_completions")
        connection.execute("DROP TABLE simulated_trade_close_observations")
        connection.execute("DROP TABLE simulated_trade_open_observations")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()

    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        learning = store.export_learning_rows(as_of=close_time)
        snapshot = store.snapshot(now=close_time)

    assert learning["contract"] == "virtual-portfolio-learning-pit-v3"
    assert learning["row_count"] == 0
    assert learning["excluded_legacy_or_observer_rows"] == 1
    assert snapshot["learning"]["eligible_rows"] == 0
    assert snapshot["research_kpis"]["rolling_20d"]["trade_count"] == 0
    assert snapshot["research_kpis"]["rolling_20d"]["excluded_from_primary"]["trade_count"] == 1
    assert snapshot["trades"][0]["open_known_at"] == START.isoformat()
    assert snapshot["trades"][0]["open_known_at_basis"] == "legacy_provenance_backfill"
    assert snapshot["trades"][0]["close_known_at"] == close_time.isoformat()
    assert snapshot["trades"][0]["close_known_at_basis"] == "legacy_provenance_backfill"


def test_loss_classification_and_daily_stop_are_fail_closed(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        first = store.process_decision(
            _decision(policy, decision_id="loss-1"),
            quote=_quote(record_id="open-loss"),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        loss = store.close_trade(
            TradeCloseRequest(
                trade_id=str(first["trade_id"]),
                close_time=START + timedelta(minutes=1),
                quote=_quote(
                    moment=START + timedelta(minutes=1),
                    bid=148.48,
                    ask=148.50,
                    record_id="close-loss",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="stop_gap",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )
        second = store.process_decision(
            _decision(
                policy,
                decision_id="after-stop",
                prediction_time=START + timedelta(minutes=2),
            ),
            quote=_quote(
                moment=START + timedelta(minutes=2),
                record_id="after-stop-quote",
            ),
            observed_at=START + timedelta(minutes=2),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert loss["net_pnl_jpy"] == -15_400
    assert {row["code"] for row in loss["failures"]} >= {
        "wrong_direction",
        "stop_loss",
        "high_conviction_loss",
    }
    assert second["disposition"] == "abstained"
    assert "日次損失上限に到達" in second["rejection_reasons"]


def test_duplicate_decision_conflict_and_missing_db_snapshot(tmp_path) -> None:
    missing = portfolio_snapshot(tmp_path / "missing.sqlite3", now=START)
    assert missing["configured"] is False
    assert missing["risk_gate"]["can_open"] is False

    path, policy = _initialized(tmp_path)
    decision = _decision(policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        first = store.process_decision(decision, observed_at=START)
        retry = store.process_decision(decision, observed_at=START)
        assert first["inserted"] is True
        assert retry["inserted"] is False
        conflicting = {**decision, "reason": "changed after the fact"}
        with pytest.raises(ImmutableVirtualRecordConflict):
            store.process_decision(conflicting, observed_at=START)


def test_close_rejects_missing_cost_contract_and_conflicting_retry(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy),
            quote=_quote(),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        base = {
            "trade_id": str(opened["trade_id"]),
            "close_time": START + timedelta(minutes=1),
            "quote": _quote(
                moment=START + timedelta(minutes=1),
                bid=150.50,
                ask=150.52,
                record_id="quote-close",
            ),
            "quote_to_jpy_rate": 1,
            "quote_conversion_source": "JPY quote currency",
            "close_reason": "time_exit",
        }
        with pytest.raises(InvalidVirtualRecord, match="complete cost evidence"):
            store.close_trade(TradeCloseRequest(**base))
        request = TradeCloseRequest(
            **base,
            cost_model_id="explicit-test-costs-v1",
            cost_model_version="1",
            cost_source="test-fixture",
            costs_complete=True,
        )
        first = store.close_trade(request)
        retry = store.close_trade(request)
        assert first["inserted"] is True
        assert retry["inserted"] is False
        with pytest.raises(ImmutableVirtualRecordConflict):
            store.close_trade(
                TradeCloseRequest(
                    **{
                        **base,
                        "close_reason": "changed_after_close",
                    },
                    cost_model_id="explicit-test-costs-v1",
                    cost_model_version="1",
                    cost_source="test-fixture",
                    costs_complete=True,
                )
            )


def test_store_itself_rejects_weekend_open(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    weekend = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.process_decision(
            _decision(
                policy,
                decision_id="weekend",
                prediction_time=weekend,
            ),
            quote=_quote(
                moment=weekend,
                record_id="weekend-quote",
            ),
            observed_at=weekend,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert result["disposition"] == "abstained"
    assert "市場休場中" in result["rejection_reasons"]


def test_mature_loss_memory_vetoes_only_the_same_future_segment(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    moments = [
        START,
        START + timedelta(days=1),
        START + timedelta(days=5),
    ]
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        for index, moment in enumerate(moments):
            opened = store.process_decision(
                _decision(
                    policy,
                    decision_id=f"learned-loss-{index}",
                    prediction_time=moment,
                ),
                quote=_quote(moment=moment, record_id=f"loss-entry-{index}"),
                observed_at=moment,
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
            assert opened["disposition"] == "opened"
            # P0-D: only labelled losses feed the safety gate, so the path must
            # be observed for these fixtures to remain learning-eligible.
            store.record_position_mark(
                PositionMarkRequest(
                    trade_id=str(opened["trade_id"]),
                    mark_time=moment + timedelta(seconds=30),
                    quote=_quote(
                        moment=moment + timedelta(seconds=30),
                        bid=149.995,
                        ask=150.015,
                        record_id=f"loss-mark-{index}",
                    ),
                    quote_to_jpy_rate=1,
                    quote_conversion_source="JPY quote currency",
                )
            )
            store.close_trade(
                TradeCloseRequest(
                    trade_id=str(opened["trade_id"]),
                    close_time=moment + timedelta(minutes=1),
                    quote=_quote(
                        moment=moment + timedelta(minutes=1),
                        bid=149.99,
                        ask=150.01,
                        record_id=f"loss-exit-{index}",
                    ),
                    quote_to_jpy_rate=1,
                    quote_conversion_source="JPY quote currency",
                    close_reason="time_exit",
                    cost_model_id="explicit-test-costs-v1",
                    cost_model_version="1",
                    cost_source="test-fixture",
                    costs_complete=True,
                )
            )
        next_moment = START + timedelta(days=6)
        next_trade = store.process_decision(
            _decision(
                policy,
                decision_id="learned-veto",
                prediction_time=next_moment,
            ),
            quote=_quote(moment=next_moment, record_id="learned-veto-entry"),
            observed_at=next_moment,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert next_trade["disposition"] == "abstained"
    assert any(reason.startswith("学習安全ゲート:") for reason in next_trade["rejection_reasons"])


def test_sizing_invariants_quarantine_the_production_denominator_collapse() -> None:
    """Reproduce sim-718c586007c93eec140f9dc6 exactly (2026-08-06 production).

    The stop sat 0.491 pips from entry against a 0.400 pip spread, so a normal
    22.7 pip move was reported as +46.4R. See
    docs/audits/NET_R_OUTLIER_FORENSIC_20260806.md.
    """

    assessment = classify_sizing_invariants(
        executable=Decimal("160.285"),
        stop=Decimal("160.289908884407"),
        target=Decimal("160.058091115593"),
        bid=Decimal("160.285"),
        ask=Decimal("160.289"),
        units=Decimal("964170"),
        conversion_rate=Decimal("1"),
        planned_risk_jpy=Decimal("4733"),
        equity_jpy=Decimal("1000000"),
    )

    assert assessment["canonical"] is False
    assert assessment["allocation_stream"] == "quarantine_shadow"
    assert assessment["denominator_unstable"] is True
    # All three guards fire on this trade: the stop sat 0.491 pips from entry
    # against a 0.400 pip spread, which forced 154x gross leverage and a
    # planned reward-to-risk of 46.2.
    assert "microstructure_floor" in assessment["violations"]
    assert "gross_leverage" in assessment["violations"]
    assert "planned_reward_to_risk" in assessment["violations"]


def test_sizing_invariants_accept_a_normal_stop() -> None:
    """A stop well outside the spread stays canonical."""

    assessment = classify_sizing_invariants(
        executable=Decimal("150.00"),
        stop=Decimal("149.80"),
        target=Decimal("150.30"),
        bid=Decimal("150.00"),
        ask=Decimal("150.02"),
        units=Decimal("10000"),
        conversion_rate=Decimal("1"),
        planned_risk_jpy=Decimal("2000"),
        equity_jpy=Decimal("1000000"),
    )

    assert assessment["canonical"] is True
    assert assessment["allocation_stream"] == "canonical"
    assert assessment["denominator_unstable"] is False
    assert assessment["violations"] == ()


def test_sizing_invariants_accept_an_ordinary_tight_stop() -> None:
    """A 5 pip EURUSD stop must stay canonical.

    Regression guard for a calibration error found by the 2026-08-06 dry-run: a
    notional/planned-risk ceiling reduces algebraically to `price /
    risk_distance`, so it rejected ordinary tight stops and quarantined 32 of 50
    production trades. Gross leverage against equity is the correct measure.
    """

    assessment = classify_sizing_invariants(
        executable=Decimal("1.15000"),
        stop=Decimal("1.14950"),
        target=Decimal("1.15100"),
        bid=Decimal("1.14999"),
        ask=Decimal("1.15001"),
        units=Decimal("40000"),
        conversion_rate=Decimal("150"),
        planned_risk_jpy=Decimal("3000"),
        equity_jpy=Decimal("1000000"),
    )

    assert assessment["canonical"] is True
    assert assessment["violations"] == ()


def test_sizing_invariants_reject_non_positive_risk_distance() -> None:
    assessment = classify_sizing_invariants(
        executable=Decimal("150.00"),
        stop=Decimal("150.00"),
        target=Decimal("150.30"),
        bid=Decimal("150.00"),
        ask=Decimal("150.02"),
        units=Decimal("10000"),
        conversion_rate=Decimal("1"),
        planned_risk_jpy=Decimal("2000"),
        equity_jpy=Decimal("1000000"),
    )

    assert assessment["canonical"] is False
    assert assessment["denominator_unstable"] is True
    assert "risk_distance_not_positive" in assessment["violations"]


def test_robust_summary_detects_single_sample_domination() -> None:
    """The production 50-trade shape: mean positive, every robust stat negative."""

    values = [46.406085] + [-0.5] * 49
    summary = summarize_net_r_robust(values)

    assert summary["count"] == 50
    assert summary["mean_unsafe"] > 0
    assert summary["median"] < 0
    assert summary["trimmed_mean_10pct"] < 0
    assert summary["winsorized_mean_5r"] < 0
    assert summary["single_sample_dominated"] is True


def test_robust_summary_is_stable_without_outliers() -> None:
    summary = summarize_net_r_robust([-0.5, -0.4, -0.6, -0.5, -0.45])

    assert summary["single_sample_dominated"] is False
    assert summary["median"] < 0
    assert summary["winsorized_mean_5r"] == summary["mean_unsafe"]


def test_robust_summary_handles_empty_input() -> None:
    summary = summarize_net_r_robust([])

    assert summary["count"] == 0
    assert summary["mean_unsafe"] is None
    assert summary["single_sample_dominated"] is False


def _open_and_close(
    store,
    policy,
    *,
    decision_id: str,
    moment: datetime,
    stop: float,
    target: float,
    bid: float,
    ask: float,
    exit_bid: float,
    exit_ask: float,
    with_mark: bool = True,
) -> dict:
    """Open, optionally observe, and close one trade. Returns the close result."""

    decision = _decision(policy, decision_id=decision_id, prediction_time=moment)
    decision["stop"] = stop
    decision["target1"] = target
    opened = store.process_decision(
        decision,
        quote=_quote(moment=moment, bid=bid, ask=ask, record_id=f"entry-{decision_id}"),
        observed_at=moment,
        quote_to_jpy_rate=1,
        quote_conversion_source="JPY quote currency",
    )
    assert opened["disposition"] == "opened", opened.get("rejection_reasons")
    trade_id = str(opened["trade_id"])
    if with_mark:
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=trade_id,
                mark_time=moment + timedelta(seconds=30),
                quote=_quote(
                    moment=moment + timedelta(seconds=30),
                    bid=bid,
                    ask=ask,
                    record_id=f"mark-{decision_id}",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
        )
    store.close_trade(
        TradeCloseRequest(
            trade_id=trade_id,
            close_time=moment + timedelta(minutes=1),
            quote=_quote(
                moment=moment + timedelta(minutes=1),
                bid=exit_bid,
                ask=exit_ask,
                record_id=f"exit-{decision_id}",
            ),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
            close_reason="target1",
            cost_model_id="explicit-test-costs-v1",
            cost_model_version="1",
            cost_source="test-fixture",
            costs_complete=True,
        )
    )
    return dict(opened)


def test_denominator_unstable_trade_is_rejected_before_every_evaluation_surface(
    tmp_path,
) -> None:
    """A collapsed stop denominator must never create a simulated open."""

    baseline_path, policy = _initialized(tmp_path / "baseline")
    with open_virtual_portfolio_writer(baseline_path, writer_id="test") as store:
        _open_and_close(
            store,
            policy,
            decision_id="canonical-1",
            moment=START,
            stop=149.50,
            target=150.50,
            bid=150.00,
            ask=150.02,
            exit_bid=150.30,
            exit_ask=150.32,
        )
        baseline_learning = store.export_learning_rows(as_of=START + timedelta(minutes=5))
        baseline_snapshot = store.snapshot(now=START + timedelta(minutes=5))

    with_outlier_path, policy2 = _initialized(tmp_path / "with_outlier")
    with open_virtual_portfolio_writer(with_outlier_path, writer_id="test") as store:
        _open_and_close(
            store,
            policy2,
            decision_id="canonical-1",
            moment=START,
            stop=149.50,
            target=150.50,
            bid=150.00,
            ask=150.02,
            exit_bid=150.30,
            exit_ask=150.32,
        )
        decision = _decision(
            policy2,
            decision_id="outlier-1",
            prediction_time=START + timedelta(minutes=2),
        )
        decision["stop"] = 150.015
        decision["target1"] = 150.10
        outlier = store.process_decision(
            decision,
            quote=_quote(
                moment=START + timedelta(minutes=2),
                bid=150.00,
                ask=150.02,
                record_id="entry-outlier-1",
            ),
            observed_at=START + timedelta(minutes=2),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        outlier_learning = store.export_learning_rows(as_of=START + timedelta(minutes=5))
        outlier_snapshot = store.snapshot(now=START + timedelta(minutes=5))

    assert outlier["disposition"] == "abstained"
    assert outlier["trade_id"] is None
    assert any(
        "sizing invariant rejected before open" in row for row in outlier["rejection_reasons"]
    )
    with sqlite3.connect(with_outlier_path) as raw:
        assert int(raw.execute("SELECT COUNT(*) FROM simulated_trade_opens").fetchone()[0]) == 1

    # Learning export: identical row count and identical net-R values.
    assert outlier_learning["row_count"] == baseline_learning["row_count"]
    assert [row["canonical_outcome"]["realized_net_r"] for row in outlier_learning["rows"]] == [
        row["canonical_outcome"]["realized_net_r"] for row in baseline_learning["rows"]
    ]

    # Canonical KPI: the outlier's P&L must not enter research expectancy, and
    # must not inflate the mature learning population.
    baseline_kpi = baseline_snapshot["research_kpis"]
    outlier_kpi = outlier_snapshot["research_kpis"]
    assert (
        outlier_kpi["rolling_20d"]["cost_adjusted_expectancy_net_r"]
        == baseline_kpi["rolling_20d"]["cost_adjusted_expectancy_net_r"]
    )
    assert outlier_kpi["rolling_20d"]["net_r"] == baseline_kpi["rolling_20d"]["net_r"]
    assert outlier_kpi["rolling_20d"]["trade_count"] == baseline_kpi["rolling_20d"]["trade_count"]
    assert outlier_kpi["mature_learning_rows"] == baseline_kpi["mature_learning_rows"]

    # Robust net-R aggregation over the canonical population is unchanged.
    assert summarize_net_r_robust(
        row["canonical_outcome"]["realized_net_r"] for row in outlier_learning["rows"]
    ) == summarize_net_r_robust(
        row["canonical_outcome"]["realized_net_r"] for row in baseline_learning["rows"]
    )


def test_rejected_sizing_attempts_do_not_arm_the_safety_gate(tmp_path) -> None:

    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        for index in range(3):
            moment = START + timedelta(days=index)
            decision = _decision(
                policy,
                decision_id=f"quarantined-loss-{index}",
                prediction_time=moment,
            )
            decision["stop"] = 150.015
            decision["target1"] = 150.10
            rejected = store.process_decision(
                decision,
                quote=_quote(moment=moment, record_id=f"rejected-{index}"),
                observed_at=moment,
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
            assert rejected["disposition"] == "abstained"
        next_moment = START + timedelta(days=5)
        following = store.process_decision(
            _decision(policy, decision_id="after-quarantined", prediction_time=next_moment),
            quote=_quote(moment=next_moment, record_id="after-quarantined-entry"),
            observed_at=next_moment,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )

    assert not any(
        reason.startswith("学習安全ゲート:") for reason in following["rejection_reasons"]
    )
    assert following["disposition"] == "opened"


def test_label_quality_reports_the_largest_unobserved_gap(tmp_path) -> None:
    """The metric must expose how long the path went unwatched.

    A high sampling ratio does not prove the path was observed: the production
    +46.4R trade survived its stop by 0.09 pips between two marks. Recording the
    largest gap keeps that limitation visible to any consumer of the label.
    """

    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision = _decision(policy, decision_id="gap-1")
        opened = store.process_decision(
            decision,
            quote=_quote(),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        trade_id = str(opened["trade_id"])
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=trade_id,
                mark_time=START + timedelta(seconds=10),
                quote=_quote(moment=START + timedelta(seconds=10), record_id="gap-mark"),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=START + timedelta(minutes=1),
                quote=_quote(
                    moment=START + timedelta(minutes=1),
                    bid=150.10,
                    ask=150.12,
                    record_id="gap-exit",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="target1",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
            )
        )

    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        stored = raw.execute(
            "SELECT attribution_json FROM simulated_trade_closes WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    quality = json.loads(stored["attribution_json"])["label_quality"]

    assert quality["contract"] == "virtual-trade-label-quality-v2"
    assert quality["mark_count"] == 1
    # open -> mark is 10s, mark -> close is 50s, so the largest gap is 50s.
    assert quality["largest_observation_gap_seconds"] == 50.0
    # The name and the disclaimer must both survive refactors.
    assert "path_sampling_adequacy" in quality
    assert "mark_coverage" not in quality
    assert "does_not_guarantee" in quality


def test_session_end_close_is_ambiguous_and_not_learning_eligible(tmp_path) -> None:
    """Session bookkeeping exits do not reflect the hypothesis under test."""

    quality = _classify_label_quality(
        mark_count=10,
        holding_seconds=60.0,
        close_reason="session_end",
        sizing_invariants={"canonical": True, "denominator_unstable": False},
        largest_gap_seconds=6.0,
    )

    assert quality["path_evidence"] == "sufficient"
    assert quality["learning_eligible"] is False
    assert "ambiguous_close" in quality["exclusion_reasons"]


def test_backfilled_verdict_excludes_legacy_trades_from_evaluation(tmp_path) -> None:
    """A trade opened before P0-C must still be excluded once backfilled.

    `simulated_trade_closes.learning_eligible` is append-only, so legacy rows
    keep a stale 1. The backfilled sidecar has to override that, otherwise the
    production +46.4R trade keeps reaching learning export and KPIs.
    """

    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _open_and_close(
            store,
            policy,
            decision_id="legacy-unstable",
            moment=START,
            stop=149.50,
            target=150.50,
            bid=150.00,
            ask=150.02,
            exit_bid=150.10,
            exit_ask=150.12,
        )

    # Simulate a pre-P0-C row: drop the sidecar verdict and force the stored
    # column back to eligible, which is exactly the legacy on-disk shape.
    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        raw.execute("DROP TRIGGER simulated_trade_opens_no_update")
        raw.execute("DROP TRIGGER simulated_trade_closes_no_update")
        raw.execute("DROP TRIGGER simulated_trade_sizing_assessments_no_delete")
        raw.execute("DELETE FROM simulated_trade_sizing_assessments")
        raw.execute("UPDATE simulated_trade_opens SET stop_price = 150.015, target_price = 150.10")
        opened = raw.execute("SELECT * FROM simulated_trade_opens").fetchone()
        open_payload = {key: opened[key] for key in _OPEN_INSERT_KEYS if key != "record_sha256"}
        raw.execute(
            "UPDATE simulated_trade_opens SET record_sha256 = ?",
            (_record_hash(open_payload),),
        )
        raw.execute("UPDATE simulated_trade_closes SET learning_eligible = 1")
        raw.execute("PRAGMA user_version=8")
        raw.commit()

    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        assert (
            int(raw.execute("SELECT learning_eligible FROM simulated_trade_closes").fetchone()[0])
            == 1
        )
        assert (
            int(
                raw.execute("SELECT COUNT(*) FROM simulated_trade_sizing_assessments").fetchone()[0]
            )
            == 0
        )

    # Reopening runs the backfill, which must restore the exclusion.
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        exported = store.export_learning_rows(as_of=START + timedelta(minutes=5))

    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        verdict = raw.execute(
            "SELECT canonical, denominator_unstable FROM simulated_trade_sizing_assessments"
        ).fetchone()

    assert int(verdict["canonical"]) == 0
    assert int(verdict["denominator_unstable"]) == 1
    assert exported["row_count"] == 0


def test_backfill_is_idempotent_and_leaves_source_rows_untouched(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _open_and_close(
            store,
            policy,
            decision_id="stable-1",
            moment=START,
            stop=149.50,
            target=150.50,
            bid=150.00,
            ask=150.02,
            exit_bid=150.30,
            exit_ask=150.32,
        )

    def _fingerprint() -> tuple[str, ...]:
        with sqlite3.connect(path) as raw:
            raw.row_factory = sqlite3.Row
            return tuple(
                str(row["record_sha256"])
                for row in raw.execute(
                    "SELECT record_sha256 FROM simulated_trade_opens ORDER BY trade_id"
                )
            )

    before = _fingerprint()
    for _ in range(3):
        with open_virtual_portfolio_writer(path, writer_id="test"):
            pass

    with sqlite3.connect(path) as raw:
        rows = int(
            raw.execute("SELECT COUNT(*) FROM simulated_trade_sizing_assessments").fetchone()[0]
        )

    assert rows == 1
    assert _fingerprint() == before


def _anchor_payload(**overrides: object) -> dict:
    payload: dict = {
        "contract": "decision-price-anchor-v1",
        "status": "anchored",
        "anchor_basis": "first_canonical_tick_at_or_after_decision",
        "canonical_source": "dukascopy",
        "entry": "150.02",
        "stop": "149.52",
        "target": "150.52",
        "risk_distance": "0.5",
        "spread_at_anchor": "0.02",
        "risk_distance_to_spread": "25",
        "anchor_event_time": (START + timedelta(seconds=2)).isoformat(),
        "anchor_lag_seconds": 2.0,
        "stop_atr_multiple": "2.5",
        "target_rr": "1.0",
        "reference_atr": "0.2",
    }
    payload.update(overrides)
    return payload


def _one_decision(store, policy) -> str:
    decision = _decision(policy, decision_id="anchor-1")
    store.process_decision(
        decision,
        quote=_quote(),
        observed_at=START,
        quote_to_jpy_rate=1,
        quote_conversion_source="JPY quote currency",
    )
    return str(decision["decision_id"])


def test_decision_price_anchor_round_trips(tmp_path) -> None:
    """S1: the canonical-frame verdict is stored beside the decision."""

    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        result = store.record_decision_price_anchor(decision_id, _anchor_payload())
        loaded = store.load_decision_price_anchor(decision_id)

    assert result == {
        "inserted": True,
        "decision_id": decision_id,
        "anchor_status": "anchored",
    }
    assert loaded is not None
    assert loaded["entry"] == "150.02"
    assert loaded["canonical_source"] == "dukascopy"


def test_rejected_anchors_are_recorded_not_discarded(tmp_path) -> None:
    """The rejection count is the S1 measurement; dropping it destroys it."""

    path, policy = _initialized(tmp_path)
    rejected = {
        "contract": "decision-price-anchor-v1",
        "status": "rejected",
        "reason": "microstructure_floor",
        "risk_distance": "0.00001",
        "spread": "0.0002",
    }
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        store.record_decision_price_anchor(decision_id, rejected)
        loaded = store.load_decision_price_anchor(decision_id)

    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        row = raw.execute(
            "SELECT anchor_status, rejection_reason, entry FROM decision_price_anchors"
        ).fetchone()

    assert loaded is not None and loaded["status"] == "rejected"
    assert row["anchor_status"] == "rejected"
    assert row["rejection_reason"] == "microstructure_floor"
    # A rejected anchor has no levels; the column must stay NULL rather than "".
    assert row["entry"] is None


def test_recording_the_same_anchor_twice_is_idempotent(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        first = store.record_decision_price_anchor(decision_id, _anchor_payload())
        second = store.record_decision_price_anchor(decision_id, _anchor_payload())

    with sqlite3.connect(path) as raw:
        rows = int(raw.execute("SELECT COUNT(*) FROM decision_price_anchors").fetchone()[0])

    assert first["inserted"] is True
    assert second["inserted"] is False
    assert rows == 1


def test_a_conflicting_anchor_for_the_same_decision_is_refused(tmp_path) -> None:
    """Anchoring resolves once and is immutable afterwards."""

    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        store.record_decision_price_anchor(decision_id, _anchor_payload())

        with pytest.raises(ImmutableVirtualRecordConflict):
            store.record_decision_price_anchor(decision_id, _anchor_payload(entry="151.00"))


def test_anchor_requires_a_known_decision(tmp_path) -> None:
    path, _policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        with pytest.raises(InvalidVirtualRecord):
            store.record_decision_price_anchor("no-such-decision", _anchor_payload())


def test_anchor_rejects_an_unsupported_status(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        with pytest.raises(InvalidVirtualRecord):
            store.record_decision_price_anchor(decision_id, _anchor_payload(status="pending"))


def test_missing_anchor_reads_as_none(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        decision_id = _one_decision(store, policy)
        assert store.load_decision_price_anchor(decision_id) is None
