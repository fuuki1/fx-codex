"""Shadow execution: intents, simulated events, pre-trade vetoes and TCA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.shadow_execution import (
    DisabledOrderGateway,
    ExecutionEvent,
    Fill,
    MockQuoteAdapter,
    OrderIntent,
    PreTradePolicy,
    ReplayQuoteAdapter,
    evaluate_intent_against_quote,
    tca_decompose,
)
from fx_intel.source_contracts import (
    BrokerQuote,
    MacroCalendarEvent,
    SourceImplementation,
    SourceKind,
    enforce_quote_slo,
    measure_quote_slo,
    require_source_adapter,
)

T0 = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _quote(
    *,
    bid: float = 145.000,
    ask: float = 145.008,
    received_offset: timedelta = timedelta(seconds=0),
    sequence_id: int = 1,
    symbol: str = "USDJPY",
) -> BrokerQuote:
    received = T0 + received_offset
    return BrokerQuote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        source_event_time=received - timedelta(milliseconds=50),
        broker_server_time=received - timedelta(milliseconds=20),
        received_at=received,
        ingested_at=received + timedelta(milliseconds=5),
        sequence_id=sequence_id,
        source="mock-broker",
        spread_observed=True,
    )


def _intent(*, valid_seconds: int = 60) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        decision_id="decision-1",
        model_id="model-1",
        symbol="USDJPY",
        side="long",
        quantity=10_000.0,
        decision_time=T0,
        valid_until=T0 + timedelta(seconds=valid_seconds),
        reference_bid=145.000,
        reference_ask=145.008,
        risk_budget_r=1.0,
        stop_loss=144.90,
        take_profit=145.20,
        reason="shadow self-test",
        data_hash="a" * 64,
        model_hash="b" * 64,
    )


class TestQuoteSchema:
    def test_one_sided_quote_rejected(self) -> None:
        with pytest.raises(TypedFailure):
            _quote(bid=145.01, ask=145.0)

    def test_estimated_spread_cannot_pose_as_observed(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            BrokerQuote(
                symbol="USDJPY",
                bid=145.0,
                ask=145.01,
                source_event_time=T0,
                broker_server_time=T0,
                received_at=T0,
                ingested_at=T0,
                sequence_id=1,
                source="ohlc-close-masquerade",
                spread_observed=False,
            )
        assert excinfo.value.reason is FailureReason.INVALID

    def test_clock_skew_rejected(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            BrokerQuote(
                symbol="USDJPY",
                bid=145.0,
                ask=145.01,
                source_event_time=T0 + timedelta(minutes=5),
                broker_server_time=T0 + timedelta(minutes=5),
                received_at=T0,
                ingested_at=T0,
                sequence_id=1,
                source="mock",
                spread_observed=True,
            )
        assert excinfo.value.reason is FailureReason.CLOCK_SKEW


class TestMacroSchema:
    def test_backdated_revision_rejected(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            MacroCalendarEvent(
                event_id="cpi-2026-06",
                indicator="CPI",
                country="US",
                scheduled_release_at=T0,
                actual_release_at=T0,
                first_observed_at=T0 - timedelta(days=1),
                period="2026-06",
                actual=3.1,
                consensus=3.0,
                previous_initial=2.9,
                previous_revised=None,
                revision=0,
                source="test",
            )
        assert excinfo.value.reason is FailureReason.REVISION_CONFLICT

    def test_revision_requires_prior_figures(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            MacroCalendarEvent(
                event_id="cpi-2026-06",
                indicator="CPI",
                country="US",
                scheduled_release_at=T0,
                actual_release_at=T0,
                first_observed_at=T0,
                period="2026-06",
                actual=3.1,
                consensus=3.0,
                previous_initial=None,
                previous_revised=None,
                revision=1,
                source="test",
            )
        assert excinfo.value.reason is FailureReason.INCOMPLETE


class TestSourceRegistry:
    def test_unimplemented_sources_fail_closed(self) -> None:
        for kind in SourceKind:
            if kind is SourceKind.COT:
                require_source_adapter(kind)
            else:
                with pytest.raises(TypedFailure) as excinfo:
                    require_source_adapter(kind)
                assert excinfo.value.reason is FailureReason.UNAVAILABLE

    def test_cot_is_research_only(self) -> None:
        from fx_intel.source_contracts import SOURCE_ADAPTER_STATUS

        assert (
            SOURCE_ADAPTER_STATUS[SourceKind.COT] is SourceImplementation.IMPLEMENTED_RESEARCH_ONLY
        )


class TestPreTradeVetoes:
    def test_stale_quote_rejected(self) -> None:
        decision = evaluate_intent_against_quote(
            _intent(),
            _quote(),
            now=T0 + timedelta(seconds=30),
            policy=PreTradePolicy(max_quote_age=timedelta(seconds=5), max_spread=0.05),
        )
        assert decision["action"] == "reject"
        assert decision["reason"] == "stale_quote"

    def test_expired_intent_rejected(self) -> None:
        decision = evaluate_intent_against_quote(
            _intent(valid_seconds=10),
            _quote(received_offset=timedelta(seconds=59)),
            now=T0 + timedelta(seconds=60),
            policy=PreTradePolicy(max_quote_age=timedelta(seconds=5), max_spread=0.05),
        )
        assert decision["reason"] == "expired_intent"

    def test_wide_spread_rejected(self) -> None:
        decision = evaluate_intent_against_quote(
            _intent(),
            _quote(bid=145.0, ask=145.2),
            now=T0,
            policy=PreTradePolicy(max_quote_age=timedelta(seconds=5), max_spread=0.05),
        )
        assert decision["reason"] == "max_spread_exceeded"

    def test_fresh_quote_proceeds(self) -> None:
        decision = evaluate_intent_against_quote(
            _intent(),
            _quote(),
            now=T0 + timedelta(seconds=1),
            policy=PreTradePolicy(max_quote_age=timedelta(seconds=5), max_spread=0.05),
        )
        assert decision["action"] == "proceed"


class TestExecutionEvents:
    def test_partial_fills_aggregate(self) -> None:
        event = ExecutionEvent(
            intent_id="intent-1",
            venue="simulated_replay",
            order_send_time=T0,
            broker_ack_time=T0 + timedelta(milliseconds=80),
            requested_quantity=10_000.0,
            fills=(
                Fill(T0 + timedelta(milliseconds=100), 145.010, 4_000.0),
                Fill(T0 + timedelta(milliseconds=200), 145.012, 3_000.0),
            ),
        )
        assert event.filled_quantity == 7_000.0
        assert event.partial_fill is True
        assert event.fill_price == pytest.approx(145.0108571428, rel=1e-9)
        assert event.first_fill_time == T0 + timedelta(milliseconds=100)

    def test_reject_must_preserve_reason(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            ExecutionEvent(
                intent_id="intent-1",
                venue="simulated_mock",
                order_send_time=T0,
                broker_ack_time=None,
                requested_quantity=1.0,
                rejected=True,
                reject_reason="  ",
            )
        assert excinfo.value.reason is FailureReason.INCOMPLETE

    def test_paper_or_live_venues_are_not_representable(self) -> None:
        for venue in ("paper", "live", "broker"):
            with pytest.raises(TypedFailure) as excinfo:
                ExecutionEvent(
                    intent_id="intent-1",
                    venue=venue,
                    order_send_time=T0,
                    broker_ack_time=None,
                    requested_quantity=1.0,
                )
            assert excinfo.value.reason is FailureReason.EXECUTION_MODEL_UNAVAILABLE


class TestDisabledGateway:
    def test_send_is_disabled_by_default(self) -> None:
        gateway = DisabledOrderGateway()
        assert gateway.enabled is False
        with pytest.raises(TypedFailure) as excinfo:
            gateway.send(_intent())
        assert excinfo.value.reason is FailureReason.EXECUTION_MODEL_UNAVAILABLE
        with pytest.raises(TypedFailure):
            gateway.cancel("intent-1")


class TestTca:
    def test_full_fill_decomposition_is_consistent(self) -> None:
        intent = _intent()
        event = ExecutionEvent(
            intent_id="intent-1",
            venue="simulated_replay",
            order_send_time=T0 + timedelta(milliseconds=50),
            broker_ack_time=T0 + timedelta(milliseconds=90),
            requested_quantity=10_000.0,
            fills=(Fill(T0 + timedelta(milliseconds=120), 145.0125, 10_000.0),),
            commission=0.0005,
            financing=0.0002,
        )
        report = tca_decompose(
            intent,
            event,
            gross_alpha=0.05,
            decision_mid=145.004,
            send_mid=145.006,
            post_fill_mid=145.010,
        )
        assert report.spread_cost == pytest.approx(0.004)
        assert report.latency_cost == pytest.approx(0.002)
        assert report.slippage_cost == pytest.approx(145.0125 - 145.010)
        assert report.adverse_selection == pytest.approx(145.0125 - 145.010)
        assert report.rejected_opportunity_cost == 0.0
        expected_net = (
            0.05
            - report.spread_cost
            - report.slippage_cost
            - report.latency_cost
            - report.commission
            - report.financing
            - report.adverse_selection
        )
        assert report.realized_net_alpha == pytest.approx(expected_net)

    def test_rejected_intent_converts_alpha_to_opportunity_cost(self) -> None:
        intent = _intent()
        event = ExecutionEvent(
            intent_id="intent-1",
            venue="simulated_mock",
            order_send_time=T0,
            broker_ack_time=None,
            requested_quantity=10_000.0,
            rejected=True,
            reject_reason="stale_quote",
        )
        report = tca_decompose(intent, event, gross_alpha=0.05, decision_mid=145.004, send_mid=None)
        assert report.rejected_opportunity_cost == 0.05
        assert report.realized_net_alpha == 0.0


class TestAdaptersAndSlo:
    def test_replay_adapter_is_point_in_time(self) -> None:
        quotes = [
            _quote(sequence_id=1, received_offset=timedelta(seconds=0)),
            _quote(sequence_id=2, received_offset=timedelta(seconds=10)),
        ]
        adapter = ReplayQuoteAdapter(quotes)
        assert adapter.as_of(T0 + timedelta(seconds=5)).sequence_id == 1
        with pytest.raises(TypedFailure) as excinfo:
            adapter.as_of(T0 - timedelta(seconds=1))
        assert excinfo.value.reason is FailureReason.UNAVAILABLE

    def test_mock_adapter_returns_latest(self) -> None:
        adapter = MockQuoteAdapter(
            [
                _quote(sequence_id=1),
                _quote(sequence_id=2, received_offset=timedelta(seconds=3)),
            ]
        )
        assert adapter.latest("USDJPY").sequence_id == 2
        with pytest.raises(TypedFailure):
            adapter.latest("EURUSD")

    def test_slo_measures_duplicates_and_order(self) -> None:
        quotes = [
            _quote(sequence_id=2, received_offset=timedelta(seconds=0)),
            _quote(sequence_id=2, received_offset=timedelta(seconds=1)),
            _quote(sequence_id=1, received_offset=timedelta(seconds=2)),
        ]
        slo = measure_quote_slo(
            quotes,
            now=T0 + timedelta(seconds=10),
            expected_interval=timedelta(seconds=1),
            late_threshold=timedelta(seconds=5),
        )
        assert slo.duplicate_rate == pytest.approx(1 / 3)
        assert slo.out_of_order_rate == pytest.approx(1 / 3)
        assert slo.revision_rate is None

    def test_slo_enforcement_rejects_bad_windows(self) -> None:
        quotes = [_quote(sequence_id=1)]
        slo = measure_quote_slo(
            quotes,
            now=T0 + timedelta(hours=2),
            expected_interval=timedelta(seconds=1),
            late_threshold=timedelta(seconds=5),
        )
        with pytest.raises(TypedFailure) as excinfo:
            enforce_quote_slo(
                slo,
                max_freshness_seconds=60.0,
                min_completeness=0.9,
                max_duplicate_rate=0.01,
                max_late_arrival_rate=0.01,
                max_out_of_order_rate=0.01,
            )
        assert excinfo.value.reason is FailureReason.INVALID

    def test_empty_stream_is_unavailable_not_perfect(self) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            measure_quote_slo(
                [],
                now=T0,
                expected_interval=timedelta(seconds=1),
                late_threshold=timedelta(seconds=5),
            )
        assert excinfo.value.reason is FailureReason.UNAVAILABLE
