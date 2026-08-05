from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
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
    five_x_virtual_portfolio_policy,
    open_virtual_portfolio_reader,
    open_virtual_portfolio_writer,
    portfolio_snapshot,
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
        writer_id="cycle-test",
        decisions=decisions,
        closes=tmp_path / "missing-closes.json",
        quotes=tmp_path / "missing-quotes.jsonl",
        quote_index=None,
        close_session=False,
        audit_run_root=tmp_path / "runs",
        no_audit_artifact=True,
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

    with open_virtual_portfolio_writer(path, writer_id="five-x-test") as store:
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

    with open_virtual_portfolio_writer(path, writer_id="v6-migration") as store:
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
    with open_virtual_portfolio_writer(path, writer_id="pre-v4") as store:
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

    with open_virtual_portfolio_writer(path, writer_id="migration-test") as store:
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

    with open_virtual_portfolio_writer(path, writer_id="v1-migration") as store:
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
    with open_virtual_portfolio_writer(path, writer_id="pre-v6") as store:
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

    with open_virtual_portfolio_writer(path, writer_id="v4-migration") as store:
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
        with open_virtual_portfolio_writer(tampered_path, writer_id="must-not-repair"):
            pass

    audit_path, audit_policy = _initialized(tmp_path / "audit-tampered")
    with open_virtual_portfolio_writer(audit_path, writer_id="audit-test") as store:
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
    with open_virtual_portfolio_writer(path, writer_id="fault-injection") as store:
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
    with open_virtual_portfolio_reader(path) as store:
        assert store.pending_session_close_request(as_of=cycle_time) is not None
        assert (
            store.connection.execute("SELECT COUNT(*) FROM session_close_completions").fetchone()[0]
            == 0
        )


def test_cycle_does_not_hide_completed_session_with_open_position(tmp_path, monkeypatch) -> None:
    from tools import virtual_portfolio as cli

    path, policy = _initialized(tmp_path)
    requested_at = START + timedelta(minutes=1)
    cycle_time = START + timedelta(minutes=2)
    session_date = cycle_time.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with open_virtual_portfolio_writer(path, writer_id="fault-injection") as store:
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

    with open_virtual_portfolio_writer(path, writer_id="migration-test") as store:
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
