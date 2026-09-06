"""Reconstructed counterfactual action labels for research-only learning.

The measured learning dataset is the heuristic's own accepted trades: 0.44% of
directional decisions ever became a trade, and every learning-eligible row is a
SHORT. ``E[R | X, LONG]`` is therefore unobserved, so a model fit on it cannot
compare LONG against SHORT against ABSTAIN.

This module reconstructs, for a decision the system did **not** trade, what each
action would have realised against the same delayed bid/ask tape, under the same
event-time entry window, the same ingest-delay budget, and the same modeled cost
policy that the replay writer applies.

Separation of evidence
----------------------
Reconstructed labels are **never** ``measured``. They carry
``label_source="reconstructed_counterfactual"`` and their own contract string, and
they are written to immutable run artifacts -- never into the canonical portfolio
database. A trade that actually executed is the only thing that may be called
measured. Mixing the two populations in one training set has to earn its place
out-of-sample before it is used, not be assumed.

What this is not
----------------
A reconstruction is not a fill. It says "had this action been taken, the published
tape implies this outcome". It inherits every limitation of the tape: Dukascopy
historical download is ``tradable=false`` by construction, so any model taught
from these labels stays shadow-only. Real-time tradable evidence would have to be
gathered independently before promotion could be argued.

Fail-closed
-----------
When PIT or execution integrity cannot be established the label is not invented.
The row records ``counterfactual_unavailable_reason`` and is excluded from the
dataset, so a gap is visible as a gap rather than as a zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .counterfactual_cost_policy import (
    CounterfactualAccountingCostPolicy,
    accounting_cost_r,
)
from .virtual_portfolio_replay import (
    QuoteTape,
    ReplayDataError,
    ReplayTick,
    VirtualReplayCostPolicy,
    VirtualReplayQuotePolicy,
    _first_at_or_after,
    _sha256_json,
    _ticks_between,
    horizon_for_timeframe,
)

COUNTERFACTUAL_CONTRACT = "virtual-decision-counterfactual-label-v1"
LABEL_SOURCE = "reconstructed_counterfactual"

Action = Literal["long", "short", "abstain"]
ACTIONS: tuple[Action, ...] = ("long", "short", "abstain")


class CounterfactualUnavailable(Exception):
    """Raised with a stable reason code when a label cannot be reconstructed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CounterfactualOutcome:
    """One action's reconstructed outcome for a single decision."""

    action: Action
    available: bool
    unavailable_reason: str | None
    net_r: float | None
    gross_r: float | None
    cost_r: float | None
    close_reason: str | None
    maximum_favorable_excursion_r: float | None
    maximum_adverse_excursion_r: float | None
    entry_executable: float | None
    exit_executable: float | None
    entry_event_time: datetime | None
    exit_event_time: datetime | None
    path_tick_count: int | None
    evidence: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "available": self.available,
            "counterfactual_unavailable_reason": self.unavailable_reason,
            "net_r": self.net_r,
            "gross_r": self.gross_r,
            "cost_r": self.cost_r,
            "close_reason": self.close_reason,
            "maximum_favorable_excursion_r": self.maximum_favorable_excursion_r,
            "maximum_adverse_excursion_r": self.maximum_adverse_excursion_r,
            "entry_executable": self.entry_executable,
            "exit_executable": self.exit_executable,
            "entry_event_time": (
                self.entry_event_time.isoformat() if self.entry_event_time else None
            ),
            "exit_event_time": (self.exit_event_time.isoformat() if self.exit_event_time else None),
            "path_tick_count": self.path_tick_count,
            "evidence": self.evidence,
        }


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReplayDataError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplayDataError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayDataError(f"{name} must be numeric")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ReplayDataError(f"{name} must be finite")
    return result


def _entry_tick(
    tape: QuoteTape,
    *,
    symbol: str,
    decision_time: datetime,
    quote_max_age_seconds: int,
    quote_policy: VirtualReplayQuotePolicy,
) -> ReplayTick:
    """Select the entry tick under the unchanged event-time window.

    The event-time window is the live contract and is not widened here. Only the
    tick's *arrival* may be late, bounded by the replay ingest budget.
    """

    deadline = decision_time + timedelta(seconds=quote_max_age_seconds)
    entry = _first_at_or_after(tape, symbol, decision_time, deadline)
    if entry is None:
        watermark = tape.watermark.get(symbol)
        if watermark is None or watermark < deadline:
            raise CounterfactualUnavailable("entry_window_not_yet_mature")
        raise CounterfactualUnavailable("no_executable_tick_in_entry_window")
    ingest_delay = (entry.available_time - entry.event_time).total_seconds()
    if ingest_delay > quote_policy.max_ingest_delay_seconds:
        raise CounterfactualUnavailable("entry_tick_exceeds_ingest_delay_budget")
    return entry


def _resolve_path(
    tape: QuoteTape,
    *,
    symbol: str,
    action: Action,
    entry: ReplayTick,
    entry_executable: float,
    stop: float,
    target: float,
    decision_time: datetime,
    horizon: timedelta,
) -> tuple[ReplayTick, str, float, float, Sequence[ReplayTick]]:
    """Walk the tape exactly as the replay writer does, for the chosen action."""

    horizon_end = decision_time + horizon
    watermark = tape.watermark.get(symbol)
    if watermark is None:
        raise CounterfactualUnavailable("no_tape_watermark_for_symbol")
    available_end = min(watermark, horizon_end)
    path = _ticks_between(tape, symbol, after=entry.event_time, through=available_end)
    if not path:
        if watermark >= horizon_end:
            raise CounterfactualUnavailable("mature_quote_path_is_empty")
        raise CounterfactualUnavailable("horizon_not_yet_mature")

    risk_distance = abs(entry_executable - stop)
    if risk_distance <= 0:
        raise CounterfactualUnavailable("zero_risk_distance")

    exit_index: int | None = None
    close_reason: str | None = None
    for index, tick in enumerate(path):
        executable = tick.bid if action == "long" else tick.ask
        stop_hit = executable <= stop if action == "long" else executable >= stop
        target_hit = executable >= target if action == "long" else executable <= target
        if stop_hit or target_hit:
            exit_index = index
            close_reason = "stop_loss" if stop_hit else "target1"
            break
    if exit_index is None:
        if watermark < horizon_end:
            raise CounterfactualUnavailable("horizon_not_yet_mature")
        exit_index = len(path) - 1
        close_reason = "time_exit"

    exit_path = list(path[: exit_index + 1])
    executable_prices = [tick.bid if action == "long" else tick.ask for tick in exit_path]
    if action == "long":
        mfe = (max(executable_prices) - entry_executable) / risk_distance
        mae = (entry_executable - min(executable_prices)) / risk_distance
    else:
        mfe = (entry_executable - min(executable_prices)) / risk_distance
        mae = (max(executable_prices) - entry_executable) / risk_distance
    assert close_reason is not None
    return exit_path[-1], close_reason, max(0.0, mfe), max(0.0, mae), exit_path


def _cost_r(
    *,
    cost_policy: VirtualReplayCostPolicy | CounterfactualAccountingCostPolicy,
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
) -> float:
    """Modeled cost expressed in R.

    ``CounterfactualAccountingCostPolicy`` は観測可能性で分解した会計policy
    (③)。スプレッドは ``gross_r`` で控除済みなので課金せず、financing は
    跨いだNY 17:00ロールオーバー回数で課金する。
    ``VirtualReplayCostPolicy`` は旧policyで、既存runの再現用に残す。
    """

    if isinstance(cost_policy, CounterfactualAccountingCostPolicy):
        return accounting_cost_r(
            policy=cost_policy,
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
        )

    started_days = max(1, int((exit_time - entry_time).total_seconds() // 86_400) + 1)
    bps = (
        cost_policy.slippage_r_bps
        + cost_policy.commission_r_bps
        + cost_policy.financing_r_bps_per_started_day * started_days
        + (0 if symbol[3:] == "JPY" else cost_policy.conversion_r_bps)
    )
    return bps / 10_000.0


def reconstruct_action_outcome(
    decision: Mapping[str, Any],
    *,
    action: Action,
    tape: QuoteTape,
    quote_policy: VirtualReplayQuotePolicy,
    cost_policy: VirtualReplayCostPolicy | CounterfactualAccountingCostPolicy,
    quote_max_age_seconds: int = 300,
) -> CounterfactualOutcome:
    """Reconstruct one action's outcome, or record why it cannot be reconstructed."""

    if action == "abstain":
        return CounterfactualOutcome(
            action="abstain",
            available=True,
            unavailable_reason=None,
            net_r=0.0,
            gross_r=0.0,
            cost_r=0.0,
            close_reason="abstain",
            maximum_favorable_excursion_r=0.0,
            maximum_adverse_excursion_r=0.0,
            entry_executable=None,
            exit_executable=None,
            entry_event_time=None,
            exit_event_time=None,
            path_tick_count=0,
            evidence={"contract": COUNTERFACTUAL_CONTRACT, "basis": "no position is taken"},
        )

    def unavailable(reason: str) -> CounterfactualOutcome:
        return CounterfactualOutcome(
            action=action,
            available=False,
            unavailable_reason=reason,
            net_r=None,
            gross_r=None,
            cost_r=None,
            close_reason=None,
            maximum_favorable_excursion_r=None,
            maximum_adverse_excursion_r=None,
            entry_executable=None,
            exit_executable=None,
            entry_event_time=None,
            exit_event_time=None,
            path_tick_count=None,
            evidence={"contract": COUNTERFACTUAL_CONTRACT, "basis": reason},
        )

    try:
        symbol = str(decision["symbol"]).upper()
        timeframe = str(decision["timeframe"])
        decision_time = _aware(decision["decision_time"], "decision_time")
        close_price = _number(decision["close"], "close")
        stop = _number(decision["stop"], "stop")
        horizon = horizon_for_timeframe(timeframe)
    except (KeyError, ReplayDataError) as error:
        return unavailable(f"invalid_decision_inputs:{error}")

    target = decision.get("target1")
    if target is None:
        return unavailable("missing_target")
    try:
        target_price = _number(target, "target1")
    except ReplayDataError:
        return unavailable("missing_target")

    # A counterfactual for the opposite action mirrors stop/target about the
    # decision close, so both actions are evaluated at the same risk distance.
    if action != str(decision.get("direction", "")).lower():
        risk = abs(close_price - stop)
        reward = abs(target_price - close_price)
        if action == "long":
            stop, target_price = close_price - risk, close_price + reward
        else:
            stop, target_price = close_price + risk, close_price - reward

    try:
        entry = _entry_tick(
            tape,
            symbol=symbol,
            decision_time=decision_time,
            quote_max_age_seconds=quote_max_age_seconds,
            quote_policy=quote_policy,
        )
    except CounterfactualUnavailable as error:
        return unavailable(error.reason)

    entry_executable = entry.ask if action == "long" else entry.bid
    try:
        exit_tick, close_reason, mfe, mae, exit_path = _resolve_path(
            tape,
            symbol=symbol,
            action=action,
            entry=entry,
            entry_executable=entry_executable,
            stop=stop,
            target=target_price,
            decision_time=decision_time,
            horizon=horizon,
        )
    except CounterfactualUnavailable as error:
        return unavailable(error.reason)

    exit_executable = exit_tick.bid if action == "long" else exit_tick.ask
    risk_distance = abs(entry_executable - stop)
    move = (
        exit_executable - entry_executable
        if action == "long"
        else entry_executable - exit_executable
    )
    gross_r = move / risk_distance
    cost_r = _cost_r(
        cost_policy=cost_policy,
        symbol=symbol,
        entry_time=entry.event_time,
        exit_time=exit_tick.event_time,
    )
    evidence = {
        "contract": COUNTERFACTUAL_CONTRACT,
        "label_source": LABEL_SOURCE,
        "data_mode": "delayed_historical_download",
        "tradable": False,
        "entry_source_record_id": entry.source_record_id,
        "exit_source_record_id": exit_tick.source_record_id,
        "path_source_ids_sha256": _sha256_json([tick.source_record_id for tick in exit_path]),
        "path_raw_payload_hashes_sha256": _sha256_json(
            [tick.raw_payload_sha256 for tick in exit_path]
        ),
        "entry_ingest_delay_seconds": (entry.available_time - entry.event_time).total_seconds(),
        "replay_quote_policy_sha256": quote_policy.sha256,
        "replay_cost_policy_sha256": cost_policy.sha256,
        "gap_policy": "first_observed_executable_tick; no optimistic stop/target cap",
        "same_tick_policy": "exact ticks are strictly ordered; first observed trigger wins",
    }
    return CounterfactualOutcome(
        action=action,
        available=True,
        unavailable_reason=None,
        net_r=gross_r - cost_r,
        gross_r=gross_r,
        cost_r=cost_r,
        close_reason=close_reason,
        maximum_favorable_excursion_r=mfe,
        maximum_adverse_excursion_r=mae,
        entry_executable=entry_executable,
        exit_executable=exit_executable,
        entry_event_time=entry.event_time,
        exit_event_time=exit_tick.event_time,
        path_tick_count=len(exit_path),
        evidence=evidence,
    )


def reconstruct_decision_counterfactuals(
    decision: Mapping[str, Any],
    *,
    tape: QuoteTape,
    quote_policy: VirtualReplayQuotePolicy | None = None,
    cost_policy: VirtualReplayCostPolicy | CounterfactualAccountingCostPolicy | None = None,
    quote_max_age_seconds: int = 300,
) -> dict[str, object]:
    """Reconstruct LONG, SHORT and ABSTAIN outcomes for one decision."""

    quotes = quote_policy or VirtualReplayQuotePolicy()
    quotes.validate()
    costs = cost_policy or VirtualReplayCostPolicy()
    costs.validate()
    outcomes = {
        action: reconstruct_action_outcome(
            decision,
            action=action,
            tape=tape,
            quote_policy=quotes,
            cost_policy=costs,
            quote_max_age_seconds=quote_max_age_seconds,
        )
        for action in ACTIONS
    }
    complete = all(outcomes[action].available for action in ACTIONS)
    payload: dict[str, object] = {
        "contract": COUNTERFACTUAL_CONTRACT,
        "label_source": LABEL_SOURCE,
        "measured": False,
        "promotion_eligible": False,
        "execution_target": "shadow_decision_only",
        "broker_connected": False,
        "decision_id": str(decision.get("decision_id") or ""),
        "symbol": str(decision.get("symbol") or "").upper(),
        "timeframe": str(decision.get("timeframe") or ""),
        "heuristic_direction": str(decision.get("direction") or ""),
        "complete": complete,
        "actions": {action: outcomes[action].to_dict() for action in ACTIONS},
        "replay_quote_policy_sha256": quotes.sha256,
        "replay_cost_policy_sha256": costs.sha256,
    }
    if not complete:
        payload["counterfactual_unavailable_reason"] = ";".join(
            sorted(
                {
                    str(outcomes[action].unavailable_reason)
                    for action in ACTIONS
                    if not outcomes[action].available
                }
            )
        )
    payload["record_sha256"] = _sha256_json(payload)
    return payload


__all__ = [
    "ACTIONS",
    "COUNTERFACTUAL_CONTRACT",
    "LABEL_SOURCE",
    "Action",
    "CounterfactualOutcome",
    "CounterfactualUnavailable",
    "reconstruct_action_outcome",
    "reconstruct_decision_counterfactuals",
]
