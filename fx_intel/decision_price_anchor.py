"""Re-anchor a decision's price levels onto the canonical (Dukascopy) frame.

S1 of docs/design/CANONICAL_PRICE_FRAME_DATA_TRUTH_20260807.md.

A decision's close/stop/target are computed against the TradingView scanner,
while fills, scoring and replay all read the Dukascopy tick tape. The two
frames disagree by a median 1.6 pips, which is ~38% of a 15m/1h stop distance,
and 4.9% of decisions are already past their own stop or target at t0
(docs/audits/PRICE_SOURCE_DIVERGENCE_20260807.md).

What survives a frame change is not the absolute price but the *shape* of the
plan: the stop distance in ATR units and the reward-to-risk ratio. Anchoring
replays those two onto the first canonical tick at or after the decision, so
the resulting levels live in the same frame as everything downstream.

This module is pure: it takes a decision and a tick and returns a verdict. It
performs no I/O, reads no database and reaches no network, so the anchoring
rule can be tested directly and reused by shadow and production paths alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

ANCHOR_CONTRACT = "decision-price-anchor-v1"

# The same microstructure floor the live sizing path enforces
# (virtual_portfolio.VirtualSizingInvariantPolicy). P0-C applies it when a
# trade opens; without it here, a decision whose stop lands inside the spread
# would produce a canonical risk distance near zero and an R that explodes.
MICROSTRUCTURE_FLOOR_MULTIPLE = Decimal("2")


def anchor_decision(
    *,
    direction: str,
    stop_atr_multiple: object,
    target_rr: object,
    reference_atr: object,
    decision_time: datetime,
    tick_event_time: datetime,
    tick_bid: object,
    tick_ask: object,
) -> dict[str, object]:
    """Resolve one decision's levels on the canonical frame.

    The caller must supply the **first** tick with
    ``event_time >= decision_time``; picking any later tick would leak future
    information into the anchor, and picking an earlier one would price the
    decision before it existed. This function re-checks that ordering rather
    than trusting it.

    Returns a verdict mapping in every case. An unanchorable decision yields
    ``status="rejected"`` with a reason instead of raising, so the caller can
    record why a decision has no canonical levels — that count is itself the
    measurement S1 exists to produce.
    """

    if direction not in {"long", "short"}:
        return _rejected("direction_not_tradeable", direction=direction)
    if tick_event_time < decision_time:
        return _rejected("tick_precedes_decision")

    atr = _positive(reference_atr)
    multiple = _positive(stop_atr_multiple)
    rr = _positive(target_rr)
    bid = _positive(tick_bid)
    ask = _positive(tick_ask)
    if atr is None or multiple is None:
        return _rejected("missing_atr_or_multiple")
    if rr is None:
        return _rejected("missing_target_rr")
    if bid is None or ask is None:
        return _rejected("missing_canonical_quote")
    if ask < bid:
        return _rejected("inverted_canonical_quote")

    is_long = direction == "long"
    # Executable side: a long pays the ask, a short receives the bid.
    entry = ask if is_long else bid
    spread = ask - bid
    risk_distance = atr * multiple

    if spread > 0 and risk_distance < spread * MICROSTRUCTURE_FLOOR_MULTIPLE:
        return _rejected(
            "microstructure_floor",
            risk_distance=str(risk_distance),
            spread=str(spread),
            entry=str(entry),
        )

    sign = Decimal(1) if is_long else Decimal(-1)
    stop = entry - sign * risk_distance
    target = entry + sign * risk_distance * rr

    return {
        "contract": ANCHOR_CONTRACT,
        "status": "anchored",
        "anchor_basis": "first_canonical_tick_at_or_after_decision",
        "canonical_source": "dukascopy",
        "entry": str(entry),
        "stop": str(stop),
        "target": str(target),
        "risk_distance": str(risk_distance),
        "spread_at_anchor": str(spread),
        "risk_distance_to_spread": (str(risk_distance / spread) if spread > 0 else None),
        "anchor_event_time": tick_event_time.isoformat(),
        "anchor_lag_seconds": (tick_event_time - decision_time).total_seconds(),
        "stop_atr_multiple": str(multiple),
        "target_rr": str(rr),
        "reference_atr": str(atr),
    }


def anchor_from_price_frame(
    price_frame: Mapping[str, object],
    *,
    direction: str,
    decision_time: datetime,
    tick_event_time: datetime,
    tick_bid: object,
    tick_ask: object,
) -> dict[str, object]:
    """Convenience wrapper taking the S0 ``price_frame`` payload directly."""

    return anchor_decision(
        direction=direction,
        stop_atr_multiple=price_frame.get("stop_atr_multiple"),
        target_rr=price_frame.get("target_rr"),
        reference_atr=price_frame.get("reference_atr"),
        decision_time=decision_time,
        tick_event_time=tick_event_time,
        tick_bid=tick_bid,
        tick_ask=tick_ask,
    )


def compare_to_reference(
    anchor: Mapping[str, object],
    *,
    reference_close: object,
    reference_stop: object,
    pip_size: object,
) -> dict[str, object]:
    """Quantify how far the canonical levels moved from the reference ones.

    This is the per-decision form of the population divergence already measured
    in docs/audits/PRICE_SOURCE_DIVERGENCE_20260807.md, kept alongside each
    anchor so the shift is auditable without recomputing it from two sources.
    """

    if anchor.get("status") != "anchored":
        return {"status": "unavailable", "reason": "decision was not anchored"}

    entry = _positive(anchor.get("entry"))
    close = _positive(reference_close)
    pip = _positive(pip_size)
    if entry is None or close is None or pip is None:
        return {"status": "unavailable", "reason": "missing comparison inputs"}

    divergence = (close - entry) / pip
    result: dict[str, object] = {
        "status": "available",
        "entry_divergence_pips": str(divergence),
        "abs_entry_divergence_pips": str(abs(divergence)),
    }
    ref_stop = _positive(reference_stop)
    if ref_stop is not None:
        reference_risk = abs(close - ref_stop)
        if reference_risk > 0:
            result["divergence_over_reference_risk"] = str(abs(close - entry) / reference_risk)
    return result


def _rejected(reason: str, **detail: object) -> dict[str, object]:
    return {
        "contract": ANCHOR_CONTRACT,
        "status": "rejected",
        "reason": reason,
        **detail,
    }


def _positive(value: object) -> Decimal | None:
    """Coerce to a strictly positive Decimal, or None if unusable."""

    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


__all__ = [
    "ANCHOR_CONTRACT",
    "MICROSTRUCTURE_FLOOR_MULTIPLE",
    "anchor_decision",
    "anchor_from_price_frame",
    "compare_to_reference",
]
