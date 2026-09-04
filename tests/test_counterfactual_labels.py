from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

import pytest

from fx_intel.counterfactual_labels import (
    ACTIONS,
    COUNTERFACTUAL_CONTRACT,
    LABEL_SOURCE,
    reconstruct_action_outcome,
    reconstruct_decision_counterfactuals,
)
from fx_intel.virtual_portfolio_replay import (
    QuoteTape,
    ReplayTick,
    VirtualReplayCostPolicy,
    VirtualReplayQuotePolicy,
)

START = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def _tick(offset_minutes: float, bid: float, ask: float, *, ingest_minutes: float = 110.0):
    event = START + timedelta(minutes=offset_minutes)
    identity = f"USDJPY:{event.isoformat()}:{bid}:{ask}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return ReplayTick(
        symbol="USDJPY",
        event_time=event,
        available_time=event + timedelta(minutes=ingest_minutes),
        bid=bid,
        ask=ask,
        raw_payload_sha256=digest,
        revision_id=digest[:32],
        sequence_id=None,
    )


def _tape(ticks) -> QuoteTape:
    ordered = tuple(sorted(ticks, key=lambda t: t.event_time))
    return QuoteTape(
        ticks={"USDJPY": ordered},
        watermark={"USDJPY": ordered[-1].event_time},
        source_path="/tmp/quotes.jsonl",
        source_size_bytes=1,
        source_prefix_sha256="0" * 64,
        rejected_rows=0,
    )


def _decision(direction: str = "short") -> dict[str, object]:
    # close 150.00, risk 0.50, reward 1.00 -> RR 2.0
    return {
        "decision_id": "cf-decision-1",
        "symbol": "USDJPY",
        "timeframe": "15m",
        "decision_time": START,
        "direction": direction,
        "close": 150.00,
        "stop": 150.50 if direction == "short" else 149.50,
        "target1": 149.00 if direction == "short" else 151.00,
    }


def _rising_tape() -> QuoteTape:
    # price climbs: a LONG should win, a SHORT should lose.
    return _tape(
        [
            _tick(1, 149.99, 150.01),
            _tick(5, 150.40, 150.42),
            _tick(10, 151.05, 151.07),
            _tick(20, 151.30, 151.32),
        ]
    )


def test_both_actions_are_valued_for_the_same_decision() -> None:
    """The point of the reconstruction: LONG and SHORT on one market state."""

    payload = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    assert payload["complete"] is True
    actions = payload["actions"]
    assert set(actions) == set(ACTIONS)
    long_r = actions["long"]["net_r"]
    short_r = actions["short"]["net_r"]
    assert long_r is not None and short_r is not None
    # On a rising tape the long is the better action, and they disagree in sign.
    assert long_r > short_r
    assert actions["abstain"]["net_r"] == 0.0


def test_reconstruction_is_never_labelled_measured() -> None:
    """Reconstructed evidence must not be confusable with an executed fill."""

    payload = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    assert payload["measured"] is False
    assert payload["label_source"] == LABEL_SOURCE
    assert payload["contract"] == COUNTERFACTUAL_CONTRACT
    assert payload["promotion_eligible"] is False
    assert payload["broker_connected"] is False
    assert payload["execution_target"] == "shadow_decision_only"
    for action in ("long", "short"):
        assert payload["actions"][action]["evidence"]["tradable"] is False


def test_costs_are_deducted_so_net_is_below_gross() -> None:
    payload = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    for action in ("long", "short"):
        row = payload["actions"][action]
        assert row["cost_r"] > 0.0
        assert row["net_r"] == pytest.approx(row["gross_r"] - row["cost_r"])


def test_long_and_short_use_opposite_sides_of_the_spread() -> None:
    """A long pays the ask on entry; a short receives the bid."""

    tape = _rising_tape()
    first = tape.ticks["USDJPY"][0]
    payload = reconstruct_decision_counterfactuals(_decision(), tape=tape)
    assert payload["actions"]["long"]["entry_executable"] == first.ask
    assert payload["actions"]["short"]["entry_executable"] == first.bid


def test_entry_outside_the_event_time_window_is_refused(tmp_path) -> None:
    """M2: the event-time entry window is not widened for reconstruction."""

    # first tick is 30 minutes after the decision: outside the 300s window
    tape = _tape([_tick(30, 149.99, 150.01), _tick(40, 150.40, 150.42)])
    outcome = reconstruct_action_outcome(
        _decision(),
        action="long",
        tape=tape,
        quote_policy=VirtualReplayQuotePolicy(),
        cost_policy=VirtualReplayCostPolicy(),
    )
    assert outcome.available is False
    assert outcome.unavailable_reason == "no_executable_tick_in_entry_window"
    assert outcome.net_r is None


def test_entry_beyond_the_ingest_budget_is_refused() -> None:
    """M3: the replay ingest budget still governs reconstruction."""

    tape = _tape(
        [
            _tick(1, 149.99, 150.01, ingest_minutes=400.0),
            _tick(10, 151.05, 151.07, ingest_minutes=400.0),
            _tick(20, 151.30, 151.32, ingest_minutes=400.0),
        ]
    )
    outcome = reconstruct_action_outcome(
        _decision(),
        action="long",
        tape=tape,
        quote_policy=VirtualReplayQuotePolicy(),  # 12600s = 210min budget
        cost_policy=VirtualReplayCostPolicy(),
    )
    assert outcome.available is False
    assert outcome.unavailable_reason == "entry_tick_exceeds_ingest_delay_budget"


def test_immature_horizon_is_reported_not_invented() -> None:
    """Fail-closed: a gap is recorded as a reason, never as a zero."""

    # tape stops 5 minutes in, well inside the 15m horizon and with no exit
    tape = _tape([_tick(1, 149.99, 150.01), _tick(5, 150.02, 150.04)])
    payload = reconstruct_decision_counterfactuals(_decision(), tape=tape)
    assert payload["complete"] is False
    assert "counterfactual_unavailable_reason" in payload
    assert payload["actions"]["long"]["net_r"] is None
    # abstain remains valued: taking no position has a defined outcome.
    assert payload["actions"]["abstain"]["net_r"] == 0.0


def test_stop_and_target_are_mirrored_for_the_untaken_action() -> None:
    """The opposite action is evaluated at the same risk distance, not a free one."""

    decision = _decision("short")  # close 150.00, stop 150.50, target 149.00
    tape = _rising_tape()
    payload = reconstruct_decision_counterfactuals(decision, tape=tape)
    long_row = payload["actions"]["long"]
    # Mirrored: stop 149.50, target 151.00 -> the rising tape hits the target.
    assert long_row["close_reason"] == "target1"
    assert long_row["net_r"] > 0


def test_record_hash_is_stable_and_covers_the_payload() -> None:
    a = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    b = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    assert a["record_sha256"] == b["record_sha256"]
    c = reconstruct_decision_counterfactuals(_decision("long"), tape=_rising_tape())
    assert c["record_sha256"] != a["record_sha256"]


def test_no_tick_before_the_decision_can_enter_the_label() -> None:
    """M1: a future/past-leak guard -- entry is never taken from before the decision.

    The entry search starts at decision_time, so a tick that predates the decision
    must not be selectable even when it is the closest one."""

    pre = _tick(-30, 100.00, 100.02)  # absurd price well before the decision
    tape = _tape(
        [pre, _tick(1, 149.99, 150.01), _tick(10, 151.05, 151.07), _tick(20, 151.30, 151.32)]
    )
    payload = reconstruct_decision_counterfactuals(_decision(), tape=tape)
    entry = payload["actions"]["long"]["entry_executable"]
    assert entry == 150.01, "entry must come from the first tick at or after the decision"
    assert (
        payload["actions"]["long"]["entry_event_time"] == (START + timedelta(minutes=1)).isoformat()
    )


def test_path_never_uses_ticks_that_had_not_yet_arrived() -> None:
    """PIT: every tick contributing to the label was ingested within budget."""

    payload = reconstruct_decision_counterfactuals(_decision(), tape=_rising_tape())
    delay = payload["actions"]["long"]["evidence"]["entry_ingest_delay_seconds"]
    assert 0 < delay <= VirtualReplayQuotePolicy().max_ingest_delay_seconds


def test_policy_hashes_are_recorded_for_audit() -> None:
    """Every label carries the exact policies it was produced under."""

    quotes = VirtualReplayQuotePolicy()
    costs = VirtualReplayCostPolicy()
    payload = reconstruct_decision_counterfactuals(
        _decision(), tape=_rising_tape(), quote_policy=quotes, cost_policy=costs
    )
    assert payload["replay_quote_policy_sha256"] == quotes.sha256
    assert payload["replay_cost_policy_sha256"] == costs.sha256
