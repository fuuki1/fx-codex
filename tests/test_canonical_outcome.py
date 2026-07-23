"""Executable bid/askを使う正準paper outcome会計の反証テスト。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel.evaluation_labels import (
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_STATUS,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
    CostModelResult,
)
from fx_intel.trade_outcome import (
    CanonicalOutcomeConflict,
    CanonicalTradeRequest,
    ExecutableQuoteBar,
    assert_canonical_outcome_idempotent,
    score_canonical_outcome,
)

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _cost(
    *,
    slippage_r: float = 0.0,
    commission_r: float = 0.0,
    financing_r: float = 0.0,
    cost_model_id: str = DEFAULT_COST_MODEL_ID,
) -> CostModelResult:
    return CostModelResult(
        cost_model_id=cost_model_id,
        cost_model_version="1",
        entry_quote_source="fixture",
        spread_source="fixture",
        slippage_model_id="zero-slippage-v1",
        commission_model_id="zero-commission-v1",
        slippage_r=slippage_r,
        commission_r=commission_r,
        financing_r=financing_r,
        cost_status=DEFAULT_COST_STATUS,
    )


def _bar(
    minutes: int,
    *,
    bid_open: float,
    bid_high: float,
    bid_low: float,
    bid_close: float,
    ask_open: float,
    ask_high: float,
    ask_low: float,
    ask_close: float,
) -> ExecutableQuoteBar:
    return ExecutableQuoteBar(
        ts=NOW + timedelta(minutes=minutes),
        bid_open=bid_open,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=bid_close,
        ask_open=ask_open,
        ask_high=ask_high,
        ask_low=ask_low,
        ask_close=ask_close,
        quote_source="fixture",
        source_record_id=f"bar-{minutes}",
        path_quality=1.0,
        bar_start=NOW + timedelta(minutes=minutes - 5),
        available_time=NOW + timedelta(minutes=minutes),
    )


def _request(
    *,
    direction: str = "long",
    entry_bid: float | None = 99.9,
    entry_ask: float | None = 100.1,
    stop: float | None = None,
    target1: float | None = None,
    target2: float | None = None,
    path: tuple[ExecutableQuoteBar, ...] | None = None,
    quote_observed_at: datetime = NOW,
    label_version: str = NET_LABEL_VERSION,
    cost_model: CostModelResult | None = None,
) -> CanonicalTradeRequest:
    if direction == "long":
        stop = 99.1 if stop is None else stop
        target1 = 101.1 if target1 is None else target1
        target2 = 102.1 if target2 is None else target2
    else:
        stop = 100.9 if stop is None else stop
        target1 = 98.9 if target1 is None else target1
        target2 = 97.9 if target2 is None else target2
    default_path = (
        _bar(
            60,
            bid_open=100.0,
            bid_high=101.2,
            bid_low=99.8,
            bid_close=101.1,
            ask_open=100.2,
            ask_high=101.4,
            ask_low=100.0,
            ask_close=101.3,
        ),
    )
    return CanonicalTradeRequest(
        decision_id="decision-1",
        label_version=label_version,
        label_provenance=NET_LABEL_PROVENANCE,
        symbol="USDJPY",
        mode="per_timeframe",
        timeframe="1h",
        horizon_hours=1.0,
        direction=direction,
        prediction_time=NOW,
        entry_mid=100.0,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        quote_observed_at=quote_observed_at,
        quote_available_at=quote_observed_at,
        quote_source="fixture",
        source_record_id="entry-1",
        stop=stop,
        target1=target1,
        target2=target2,
        planned_risk_distance=1.0,
        path=default_path if path is None else path,
        cost_model=cost_model if cost_model is not None else _cost(),
    )


def test_long_uses_ask_entry_and_bid_exit_without_spread_double_subtraction() -> None:
    outcome = score_canonical_outcome(_request())

    assert outcome.first_touch == "tp1"
    assert outcome.entry_executable == 100.1
    assert outcome.exit_executable == 101.1
    assert outcome.quote_realized_r == pytest.approx(1.0)
    assert outcome.realized_net_r == pytest.approx(1.0)
    assert outcome.entry_spread_r == pytest.approx(0.2)
    assert outcome.additional_cost_r == 0.0
    assert outcome.execution_cost_r == pytest.approx(
        outcome.gross_realized_r - outcome.realized_net_r
    )
    assert outcome.net_label_eligible is True


def test_short_uses_bid_entry_and_ask_exit() -> None:
    path = (
        _bar(
            60,
            bid_open=99.8,
            bid_high=100.0,
            bid_low=98.6,
            bid_close=98.7,
            ask_open=100.0,
            ask_high=100.2,
            ask_low=98.9,
            ask_close=98.9,
        ),
    )
    outcome = score_canonical_outcome(_request(direction="short", path=path))

    assert outcome.first_touch == "tp1"
    assert outcome.entry_executable == 99.9
    assert outcome.exit_executable == 98.9
    assert outcome.quote_realized_r == pytest.approx(1.0)
    assert outcome.realized_net_r == pytest.approx(1.0)


def test_terminal_exit_and_spread_widening_are_embedded_in_quote_r() -> None:
    path = (
        _bar(
            60,
            bid_open=100.0,
            bid_high=100.3,
            bid_low=99.8,
            bid_close=100.0,
            ask_open=100.2,
            ask_high=100.6,
            ask_low=100.0,
            ask_close=100.4,
        ),
    )
    outcome = score_canonical_outcome(_request(path=path))

    assert outcome.first_touch == "terminal"
    assert outcome.quote_realized_r == pytest.approx(-0.1)
    assert outcome.realized_net_r == pytest.approx(-0.1)
    assert outcome.gross_realized_r == pytest.approx(0.2)
    assert outcome.execution_cost_r == pytest.approx(0.3)


def test_planned_risk_distance_is_the_only_r_denominator() -> None:
    path = (
        _bar(
            60,
            bid_open=100.0,
            bid_high=100.6,
            bid_low=99.8,
            bid_close=100.4,
            ask_open=100.2,
            ask_high=100.8,
            ask_low=100.0,
            ask_close=100.6,
        ),
    )
    # stopまで10価格単位でも、凍結済みplanned risk=1.0を唯一の分母にする。
    request = _request(stop=90.0, target1=110.0, target2=120.0, path=path)
    outcome = score_canonical_outcome(request)

    assert outcome.first_touch == "terminal"
    assert outcome.quote_realized_r == pytest.approx(0.3)
    assert outcome.gross_realized_r == pytest.approx(0.5)


def test_modelled_costs_are_subtracted_once_after_executable_quote_r() -> None:
    outcome = score_canonical_outcome(
        _request(cost_model=_cost(slippage_r=0.05, commission_r=0.02, financing_r=0.01))
    )

    assert outcome.quote_realized_r == pytest.approx(1.0)
    assert outcome.slippage_r == pytest.approx(0.05)
    assert outcome.commission_r == pytest.approx(0.02)
    assert outcome.financing_r == pytest.approx(0.01)
    assert outcome.additional_cost_r == pytest.approx(0.08)
    assert outcome.realized_net_r == pytest.approx(0.92)


@pytest.mark.parametrize(
    ("bar", "touch", "expected_quote_r"),
    [
        (
            _bar(
                60,
                bid_open=100.0,
                bid_high=102.2,
                bid_low=99.8,
                bid_close=102.1,
                ask_open=100.2,
                ask_high=102.4,
                ask_low=100.0,
                ask_close=102.3,
            ),
            "tp2",
            2.0,
        ),
        (
            _bar(
                60,
                bid_open=100.0,
                bid_high=100.2,
                bid_low=99.0,
                bid_close=99.2,
                ask_open=100.2,
                ask_high=100.4,
                ask_low=99.2,
                ask_close=99.4,
            ),
            "sl",
            -1.0,
        ),
    ],
)
def test_tp2_and_non_gap_stop_use_planned_levels(
    bar: ExecutableQuoteBar,
    touch: str,
    expected_quote_r: float,
) -> None:
    outcome = score_canonical_outcome(_request(path=(bar,)))

    assert outcome.first_touch == touch
    assert outcome.quote_realized_r == pytest.approx(expected_quote_r)
    assert outcome.planned_payoff_r == pytest.approx(expected_quote_r)


def test_gap_through_stop_fills_at_first_executable_worse_price() -> None:
    path = (
        _bar(
            60,
            bid_open=98.5,
            bid_high=98.8,
            bid_low=98.2,
            bid_close=98.6,
            ask_open=98.7,
            ask_high=99.0,
            ask_low=98.4,
            ask_close=98.8,
        ),
    )
    outcome = score_canonical_outcome(_request(path=path))

    assert outcome.first_touch == "sl_gap"
    assert outcome.exit_executable == 98.5
    assert outcome.realized_net_r == pytest.approx(-1.6)
    assert outcome.planned_payoff_r == -1.0


def test_same_bar_stop_and_target_is_fail_closed_stop_first() -> None:
    path = (
        _bar(
            60,
            bid_open=100.0,
            bid_high=101.2,
            bid_low=99.0,
            bid_close=100.5,
            ask_open=100.2,
            ask_high=101.4,
            ask_low=99.2,
            ask_close=100.7,
        ),
    )
    outcome = score_canonical_outcome(_request(path=path))

    assert outcome.first_touch == "ambiguous_sl_tp"
    assert outcome.exit_executable == 99.1
    assert outcome.realized_net_r == pytest.approx(-1.0)
    assert "same_bar_ambiguous_stop_first" in outcome.quality_flags


@pytest.mark.parametrize(
    ("case_request", "flag"),
    [
        (_request(entry_bid=None), "missing_entry_quote"),
        (
            _request(quote_observed_at=NOW - timedelta(minutes=6)),
            "stale_entry_quote",
        ),
        (_request(entry_bid=100.2, entry_ask=100.1), "invalid_entry_quote"),
        (_request(label_version="unknown-label"), "unknown_label_version"),
        (
            _request(cost_model=_cost(cost_model_id="unknown-cost-model")),
            "unknown_cost_model",
        ),
    ],
)
def test_missing_stale_crossed_or_unknown_inputs_make_net_label_unavailable(
    case_request: CanonicalTradeRequest,
    flag: str,
) -> None:
    outcome = score_canonical_outcome(case_request)

    assert outcome.realized_net_r is None
    assert outcome.net_label_eligible is False
    assert flag in outcome.quality_flags


def test_invalid_future_quote_fails_closed() -> None:
    crossed = _bar(
        60,
        bid_open=100.2,
        bid_high=100.3,
        bid_low=100.0,
        bid_close=100.2,
        ask_open=100.1,
        ask_high=100.2,
        ask_low=99.9,
        ask_close=100.1,
    )
    outcome = score_canonical_outcome(_request(path=(crossed,)))

    assert outcome.realized_net_r is None
    assert outcome.net_label_eligible is False
    assert "invalid_path_quote" in outcome.quality_flags


def test_forming_bar_that_started_before_prediction_is_rejected() -> None:
    forming = replace(
        _request().path[0],
        bar_start=NOW - timedelta(minutes=1),
    )
    outcome = score_canonical_outcome(_request(path=(forming,)))

    assert outcome.realized_net_r is None
    assert outcome.net_label_eligible is False
    assert "forming_bar_path" in outcome.quality_flags


def test_scoring_is_deterministic_and_conflicting_same_key_is_hard_error() -> None:
    request = _request()
    first = score_canonical_outcome(request)
    replay = score_canonical_outcome(request)

    assert first == replay
    assert_canonical_outcome_idempotent(first, replay)

    conflicting = replace(replay, realized_net_r=0.5)
    with pytest.raises(CanonicalOutcomeConflict, match="decision-1.*net-r-v1"):
        assert_canonical_outcome_idempotent(first, conflicting)
