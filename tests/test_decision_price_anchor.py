"""S1: re-anchoring decision levels onto the canonical price frame.

The production defect these tests pin: decision levels come from the
TradingView scanner while everything downstream reads the Dukascopy tape, so a
decision's stop can already sit on the wrong side of the canonical price
(docs/audits/PRICE_SOURCE_DIVERGENCE_20260807.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fx_intel.decision_price_anchor import (
    anchor_decision,
    anchor_from_price_frame,
    compare_to_reference,
)

T0 = datetime(2026, 8, 3, 1, 18, 12, tzinfo=UTC)


def _frame(**overrides: object) -> dict[str, object]:
    frame: dict[str, object] = {
        "stop_atr_multiple": 0.25,
        "target_rr": 1.0,
        "reference_atr": 0.00135,
    }
    frame.update(overrides)
    return frame


def test_anchoring_repairs_the_production_divergence_case() -> None:
    """decision 5a731b85: TV close 1.15382 vs tick ~1.15342, stop in between.

    In the reference frame the stop sat between the decision close and the real
    price, so a long was already stopped out at t0. Re-anchoring rebuilds the
    same 0.25 ATR shape around the canonical entry, restoring a usable
    denominator.
    """

    anchor = anchor_from_price_frame(
        _frame(),
        direction="long",
        decision_time=T0,
        tick_event_time=T0 + timedelta(seconds=2),
        tick_bid=1.15337,
        tick_ask=1.15342,
    )

    assert anchor["status"] == "anchored"
    # Long enters at the ask.
    assert anchor["entry"] == "1.15342"
    assert anchor["risk_distance"] == "0.0003375"
    # The stop is now well outside the spread rather than inside it.
    assert float(str(anchor["risk_distance_to_spread"])) > 2.0
    assert anchor["anchor_lag_seconds"] == 2.0


def test_short_anchors_on_the_bid_and_inverts_the_levels() -> None:
    anchor = anchor_decision(
        direction="short",
        stop_atr_multiple=0.25,
        target_rr=1.5,
        reference_atr=0.00135,
        decision_time=T0,
        tick_event_time=T0,
        tick_bid=1.15337,
        tick_ask=1.15342,
    )

    assert anchor["entry"] == "1.15337"
    # A short's stop sits above entry and its target below.
    assert float(str(anchor["stop"])) > float(str(anchor["entry"]))
    assert float(str(anchor["target"])) < float(str(anchor["entry"]))


def test_microstructure_floor_rejects_a_stop_inside_the_spread() -> None:
    """Same guard as P0-C, applied at decision time instead of at fill.

    Production measured 10.9% of 15m and 10.6% of 1h decisions already past
    their own stop at t0; without this floor those become near-zero
    denominators and R explodes.
    """

    anchor = anchor_decision(
        direction="long",
        stop_atr_multiple=0.01,
        target_rr=1.0,
        reference_atr=0.00135,
        decision_time=T0,
        tick_event_time=T0,
        tick_bid=1.15337,
        tick_ask=1.15342,
    )

    assert anchor["status"] == "rejected"
    assert anchor["reason"] == "microstructure_floor"


def test_a_tick_before_the_decision_is_refused() -> None:
    """PIT: the anchor may not be priced before the decision existed."""

    anchor = anchor_decision(
        direction="long",
        stop_atr_multiple=0.25,
        target_rr=1.0,
        reference_atr=0.00135,
        decision_time=T0,
        tick_event_time=T0 - timedelta(seconds=1),
        tick_bid=1.15337,
        tick_ask=1.15342,
    )

    assert anchor["status"] == "rejected"
    assert anchor["reason"] == "tick_precedes_decision"


def test_non_directional_and_malformed_inputs_are_rejected_not_raised() -> None:
    """Rejection counts are the S1 measurement, so nothing may raise."""

    common = {
        "decision_time": T0,
        "tick_event_time": T0,
        "tick_bid": 1.15337,
        "tick_ask": 1.15342,
    }
    neutral = anchor_decision(
        direction="neutral",
        stop_atr_multiple=0.25,
        target_rr=1.0,
        reference_atr=0.00135,
        **common,
    )
    no_atr = anchor_decision(
        direction="long",
        stop_atr_multiple=0.25,
        target_rr=1.0,
        reference_atr=None,
        **common,
    )
    no_rr = anchor_decision(
        direction="long",
        stop_atr_multiple=0.25,
        target_rr=None,
        reference_atr=0.00135,
        **common,
    )
    inverted = anchor_decision(
        direction="long",
        stop_atr_multiple=0.25,
        target_rr=1.0,
        reference_atr=0.00135,
        decision_time=T0,
        tick_event_time=T0,
        tick_bid=1.15342,
        tick_ask=1.15337,
    )

    assert neutral["reason"] == "direction_not_tradeable"
    assert no_atr["reason"] == "missing_atr_or_multiple"
    assert no_rr["reason"] == "missing_target_rr"
    assert inverted["reason"] == "inverted_canonical_quote"


def test_anchoring_is_deterministic_for_the_same_inputs() -> None:
    """Anchoring runs once and is immutable afterwards; it must be stable."""

    kwargs = {
        "direction": "long",
        "stop_atr_multiple": 0.25,
        "target_rr": 1.0,
        "reference_atr": 0.00135,
        "decision_time": T0,
        "tick_event_time": T0 + timedelta(seconds=2),
        "tick_bid": 1.15337,
        "tick_ask": 1.15342,
    }

    assert anchor_decision(**kwargs) == anchor_decision(**kwargs)


def test_comparison_reports_the_shift_from_the_reference_frame() -> None:
    anchor = anchor_from_price_frame(
        _frame(),
        direction="long",
        decision_time=T0,
        tick_event_time=T0,
        tick_bid=1.15337,
        tick_ask=1.15342,
    )
    comparison = compare_to_reference(
        anchor,
        reference_close=1.15382,
        reference_stop=1.15341974,
        pip_size=0.0001,
    )

    assert comparison["status"] == "available"
    assert float(str(comparison["abs_entry_divergence_pips"])) == 4.0
    # The divergence is almost exactly one reference risk distance: this single
    # decision is why the reference-frame stop was unreachable.
    assert float(str(comparison["divergence_over_reference_risk"])) > 0.9


def test_comparison_is_unavailable_when_the_decision_was_rejected() -> None:
    rejected = anchor_decision(
        direction="neutral",
        stop_atr_multiple=0.25,
        target_rr=1.0,
        reference_atr=0.00135,
        decision_time=T0,
        tick_event_time=T0,
        tick_bid=1.15337,
        tick_ask=1.15342,
    )

    assert (
        compare_to_reference(
            rejected, reference_close=1.15382, reference_stop=1.15341974, pip_size=0.0001
        )["status"]
        == "unavailable"
    )
