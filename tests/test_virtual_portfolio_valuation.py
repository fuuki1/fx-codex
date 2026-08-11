from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from fx_intel.virtual_portfolio import (
    PositionMarkRequest,
    QuoteSnapshot,
    TradeCloseRequest,
    open_virtual_portfolio_writer,
)
from fx_intel.virtual_portfolio_valuation import (
    LiquidationReservePolicy,
    VirtualValuationError,
    append_synchronized_valuation_from_marks,
    evaluate_shadow_risk_gate_v2,
)
from tests.test_virtual_portfolio import START, _anchor_payload, _decision, _quote
from tests.test_virtual_portfolio_v2 import _initialized, _open_trade


def _mark(
    store,
    trade_id: str,
    *,
    moment,
    bid: float = 150.0,
    ask: float = 150.02,
    record_id: str = "valuation-mark",
) -> None:
    store.record_position_mark(
        PositionMarkRequest(
            trade_id=trade_id,
            mark_time=moment,
            quote=_quote(
                moment=moment,
                bid=bid,
                ask=ask,
                record_id=record_id,
            ),
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
    )


def _risk(store, *, as_of, reserve_policy=None):
    return evaluate_shadow_risk_gate_v2(
        store,
        as_of=as_of,
        known_at=datetime.now(UTC),
        reserve_policy=reserve_policy,
    )


def test_consumer_builds_cost_reserved_liquidation_equity(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    baseline_time = START
    current_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=baseline_time, record_id="baseline-mark")
        baseline = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=baseline_time,
            observed_at=baseline_time,
        )
        _mark(
            store,
            trade_id,
            moment=current_time,
            bid=150.50,
            ask=150.52,
            record_id="current-mark",
        )
        current = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=current_time,
            observed_at=current_time,
        )
        risk = _risk(store, as_of=current_time)
        snapshot = store.snapshot(now=current_time)
        audit = store.audit_virtual_portfolio_v2()

    assert baseline["quality_complete"] is True
    assert current["quality_complete"] is True
    valuation = current["valuation"]
    assert valuation["valuation_status"] == "complete"
    assert valuation["exit_cost_reserve_base_jpy"] == 200
    assert valuation["exit_cost_reserve_low_jpy"] == 400
    assert valuation["exit_cost_reserve_high_jpy"] == 150
    assert valuation["liquidation_equity_base_jpy"] == 1_004_600
    assert valuation["liquidation_equity_low_jpy"] == 1_004_400
    assert valuation["liquidation_equity_high_jpy"] == 1_004_650
    assert current["portfolio_heat_jpy"] == 6_650
    assert risk["enforcement_mode"] == "shadow_only"
    assert risk["can_open"] is False
    assert "candidate_safety_learning_gate_unavailable" in risk["reasons"]
    assert risk["risk_equity_jpy"] == 1_004_400
    assert risk["next_trade_risk_jpy"] == 5_022
    assert snapshot["risk_gate_v2_shadow"]["can_open"] is False
    assert audit["passed"] is True


def test_stale_mark_persists_incomplete_null_valuation(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        cutoff = START + timedelta(minutes=10)
        result = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=cutoff,
            observed_at=cutoff,
        )
        snapshot = store.virtual_portfolio_v2_snapshot()
        risk = _risk(store, as_of=cutoff)

    assert result["quality_complete"] is False
    assert result["valued_position_count"] == 0
    assert result["warnings"] == [f"stale_position_mark:{trade_id}:602s"]
    assert snapshot["valuation_status"] == "incomplete_synchronized_valuation"
    assert snapshot["liquidation_equity_low_jpy"] is None
    assert risk["can_open"] is False
    assert "valuation_status:incomplete_synchronized_valuation" in risk["reasons"]


def test_valuation_position_set_uses_delayed_close_knowledge_cutoff(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    close_time = START + timedelta(minutes=1)
    close_known_at = START + timedelta(minutes=2)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        store.close_trade(
            TradeCloseRequest(
                trade_id=trade_id,
                close_time=close_time,
                quote=QuoteSnapshot(
                    source_record_id="delayed-valuation-close",
                    source="test-executable-quotes",
                    revision_id="original",
                    event_time=close_time,
                    available_time=close_known_at,
                    ingested_time=close_known_at,
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
                observed_at=close_known_at,
            )
        )
        _mark(
            store,
            trade_id,
            moment=close_time,
            record_id="pre-close-knowledge-mark",
        )
        before_known = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=close_time,
            observed_at=close_time,
        )
        when_known = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=close_known_at,
            observed_at=close_known_at,
        )

    assert before_known["expected_position_count"] == 1
    assert before_known["valued_position_count"] == 1
    assert when_known["expected_position_count"] == 0
    assert when_known["valued_position_count"] == 0


def test_valuation_position_set_uses_delayed_open_knowledge_cutoff(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    open_time = START + timedelta(minutes=1)
    open_known_at = START + timedelta(minutes=2)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        opened = store.process_decision(
            _decision(
                policy,
                decision_id="delayed-valuation-open",
                prediction_time=open_time,
            ),
            quote=QuoteSnapshot(
                source_record_id="delayed-valuation-open",
                source="test-executable-quotes",
                revision_id="original",
                event_time=open_time,
                available_time=open_known_at,
                ingested_time=open_known_at,
                bid=150.0,
                ask=150.02,
            ),
            observed_at=open_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
            historical_replay=True,
            replay_as_of=open_known_at,
            price_anchor=_anchor_payload(
                anchor_event_time=open_time.isoformat(),
                anchor_lag_seconds=0.0,
            ),
        )
        before_known = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=open_time,
            observed_at=open_time,
        )
        when_known = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=open_known_at,
            observed_at=open_known_at,
        )

    assert opened["disposition"] == "opened"
    assert before_known["expected_position_count"] == 0
    assert before_known["valued_position_count"] == 0
    assert when_known["expected_position_count"] == 1
    assert when_known["valued_position_count"] == 0
    assert when_known["warnings"] == [f"missing_position_mark:{opened['trade_id']}"]


def test_stale_mark_rounds_positive_fraction_up(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        cutoff = START + timedelta(seconds=298, microseconds=1)
        result = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=cutoff,
            observed_at=cutoff,
        )

    assert result["quality_complete"] is False
    assert result["warnings"] == [f"stale_position_mark:{trade_id}:301s"]


def test_consumer_fails_closed_on_malformed_mark_quote_json(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START, record_id="malformed-mark-quote")
        store.connection.execute("DROP TRIGGER simulated_position_marks_no_update")
        store.connection.execute(
            "UPDATE simulated_position_marks SET quote_json = '{' WHERE trade_id = ?",
            (trade_id,),
        )
        result = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )

    assert result["quality_complete"] is False
    assert result["warnings"] == [f"invalid_position_mark_quote_json:{trade_id}"]


def test_shadow_gate_blocks_liquidation_loss_before_cash_is_realized(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    loss_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START, record_id="risk-baseline")
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        _mark(
            store,
            trade_id,
            moment=loss_time,
            bid=148.50,
            ask=148.52,
            record_id="risk-open-loss",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=loss_time,
            observed_at=loss_time,
        )
        risk = _risk(store, as_of=loss_time)
        cash = store.cash_balance_jpy()

    assert cash == 1_000_000
    assert risk["risk_equity_jpy"] == 984_400
    assert risk["period_losses_jpy"]["day"] == 15_600
    assert risk["can_open"] is False
    assert "day_liquidation_loss_limit_reached" in risk["reasons"]


def test_shadow_gate_uses_initial_equity_when_first_inception_mark_is_delayed(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    delayed_baseline = START + timedelta(seconds=301)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(
            store,
            trade_id,
            moment=delayed_baseline,
            record_id="delayed-inception-baseline",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=delayed_baseline,
            observed_at=delayed_baseline,
        )
        risk = _risk(store, as_of=delayed_baseline)

    assert risk["period_losses_jpy"]["day"] == 600
    assert risk["period_baselines"]["day"]["status"] == "eligible"
    assert risk["period_baselines"]["day"]["selection_mode"] == "portfolio_inception_equity"
    assert risk["period_baselines"]["day"]["baseline_equity_low_jpy"] == 1_000_000


def test_shadow_gate_counts_loss_before_first_synchronized_valuation(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    loss_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(
            store,
            trade_id,
            moment=loss_time,
            bid=148.50,
            ask=148.52,
            record_id="first-valuation-already-loss",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=loss_time,
            observed_at=loss_time,
        )
        risk = _risk(store, as_of=loss_time)

    assert risk["risk_equity_jpy"] == 984_400
    assert risk["period_losses_jpy"]["day"] == 15_600
    assert "day_liquidation_loss_limit_reached" in risk["reasons"]


def test_shadow_gate_does_not_select_valuation_recorded_after_knowledge_cutoff(
    tmp_path,
) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START, record_id="future-recorded-valuation")
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        risk = evaluate_shadow_risk_gate_v2(
            store,
            as_of=START,
            known_at=START,
        )

    assert "valuation_id" not in risk
    assert risk["risk_equity_jpy"] is None
    assert "synchronized_liquidation_valuation_unavailable" in risk["reasons"]


def test_shadow_gate_rejects_baseline_observed_after_period_boundary(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    day_boundary = START + timedelta(hours=15)
    current_time = day_boundary + timedelta(hours=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        baseline_mark = day_boundary - timedelta(seconds=1)
        _mark(
            store,
            trade_id,
            moment=baseline_mark,
            record_id="late-observed-boundary-mark",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=baseline_mark,
            observed_at=day_boundary + timedelta(seconds=1),
        )
        _mark(
            store,
            trade_id,
            moment=current_time,
            record_id="post-boundary-current-mark",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=current_time,
            observed_at=current_time,
        )
        risk = _risk(store, as_of=current_time)

    assert risk["period_losses_jpy"]["day"] is None
    assert risk["period_baselines"]["day"]["status"] == "unavailable"
    assert "day_liquidation_baseline_unavailable" in risk["reasons"]


def test_shadow_gate_rejects_model_discontinuity_between_baseline_and_current(
    tmp_path,
) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    changed_time = START + timedelta(minutes=1)
    current_time = START + timedelta(minutes=2)
    changed_policy = LiquidationReservePolicy(slippage_r_bps=201)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START, record_id="continuity-baseline")
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        _mark(store, trade_id, moment=changed_time, record_id="continuity-changed")
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=changed_time,
            observed_at=changed_time,
            reserve_policy=changed_policy,
        )
        _mark(store, trade_id, moment=current_time, record_id="continuity-current")
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=current_time,
            observed_at=current_time,
        )
        risk = _risk(store, as_of=current_time)

    assert risk["period_losses_jpy"]["day"] is None
    assert risk["period_baselines"]["day"]["model_continuity"] is False
    assert "day_liquidation_baseline_model_discontinuity" in risk["reasons"]


def test_shadow_gate_rejects_valuation_after_open_position_set_changes(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    second_open_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        opened = store.process_decision(
            _decision(
                policy,
                decision_id="decision-after-valuation",
                prediction_time=second_open_time,
            ),
            quote=_quote(
                moment=second_open_time,
                record_id="quote-after-valuation",
            ),
            observed_at=second_open_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        risk = _risk(store, as_of=second_open_time)

    assert opened["disposition"] == "opened"
    assert risk["valuation_position_count"] == 1
    assert risk["current_position_count"] == 2
    assert risk["can_open"] is False
    assert "valuation_position_set_changed" in risk["reasons"]


def test_shadow_gate_includes_existing_max_position_limit(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    first_trade_id = _open_trade(path, policy)
    second_open_time = START + timedelta(minutes=1)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        second = store.process_decision(
            _decision(
                policy,
                decision_id="second-open-for-limit",
                prediction_time=second_open_time,
            ),
            quote=_quote(
                moment=second_open_time,
                record_id="second-open-for-limit",
            ),
            observed_at=second_open_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        _mark(
            store,
            first_trade_id,
            moment=second_open_time,
            record_id="first-limit-mark",
        )
        _mark(
            store,
            str(second["trade_id"]),
            moment=second_open_time,
            record_id="second-limit-mark",
        )
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=second_open_time,
            observed_at=second_open_time,
        )
        risk = _risk(store, as_of=second_open_time)

    assert risk["valuation_position_count"] == 2
    assert risk["current_position_count"] == 2
    assert risk["legacy_risk_gate_reason"] == "同時保有上限2件に到達"
    assert risk["can_open"] is False
    assert "legacy_risk_gate_blocked" in risk["reasons"]


def test_shadow_gate_ages_original_source_not_only_valuation_cutoff(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    gate_time = START + timedelta(seconds=299)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        risk = _risk(store, as_of=gate_time)

    assert risk["valuation_age_seconds"] == 299
    assert risk["recorded_source_age_seconds"] == 2
    assert risk["effective_source_age_seconds"] == 301
    assert risk["can_open"] is False
    assert "source_bundle_stale:301s" in risk["reasons"]


def test_shadow_gate_rejects_cash_change_when_position_set_returns_to_same_state(
    tmp_path,
) -> None:
    path, policy = _initialized(tmp_path)
    first_trade_id = _open_trade(path, policy)
    second_open_time = START + timedelta(minutes=1)
    second_close_time = START + timedelta(minutes=2)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, first_trade_id, moment=START)
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        second = store.process_decision(
            _decision(
                policy,
                decision_id="round-trip-after-valuation",
                prediction_time=second_open_time,
            ),
            quote=_quote(
                moment=second_open_time,
                record_id="round-trip-open",
            ),
            observed_at=second_open_time,
            quote_to_jpy_rate=1,
            quote_conversion_source="JPY quote currency",
        )
        store.close_trade(
            TradeCloseRequest(
                trade_id=str(second["trade_id"]),
                close_time=second_close_time,
                quote=_quote(
                    moment=second_close_time,
                    bid=149.50,
                    ask=149.52,
                    record_id="round-trip-close",
                ),
                quote_to_jpy_rate=1,
                quote_conversion_source="JPY quote currency",
                close_reason="risk_test",
                cost_model_id="explicit-risk-test-costs-v1",
                cost_model_version="1",
                cost_source="test-fixture",
                costs_complete=True,
                slippage_cost_jpy=100,
                commission_jpy=50,
                financing_jpy=25,
                conversion_cost_jpy=0,
            )
        )
        risk = _risk(store, as_of=second_close_time)

    assert risk["valuation_position_count"] == 1
    assert risk["current_position_count"] == 1
    assert risk["valuation_cash_jpy"] == 1_000_000
    assert risk["current_cash_jpy"] < 1_000_000
    assert risk["current_event_seq"] > risk["valuation_event_seq"]
    assert risk["can_open"] is False
    assert "valuation_cash_state_changed" in risk["reasons"]
    assert "valuation_event_head_changed" in risk["reasons"]


def test_shadow_gate_rejects_valuation_model_change(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        changed_policy = LiquidationReservePolicy(slippage_r_bps=201)
        risk = _risk(
            store,
            as_of=START,
            reserve_policy=changed_policy,
        )

    assert risk["can_open"] is False
    assert "valuation_model_changed" in risk["reasons"]


def test_shadow_gate_uses_canonical_heat_when_row_detail_is_tampered(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START, record_id="heat-tamper-mark")
        appended = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        row = store.connection.execute(
            """
            SELECT detail_json
            FROM portfolio_liquidation_valuations
            WHERE valuation_id = ?
            """,
            (appended["valuation_id"],),
        ).fetchone()
        detail = json.loads(str(row["detail_json"]))
        detail["portfolio_heat_jpy"] = 0
        store.connection.execute("DROP TRIGGER portfolio_liquidation_valuations_no_update")
        store.connection.execute(
            """
            UPDATE portfolio_liquidation_valuations
            SET detail_json = ?
            WHERE valuation_id = ?
            """,
            (json.dumps(detail), appended["valuation_id"]),
        )
        audit = store.audit_virtual_portfolio_v2()
        risk = _risk(store, as_of=START)

    assert audit["passed"] is False
    assert f"valuation_detail_identity:{appended['valuation_id']}" in audit["errors"]
    assert risk["portfolio_heat_jpy"] == 6_650
    assert "v2_integrity_audit_failed" in risk["reasons"]


def test_consumer_records_actual_append_time_and_reuses_it_on_retry(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    before_append = datetime.now(UTC)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        first = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        retry = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        recorded_at_ns = int(
            store.connection.execute(
                """
                SELECT recorded_at_ns
                FROM portfolio_liquidation_valuations
                WHERE valuation_id = ?
                """,
                (first["valuation_id"],),
            ).fetchone()[0]
        )

    recorded_at = datetime.fromtimestamp(recorded_at_ns / 1_000_000_000, tz=UTC)
    assert recorded_at >= before_append
    assert recorded_at > START
    assert retry["valuation"]["inserted"] is False


def test_consumer_is_idempotent_at_same_cutoff(tmp_path) -> None:
    path, policy = _initialized(tmp_path)
    trade_id = _open_trade(path, policy)
    with open_virtual_portfolio_writer(path, writer_id="test") as store:
        _mark(store, trade_id, moment=START)
        first = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        retry = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=START,
            observed_at=START,
        )
        count = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM portfolio_liquidation_valuations"
            ).fetchone()[0]
        )

    assert first["valuation"]["inserted"] is True
    assert retry["valuation"]["inserted"] is False
    assert count == 1


def test_reserve_policy_rejects_optimistic_low_scenario() -> None:
    with pytest.raises(VirtualValuationError, match="low valuation"):
        LiquidationReservePolicy(low_cost_multiplier_bps=9_999).validate()
