"""Provider-neutral decision-event to canonical-outcome offline pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, UTC
import hashlib
import json
import math
from pathlib import Path

from . import journal
from .canonical_outcome_store import append_canonical_outcomes
from .evaluation_labels import CostModelResult
from .market import open_hours_between
from .trade_outcome import (
    CanonicalTradeOutcome,
    CanonicalTradeRequest,
    ExecutableQuoteBar,
    score_canonical_outcome,
)

COMPLETED_BID_ASK_SCOPE = "completed_bid_ask_bar"
INVALID_TIME = datetime.min
HORIZON_EPSILON_HOURS = 1e-9
RETRYABLE_PATH_FLAGS = frozenset(
    {
        "forming_bar_rejected",
        "missing_path_content_hash",
        "path_content_hash_mismatch",
        "invalid_path_row_time",
        "invalid_path_row",
        "path_gap",
        "incomplete_horizon_path",
        "missing_executable_path",
        "invalid_path_time",
        "missing_path_availability",
        "forming_bar_path",
        "invalid_path_availability",
        "out_of_order_path",
        "missing_path_provenance",
        "path_source_mismatch",
        "invalid_path_quality",
        "invalid_path_quote",
        "invalid_path_ohlc",
    }
)


class CanonicalOutcomePipelineError(RuntimeError):
    """Decision or price inputs conflict before canonical scoring."""


@dataclass(frozen=True)
class CanonicalPipelineReport:
    input_events: int
    counterfactual_events: int
    events: int
    scored: int
    eligible: int
    ineligible: int
    pending: int
    persistable: int
    appended: int
    outcomes: tuple[CanonicalTradeOutcome, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "input_events": self.input_events,
            "counterfactual_events": self.counterfactual_events,
            "events": self.events,
            "scored": self.scored,
            "eligible": self.eligible,
            "ineligible": self.ineligible,
            "pending": self.pending,
            "persistable": self.persistable,
            "appended": self.appended,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def score_and_store_decision_events(
    events: Sequence[Mapping[str, object]],
    price_rows: Sequence[Mapping[str, object]],
    store_path: str | Path,
    *,
    include_guard_counterfactuals: bool = True,
) -> CanonicalPipelineReport:
    """Score an immutable event batch and idempotently persist every outcome."""

    counterfactuals = (
        guard_counterfactual_decision_events(events) if include_guard_counterfactuals else ()
    )
    scoring_events = (*events, *counterfactuals)
    outcomes = tuple(
        score_canonical_outcome(build_canonical_trade_request(event, price_rows))
        for event in scoring_events
    )
    persistable = tuple(outcome for outcome in outcomes if not _retryable_path_outcome(outcome))
    appended = append_canonical_outcomes(store_path, persistable) if persistable else 0
    eligible = sum(outcome.net_label_eligible for outcome in outcomes)
    return CanonicalPipelineReport(
        input_events=len(events),
        counterfactual_events=len(counterfactuals),
        events=len(scoring_events),
        scored=len(outcomes),
        eligible=eligible,
        ineligible=len(outcomes) - eligible,
        pending=len(outcomes) - len(persistable),
        persistable=len(persistable),
        appended=appended,
        outcomes=outcomes,
    )


def guard_counterfactual_decision_events(
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Materialize guard-only pre-guard plans as immutable canonical child events.

    The compact journal owns the eligibility rules. Complete decision events are
    flattened only for that validation, then restored to the canonical pipeline's
    nested event schema. No raw score or shadow prediction is consulted.
    """

    output: list[dict[str, object]] = []
    event_envelope_keys = (
        "ts",
        "prediction_time",
        "source_cutoff",
        "max_feature_available_time",
        "pit_eligible",
        "pit_contract",
        "decision_id",
        "run_id",
        "mode",
        "producer",
        "producer_version",
        "input_context_id",
        "source_record_ids",
        "symbol",
        "timeframe",
        "horizon_hours",
    )
    decision_fields = (
        "direction",
        "conviction",
        "stop",
        "target1",
        "target2",
        "target_policy",
        "entry_bid",
        "entry_ask",
        "quote_observed_at",
        "quote_available_at",
        "quote_source",
        "quote_source_record_id",
        "planned_risk_distance",
        "label_version",
        "label_provenance",
        "cost_model_id",
        "cost_model_version",
        "cost_status",
        "slippage_model_id",
        "commission_model_id",
        "slippage_r",
        "commission_r",
        "financing_r",
        "canonical_net_label_input_eligible",
        "canonical_net_label_status",
    )
    for event in events:
        decision = event.get("decision")
        if not isinstance(decision, Mapping):
            continue
        flat = dict(decision)
        for key in event_envelope_keys:
            flat[key] = event.get(key)
        synthesized = journal.counterfactual_guard_entries((flat,))
        if not synthesized:
            continue
        restored = synthesized[0]
        child_decision = dict(decision)
        for key in decision_fields:
            child_decision[key] = restored.get(key)
        child_event = dict(event)
        child_event.update(
            {
                "decision_id": restored["decision_id"],
                "parent_decision_id": restored["parent_decision_id"],
                journal.COUNTERFACTUAL_ENTRY_KEY: True,
                "decision": child_decision,
            }
        )
        output.append(child_event)
    return tuple(output)


def build_canonical_trade_request(
    event: Mapping[str, object],
    price_rows: Sequence[Mapping[str, object]],
) -> CanonicalTradeRequest:
    """Freeze one decision event and its completed executable quote path."""

    decision = event.get("decision")
    decision = decision if isinstance(decision, Mapping) else {}
    prediction = _time(event.get("ts"))
    horizon = _positive_number(event.get("horizon_hours", decision.get("horizon_hours")))
    symbol = _symbol(event.get("symbol", decision.get("symbol")))
    timeframe = str(event.get("timeframe", decision.get("timeframe", "")) or "").strip()
    path, path_flags = _executable_path(
        price_rows,
        symbol=symbol,
        timeframe=timeframe,
        prediction=prediction,
        horizon_hours=horizon,
    )
    entry_bid = _number(decision.get("entry_bid"))
    entry_ask = _number(decision.get("entry_ask"))
    entry_mid = (
        (entry_bid + entry_ask) / 2.0
        if entry_bid is not None and entry_ask is not None
        else _number(decision.get("close"))
    )
    quote_source = str(decision.get("quote_source") or "")
    return CanonicalTradeRequest(
        decision_id=str(event.get("decision_id") or ""),
        label_version=str(decision.get("label_version") or ""),
        label_provenance=str(decision.get("label_provenance") or ""),
        symbol=symbol,
        mode=str(event.get("mode") or ""),
        timeframe=timeframe,
        horizon_hours=horizon or 0.0,
        direction=str(decision.get("direction") or ""),
        prediction_time=prediction,
        entry_mid=entry_mid,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        quote_observed_at=_time(decision.get("quote_observed_at")),
        quote_available_at=_time(decision.get("quote_available_at")),
        quote_source=quote_source,
        source_record_id=str(decision.get("quote_source_record_id") or ""),
        stop=_number(decision.get("stop")),
        target1=_number(decision.get("target1")),
        target2=_number(decision.get("target2")),
        planned_risk_distance=_number(decision.get("planned_risk_distance")),
        path=path,
        cost_model=_cost_model(decision, quote_source),
        quality_flags=path_flags,
    )


def _executable_path(
    rows: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    timeframe: str,
    prediction: datetime,
    horizon_hours: float | None,
) -> tuple[tuple[ExecutableQuoteBar, ...], tuple[str, ...]]:
    flags: list[str] = []
    if (
        prediction.tzinfo is None
        or prediction.utcoffset() is None
        or horizon_hours is None
        or not symbol
        or not timeframe
    ):
        return (), ("invalid_path_selection_context",)

    by_start: dict[datetime, tuple[str, ExecutableQuoteBar]] = {}
    record_owners: dict[tuple[str, str], datetime] = {}
    terminal_age = 0.0
    for row in rows:
        if _symbol(row.get("symbol")) != symbol or str(row.get("timeframe") or "") != timeframe:
            continue
        if row.get("ohlc_scope") != COMPLETED_BID_ASK_SCOPE or row.get("complete") is not True:
            flags.append("forming_bar_rejected")
            continue
        content_hash = str(row.get("content_hash") or "")
        if not content_hash:
            flags.append("missing_path_content_hash")
            continue
        if content_hash != _content_hash(row):
            flags.append("path_content_hash_mismatch")
            continue
        start = _optional_time(row.get("bar_start"))
        end = _optional_time(row.get("bar_end") or row.get("event_time") or row.get("ts"))
        available = _optional_time(row.get("available_time"))
        if start is None or end is None or available is None:
            flags.append("invalid_path_row_time")
            continue
        if start < prediction.astimezone(UTC):
            continue
        age = open_hours_between(prediction, end)
        if age <= 0 or age > horizon_hours + HORIZON_EPSILON_HOURS:
            continue
        bar = _quote_bar(row, start=start, end=end, available=available)
        if bar is None:
            flags.append("invalid_path_row")
            continue
        quality_flags = row.get("data_quality_flags")
        if isinstance(quality_flags, list) and quality_flags:
            flags.extend(f"path_quality_flag:{value}" for value in quality_flags)
        prior = by_start.get(start)
        if prior is not None:
            prior_hash, _prior_bar = prior
            if prior_hash != content_hash:
                raise CanonicalOutcomePipelineError(
                    f"conflicting completed bars for {symbol} {timeframe} at {start.isoformat()}"
                )
            continue
        record_key = (bar.quote_source, bar.source_record_id)
        prior_start = record_owners.get(record_key)
        if prior_start is not None and prior_start != start:
            raise CanonicalOutcomePipelineError(
                f"source record {record_key} claims multiple bar starts"
            )
        record_owners[record_key] = start
        by_start[start] = (content_hash, bar)
        terminal_age = max(terminal_age, age)

    path = tuple(item[1] for _start, item in sorted(by_start.items()))
    if path:
        first_start = path[0].bar_start
        if first_start is None or first_start != prediction.astimezone(UTC):
            flags.append("path_gap")
        for previous, current in zip(path, path[1:]):
            if current.bar_start is None or current.bar_start != previous.ts:
                flags.append("path_gap")
    if terminal_age + HORIZON_EPSILON_HOURS < horizon_hours:
        flags.append("incomplete_horizon_path")
    return path, tuple(dict.fromkeys(flags))


def _retryable_path_outcome(outcome: CanonicalTradeOutcome) -> bool:
    return any(
        flag in RETRYABLE_PATH_FLAGS or flag.startswith("path_quality_flag:")
        for flag in outcome.quality_flags
    )


def _quote_bar(
    row: Mapping[str, object],
    *,
    start: datetime,
    end: datetime,
    available: datetime,
) -> ExecutableQuoteBar | None:
    values = [
        _number(row.get(key))
        for key in (
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
        )
    ]
    if any(value is None for value in values):
        return None
    numeric = tuple(value for value in values if value is not None)
    if len(numeric) != 8:
        return None
    source = str(row.get("source") or "")
    source_record_id = str(row.get("source_record_id") or "")
    if not source or not source_record_id:
        return None
    quality = _number(row.get("path_quality"))
    if quality is None:
        quality = 1.0
    raw_quality_flags = row.get("data_quality_flags")
    quality_flags = (
        tuple(str(value) for value in raw_quality_flags)
        if isinstance(raw_quality_flags, list | tuple)
        else ()
    )
    return ExecutableQuoteBar(
        ts=end,
        bid_open=numeric[0],
        bid_high=numeric[1],
        bid_low=numeric[2],
        bid_close=numeric[3],
        ask_open=numeric[4],
        ask_high=numeric[5],
        ask_low=numeric[6],
        ask_close=numeric[7],
        quote_source=source,
        source_record_id=source_record_id,
        path_quality=quality,
        bar_start=start,
        available_time=available,
        closed=True,
        quality_flags=quality_flags,
    )


def _cost_model(
    decision: Mapping[str, object],
    quote_source: str,
) -> CostModelResult | None:
    slippage = _number(decision.get("slippage_r"))
    commission = _number(decision.get("commission_r"))
    financing = _number(decision.get("financing_r"))
    if slippage is None or commission is None or financing is None:
        return None
    return CostModelResult(
        cost_model_id=str(decision.get("cost_model_id") or ""),
        cost_model_version=str(decision.get("cost_model_version") or ""),
        entry_quote_source=quote_source,
        spread_source=quote_source,
        slippage_model_id=str(decision.get("slippage_model_id") or ""),
        commission_model_id=str(decision.get("commission_model_id") or ""),
        slippage_r=slippage,
        commission_r=commission,
        financing_r=financing,
        cost_status=str(decision.get("cost_status") or ""),
    )


def _content_hash(row: Mapping[str, object]) -> str:
    payload = {key: value for key, value in row.items() if key != "content_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _time(value: object) -> datetime:
    return _optional_time(value) or INVALID_TIME


def _optional_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _symbol(value: object) -> str:
    symbol = str(value or "").upper()
    for separator in ("/", "_", "-"):
        symbol = symbol.replace(separator, "")
    return symbol if len(symbol) == 6 and symbol.isalpha() else ""
