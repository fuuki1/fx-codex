from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import fx_intel.virtual_portfolio as virtual_portfolio_module
from fx_intel.virtual_portfolio import (
    PositionMarkRequest,
    TradeCloseRequest,
    open_virtual_portfolio_writer,
)
from fx_intel.virtual_portfolio_v2 import (
    InvalidV2Record,
    PortfolioValuationRequest,
    PositionLiquidationValuation,
)
from fx_intel.virtual_portfolio_valuation import (
    LiquidationReservePolicy,
    append_synchronized_valuation_from_marks,
    evaluate_shadow_risk_gate_v2,
)
from tests.test_virtual_portfolio import START, _decision, _initialized, _quote

RESERVE_POLICY = LiquidationReservePolicy()
MODEL_SHA = RESERVE_POLICY.sha256


def _open_trade(path, policy) -> str:
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(policy),
            quote=_quote(),
            observed_at=START,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        return str(opened["trade_id"])


def _position(
    trade_id: str,
    *,
    cutoff=None,
) -> PositionLiquidationValuation:
    valuation_cutoff = cutoff or START + timedelta(minutes=1)
    return PositionLiquidationValuation(
        trade_id=trade_id,
        valuation_cutoff=valuation_cutoff,
        observed_at=valuation_cutoff + timedelta(seconds=2),
        source_event_time=valuation_cutoff - timedelta(seconds=2),
        mid_unrealized_pnl_jpy=5_000,
        executable_unrealized_pnl_jpy=4_800,
        exit_cost_reserve_base_jpy=200,
        exit_cost_reserve_low_jpy=400,
        exit_cost_reserve_high_jpy=150,
        evidence_kind="modeled",
        quote_ids=("position-quote",),
        valuation_model_sha256=MODEL_SHA,
        max_source_age_seconds=2,
        detail={
            "mark_id": "test-mark",
            "reserve": {
                "evidence_kind": "modeled",
                "started_financing_days": 1,
                "components_base_jpy": {
                    "slippage_cost_jpy": 100,
                    "commission_jpy": 50,
                    "financing_jpy": 50,
                    "conversion_cost_jpy": 0,
                },
                "reserve_base_jpy": 200,
                "reserve_low_jpy": 400,
                "reserve_high_jpy": 150,
                "gap_buffer_jpy": 1_250,
            },
        },
    )


def _valuation(
    policy,
    positions,
    *,
    valuation_id: str = "valuation-1",
    cutoff=None,
) -> PortfolioValuationRequest:
    valuation_cutoff = cutoff or START + timedelta(minutes=1)
    return PortfolioValuationRequest(
        valuation_id=valuation_id,
        valuation_cutoff=valuation_cutoff,
        observed_at=valuation_cutoff + timedelta(seconds=3),
        recorded_at=valuation_cutoff + timedelta(seconds=4),
        positions=tuple(positions),
        policy_sha256=policy.policy_sha256,
        valuation_model_sha256=MODEL_SHA,
        writer_id="test",
        detail={
            "consumer_contract": "virtual-portfolio-valuation-consumer-v1",
            "reserve_policy": asdict(RESERVE_POLICY),
            "reserve_policy_sha256": MODEL_SHA,
            "planned_stop_loss_jpy": 5_000 if positions else 0,
            "gap_buffer_jpy": 1_250 if positions else 0,
            "low_exit_reserve_jpy": 400 if positions else 0,
            "portfolio_heat_jpy": 6_650 if positions else 0,
            "quality_complete": True,
        },
    )


def test_schema_install_is_atomic_when_v2_install_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "schema-rollback.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row

    def reject_v2_schema(_: sqlite3.Connection) -> None:
        raise RuntimeError("injected V2 schema failure")

    monkeypatch.setattr(
        virtual_portfolio_module,
        "install_v2_schema",
        reject_v2_schema,
    )
    try:
        with pytest.raises(RuntimeError, match="injected V2 schema failure"):
            virtual_portfolio_module._initialize(connection)
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()

    assert tables == []


def test_initial_capital_is_hash_chained_and_double_entry_balanced(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        snapshot = store.virtual_portfolio_v2_snapshot()
        audit = store.audit_virtual_portfolio_v2()
        postings = store.connection.execute("""
            SELECT account, amount_jpy
            FROM journal_postings
            ORDER BY line_no
            """).fetchall()

    assert snapshot["accounting_status"] == "ready"
    assert snapshot["event_count"] == 1
    assert snapshot["valuation_status"] == "unavailable_without_synchronized_valuation"
    assert [(row["account"], row["amount_jpy"]) for row in postings] == [
        ("asset.cash_jpy", 1_000_000),
        ("equity.initial_capital", -1_000_000),
    ]
    assert audit["passed"] is True
    assert audit["journal_transaction_count"] == 1


def test_trade_close_dual_writes_cost_attribution_in_same_journal(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=2)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="v2-close",
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
        rows = store.connection.execute("""
            SELECT p.account, p.amount_jpy
            FROM journal_postings AS p
            JOIN journal_transactions AS t
              ON t.transaction_id = p.transaction_id
            WHERE t.transaction_type = 'trade_closed'
            ORDER BY p.line_no
            """).fetchall()
        audit = store.audit_virtual_portfolio_v2()

    amounts = {str(row["account"]): int(row["amount_jpy"]) for row in rows}
    assert amounts == {
        "asset.cash_jpy": 4_600,
        "pnl.market_move_unseparated_fx": -5_000,
        "expense.spread": 200,
        "expense.slippage": 100,
        "expense.commission": 50,
        "expense.financing": 25,
        "expense.conversion_spread": 25,
    }
    assert sum(amounts.values()) == 0
    assert audit["passed"] is True
    assert audit["cash_parity"] is True
    assert audit["legacy_cash_jpy"] == 1_004_600
    assert audit["journal_cash_jpy"] == 1_004_600
    assert audit["event_count"] == 2


def test_v2_snapshot_and_risk_head_exclude_future_events(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    valuation_cutoff = START + timedelta(minutes=1)
    valuation = _valuation(
        policy,
        [_position(trade_id, cutoff=valuation_cutoff)],
        valuation_id="pit-valuation",
        cutoff=valuation_cutoff,
    )
    close_time = START + timedelta(minutes=2)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(valuation)
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="future-v2-close",
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
        historical = store.virtual_portfolio_v2_snapshot(
            as_of=valuation_cutoff,
            known_at=valuation.recorded_at,
        )
        current = store.virtual_portfolio_v2_snapshot()
        risk = evaluate_shadow_risk_gate_v2(
            store,
            as_of=valuation_cutoff,
            known_at=valuation.recorded_at,
        )

    assert historical["event_count"] == 2
    assert historical["valuation_id"] == "pit-valuation"
    assert historical["cash_jpy"] == policy.initial_capital_jpy
    assert current["event_count"] == 3
    assert "valuation_event_head_changed" not in risk["reasons"]


def test_complete_synchronized_valuation_reconciles_three_equities(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    cutoff = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=trade_id,
                mark_time=cutoff,
                quote=_quote(
                    moment=cutoff,
                    bid=150.50,
                    ask=150.52,
                    record_id="complete-valuation-mark",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
            )
        )
        result = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=cutoff,
            observed_at=cutoff,
        )
        retry = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=cutoff,
            observed_at=cutoff,
        )
        snapshot = store.virtual_portfolio_v2_snapshot()
        audit = store.audit_virtual_portfolio_v2()

    assert result["valuation"]["valuation_status"] == "complete"
    assert retry["valuation"] == {
        "inserted": False,
        "valuation_id": result["valuation_id"],
        "valuation_status": "complete",
    }
    assert snapshot["mid_equity_jpy"] == 1_005_000
    assert snapshot["executable_equity_jpy"] == 1_004_800
    assert snapshot["liquidation_equity_base_jpy"] == 1_004_600
    assert snapshot["liquidation_equity_low_jpy"] == 1_004_400
    assert snapshot["liquidation_equity_high_jpy"] == 1_004_650
    assert snapshot["liquidation_drawdown_base_jpy"] == 0
    assert snapshot["accounting_status"] == "ready"
    assert audit["passed"] is True
    assert audit["valuation_count"] == 1


def test_first_valuation_preserves_realized_cash_high_water_mark(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    valuation_time = close_time + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=148.48,
                    ask=148.50,
                    record_id="v2-loss-close",
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
        result = store.record_synchronized_liquidation_valuation(
            _valuation(
                policy,
                (),
                valuation_id="valuation-after-loss",
                cutoff=valuation_time,
            )
        )

    assert result["liquidation_equity_base_jpy"] == 984_600
    assert result["liquidation_hwm_base_jpy"] == 1_000_000
    assert result["liquidation_drawdown_base_jpy"] == 15_400


def test_incomplete_synchronized_valuation_persists_null_equities(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        result = store.record_synchronized_liquidation_valuation(_valuation(policy, ()))
        snapshot = store.virtual_portfolio_v2_snapshot()

    assert result["valuation_status"] == "incomplete_synchronized_valuation"
    assert snapshot["position_count"] == 1
    assert snapshot["marked_position_count"] == 0
    assert snapshot["mid_equity_jpy"] is None
    assert snapshot["executable_equity_jpy"] is None
    assert snapshot["liquidation_equity_base_jpy"] is None
    assert snapshot["liquidation_equity_low_jpy"] is None
    assert snapshot["liquidation_equity_high_jpy"] is None


def test_valuation_rejects_asynchronous_cutoff_and_unpriced_costs(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    portfolio_cutoff = START + timedelta(minutes=1)
    asynchronous = _position(
        trade_id,
        cutoff=portfolio_cutoff - timedelta(seconds=1),
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        with pytest.raises(InvalidV2Record, match="all positions"):
            store.record_synchronized_liquidation_valuation(
                _valuation(
                    policy,
                    (asynchronous,),
                    cutoff=portfolio_cutoff,
                )
            )
        unavailable = PositionLiquidationValuation(
            **{
                **_position(trade_id).__dict__,
                "evidence_kind": "unavailable",
            }
        )
        with pytest.raises(InvalidV2Record, match="cost evidence"):
            store.record_synchronized_liquidation_valuation(_valuation(policy, (unavailable,)))
        different_model = PositionLiquidationValuation(
            **{
                **_position(trade_id).__dict__,
                "valuation_model_sha256": "d" * 64,
            }
        )
        with pytest.raises(InvalidV2Record, match="must match"):
            store.record_synchronized_liquidation_valuation(_valuation(policy, (different_model,)))


def test_position_source_age_rounds_positive_fraction_up(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    position = PositionLiquidationValuation(
        **{
            **_position(trade_id).__dict__,
            "source_event_time": (START + timedelta(minutes=1, seconds=-2, microseconds=-1)),
            "max_source_age_seconds": 2,
        }
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        with pytest.raises(InvalidV2Record, match="below the price source age"):
            store.record_synchronized_liquidation_valuation(_valuation(policy, (position,)))


def test_cross_position_skew_rounds_positive_fraction_up(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    first_trade_id = _open_trade(path, policy)
    second_time = START + timedelta(seconds=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        second = store.process_decision(
            _decision(
                policy,
                decision_id="second-skew-position",
                prediction_time=second_time,
            ),
            quote=_quote(
                moment=second_time,
                record_id="second-skew-quote",
            ),
            observed_at=second_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        cutoff = START + timedelta(minutes=1)
        first = _position(first_trade_id, cutoff=cutoff)
        second_position = PositionLiquidationValuation(
            **{
                **_position(str(second["trade_id"]), cutoff=cutoff).__dict__,
                "source_event_time": first.source_event_time + timedelta(microseconds=1),
            }
        )
        result = store.record_synchronized_liquidation_valuation(
            _valuation(
                policy,
                (first, second_position),
                cutoff=cutoff,
            )
        )

    assert result["cross_position_skew_seconds"] == 1


def test_v2_tables_are_append_only_and_journal_is_sealed(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        event = store.connection.execute(
            "SELECT event_id FROM portfolio_event_log LIMIT 1"
        ).fetchone()
        transaction = store.connection.execute(
            "SELECT transaction_id FROM journal_transactions LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute("UPDATE portfolio_event_log SET writer_id = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            store.connection.execute(
                """
                INSERT INTO journal_postings(
                    posting_id, transaction_id, line_no, account, amount_jpy,
                    currency, detail_json, record_sha256, writer_id
                ) VALUES (?, ?, 99, 'bad', 1, 'JPY', '{}', ?, 'bad')
                """,
                ("bad-posting", transaction["transaction_id"], "f" * 64),
            )
        assert event is not None


def test_v2_failure_rolls_back_legacy_close_atomically(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.connection.execute("""
            CREATE TRIGGER fail_v2_trade_close
            BEFORE INSERT ON portfolio_event_log
            WHEN NEW.event_type = 'trade_closed'
            BEGIN
                SELECT RAISE(ABORT, 'injected V2 failure');
            END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected V2 failure"):
            store.close_trade(
                TradeCloseRequest(
                    trade_id=trade_id,
                    close_time=close_time,
                    quote=_quote(
                        moment=close_time,
                        bid=150.50,
                        ask=150.52,
                        record_id="atomic-close",
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
        close_count = int(
            store.connection.execute("SELECT COUNT(*) FROM simulated_trade_closes").fetchone()[0]
        )
        cash = store.cash_balance_jpy()
        event_count = store.virtual_portfolio_v2_snapshot()["event_count"]

    assert close_count == 0
    assert cash == 1_000_000
    assert event_count == 1


def test_audit_detects_hash_tampering_after_guard_is_removed(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.connection.execute("DROP TRIGGER portfolio_event_log_no_update")
        store.connection.execute(
            "UPDATE portfolio_event_log SET payload_json = '{\"tampered\":true}'"
        )
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert any(str(error).startswith("payload_hash_mismatch:") for error in audit["errors"])
    assert "immutable_trigger_missing:portfolio_event_log_no_update" in audit["errors"]


def test_audit_recomputes_close_cost_components(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="component-audit-close",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="time_exit",
                cost_model_id="explicit-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
                commission_jpy=50,
            )
        )
        store.connection.execute("DROP TRIGGER simulated_trade_closes_no_update")
        store.connection.execute(
            """
            UPDATE simulated_trade_closes
            SET commission_jpy = commission_jpy + 1
            WHERE trade_id = ?
            """,
            (trade_id,),
        )
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert f"close_cost_identity:{trade_id}" in audit["errors"]


def test_audit_rejects_duplicate_accounts_in_one_journal_transaction(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="duplicate-account-close",
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
        event_id = str(store.connection.execute("""
                SELECT event_id
                FROM journal_transactions
                WHERE transaction_type = 'trade_closed'
                """).fetchone()[0])
        store.connection.execute("DROP TRIGGER journal_postings_no_update")
        store.connection.execute(
            """
            UPDATE journal_postings
            SET account = 'expense.spread'
            WHERE transaction_id = (
                SELECT transaction_id
                FROM journal_transactions
                WHERE event_id = ?
            ) AND account = 'pnl.market_move_unseparated_fx'
            """,
            (event_id,),
        )
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert any(str(error).startswith("journal_duplicate_account:") for error in audit["errors"])
    assert any(str(error).startswith("journal_component_identity:") for error in audit["errors"])


def test_audit_rejects_position_without_backing_mark(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(_valuation(policy, (_position(trade_id),)))
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert f"position_mark_missing:valuation-1:{trade_id}" in audit["errors"]


def test_audit_binds_valuation_row_timeline_to_canonical_event(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(_valuation(policy, ()))
        store.connection.execute("DROP TRIGGER portfolio_liquidation_valuations_no_update")
        store.connection.execute("""
            UPDATE portfolio_liquidation_valuations
            SET recorded_at_ns = observed_at_ns
            WHERE valuation_id = 'valuation-1'
            """)
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert "valuation_timeline_identity:valuation-1:recorded_at_ns" in audit["errors"]


def test_audit_rejects_future_non_jpy_conversion_evidence(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    cutoff = START + timedelta(minutes=1)
    future_conversion = cutoff + timedelta(minutes=5)
    conversion_record_id = f"dukascopy:USDJPY:{future_conversion.isoformat()}:future-conversion"
    conversion_source = f"{conversion_record_id}; test conversion"
    decision = _decision(policy)
    decision.update(
        {
            "symbol": "EURUSD",
            "stop": 1.095,
            "target1": 1.11,
        }
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            decision,
            quote=_quote(
                moment=START,
                bid=1.1000,
                ask=1.1002,
                record_id="eurusd-open",
            ),
            observed_at=START,
            quote_to_jpy_rate=150,
            quote_conversion_source=conversion_source,
        )
        trade_id = str(opened["trade_id"])
        store.record_position_mark(
            PositionMarkRequest(
                trade_id=trade_id,
                mark_time=cutoff,
                quote=_quote(
                    moment=cutoff,
                    bid=1.1005,
                    ask=1.1007,
                    record_id="eurusd-future-conversion-mark",
                ),
                quote_to_jpy_rate=150,
                quote_conversion_source=conversion_source,
            )
        )
        mark = store.connection.execute(
            "SELECT * FROM simulated_position_marks WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        reserve = {
            "evidence_kind": "modeled",
            "started_financing_days": 1,
            "components_base_jpy": {
                "slippage_cost_jpy": 100,
                "commission_jpy": 50,
                "financing_jpy": 50,
                "conversion_cost_jpy": 25,
            },
            "reserve_base_jpy": 225,
            "reserve_low_jpy": 450,
            "reserve_high_jpy": 169,
            "gap_buffer_jpy": 1_250,
        }
        position = PositionLiquidationValuation(
            trade_id=trade_id,
            valuation_cutoff=cutoff,
            observed_at=cutoff,
            source_event_time=cutoff - timedelta(seconds=2),
            mid_unrealized_pnl_jpy=int(mark["gross_unrealized_pnl_jpy"]),
            executable_unrealized_pnl_jpy=int(mark["executable_unrealized_pnl_jpy"]),
            exit_cost_reserve_base_jpy=225,
            exit_cost_reserve_low_jpy=450,
            exit_cost_reserve_high_jpy=169,
            evidence_kind="modeled",
            quote_ids=(
                "eurusd-future-conversion-mark",
                conversion_record_id,
            ),
            valuation_model_sha256=MODEL_SHA,
            max_source_age_seconds=2,
            detail={
                "mark_id": str(mark["mark_id"]),
                "mark_observed_at": cutoff.isoformat(),
                "quote_conversion_source": conversion_source,
                "reserve": reserve,
            },
        )
        request = PortfolioValuationRequest(
            valuation_id="future-conversion-valuation",
            valuation_cutoff=cutoff,
            observed_at=cutoff,
            recorded_at=cutoff + timedelta(seconds=1),
            positions=(position,),
            policy_sha256=policy.policy_sha256,
            valuation_model_sha256=MODEL_SHA,
            writer_id="test",
            detail={
                "consumer_contract": "virtual-portfolio-valuation-consumer-v1",
                "reserve_policy": asdict(RESERVE_POLICY),
                "reserve_policy_sha256": MODEL_SHA,
                "planned_stop_loss_jpy": 5_000,
                "gap_buffer_jpy": 1_250,
                "low_exit_reserve_jpy": 450,
                "portfolio_heat_jpy": 6_700,
                "quality_complete": True,
            },
        )
        store.record_synchronized_liquidation_valuation(request)
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert (
        f"position_mark_future_conversion:future-conversion-valuation:{trade_id}" in audit["errors"]
    )


def test_audit_recomputes_position_reserve_equity_and_hwm(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(_valuation(policy, (_position(trade_id),)))
        store.connection.execute("DROP TRIGGER position_liquidation_valuations_no_update")
        store.connection.execute("""
            UPDATE position_liquidation_valuations
            SET exit_cost_reserve_base_jpy = exit_cost_reserve_base_jpy + 1
            """)
        store.connection.execute("DROP TRIGGER portfolio_liquidation_valuations_no_update")
        store.connection.execute("""
            UPDATE portfolio_liquidation_valuations
            SET mid_equity_jpy = mid_equity_jpy + 1,
                liquidation_hwm_low_jpy = liquidation_hwm_low_jpy + 1
            """)
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert any(
        str(error).startswith("position_payload_identity:valuation-1:") for error in audit["errors"]
    )
    assert "valuation_mid_equity_jpy_identity:valuation-1" in audit["errors"]
    assert "valuation_liquidation_hwm_low_jpy_identity:valuation-1" in audit["errors"]


def test_audit_fails_closed_on_malformed_nested_json(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(_valuation(policy, (_position(trade_id),)))
        store.connection.execute("DROP TRIGGER position_liquidation_valuations_no_update")
        store.connection.execute("UPDATE position_liquidation_valuations SET detail_json = '{'")
        store.connection.commit()
        audit = store.audit_virtual_portfolio_v2()

    assert audit["passed"] is False
    assert f"position_detail_json:valuation-1:{trade_id}" in audit["errors"]


def test_explicit_migration_restores_missing_v1_projection(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=_quote(
                    moment=close_time,
                    bid=150.50,
                    ask=150.52,
                    record_id="migration-close",
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
        for trigger in (
            "journal_postings_no_delete",
            "journal_transactions_no_delete",
            "portfolio_event_log_no_delete",
        ):
            store.connection.execute(f"DROP TRIGGER {trigger}")
        store.connection.execute("DELETE FROM journal_postings")
        store.connection.execute("DELETE FROM journal_transactions")
        store.connection.execute("DELETE FROM portfolio_event_log")
        store.connection.commit()
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        before = store.virtual_portfolio_v2_snapshot()
        blocked_time = START + timedelta(minutes=2)
        blocked = store.process_decision(
            _decision(
                policy,
                decision_id="migration-required",
                prediction_time=blocked_time,
            ),
            quote=_quote(
                moment=blocked_time,
                record_id="migration-required-quote",
            ),
            observed_at=blocked_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        migration = store.migrate_virtual_portfolio_v2_accounting(
            recorded_at=START + timedelta(days=1),
        )
        after = store.virtual_portfolio_v2_snapshot()
        audit = store.audit_virtual_portfolio_v2()

    assert before["accounting_status"] == "migration_required"
    assert blocked["disposition"] == "abstained"
    assert "V2会計台帳の明示移行が未完了" in blocked["rejection_reasons"]
    assert migration == {
        "contract": "virtual-portfolio-v2-accounting-migration",
        "inserted": 2,
        "remaining": 0,
        "original_recorded_at_available": False,
    }
    assert after["accounting_status"] == "ready"
    assert audit["passed"] is True


def test_migration_cli_requires_exact_confirmation_without_writing(tmp_path) -> None:
    path, _ = _initialized(tmp_path)
    script = Path(__file__).resolve().parents[1] / "tools" / "virtual_portfolio_v2_migrate.py"
    command = [
        sys.executable,
        str(script),
        "--database",
        str(path),
        "migrate",
    ]
    rejected = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        unchanged = store.virtual_portfolio_v2_snapshot()
    accepted = subprocess.run(
        [*command, "--confirm", "append-v2-accounting"],
        capture_output=True,
        check=False,
        text=True,
    )
    payload = json.loads(accepted.stdout)

    assert rejected.returncode == 1
    assert "no rows were written" in rejected.stdout
    assert unchanged["event_count"] == 1
    assert accepted.returncode == 0
    assert payload["result"]["migration"]["inserted"] == 0
    assert payload["result"]["audit"]["passed"] is True


def test_cycle_cli_returns_failure_when_batch_has_conflicts(monkeypatch, capsys) -> None:
    import tools.virtual_portfolio as virtual_portfolio_cli

    monkeypatch.setattr(
        virtual_portfolio_cli,
        "_cycle",
        lambda _: {"ok": False, "conflicts": [{"error": "injected conflict"}]},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["virtual_portfolio.py", "cycle", "--no-audit-artifact"],
    )

    assert virtual_portfolio_cli.main() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False


def test_audit_v2_does_not_rescan_cash_postings_per_valuation(tmp_path) -> None:
    """現金postingの走査は valuation 件数に比例して増えてはならない。

    旧実装は valuation 1件ごとに portfolio_event_log/journal_transactions/
    journal_postings の3表joinを再実行しており、実機では1スナップショットあたり
    1,581回の重複クエリになっていた(N+1)。全件でも数十行しかないため
    一度読み出して Python 側で絞り込む方式へ変更した。
    valuation を増やしても当該クエリが1回に留まることを固定する。
    """
    from fx_intel.virtual_portfolio_v2 import audit_v2

    path, policy = _initialized(tmp_path)
    _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        for index in range(5):
            store.record_synchronized_liquidation_valuation(
                _valuation(
                    policy,
                    (),
                    valuation_id=f"valuation-nplus1-{index}",
                    cutoff=START + timedelta(minutes=10 + index),
                )
            )

    executed: list[str] = []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.set_trace_callback(executed.append)
    try:
        result = audit_v2(connection)
    finally:
        connection.close()

    assert result["passed"] is True
    cash_scans = [
        sql for sql in executed if "'asset.cash_jpy'" in sql and "journal_transactions" in sql
    ]
    assert len(cash_scans) == 1, f"cash posting scan ran {len(cash_scans)} times"
