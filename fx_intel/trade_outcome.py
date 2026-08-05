"""MFE/MAE/TP/SL based expectancy audit for briefing journal decisions.

The existing direction journal answers "was the direction right?".  This module
answers the trade question: "did the planned entry/SL/TP have positive
expectancy?".  It uses the same JSONL journal and the subsequent close series,
so it works offline without new market data.  Because close-only paths cannot
prove intrabar touch order, every result carries path-quality flags and sample
guards.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
import hashlib
import json
import math
from pathlib import Path

from .effective_samples import (
    DEFAULT_MIN_EFFECTIVE_SAMPLES,
    EffectiveSampleConflict,
    select_effective_samples,
    summarize_effective_samples,
)
from .evaluation_labels import (
    DEFAULT_COST_STATUS,
    KNOWN_EXECUTABLE_COST_MODEL_IDS,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
    PROMOTION_NET_LABEL_PROVENANCES,
    PROMOTION_NET_LABEL_VERSIONS,
    CostModelResult,
    canonical_net_label_contract_flags,
    cost_model_contract_flags,
)
from .journal import (
    COUNTERFACTUAL_ENTRY_KEY,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_TOLERANCE_HOURS,
    counterfactual_guard_entries,
)
from .market import WEEKEND_CLOSURE, WeekendOpenHours, open_hours_between

MIN_PATH_POINTS = 3
MIN_PATH_COVERAGE = 0.50
MIN_PATH_QUALITY = 0.35
MIN_EXPECTANCY_SAMPLES = DEFAULT_MIN_EFFECTIVE_SAMPLES
MIN_GROUP_EXPECTANCY_SAMPLES = 12
CLOSE_ONLY_QUALITY_CAP = 0.70
PARTIAL_OHLC_QUALITY_CAP = 0.85
OHLC_QUALITY_CAP = 0.95
POINTS_FOR_FULL_DENSITY = 12
WEAK_PROFIT_FACTOR = 1.05
QUALITY_WARN_THRESHOLD = 0.55
EXPECTANCY_BLOCK_FACTOR = 0.45
EXPECTANCY_WEAK_FACTOR = 0.75
# ガード見送り中のシャドー計画を採点した反実仮想アウトカムに付く品質フラグ。
# quality_summary の flags 集計にそのまま現れ、根拠に占める反実仮想の量を監査できる
COUNTERFACTUAL_QUALITY_FLAG = "expectancy_guard_counterfactual"
NONCANONICAL_COST_QUALITY_FLAG = "noncanonical_cost_estimate_ignored"
MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R = 0.05
DEFAULT_TP1_R_CANDIDATES = (0.75, 1.0, 1.25)
DEFAULT_TP2_R_CANDIDATES = (1.5, 2.0, 2.5)
IMPROVEMENT_REGISTRY_SCHEMA = 2
PROSPECTIVE_EVIDENCE_SCHEMA = 1
PROSPECTIVE_VALID_DAYS = 90
PROSPECTIVE_MIN_EFFECTIVE_SAMPLES = MIN_EXPECTANCY_SAMPLES
PROSPECTIVE_MIN_NET_COVERAGE = 0.95
PROSPECTIVE_MIN_DSR = 0.95
PROSPECTIVE_MAX_DRAWDOWN_R = 5.0
PROSPECTIVE_COST_STRESS_R = 0.05
PROSPECTIVE_MAX_CONCENTRATION = 0.75
EXPECTANCY_CANDIDATE_ACTION_TYPES = {
    "collect_samples",
    "improve_path_quality",
    "expectancy_guard",
    "tp_sl_entry_retest",
}
VARIANT_CANDIDATE_ACTION_TYPES = {"tp_sl_variant_paper_test"}

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
CONFIDENCE_BINS = ((0, 25), (25, 50), (50, 75), (75, 101))
APPROVAL_PRESERVED_STAGES = {"approved", "rejected", "auto_paused", "expired"}
TRUSTED_POST_PREDICTION_OHLC_SCOPES = {
    "closed_bar_after_prediction",
    "lower_timeframe_closed_bar",
    "post_prediction_interval",
}


@dataclass(frozen=True)
class PricePathPoint:
    ts: datetime
    close: float
    high: float | None = None
    low: float | None = None
    range_scope: str = ""
    rejected_range: bool = False

    @property
    def has_range(self) -> bool:
        return self.high is not None and self.low is not None


@dataclass(frozen=True)
class ExecutableQuoteBar:
    """One completed provider-neutral executable bid/ask bar."""

    ts: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    quote_source: str
    source_record_id: str
    path_quality: float
    bar_start: datetime | None = None
    available_time: datetime | None = None
    closed: bool = True
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CanonicalTradeRequest:
    """All point-in-time inputs required to create a canonical net-R label."""

    decision_id: str
    label_version: str
    label_provenance: str
    symbol: str
    mode: str
    timeframe: str
    horizon_hours: float
    direction: str
    prediction_time: datetime
    entry_mid: float | None
    entry_bid: float | None
    entry_ask: float | None
    quote_observed_at: datetime
    quote_available_at: datetime
    quote_source: str
    source_record_id: str
    stop: float | None
    target1: float | None
    target2: float | None
    planned_risk_distance: float | None
    path: tuple[ExecutableQuoteBar, ...]
    cost_model: CostModelResult | None
    max_entry_quote_age_seconds: float = 300.0
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CanonicalTradeOutcome:
    """Deterministic result keyed by ``decision_id + label_version``."""

    decision_id: str
    label_version: str
    label_provenance: str
    symbol: str
    mode: str
    timeframe: str
    horizon_hours: float
    direction: str
    prediction_time: str | None = None
    holding_end_time: str | None = None
    label_available_time: str | None = None
    label_ingested_time: str | None = None
    first_touch: str = "unavailable"
    first_touch_ts: str | None = None
    entry_bid: float | None = None
    entry_ask: float | None = None
    entry_executable: float | None = None
    planned_risk_distance: float | None = None
    planned_risk_jpy: int | None = None
    units: float | None = None
    entry_quote_to_jpy_rate: float | None = None
    entry_quote_conversion_source: str = ""
    exit_bid: float | None = None
    exit_ask: float | None = None
    exit_executable: float | None = None
    exit_mid: float | None = None
    exit_quote_to_jpy_rate: float | None = None
    exit_quote_conversion_source: str = ""
    gross_realized_r: float | None = None
    quote_realized_r: float | None = None
    planned_payoff_r: float | None = None
    slippage_r: float | None = None
    commission_r: float | None = None
    financing_r: float | None = None
    conversion_r: float | None = None
    entry_spread_r: float | None = None
    realized_spread_cost_r: float | None = None
    additional_cost_r: float | None = None
    execution_cost_r: float | None = None
    realized_net_r: float | None = None
    gross_market_pnl_jpy: int | None = None
    executable_pnl_jpy: int | None = None
    spread_quote_cost_jpy: int | None = None
    slippage_cost_jpy: int | None = None
    commission_jpy: int | None = None
    financing_jpy: int | None = None
    conversion_cost_jpy: int | None = None
    net_pnl_jpy: int | None = None
    cost_model_id: str = "missing"
    cost_model_version: str = ""
    cost_status: str = "missing"
    entry_quote_source: str = ""
    spread_source: str = ""
    slippage_model_id: str = ""
    commission_model_id: str = ""
    conversion_model_id: str = ""
    cost_source: str = ""
    exit_quote_source: str = ""
    execution_observation_mode: str = ""
    entry_source_record_id: str = ""
    exit_source_record_id: str = ""
    entry_quote_event_time: str | None = None
    entry_quote_available_time: str | None = None
    entry_quote_ingested_time: str | None = None
    entry_quote_revision_id: str = ""
    exit_quote_event_time: str | None = None
    exit_quote_available_time: str | None = None
    exit_quote_ingested_time: str | None = None
    exit_quote_revision_id: str = ""
    decision_record_sha256: str = ""
    open_record_sha256: str = ""
    open_observation_sha256: str = ""
    close_record_sha256: str = ""
    close_observation_sha256: str = ""
    entry_knowledge_basis: str = ""
    label_knowledge_basis: str = ""
    costs_complete: bool = False
    research_only: bool = False
    promotion_eligible: bool = False
    cost_quality_flags: tuple[str, ...] = field(default_factory=tuple)
    net_label_eligible: bool = False
    path_quality: float | None = None
    path_source: str = "executable_bid_ask"
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def outcome_key(self) -> tuple[str, str]:
        return self.decision_id, self.label_version

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "label_version": self.label_version,
            "label_provenance": self.label_provenance,
            "symbol": self.symbol,
            "mode": self.mode,
            "timeframe": self.timeframe,
            "horizon_hours": self.horizon_hours,
            "direction": self.direction,
            "prediction_time": self.prediction_time,
            "holding_end_time": self.holding_end_time,
            "label_available_time": self.label_available_time,
            "label_ingested_time": self.label_ingested_time,
            "first_touch": self.first_touch,
            "first_touch_ts": self.first_touch_ts,
            "entry_bid": self.entry_bid,
            "entry_ask": self.entry_ask,
            "entry_executable": self.entry_executable,
            "planned_risk_distance": self.planned_risk_distance,
            "planned_risk_jpy": self.planned_risk_jpy,
            "units": self.units,
            "entry_quote_to_jpy_rate": self.entry_quote_to_jpy_rate,
            "entry_quote_conversion_source": self.entry_quote_conversion_source,
            "exit_bid": self.exit_bid,
            "exit_ask": self.exit_ask,
            "exit_executable": self.exit_executable,
            "exit_mid": self.exit_mid,
            "exit_quote_to_jpy_rate": self.exit_quote_to_jpy_rate,
            "exit_quote_conversion_source": self.exit_quote_conversion_source,
            "gross_realized_r": self.gross_realized_r,
            "quote_realized_r": self.quote_realized_r,
            "planned_payoff_r": self.planned_payoff_r,
            "slippage_r": self.slippage_r,
            "commission_r": self.commission_r,
            "financing_r": self.financing_r,
            "conversion_r": self.conversion_r,
            "entry_spread_r": self.entry_spread_r,
            "realized_spread_cost_r": self.realized_spread_cost_r,
            "additional_cost_r": self.additional_cost_r,
            "execution_cost_r": self.execution_cost_r,
            "realized_net_r": self.realized_net_r,
            "gross_market_pnl_jpy": self.gross_market_pnl_jpy,
            "executable_pnl_jpy": self.executable_pnl_jpy,
            "spread_quote_cost_jpy": self.spread_quote_cost_jpy,
            "slippage_cost_jpy": self.slippage_cost_jpy,
            "commission_jpy": self.commission_jpy,
            "financing_jpy": self.financing_jpy,
            "conversion_cost_jpy": self.conversion_cost_jpy,
            "net_pnl_jpy": self.net_pnl_jpy,
            "cost_model_id": self.cost_model_id,
            "cost_model_version": self.cost_model_version,
            "cost_status": self.cost_status,
            "entry_quote_source": self.entry_quote_source,
            "spread_source": self.spread_source,
            "slippage_model_id": self.slippage_model_id,
            "commission_model_id": self.commission_model_id,
            "conversion_model_id": self.conversion_model_id,
            "cost_source": self.cost_source,
            "exit_quote_source": self.exit_quote_source,
            "execution_observation_mode": self.execution_observation_mode,
            "entry_source_record_id": self.entry_source_record_id,
            "exit_source_record_id": self.exit_source_record_id,
            "entry_quote_event_time": self.entry_quote_event_time,
            "entry_quote_available_time": self.entry_quote_available_time,
            "entry_quote_ingested_time": self.entry_quote_ingested_time,
            "entry_quote_revision_id": self.entry_quote_revision_id,
            "exit_quote_event_time": self.exit_quote_event_time,
            "exit_quote_available_time": self.exit_quote_available_time,
            "exit_quote_ingested_time": self.exit_quote_ingested_time,
            "exit_quote_revision_id": self.exit_quote_revision_id,
            "decision_record_sha256": self.decision_record_sha256,
            "open_record_sha256": self.open_record_sha256,
            "open_observation_sha256": self.open_observation_sha256,
            "close_record_sha256": self.close_record_sha256,
            "close_observation_sha256": self.close_observation_sha256,
            "entry_knowledge_basis": self.entry_knowledge_basis,
            "label_knowledge_basis": self.label_knowledge_basis,
            "costs_complete": self.costs_complete,
            "research_only": self.research_only,
            "promotion_eligible": self.promotion_eligible,
            "cost_quality_flags": list(self.cost_quality_flags),
            "net_label_eligible": self.net_label_eligible,
            "path_quality": self.path_quality,
            "path_source": self.path_source,
            "quality_flags": list(self.quality_flags),
        }


class CanonicalOutcomeConflict(ValueError):
    """Raised when the same canonical key has different scored values."""


def assert_canonical_outcome_idempotent(
    existing: CanonicalTradeOutcome,
    candidate: CanonicalTradeOutcome,
) -> None:
    """Accept identical replay and hard-fail a conflicting canonical key."""

    if existing.outcome_key != candidate.outcome_key:
        return
    if existing != candidate:
        decision_id, label_version = existing.outcome_key
        raise CanonicalOutcomeConflict(
            f"conflicting canonical outcome for {decision_id} + {label_version}"
        )


def score_canonical_outcome(request: CanonicalTradeRequest) -> CanonicalTradeOutcome:
    """Score executable quotes using one planned-risk denominator.

    Long uses ask entry/bid exit and short uses bid entry/ask exit. Spread is
    therefore embedded in ``quote_realized_r`` and is never deducted again.
    Invalid or incomplete inputs return an ineligible outcome rather than a
    fabricated zero.
    """

    flags = _canonical_request_flags(request)
    if flags:
        return _unavailable_canonical_outcome(request, flags)

    assert request.entry_mid is not None
    assert request.entry_bid is not None
    assert request.entry_ask is not None
    assert request.stop is not None
    assert request.target1 is not None
    assert request.target2 is not None
    assert request.planned_risk_distance is not None
    assert request.cost_model is not None

    entry_executable = request.entry_ask if request.direction == "long" else request.entry_bid
    touch, touch_bar, exit_executable, exit_mid, touch_flags = _canonical_exit(request)
    risk = request.planned_risk_distance
    quote_realized_r = _signed_move(request.direction, entry_executable, exit_executable) / risk
    gross_realized_r = _signed_move(request.direction, request.entry_mid, exit_mid) / risk
    planned_payoff_r = _canonical_planned_payoff(request, touch, entry_executable)
    additional_cost_r = request.cost_model.total_cost_r
    entry_spread_r = (request.entry_ask - request.entry_bid) / risk
    realized_net_r = quote_realized_r - additional_cost_r
    execution_cost_r = gross_realized_r - realized_net_r
    path_quality = min(bar.path_quality for bar in request.path)
    all_flags = tuple(
        dict.fromkeys(
            (
                *request.cost_model.quality_flags,
                *(flag for bar in request.path for flag in bar.quality_flags),
                *touch_flags,
            )
        )
    )
    return CanonicalTradeOutcome(
        decision_id=request.decision_id,
        label_version=request.label_version,
        label_provenance=request.label_provenance,
        symbol=request.symbol,
        mode=request.mode,
        timeframe=request.timeframe,
        horizon_hours=request.horizon_hours,
        direction=request.direction,
        prediction_time=request.prediction_time.astimezone(UTC).isoformat(),
        holding_end_time=touch_bar.ts.astimezone(UTC).isoformat(),
        first_touch=touch,
        first_touch_ts=touch_bar.ts.astimezone(UTC).isoformat(),
        entry_bid=request.entry_bid,
        entry_ask=request.entry_ask,
        entry_executable=entry_executable,
        planned_risk_distance=request.planned_risk_distance,
        exit_executable=exit_executable,
        exit_mid=exit_mid,
        gross_realized_r=_canonical_round(gross_realized_r),
        quote_realized_r=_canonical_round(quote_realized_r),
        planned_payoff_r=_canonical_round(planned_payoff_r),
        slippage_r=_canonical_round(request.cost_model.slippage_r),
        commission_r=_canonical_round(request.cost_model.commission_r),
        financing_r=_canonical_round(request.cost_model.financing_r),
        entry_spread_r=_canonical_round(entry_spread_r),
        additional_cost_r=_canonical_round(additional_cost_r),
        execution_cost_r=_canonical_round(execution_cost_r),
        realized_net_r=_canonical_round(realized_net_r),
        cost_model_id=request.cost_model.cost_model_id,
        cost_model_version=request.cost_model.cost_model_version,
        cost_status=request.cost_model.cost_status,
        entry_quote_source=request.cost_model.entry_quote_source,
        spread_source=request.cost_model.spread_source,
        slippage_model_id=request.cost_model.slippage_model_id,
        commission_model_id=request.cost_model.commission_model_id,
        cost_quality_flags=request.cost_model.quality_flags,
        net_label_eligible=True,
        path_quality=_canonical_round(path_quality),
        quality_flags=all_flags,
    )


def aggregate_canonical_expectancy(
    outcomes: Sequence[CanonicalTradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
) -> dict[str, object]:
    """Aggregate canonical gross/net R while gating on effective net samples."""

    gross_values = [
        float(outcome.gross_realized_r)
        for outcome in outcomes
        if outcome.gross_realized_r is not None
    ]
    valid_net_records = [
        outcome.to_dict()
        for outcome in outcomes
        if not canonical_net_label_contract_flags(outcome.to_dict())
    ]
    effective = summarize_effective_samples(valid_net_records, min_samples=min_samples)
    selected_keys = set(effective.selected_keys)
    effective_net_values = [
        value
        for record in valid_net_records
        if f"{record['decision_id']}+{record['label_version']}" in selected_keys
        and (value := _float(record.get("realized_net_r"))) is not None
    ]
    return {
        **effective.to_dict(),
        "raw_samples": len(outcomes),
        "effective_input_samples": effective.raw_samples,
        "gross_samples": len(gross_values),
        "net_label_samples": len(valid_net_records),
        "net_label_coverage": (
            len(valid_net_records) / len(gross_values) if gross_values else None
        ),
        "gross_expectancy_r": _canonical_round(_mean(gross_values)),
        "net_expectancy_r": _canonical_round(_mean(effective_net_values)),
        "gross_profit_factor": _canonical_profit_factor(gross_values),
        "net_profit_factor": _canonical_profit_factor(effective_net_values),
    }


def _canonical_profit_factor(values: Sequence[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses > 0:
        return _canonical_round(gains / losses)
    return None if gains <= 0 else float("inf")


def _canonical_request_flags(request: CanonicalTradeRequest) -> tuple[str, ...]:
    flags: list[str] = list(request.quality_flags)
    if not request.decision_id.strip():
        flags.append("missing_decision_id")
    if request.label_version != NET_LABEL_VERSION:
        flags.append("unknown_label_version")
    if request.label_provenance != NET_LABEL_PROVENANCE:
        flags.append("unknown_label_provenance")
    if request.direction not in {"long", "short"}:
        flags.append("invalid_direction")
    if not request.symbol.strip() or not request.mode.strip() or not request.timeframe.strip():
        flags.append("missing_decision_context")
    if not _aware_datetime(request.prediction_time):
        flags.append("invalid_prediction_time")
    if not _aware_datetime(request.quote_observed_at):
        flags.append("invalid_quote_time")
    if not _aware_datetime(request.quote_available_at):
        flags.append("invalid_quote_available_time")
    elif _aware_datetime(request.prediction_time) and _aware_datetime(request.quote_observed_at):
        observed = request.quote_observed_at.astimezone(UTC)
        available = request.quote_available_at.astimezone(UTC)
        prediction = request.prediction_time.astimezone(UTC)
        if not observed <= available <= prediction:
            flags.append("future_entry_quote")
        elif (prediction - available).total_seconds() > request.max_entry_quote_age_seconds:
            flags.append("stale_entry_quote")
    if request.entry_bid is None or request.entry_ask is None:
        flags.append("missing_entry_quote")
    elif not _valid_book(request.entry_bid, request.entry_ask):
        flags.append("invalid_entry_quote")
    if not _finite_positive(request.entry_mid) or (
        request.entry_bid is not None
        and request.entry_ask is not None
        and request.entry_mid is not None
        and not request.entry_bid <= request.entry_mid <= request.entry_ask
    ):
        flags.append("invalid_entry_mid")
    if not request.quote_source.strip() or not request.source_record_id.strip():
        flags.append("missing_entry_provenance")
    if not _finite_positive(request.planned_risk_distance):
        flags.append("invalid_planned_risk_distance")
    if not _canonical_levels_valid(request):
        flags.append("invalid_risk_levels")
    if not request.path:
        flags.append("missing_executable_path")
    else:
        flags.extend(_canonical_path_flags(request))
    cost = request.cost_model
    if cost is None:
        flags.append("missing_cost_model")
    else:
        flags.extend(cost_model_contract_flags(cost))
        if (
            not cost.cost_model_version.strip()
            or not cost.slippage_model_id.strip()
            or not cost.commission_model_id.strip()
            or cost.cost_status != DEFAULT_COST_STATUS
        ):
            flags.append("invalid_cost_model")
        if cost.entry_quote_source != request.quote_source:
            flags.append("entry_quote_source_mismatch")
        if cost.spread_source != request.quote_source:
            flags.append("spread_source_mismatch")
        planned_risk = request.planned_risk_distance
        expected_entry_spread_r = (
            (request.entry_ask - request.entry_bid) / planned_risk
            if request.entry_bid is not None
            and request.entry_ask is not None
            and planned_risk is not None
            and _finite_positive(planned_risk)
            else None
        )
        if cost.entry_spread_r is None:
            flags.append("missing_entry_spread_r")
        elif (
            expected_entry_spread_r is None
            or not math.isfinite(cost.entry_spread_r)
            or not math.isclose(
                cost.entry_spread_r,
                expected_entry_spread_r,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        ):
            flags.append("entry_spread_r_mismatch")
        if any(
            not _finite_nonnegative(value)
            for value in (cost.slippage_r, cost.commission_r, cost.financing_r)
        ):
            flags.append("invalid_cost_value")
    return tuple(dict.fromkeys(flags))


def _unavailable_canonical_outcome(
    request: CanonicalTradeRequest,
    flags: Sequence[str],
) -> CanonicalTradeOutcome:
    cost = request.cost_model
    return CanonicalTradeOutcome(
        decision_id=request.decision_id,
        label_version=request.label_version,
        label_provenance=request.label_provenance,
        symbol=request.symbol,
        mode=request.mode,
        timeframe=request.timeframe,
        horizon_hours=request.horizon_hours,
        direction=request.direction,
        prediction_time=(
            request.prediction_time.astimezone(UTC).isoformat()
            if _aware_datetime(request.prediction_time)
            else None
        ),
        entry_bid=request.entry_bid,
        entry_ask=request.entry_ask,
        planned_risk_distance=request.planned_risk_distance,
        slippage_r=(cost.slippage_r if cost is not None else None),
        commission_r=(cost.commission_r if cost is not None else None),
        financing_r=(cost.financing_r if cost is not None else None),
        cost_model_id=(cost.cost_model_id if cost is not None else "missing"),
        cost_model_version=(cost.cost_model_version if cost is not None else ""),
        cost_status=(cost.cost_status if cost is not None else "missing"),
        entry_quote_source=(cost.entry_quote_source if cost is not None else ""),
        spread_source=(cost.spread_source if cost is not None else ""),
        slippage_model_id=(cost.slippage_model_id if cost is not None else ""),
        commission_model_id=(cost.commission_model_id if cost is not None else ""),
        cost_quality_flags=(cost.quality_flags if cost is not None else ()),
        net_label_eligible=False,
        quality_flags=tuple(dict.fromkeys(flags)),
    )


def _canonical_exit(
    request: CanonicalTradeRequest,
) -> tuple[str, ExecutableQuoteBar, float, float, tuple[str, ...]]:
    assert request.stop is not None
    assert request.target1 is not None
    assert request.target2 is not None
    for bar in request.path:
        if request.direction == "long":
            open_price = bar.bid_open
            high = bar.bid_high
            low = bar.bid_low
            stop_gap = open_price <= request.stop
            target2_gap = open_price >= request.target2
            target1_gap = open_price >= request.target1
            stop_hit = low <= request.stop
            target2_hit = high >= request.target2
            target1_hit = high >= request.target1
        else:
            open_price = bar.ask_open
            high = bar.ask_high
            low = bar.ask_low
            stop_gap = open_price >= request.stop
            target2_gap = open_price <= request.target2
            target1_gap = open_price <= request.target1
            stop_hit = high >= request.stop
            target2_hit = low <= request.target2
            target1_hit = low <= request.target1
        if stop_gap:
            return (
                "sl_gap",
                bar,
                open_price,
                (bar.bid_open + bar.ask_open) / 2.0,
                ("gap_through_stop",),
            )
        if target2_gap:
            return (
                "tp2",
                bar,
                request.target2,
                _canonical_trigger_mid(request.direction, request.target2, bar),
                ("favorable_gap_target_capped",),
            )
        if target1_gap:
            return (
                "tp1",
                bar,
                request.target1,
                _canonical_trigger_mid(request.direction, request.target1, bar),
                ("favorable_gap_target_capped",),
            )
        if stop_hit and (target1_hit or target2_hit):
            return (
                "ambiguous_sl_tp",
                bar,
                request.stop,
                _canonical_trigger_mid(request.direction, request.stop, bar),
                ("same_bar_ambiguous_stop_first", "intrabar_mid_from_open_spread"),
            )
        if stop_hit:
            return (
                "sl",
                bar,
                request.stop,
                _canonical_trigger_mid(request.direction, request.stop, bar),
                ("intrabar_mid_from_open_spread",),
            )
        if target2_hit:
            return (
                "tp2",
                bar,
                request.target2,
                _canonical_trigger_mid(request.direction, request.target2, bar),
                ("intrabar_mid_from_open_spread",),
            )
        if target1_hit:
            return (
                "tp1",
                bar,
                request.target1,
                _canonical_trigger_mid(request.direction, request.target1, bar),
                ("intrabar_mid_from_open_spread",),
            )
    terminal = request.path[-1]
    if request.direction == "long":
        return (
            "terminal",
            terminal,
            terminal.bid_close,
            (terminal.bid_close + terminal.ask_close) / 2.0,
            (),
        )
    return (
        "terminal",
        terminal,
        terminal.ask_close,
        (terminal.bid_close + terminal.ask_close) / 2.0,
        (),
    )


def _canonical_trigger_mid(
    direction: str,
    executable_price: float,
    bar: ExecutableQuoteBar,
) -> float:
    half_spread = (bar.ask_open - bar.bid_open) / 2.0
    return executable_price + half_spread if direction == "long" else executable_price - half_spread


def _canonical_planned_payoff(
    request: CanonicalTradeRequest,
    touch: str,
    entry_executable: float,
) -> float | None:
    assert request.planned_risk_distance is not None
    if touch in {"sl", "sl_gap", "ambiguous_sl_tp"}:
        return -1.0
    if touch == "tp1":
        assert request.target1 is not None
        return (
            _signed_move(request.direction, entry_executable, request.target1)
            / request.planned_risk_distance
        )
    if touch == "tp2":
        assert request.target2 is not None
        return (
            _signed_move(request.direction, entry_executable, request.target2)
            / request.planned_risk_distance
        )
    return None


def _canonical_levels_valid(request: CanonicalTradeRequest) -> bool:
    if not all(
        _finite_positive(value) for value in (request.stop, request.target1, request.target2)
    ):
        return False
    if request.entry_bid is None or request.entry_ask is None:
        return False
    assert request.stop is not None
    assert request.target1 is not None
    assert request.target2 is not None
    if request.direction == "long":
        return request.stop < request.entry_ask < request.target1 < request.target2
    if request.direction == "short":
        return request.stop > request.entry_bid > request.target1 > request.target2
    return False


def _canonical_path_flags(request: CanonicalTradeRequest) -> tuple[str, ...]:
    flags: list[str] = []
    previous: datetime | None = None
    for bar in request.path:
        if not _aware_datetime(bar.ts) or not bar.closed:
            flags.append("invalid_path_time")
            continue
        current = bar.ts.astimezone(UTC)
        if not _aware_datetime(bar.bar_start) or not _aware_datetime(bar.available_time):
            flags.append("missing_path_availability")
            continue
        assert bar.bar_start is not None
        assert bar.available_time is not None
        start = bar.bar_start.astimezone(UTC)
        available = bar.available_time.astimezone(UTC)
        if current <= request.prediction_time.astimezone(UTC):
            flags.append("invalid_path_time")
        if start < request.prediction_time.astimezone(UTC):
            flags.append("forming_bar_path")
        if not start < current <= available:
            flags.append("invalid_path_availability")
        if previous is not None and current <= previous:
            flags.append("out_of_order_path")
        previous = current
        if not bar.quote_source.strip() or not bar.source_record_id.strip():
            flags.append("missing_path_provenance")
        if bar.quote_source != request.quote_source:
            flags.append("path_source_mismatch")
        if not 0.0 < bar.path_quality <= 1.0:
            flags.append("invalid_path_quality")
        bid = (bar.bid_open, bar.bid_high, bar.bid_low, bar.bid_close)
        ask = (bar.ask_open, bar.ask_high, bar.ask_low, bar.ask_close)
        if not all(_finite_positive(value) for value in (*bid, *ask)):
            flags.append("invalid_path_quote")
            continue
        if not all(ask_value >= bid_value for bid_value, ask_value in zip(bid, ask)):
            flags.append("invalid_path_quote")
        if bar.bid_high < max(bar.bid_open, bar.bid_low, bar.bid_close):
            flags.append("invalid_path_ohlc")
        if bar.bid_low > min(bar.bid_open, bar.bid_high, bar.bid_close):
            flags.append("invalid_path_ohlc")
        if bar.ask_high < max(bar.ask_open, bar.ask_low, bar.ask_close):
            flags.append("invalid_path_ohlc")
        if bar.ask_low > min(bar.ask_open, bar.ask_high, bar.ask_close):
            flags.append("invalid_path_ohlc")
    return tuple(dict.fromkeys(flags))


def _valid_book(bid: float, ask: float) -> bool:
    return _finite_positive(bid) and _finite_positive(ask) and ask >= bid


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _canonical_round(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None


@dataclass(frozen=True)
class TradeOutcome:
    """One journal decision scored as a hypothetical full-position trade."""

    symbol: str
    direction: str
    ts: str
    horizon_hours: float
    conviction: int
    data_quality: float | None
    mode: str = "fusion"
    timeframe: str = "fusion"
    # 判断ログ行の識別子(shadow予測の満期照合キー)。無い行は None。
    decision_id: str | None = None
    entry: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    target_policy_id: str | None = None
    target_policy_scope: str = ""
    target_policy_key: str = ""
    atr: float | None = None
    risk_distance: float | None = None
    terminal_price: float | None = None
    terminal_r: float | None = None
    mfe: float | None = None
    mae: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    sl_hit: bool = False
    first_touch: str = "none"
    first_touch_ts: str | None = None
    realized_r: float | None = None  # 執行コスト控除前(グロス)の実現R
    # close/OHLC-only legacy scorerはcanonical net labelを生成しない。
    realized_net_r: float | None = None
    execution_cost_r: float | None = None
    net_expected_r: float | None = None
    diagnostic_execution_cost_r: float | None = None
    diagnostic_net_expected_r: float | None = None
    net_label_eligible: bool = False
    label_version: str = ""
    label_provenance: str = ""
    entry_bid: float | None = None
    entry_ask: float | None = None
    quote_realized_r: float | None = None
    entry_spread_r: float | None = None
    slippage_r: float | None = None
    commission_r: float | None = None
    financing_r: float | None = None
    additional_cost_r: float | None = None
    cost_model_id: str = "missing"
    cost_model_version: str = ""
    cost_status: str = "missing"
    entry_quote_source: str = ""
    spread_source: str = ""
    slippage_model_id: str = ""
    commission_model_id: str = ""
    cost_quality_flags: tuple[str, ...] = field(default_factory=tuple)
    path_points: int = 0
    path_start: str | None = None
    path_end: str | None = None
    path_source: str = "close"
    path_coverage: float = 0.0
    path_quality: float = 0.0
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tradable(self) -> bool:
        return self.realized_r is not None and self.path_quality >= MIN_PATH_QUALITY

    @property
    def prediction_time(self) -> str:
        return self.ts

    @property
    def holding_end_time(self) -> str | None:
        return self.path_end

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "mode": self.mode,
            "timeframe": self.timeframe,
            "ts": self.ts,
            "prediction_time": self.ts,
            "holding_end_time": self.path_end,
            "horizon_hours": self.horizon_hours,
            "conviction": self.conviction,
            "data_quality": self.data_quality,
            "entry": self.entry,
            "stop": self.stop,
            "target1": self.target1,
            "target2": self.target2,
            "target_policy_id": self.target_policy_id,
            "target_policy_scope": self.target_policy_scope,
            "target_policy_key": self.target_policy_key,
            "atr": self.atr,
            "risk_distance": self.risk_distance,
            "terminal_price": self.terminal_price,
            "terminal_r": self.terminal_r,
            "mfe": self.mfe,
            "mae": self.mae,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "sl_hit": self.sl_hit,
            "first_touch": self.first_touch,
            "first_touch_ts": self.first_touch_ts,
            "realized_r": self.realized_r,
            "gross_realized_r": self.realized_r,
            "quote_realized_r": self.quote_realized_r,
            "realized_net_r": self.realized_net_r,
            "net_label_eligible": self.net_label_eligible,
            "label_version": self.label_version,
            "label_provenance": self.label_provenance,
            "decision_id": self.decision_id,
            "execution_cost_r": self.execution_cost_r,
            "net_expected_r": self.net_expected_r,
            "diagnostic_execution_cost_r": self.diagnostic_execution_cost_r,
            "diagnostic_net_expected_r": self.diagnostic_net_expected_r,
            "entry_bid": self.entry_bid,
            "entry_ask": self.entry_ask,
            "planned_risk_distance": self.risk_distance,
            "entry_spread_r": self.entry_spread_r,
            "slippage_r": self.slippage_r,
            "commission_r": self.commission_r,
            "financing_r": self.financing_r,
            "additional_cost_r": self.additional_cost_r,
            "cost_model_id": self.cost_model_id,
            "cost_model_version": self.cost_model_version,
            "cost_status": self.cost_status,
            "entry_quote_source": self.entry_quote_source,
            "spread_source": self.spread_source,
            "slippage_model_id": self.slippage_model_id,
            "commission_model_id": self.commission_model_id,
            "cost_quality_flags": list(self.cost_quality_flags),
            "path_points": self.path_points,
            "path_start": self.path_start,
            "path_end": self.path_end,
            "path_source": self.path_source,
            "path_coverage": self.path_coverage,
            "path_quality": self.path_quality,
            "quality_flags": list(self.quality_flags),
            "tradable": self.tradable,
        }


@dataclass(frozen=True)
class ExpectancyStats:
    evaluated: int = 0
    tradable: int = 0
    low_quality: int = 0
    raw_samples: int = 0
    effective_samples: int = 0
    gross_samples: int = 0
    net_label_samples: int = 0
    net_label_coverage: float | None = None
    overlap_ratio: float | None = None
    cluster_count: int = 0
    market_days: int = 0
    gross_expectancy_r: float | None = None
    net_expectancy_r: float | None = None
    net_sharpe_per_period: float | None = None
    gross_profit_factor_r: float | None = None
    net_profit_factor_r: float | None = None
    label_version: str = ""
    label_provenance: str = ""
    cost_model_id: str = ""
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    expectancy_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    profit_factor_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    tp1_rate: float | None = None
    tp2_rate: float | None = None
    sl_rate: float | None = None
    avg_path_quality: float | None = None
    sample_ok: bool = False
    min_samples: int = MIN_EXPECTANCY_SAMPLES

    def to_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "tradable": self.tradable,
            "low_quality": self.low_quality,
            "raw_samples": self.raw_samples,
            "effective_samples": self.effective_samples,
            "gross_samples": self.gross_samples,
            "net_label_samples": self.net_label_samples,
            "net_label_coverage": self.net_label_coverage,
            "overlap_ratio": self.overlap_ratio,
            "cluster_count": self.cluster_count,
            "market_days": self.market_days,
            "gross_expectancy_r": self.gross_expectancy_r,
            "net_expectancy_r": self.net_expectancy_r,
            "net_sharpe_per_period": self.net_sharpe_per_period,
            "gross_profit_factor_r": self.gross_profit_factor_r,
            "net_profit_factor_r": self.net_profit_factor_r,
            "label_version": self.label_version,
            "label_provenance": self.label_provenance,
            "cost_model_id": self.cost_model_id,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "avg_win_r": self.avg_win_r,
            "avg_loss_r": self.avg_loss_r,
            "profit_factor_r": self.profit_factor_r,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "tp1_rate": self.tp1_rate,
            "tp2_rate": self.tp2_rate,
            "sl_rate": self.sl_rate,
            "avg_path_quality": self.avg_path_quality,
            "sample_ok": self.sample_ok,
            "min_samples": self.min_samples,
        }


@dataclass(frozen=True)
class TradeOutcomeHealthCheck:
    name: str
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TradeOutcomeHealthReport:
    checks: list[TradeOutcomeHealthCheck]

    @property
    def status(self) -> str:
        if any(check.status == STATUS_FAIL for check in self.checks):
            return STATUS_FAIL
        if any(check.status == STATUS_WARN for check in self.checks):
            return STATUS_WARN
        return STATUS_OK

    @property
    def exit_code(self) -> int:
        return 1 if self.status == STATUS_FAIL else 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class TradeImprovementCandidate:
    candidate_id: str
    scope: str
    key: str
    priority: str
    action_type: str
    title_ja: str
    rationale_ja: str
    proposed_change: dict[str, object]
    validation_ja: str
    guardrail_ja: str
    source_finding: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "scope": self.scope,
            "key": self.key,
            "priority": self.priority,
            "action_type": self.action_type,
            "title_ja": self.title_ja,
            "rationale_ja": self.rationale_ja,
            "proposed_change": dict(self.proposed_change),
            "validation_ja": self.validation_ja,
            "guardrail_ja": self.guardrail_ja,
            "source_finding": dict(self.source_finding),
        }


@dataclass(frozen=True)
class ExpectancyAdjustment:
    action: str = "none"
    factor: float = 1.0
    block: bool = False
    reason_ja: str = ""
    matched_scope: str = ""
    matched_key: str = ""
    severity: str = ""
    tradable: int = 0
    min_samples: int = 0
    expectancy_r: float | None = None
    profit_factor_r: float | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "factor": self.factor,
            "block": self.block,
            "reason_ja": self.reason_ja,
            "matched_scope": self.matched_scope,
            "matched_key": self.matched_key,
            "severity": self.severity,
            "tradable": self.tradable,
            "min_samples": self.min_samples,
            "expectancy_r": self.expectancy_r,
            "profit_factor_r": self.profit_factor_r,
        }


@dataclass(frozen=True)
class TradeVariantScore:
    variant_id: str
    target1_r: float
    target2_r: float
    tradable: int
    sample_ok: bool
    expectancy_r: float | None
    net_sharpe_per_period: float | None
    profit_factor_r: float | None
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor_r: float | None
    net_profit_factor_r: float | None
    net_label_samples: int
    net_label_coverage: float | None
    effective_samples: int
    label_version: str
    label_provenance: str
    cost_model_id: str
    tp1_rate: float | None
    tp2_rate: float | None
    sl_rate: float | None
    avg_mfe_r: float | None
    avg_mae_r: float | None
    avg_path_quality: float | None
    delta_expectancy_r: float | None
    delta_profit_factor_r: float | None
    recommendation: str
    reason_ja: str

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "target1_r": self.target1_r,
            "target2_r": self.target2_r,
            "tradable": self.tradable,
            "sample_ok": self.sample_ok,
            "expectancy_r": self.expectancy_r,
            "net_sharpe_per_period": self.net_sharpe_per_period,
            "profit_factor_r": self.profit_factor_r,
            "gross_expectancy_r": self.gross_expectancy_r,
            "net_expectancy_r": self.net_expectancy_r,
            "gross_profit_factor_r": self.gross_profit_factor_r,
            "net_profit_factor_r": self.net_profit_factor_r,
            "net_label_samples": self.net_label_samples,
            "net_label_coverage": self.net_label_coverage,
            "effective_samples": self.effective_samples,
            "label_version": self.label_version,
            "label_provenance": self.label_provenance,
            "cost_model_id": self.cost_model_id,
            "tp1_rate": self.tp1_rate,
            "tp2_rate": self.tp2_rate,
            "sl_rate": self.sl_rate,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "avg_path_quality": self.avg_path_quality,
            "delta_expectancy_r": self.delta_expectancy_r,
            "delta_net_expectancy_r": self.delta_expectancy_r,
            "delta_profit_factor_r": self.delta_profit_factor_r,
            "recommendation": self.recommendation,
            "reason_ja": self.reason_ja,
        }


@dataclass(frozen=True)
class ApprovedTargetPolicy:
    candidate_id: str
    scope: str
    key: str
    target1_r: float
    target2_r: float
    priority: str = ""
    reason_ja: str = ""
    approved_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "scope": self.scope,
            "key": self.key,
            "target1_r": self.target1_r,
            "target2_r": self.target2_r,
            "priority": self.priority,
            "reason_ja": self.reason_ja,
            "approved_at": self.approved_at,
        }


def evaluate_trade_outcomes(
    entries: Iterable[Mapping[str, object]],
    *,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    min_path_points: int = MIN_PATH_POINTS,
    target1_r: float | None = None,
    target2_r: float | None = None,
    include_guard_counterfactuals: bool = False,
) -> list[TradeOutcome]:
    materialized = list(entries)
    scored_source: list[Mapping[str, object]] = materialized
    if include_guard_counterfactuals:
        # 期待値ガード見送り中のシャドー計画(判断時凍結のSL/TP)を採点対象に加える。
        # 価格系列は実記録行だけから作る: 合成行は実行の複製なので、混ぜると
        # 同一時刻の終値が二重に載り path 指標が歪む
        scored_source = [*materialized, *counterfactual_guard_entries(materialized)]
    prices: dict[str, list[PricePathPoint]] = {}
    for entry in materialized:
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        point = _price_path_point(ts, entry)
        if point is not None:
            prices.setdefault(str(entry.get("symbol", "")).upper(), []).append(point)
    parsed_entries: list[tuple[datetime, Mapping[str, object]]] = []
    for entry in scored_source:
        ts = _parse_ts(entry.get("ts"))
        if ts is not None:
            parsed_entries.append((ts, entry))
    for series in prices.values():
        series.sort(key=lambda point: point.ts)
    price_times = {symbol: [point.ts for point in series] for symbol, series in prices.items()}

    outcomes: list[TradeOutcome] = []
    for ts, entry in parsed_entries:
        direction = str(entry.get("direction", ""))
        if direction not in ("long", "short"):
            continue
        counterfactual = bool(entry.get(COUNTERFACTUAL_ENTRY_KEY))
        symbol = str(entry.get("symbol", "")).upper()
        timeframe = str(entry.get("timeframe") or "fusion")
        entry_price = _float(entry.get("close"))
        stop = _float(entry.get("stop"))
        target1 = _float(entry.get("target1"))
        target2 = _float(entry.get("target2"))
        target_policy_id, target_policy_scope, target_policy_key = _target_policy_meta(entry)
        atr = _float(entry.get("atr"))
        conviction = _int(entry.get("conviction"))
        data_quality = _float(entry.get("data_quality"))
        future = _future_path(
            prices.get(symbol, []),
            price_times.get(symbol, []),
            ts,
            horizon_hours,
            tolerance_hours,
        )

        missing_flags: list[str] = []
        if entry_price is None:
            missing_flags.append("missing_entry")
        if stop is None or target1 is None:
            missing_flags.append("missing_risk_levels")
        risk_distance = (
            abs(entry_price - stop)
            if entry_price is not None and stop is not None and stop != entry_price
            else None
        )
        if risk_distance is None or risk_distance <= 0:
            missing_flags.append("invalid_risk_distance")
        if not future:
            missing_flags.append("no_future_prices")
        if target1_r is not None and target2_r is not None and target2_r <= target1_r:
            missing_flags.append("invalid_target_variant")
        if missing_flags:
            if counterfactual:
                missing_flags.append(COUNTERFACTUAL_QUALITY_FLAG)
            outcomes.append(
                TradeOutcome(
                    symbol=symbol,
                    direction=direction,
                    ts=ts.isoformat(),
                    horizon_hours=horizon_hours,
                    conviction=conviction,
                    data_quality=data_quality,
                    timeframe=timeframe,
                    entry=entry_price,
                    stop=stop,
                    target1=target1,
                    target2=target2,
                    target_policy_id=target_policy_id,
                    target_policy_scope=target_policy_scope,
                    target_policy_key=target_policy_key,
                    atr=atr,
                    risk_distance=risk_distance,
                    first_touch="unscored",
                    quality_flags=tuple(dict.fromkeys(missing_flags)),
                )
            )
            continue

        assert entry_price is not None and risk_distance is not None
        if target1_r is not None:
            target1 = _target_price(direction, entry_price, risk_distance, target1_r)
        if target2_r is not None:
            target2 = _target_price(direction, entry_price, risk_distance, target2_r)
        tp1_r_value = _target_r(direction, entry_price, risk_distance, target1, default=1.0)
        tp2_r_value = _target_r(direction, entry_price, risk_distance, target2, default=2.0)
        terminal = future[-1]
        terminal_ts, terminal_price = terminal.ts, terminal.close
        terminal_r = _signed_move(direction, entry_price, terminal_price) / risk_distance
        first_touch = "none"
        first_touch_ts = None
        tp1_hit = tp2_hit = sl_hit = False
        ambiguous_intrabar = False
        active_path: list[PricePathPoint] = []
        for point in future:
            active_path.append(point)
            touch = _touch(direction, point, stop, target1, target2)
            tp1_hit = tp1_hit or touch in ("tp1", "tp2", "ambiguous_sl_tp")
            tp2_hit = tp2_hit or touch == "tp2"
            sl_hit = sl_hit or touch in ("sl", "ambiguous_sl_tp")
            ambiguous_intrabar = ambiguous_intrabar or touch == "ambiguous_sl_tp"
            if touch != "none":
                first_touch = touch
                first_touch_ts = point.ts.isoformat()
                break

        mfe = max(_favorable_move(direction, entry_price, point) for point in active_path)
        mae = max(0.0, max(_adverse_move(direction, entry_price, point) for point in active_path))

        realized_r = _realized_r(first_touch, terminal_r, tp1_r_value, tp2_r_value)
        # close/OHLC-only pathとchecklist cost estimateはcanonical会計ではない。
        # 旧キーも診断値として保持するが、realized_net_rは必ずNoneにする。
        diagnostic_execution_cost_r = _float(
            entry.get("diagnostic_execution_cost_r", entry.get("execution_cost_r"))
        )
        diagnostic_net_expected_r = _float(
            entry.get("diagnostic_net_expected_r", entry.get("net_expected_r"))
        )
        raw_decision_id = str(entry.get("decision_id", "")).strip()
        decision_id = raw_decision_id or None
        quality, flags, path_source = _path_quality(ts, future, horizon_hours, min_path_points)
        if diagnostic_execution_cost_r is not None or diagnostic_net_expected_r is not None:
            flags = tuple(dict.fromkeys((*flags, NONCANONICAL_COST_QUALITY_FLAG)))
        if ambiguous_intrabar:
            flags = tuple(dict.fromkeys((*flags, "ambiguous_intrabar_touch")))
        if counterfactual:
            flags = tuple(dict.fromkeys((*flags, COUNTERFACTUAL_QUALITY_FLAG)))
        outcomes.append(
            TradeOutcome(
                symbol=symbol,
                direction=direction,
                ts=ts.isoformat(),
                horizon_hours=horizon_hours,
                conviction=conviction,
                data_quality=data_quality,
                timeframe=timeframe,
                entry=entry_price,
                stop=stop,
                target1=target1,
                target2=target2,
                target_policy_id=target_policy_id,
                target_policy_scope=target_policy_scope,
                target_policy_key=target_policy_key,
                atr=atr,
                risk_distance=round(risk_distance, 8),
                terminal_price=terminal_price,
                terminal_r=round(terminal_r, 4),
                mfe=round(mfe, 8),
                mae=round(mae, 8),
                mfe_r=round(mfe / risk_distance, 4),
                mae_r=round(mae / risk_distance, 4),
                tp1_hit=tp1_hit,
                tp2_hit=tp2_hit,
                sl_hit=sl_hit,
                first_touch=first_touch,
                first_touch_ts=first_touch_ts,
                realized_r=round(realized_r, 4),
                realized_net_r=None,
                execution_cost_r=None,
                net_expected_r=None,
                diagnostic_execution_cost_r=diagnostic_execution_cost_r,
                diagnostic_net_expected_r=diagnostic_net_expected_r,
                decision_id=decision_id,
                path_points=len(future),
                path_start=future[0].ts.isoformat(),
                path_end=terminal_ts.isoformat(),
                path_source=path_source,
                path_coverage=round(_coverage(ts, terminal_ts, horizon_hours), 4),
                path_quality=quality,
                quality_flags=flags,
            )
        )
    return outcomes


def summarize_expectancy(
    outcomes: Sequence[TradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
    group_min_samples: int = MIN_GROUP_EXPECTANCY_SAMPLES,
) -> dict:
    return {
        "schema": 1,
        "overall": aggregate_expectancy(outcomes, min_samples=min_samples).to_dict(),
        "by_symbol": _aggregate_by(
            outcomes, lambda outcome: outcome.symbol, min_samples=group_min_samples
        ),
        "by_direction": _aggregate_by(
            outcomes, lambda outcome: outcome.direction, min_samples=group_min_samples
        ),
        "by_symbol_direction": _aggregate_by(
            outcomes,
            lambda outcome: f"{outcome.symbol}:{outcome.direction}",
            min_samples=group_min_samples,
        ),
        "by_target_policy": _aggregate_by(
            outcomes,
            lambda outcome: outcome.target_policy_id or "",
            min_samples=group_min_samples,
        ),
        "by_confidence": _aggregate_by(outcomes, _confidence_label, min_samples=group_min_samples),
        "quality": quality_summary(outcomes),
    }


def aggregate_expectancy(
    outcomes: Sequence[TradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
) -> ExpectancyStats:
    evaluated = len(outcomes)
    usable = [outcome for outcome in outcomes if outcome.realized_r is not None]
    tradable = [outcome for outcome in usable if outcome.tradable]
    r_values = [float(outcome.realized_r) for outcome in tradable if outcome.realized_r is not None]
    gross_win = sum(value for value in r_values if value > 0)
    gross_loss = abs(sum(value for value in r_values if value < 0))
    gross_expectancy = _round(_mean(r_values))
    gross_profit_factor = (
        _round(gross_win / gross_loss)
        if gross_loss > 0
        else (None if gross_win <= 0 else float("inf"))
    )
    valid_net_records = [
        outcome.to_dict()
        for outcome in tradable
        if not canonical_net_label_contract_flags(outcome.to_dict())
    ]
    effective = summarize_effective_samples(valid_net_records, min_samples=min_samples)
    selected_keys = set(effective.selected_keys)
    effective_net_values = [
        float(record["realized_net_r"])
        for record in valid_net_records
        if f"{record['decision_id']}+{record['label_version']}" in selected_keys
        and record.get("realized_net_r") is not None
    ]
    net_wins = sum(value for value in effective_net_values if value > 0)
    net_losses = abs(sum(value for value in effective_net_values if value < 0))
    net_expectancy = _round(_mean(effective_net_values))
    net_profit_factor = (
        _round(net_wins / net_losses)
        if net_losses > 0
        else (None if net_wins <= 0 else float("inf"))
    )
    mfe_values = [float(outcome.mfe_r) for outcome in tradable if outcome.mfe_r is not None]
    mae_values = [float(outcome.mae_r) for outcome in tradable if outcome.mae_r is not None]
    qualities = [outcome.path_quality for outcome in usable]
    return ExpectancyStats(
        evaluated=evaluated,
        tradable=len(tradable),
        low_quality=evaluated - len(tradable),
        raw_samples=evaluated,
        effective_samples=effective.effective_samples,
        gross_samples=len(r_values),
        net_label_samples=len(valid_net_records),
        net_label_coverage=(len(valid_net_records) / len(r_values) if r_values else None),
        overlap_ratio=effective.overlap_ratio,
        cluster_count=effective.cluster_count,
        market_days=effective.market_days,
        gross_expectancy_r=gross_expectancy,
        net_expectancy_r=net_expectancy,
        net_sharpe_per_period=_round(_sample_sharpe(effective_net_values)),
        gross_profit_factor_r=gross_profit_factor,
        net_profit_factor_r=net_profit_factor,
        label_version=_single_contract_value(valid_net_records, "label_version"),
        label_provenance=_single_contract_value(valid_net_records, "label_provenance"),
        cost_model_id=_single_contract_value(valid_net_records, "cost_model_id"),
        wins=sum(1 for value in effective_net_values if value > 0),
        losses=sum(1 for value in effective_net_values if value < 0),
        win_rate=(
            _round(
                sum(1 for value in effective_net_values if value > 0) / len(effective_net_values)
            )
            if effective_net_values
            else None
        ),
        expectancy_r=net_expectancy,
        avg_win_r=_round(_mean([value for value in effective_net_values if value > 0])),
        avg_loss_r=_round(_mean([value for value in effective_net_values if value < 0])),
        profit_factor_r=net_profit_factor,
        avg_mfe_r=_round(_mean(mfe_values)),
        avg_mae_r=_round(_mean(mae_values)),
        tp1_rate=(
            _round(sum(1 for outcome in tradable if outcome.tp1_hit) / len(tradable))
            if tradable
            else None
        ),
        tp2_rate=(
            _round(sum(1 for outcome in tradable if outcome.tp2_hit) / len(tradable))
            if tradable
            else None
        ),
        sl_rate=(
            _round(sum(1 for outcome in tradable if outcome.sl_hit) / len(tradable))
            if tradable
            else None
        ),
        avg_path_quality=_round(_mean(qualities)),
        sample_ok=effective.sample_ok,
        min_samples=min_samples,
    )


def quality_summary(outcomes: Sequence[TradeOutcome]) -> dict:
    flags: dict[str, int] = {}
    for outcome in outcomes:
        for flag in outcome.quality_flags:
            flags[flag] = flags.get(flag, 0) + 1
    scored = [outcome for outcome in outcomes if outcome.realized_r is not None]
    return {
        "evaluated": len(outcomes),
        "scored": len(scored),
        "low_quality": sum(1 for outcome in outcomes if not outcome.tradable),
        "avg_path_quality": _round(_mean([outcome.path_quality for outcome in scored])),
        "flags": dict(sorted(flags.items())),
    }


def expectancy_findings(summary: Mapping[str, object], *, limit: int = 5) -> list[dict]:
    candidates: list[dict] = []
    overall = summary.get("overall")
    if isinstance(overall, Mapping):
        finding = _expectancy_finding("全体", "", overall)
        if finding is not None:
            candidates.append(finding)
    for group_key, scope in (
        ("by_symbol_direction", "通貨ペア×方向"),
        ("by_symbol", "通貨ペア"),
        ("by_direction", "方向"),
        ("by_confidence", "確信度"),
    ):
        group = summary.get(group_key)
        if not isinstance(group, Mapping):
            continue
        for key, stats in group.items():
            if isinstance(stats, Mapping):
                finding = _expectancy_finding(scope, str(key), stats)
                if finding is not None:
                    candidates.append(finding)
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        finding = _quality_finding(quality)
        if finding is not None:
            candidates.append(finding)
    candidates.sort(key=lambda item: (int(item["rank"]), -int(item["tradable"]), item["label"]))
    return candidates[: max(0, limit)]


def improvement_candidates(
    summary: Mapping[str, object],
    *,
    limit: int = 5,
) -> list[TradeImprovementCandidate]:
    output: list[TradeImprovementCandidate] = []
    for index, finding in enumerate(expectancy_findings(summary, limit=limit), start=1):
        candidate = _candidate_from_finding(finding, index)
        if candidate is not None:
            output.append(candidate)
    return output


def variant_improvement_candidates(
    variant_report: Mapping[str, object],
    *,
    limit: int = 3,
) -> list[TradeImprovementCandidate]:
    output: list[TradeImprovementCandidate] = []
    variants = variant_report.get("variants")
    baseline = variant_report.get("baseline")
    baseline_overall = baseline.get("overall") if isinstance(baseline, Mapping) else {}
    if isinstance(variants, Sequence):
        variant_rows = [item for item in variants if isinstance(item, Mapping)]
        trial_sharpes = _variant_trial_sharpes(variant_rows)
        for raw in variant_rows:
            if raw.get("recommendation") != "paper_test" or not _variant_has_canonical_net_evidence(
                raw
            ):
                continue
            output.append(
                _candidate_from_variant(
                    raw,
                    baseline_overall,
                    len(output) + 1,
                    trial_sharpes=trial_sharpes,
                    trial_count=len(variant_rows),
                )
            )
            if len(output) >= limit:
                return output
    cells = variant_report.get("cells")
    if isinstance(cells, Mapping):
        for scope, grouped in cells.items():
            if not isinstance(grouped, Mapping):
                continue
            for key, cell_report in grouped.items():
                if not isinstance(cell_report, Mapping):
                    continue
                cell_variants = cell_report.get("variants")
                cell_baseline = cell_report.get("baseline")
                if not isinstance(cell_variants, Sequence):
                    continue
                cell_variant_rows = [item for item in cell_variants if isinstance(item, Mapping)]
                cell_trial_sharpes = _variant_trial_sharpes(cell_variant_rows)
                for raw in cell_variant_rows:
                    if raw.get(
                        "recommendation"
                    ) != "paper_test" or not _variant_has_canonical_net_evidence(raw):
                        continue
                    output.append(
                        _candidate_from_variant(
                            raw,
                            cell_baseline,
                            len(output) + 1,
                            scope=str(scope),
                            key=str(key),
                            trial_sharpes=cell_trial_sharpes,
                            trial_count=len(cell_variant_rows),
                        )
                    )
                    if len(output) >= limit:
                        return output
    return output


def decision_adjustment(
    summary: Mapping[str, object],
    symbol: str,
    direction: str,
    conviction: int,
) -> ExpectancyAdjustment:
    """今回の判断に対応する期待値ガードを返す。

    サンプル不足は警告のみ、十分なサンプルで期待Rが非正ならblock、
    PF/MFE/MAEが弱い場合は確信度減衰として返す。
    """
    findings = _matching_expectancy_findings(summary, symbol, direction, conviction)
    if not findings:
        return ExpectancyAdjustment()
    findings.sort(
        key=lambda item: (
            int(item["rank"]),
            -_finding_specificity(item),
            -int(item["tradable"]),
            item["label"],
        )
    )
    finding = findings[0]
    severity = str(finding.get("severity", ""))
    reason = str(finding.get("reason_ja", ""))
    if severity == "block":
        return _adjustment_from_finding(
            finding,
            action="block",
            factor=EXPECTANCY_BLOCK_FACTOR,
            block=True,
            reason_ja=f"{reason}。新規エントリーは見送り",
        )
    if severity == "weak":
        return _adjustment_from_finding(
            finding,
            action="dampen",
            factor=EXPECTANCY_WEAK_FACTOR,
            block=False,
            reason_ja=f"{reason}。確信度を×{EXPECTANCY_WEAK_FACTOR:.2f}に減衰",
        )
    if severity == "sample_guard":
        return _adjustment_from_finding(
            finding,
            action="sample_guard",
            factor=1.0,
            block=False,
            reason_ja=f"{reason}。実績が揃うまで参考扱い",
        )
    if severity in {"quality_warn", "quality_block"}:
        return _adjustment_from_finding(
            finding,
            action="quality_guard",
            factor=1.0,
            block=False,
            reason_ja=f"{reason}。データ補強まで最適化しない",
        )
    return ExpectancyAdjustment()


def retest_tp_sl_variants(
    entries: Iterable[Mapping[str, object]],
    *,
    target1_r_candidates: Sequence[float] = DEFAULT_TP1_R_CANDIDATES,
    target2_r_candidates: Sequence[float] = DEFAULT_TP2_R_CANDIDATES,
    cell_scopes: Sequence[str] = ("by_symbol_direction",),
    cell_limit: int = 20,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
    group_min_samples: int = MIN_GROUP_EXPECTANCY_SAMPLES,
) -> dict:
    """TP1/TP2候補をpaper再採点し、既存設定との期待値差分を返す。"""
    materialized = list(entries)
    baseline_outcomes = evaluate_trade_outcomes(
        materialized,
        horizon_hours=horizon_hours,
        tolerance_hours=tolerance_hours,
    )
    baseline_summary = summarize_expectancy(
        baseline_outcomes,
        min_samples=min_samples,
        group_min_samples=group_min_samples,
    )
    baseline_stats = baseline_summary.get("overall")
    variants: list[TradeVariantScore] = []
    variant_outcomes: list[tuple[float, float, list[TradeOutcome]]] = []
    for tp1_r in _clean_r_candidates(target1_r_candidates):
        for tp2_r in _clean_r_candidates(target2_r_candidates):
            if tp2_r <= tp1_r:
                continue
            if math.isclose(tp1_r, 1.0) and math.isclose(tp2_r, 2.0):
                continue
            outcomes = evaluate_trade_outcomes(
                materialized,
                horizon_hours=horizon_hours,
                tolerance_hours=tolerance_hours,
                target1_r=tp1_r,
                target2_r=tp2_r,
            )
            summary = summarize_expectancy(
                outcomes,
                min_samples=min_samples,
                group_min_samples=group_min_samples,
            )
            stats = summary.get("overall")
            if isinstance(stats, Mapping) and isinstance(baseline_stats, Mapping):
                variants.append(_variant_score(tp1_r, tp2_r, stats, baseline_stats))
                variant_outcomes.append((tp1_r, tp2_r, outcomes))
    variants.sort(key=_variant_sort_key)
    best = next(
        (variant for variant in variants if variant.recommendation in {"paper_test", "watch"}),
        None,
    )
    report = {
        "schema": 1,
        "baseline": baseline_summary,
        "variants": [variant.to_dict() for variant in variants],
        "best": best.to_dict() if best is not None else None,
        "cells": _variant_cell_reports(
            baseline_outcomes,
            variant_outcomes,
            scopes=cell_scopes,
            min_samples=group_min_samples,
            limit=cell_limit,
        ),
    }
    report["improvement_candidates"] = [
        candidate.to_dict() for candidate in variant_improvement_candidates(report)
    ]
    return report


def update_improvement_registry(
    previous: Mapping[str, object] | None,
    candidates: Sequence[TradeImprovementCandidate],
    *,
    now: datetime | None = None,
    managed_action_types: set[str] | None = None,
    data_contract: str | None = None,
    prospective_outcomes: Sequence[object] = (),
) -> dict:
    generated_time = _utc(now or datetime.now(UTC))
    generated_at = generated_time.isoformat()
    if (
        isinstance(previous, Mapping)
        and _stat_int(previous, "schema") != IMPROVEMENT_REGISTRY_SCHEMA
    ):
        previous = None
    previous_records = _registry_records(previous)
    events = _registry_events(previous)
    evidence_rows = _prospective_evidence_rows(prospective_outcomes)
    current_ids = {candidate.candidate_id for candidate in candidates}
    records: dict[str, dict] = {}
    for candidate in candidates:
        prior = previous_records.get(candidate.candidate_id, {})
        seen_count = _stat_int(prior, "seen_count") + 1
        prior_stage = str(prior.get("stage", ""))
        first_seen = str(prior.get("first_seen") or generated_at)
        if prior:
            prospective_metadata = _preserved_prospective_metadata(prior)
        else:
            prospective_metadata = _initial_prospective_metadata(
                candidate,
                evidence_rows,
                generated_time,
            )
        stage = (
            prior_stage if prior_stage in APPROVAL_PRESERVED_STAGES else "prospective_collecting"
        )
        record = {
            **candidate.to_dict(),
            "status": "active",
            "stage": stage,
            "first_seen": first_seen,
            "last_seen": generated_at,
            "resolved_at": None,
            "seen_count": seen_count,
            **prospective_metadata,
        }
        record_data_contract = data_contract or str(prior.get("data_contract") or "")
        if record_data_contract:
            record["data_contract"] = record_data_contract
        for key in (
            "approved_at",
            "approved_by",
            "approval_note",
            "rejected_at",
            "rejected_by",
            "rejection_note",
            "auto_paused_at",
            "auto_pause_reason_ja",
            "resumed_at",
            "resumed_by",
            "resume_note",
        ):
            if key in prior:
                record[key] = prior[key]
        if stage not in APPROVAL_PRESERVED_STAGES:
            record = _evaluate_prospective_candidate(record, evidence_rows, generated_time)
            stage = str(record.get("stage", "prospective_collecting"))
        records[candidate.candidate_id] = record
        if not prior:
            _append_registry_event(
                events,
                generated_at=generated_at,
                candidate_id=candidate.candidate_id,
                event_type="detected",
                to_stage=stage,
                details={
                    "action_type": candidate.action_type,
                    "priority": candidate.priority,
                    "candidate_created_at": record.get("candidate_created_at"),
                    "training_data_end": record.get("training_data_end"),
                    "prospective_start": record.get("prospective_start"),
                    "prospective_end": record.get("prospective_end"),
                    "lockbox_id": record.get("lockbox_id"),
                    "dataset_hash": record.get("dataset_hash"),
                },
            )
        elif str(prior.get("stage", "")) != stage:
            _append_registry_event(
                events,
                generated_at=generated_at,
                candidate_id=candidate.candidate_id,
                event_type="stage_changed",
                from_stage=str(prior.get("stage", "")),
                to_stage=stage,
                details={
                    "prospective_dataset_hash": record.get("prospective_dataset_hash"),
                    "prospective_effective_dataset_hash": record.get(
                        "prospective_effective_dataset_hash"
                    ),
                    "prospective_metrics": record.get("prospective_metrics"),
                },
            )
    for candidate_id, prior in previous_records.items():
        if candidate_id in current_ids:
            continue
        if (
            managed_action_types is not None
            and prior.get("action_type") not in managed_action_types
        ):
            records[candidate_id] = dict(prior)
            continue
        record = dict(prior)
        record["status"] = "resolved"
        record["stage"] = "resolved"
        record["resolved_at"] = record.get("resolved_at") or generated_at
        record["seen_count"] = _stat_int(record, "seen_count")
        records[candidate_id] = record
        _append_registry_event(
            events,
            generated_at=generated_at,
            candidate_id=candidate_id,
            event_type="resolved",
            from_stage=str(prior.get("stage", "")),
            to_stage="resolved",
        )
    payload = _registry_payload(records, generated_at, _bounded_registry_events(events))
    if data_contract:
        payload["data_contract"] = data_contract
    return payload


def _prospective_evidence_rows(outcomes: Sequence[object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for outcome in outcomes:
        if isinstance(outcome, Mapping):
            rows.append({str(key): value for key, value in outcome.items()})
            continue
        to_dict = getattr(outcome, "to_dict", None)
        if callable(to_dict):
            raw = to_dict()
            if isinstance(raw, Mapping):
                rows.append({str(key): value for key, value in raw.items()})
    return rows


def _initial_prospective_metadata(
    candidate: TradeImprovementCandidate,
    evidence_rows: Sequence[Mapping[str, object]],
    created_at: datetime,
) -> dict[str, object]:
    training_rows = [
        row
        for row in evidence_rows
        if (holding_end := _parse_aware_iso(row.get("holding_end_time"))) is not None
        and holding_end <= created_at
    ]
    training_end = max(
        (
            parsed
            for row in training_rows
            if (parsed := _parse_aware_iso(row.get("holding_end_time"))) is not None
        ),
        default=created_at,
    )
    prospective_start = max(created_at, training_end)
    dataset_hash = _prospective_dataset_hash(training_rows)
    lockbox_raw = (
        f"{candidate.candidate_id}|{created_at.isoformat()}|"
        f"{training_end.isoformat()}|{dataset_hash}"
    )
    lockbox_id = "prospective-" + hashlib.sha256(lockbox_raw.encode("utf-8")).hexdigest()[:20]
    return {
        "prospective_schema": PROSPECTIVE_EVIDENCE_SCHEMA,
        "candidate_created_at": created_at.isoformat(),
        "candidate_definition_hash": _candidate_definition_hash(candidate.to_dict()),
        "training_data_end": training_end.isoformat(),
        "prospective_start": prospective_start.isoformat(),
        "prospective_end": (prospective_start + timedelta(days=PROSPECTIVE_VALID_DAYS)).isoformat(),
        "lockbox_id": lockbox_id,
        "dataset_hash": dataset_hash,
        "prospective_dataset_hash": _prospective_dataset_hash(()),
        "prospective_effective_dataset_hash": _prospective_dataset_hash(()),
        "prospective_outcome_keys": [],
        "prospective_metrics": _empty_prospective_metrics(),
    }


def _preserved_prospective_metadata(prior: Mapping[str, object]) -> dict[str, object]:
    raw_outcome_keys = prior.get("prospective_outcome_keys")
    raw_metrics = prior.get("prospective_metrics")
    return {
        "prospective_schema": prior.get("prospective_schema"),
        "candidate_created_at": prior.get("candidate_created_at"),
        "candidate_definition_hash": prior.get("candidate_definition_hash"),
        "training_data_end": prior.get("training_data_end"),
        "prospective_start": prior.get("prospective_start"),
        "prospective_end": prior.get("prospective_end"),
        "lockbox_id": prior.get("lockbox_id"),
        "dataset_hash": prior.get("dataset_hash"),
        "prospective_dataset_hash": prior.get("prospective_dataset_hash"),
        "prospective_effective_dataset_hash": prior.get("prospective_effective_dataset_hash"),
        "prospective_outcome_keys": (
            list(raw_outcome_keys) if isinstance(raw_outcome_keys, list) else []
        ),
        "prospective_metrics": (
            {str(key): value for key, value in raw_metrics.items()}
            if isinstance(raw_metrics, Mapping)
            else _empty_prospective_metrics()
        ),
        "prospective_last_evaluated_at": prior.get("prospective_last_evaluated_at"),
    }


def _evaluate_prospective_candidate(
    record: dict[str, object],
    evidence_rows: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[str, object]:
    start = _parse_aware_iso(record.get("prospective_start"))
    end = _parse_aware_iso(record.get("prospective_end"))
    if start is None or end is None or end <= start:
        record["stage"] = "expired"
        record["prospective_metrics"] = {
            **_empty_prospective_metrics(),
            "blocking_reasons": ["invalid_prospective_window"],
        }
        return record
    if record.get("candidate_definition_hash") != _candidate_definition_hash(record):
        record["stage"] = "expired"
        record["expired_at"] = record.get("expired_at") or now.isoformat()
        record["prospective_metrics"] = {
            **_empty_prospective_metrics(),
            "blocking_reasons": ["candidate_definition_changed"],
        }
        return record

    scoped_rows: list[Mapping[str, object]] = []
    gross_rows: list[Mapping[str, object]] = []
    valid_rows: list[Mapping[str, object]] = []
    for row in evidence_rows:
        prediction_time = _parse_aware_iso(row.get("prediction_time"))
        holding_end_time = _parse_aware_iso(row.get("holding_end_time"))
        if (
            prediction_time is None
            or holding_end_time is None
            or prediction_time <= start
            or holding_end_time <= start
            or holding_end_time > now
            or holding_end_time > end
            or not _prospective_scope_match(record, row)
        ):
            continue
        scoped_rows.append(row)
        if _prospective_gross_value(row) is not None and row.get("tradable") is True:
            gross_rows.append(row)
        if row.get("tradable") is True and not canonical_net_label_contract_flags(row):
            valid_rows.append(row)

    try:
        effective_rows, effective = select_effective_samples(valid_rows, min_samples=1)
    except EffectiveSampleConflict:
        record["stage"] = "expired"
        record["expired_at"] = record.get("expired_at") or now.isoformat()
        record["prospective_metrics"] = {
            **_empty_prospective_metrics(),
            "raw_samples": len(gross_rows),
            "net_label_samples": len(valid_rows),
            "blocking_reasons": ["effective_sample_conflict"],
        }
        return record
    net_values = [
        value for row in effective_rows if (value := _stat_float(row, "realized_net_r")) is not None
    ]
    versions = sorted({str(row.get("label_version", "")) for row in gross_rows})
    provenances = sorted({str(row.get("label_provenance", "")) for row in gross_rows})
    cost_models = sorted({str(row.get("cost_model_id", "")) for row in gross_rows})
    proposed = record.get("proposed_change")
    proposed_change = proposed if isinstance(proposed, Mapping) else {}
    expected_version = str(proposed_change.get("label_version") or "")
    expected_provenance = str(proposed_change.get("label_provenance") or "")
    expected_cost_model = str(proposed_change.get("cost_model_id") or "")
    contract_consistent = (
        len(versions) <= 1
        and len(provenances) <= 1
        and len(cost_models) <= 1
        and (not expected_version or versions == [expected_version])
        and (not expected_provenance or provenances == [expected_provenance])
        and (not expected_cost_model or cost_models == [expected_cost_model])
    )
    coverage = len(valid_rows) / len(gross_rows) if gross_rows else None
    expectancy = _mean(net_values)
    lcb = _prospective_lcb(net_values)
    dsr, dsr_trials = _prospective_dsr(net_values, proposed_change)
    max_drawdown = _prospective_max_drawdown(net_values)
    stressed_expectancy = expectancy - PROSPECTIVE_COST_STRESS_R if expectancy is not None else None
    concentration = _prospective_concentration(effective_rows)
    scope = str(proposed_change.get("scope") or record.get("scope") or "")
    concentration_ok = concentration is not None and (
        scope in {"by_symbol", "by_symbol_direction", "通貨ペア", "通貨ペア×方向"}
        or concentration <= PROSPECTIVE_MAX_CONCENTRATION
    )
    blocking_reasons: list[str] = []
    if effective.effective_samples < PROSPECTIVE_MIN_EFFECTIVE_SAMPLES:
        blocking_reasons.append("insufficient_effective_samples")
    if coverage is None or coverage < PROSPECTIVE_MIN_NET_COVERAGE:
        blocking_reasons.append("insufficient_net_label_coverage")
    if lcb is None or lcb <= 0:
        blocking_reasons.append("nonpositive_net_expectancy_lcb")
    if dsr is None or dsr < PROSPECTIVE_MIN_DSR:
        blocking_reasons.append("insufficient_dsr")
    expected_trials = _stat_int(proposed_change, "trial_count")
    if record.get("action_type") == "tp_sl_variant_paper_test" and (
        expected_trials < 1 or dsr_trials != expected_trials
    ):
        blocking_reasons.append("incomplete_trial_sharpe_history")
    if max_drawdown is None or max_drawdown < -PROSPECTIVE_MAX_DRAWDOWN_R:
        blocking_reasons.append("max_drawdown_exceeded")
    if stressed_expectancy is None or stressed_expectancy <= 0:
        blocking_reasons.append("cost_stress_failed")
    if not concentration_ok:
        blocking_reasons.append("concentration_exceeded")
    if not contract_consistent:
        blocking_reasons.append("mixed_label_or_cost_contract")

    metrics = {
        "schema": PROSPECTIVE_EVIDENCE_SCHEMA,
        "scoped_samples": len(scoped_rows),
        "quality_excluded_samples": len(scoped_rows) - len(gross_rows),
        "raw_samples": len(gross_rows),
        "net_label_samples": len(valid_rows),
        "effective_samples": effective.effective_samples,
        "net_label_coverage": coverage,
        "overlap_ratio": effective.overlap_ratio,
        "cluster_count": effective.cluster_count,
        "market_days": effective.market_days,
        "net_expectancy_r": _round(expectancy),
        "net_expectancy_lcb": _round(lcb),
        "dsr": _round(dsr),
        "dsr_trials": dsr_trials,
        "max_drawdown_r": _round(max_drawdown),
        "cost_stressed_expectancy_r": _round(stressed_expectancy),
        "max_symbol_concentration": _round(concentration),
        "label_versions": versions,
        "label_provenances": provenances,
        "cost_model_ids": cost_models,
        "contract_consistent": contract_consistent,
        "blocking_reasons": blocking_reasons,
    }
    record["prospective_metrics"] = metrics
    record["prospective_dataset_hash"] = _prospective_dataset_hash(scoped_rows)
    record["prospective_effective_dataset_hash"] = _prospective_dataset_hash(effective_rows)
    record["prospective_outcome_keys"] = list(effective.selected_keys)
    record["prospective_last_evaluated_at"] = now.isoformat()
    if not blocking_reasons:
        record["stage"] = "ready_for_review"
        record["ready_for_review_at"] = record.get("ready_for_review_at") or now.isoformat()
    elif now >= end:
        record["stage"] = "expired"
        record["expired_at"] = record.get("expired_at") or now.isoformat()
    else:
        record["stage"] = "prospective_collecting"
    return record


def _prospective_scope_match(
    record: Mapping[str, object],
    row: Mapping[str, object],
) -> bool:
    if record.get("action_type") == "tp_sl_variant_paper_test" and str(
        row.get("target_policy_id") or ""
    ) != str(record.get("candidate_id") or ""):
        return False
    proposed = record.get("proposed_change")
    proposed_change = proposed if isinstance(proposed, Mapping) else {}
    scope = str(proposed_change.get("scope") or record.get("scope") or "")
    key = str(proposed_change.get("key") or record.get("key") or "")
    symbol = str(row.get("symbol") or "").upper()
    direction = str(row.get("direction") or "").lower()
    if scope in {"overall", "全体", "データ品質", "TP/SL候補"}:
        return True
    if scope in {"by_symbol_direction", "通貨ペア×方向"}:
        return key == f"{symbol}:{direction}"
    if scope in {"by_symbol", "通貨ペア"}:
        return key == symbol
    if scope in {"by_direction", "方向"}:
        return key == direction
    if scope in {"by_confidence", "確信度"}:
        return key == _confidence_label_from_int(_stat_int(row, "conviction"))
    return False


def _prospective_gross_value(row: Mapping[str, object]) -> float | None:
    gross = _float(row.get("gross_realized_r"))
    return gross if gross is not None else _float(row.get("realized_r"))


def _prospective_dataset_hash(rows: Sequence[Mapping[str, object]]) -> str:
    canonical = [
        {
            "decision_id": str(row.get("decision_id") or ""),
            "label_version": str(row.get("label_version") or ""),
            "label_provenance": str(row.get("label_provenance") or ""),
            "prediction_time": row.get("prediction_time"),
            "holding_end_time": row.get("holding_end_time"),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "timeframe": row.get("timeframe"),
            "horizon_hours": _float(row.get("horizon_hours")),
            "conviction": row.get("conviction"),
            "tradable": row.get("tradable"),
            "gross_realized_r": _prospective_gross_value(row),
            "quote_realized_r": _float(row.get("quote_realized_r")),
            "realized_net_r": _float(row.get("realized_net_r")),
            "net_label_eligible": row.get("net_label_eligible"),
            "entry_bid": _float(row.get("entry_bid")),
            "entry_ask": _float(row.get("entry_ask")),
            "planned_risk_distance": _float(row.get("planned_risk_distance")),
            "entry_spread_r": _float(row.get("entry_spread_r")),
            "slippage_r": _float(row.get("slippage_r")),
            "commission_r": _float(row.get("commission_r")),
            "financing_r": _float(row.get("financing_r")),
            "additional_cost_r": _float(row.get("additional_cost_r")),
            "execution_cost_r": _float(row.get("execution_cost_r")),
            "cost_model_id": row.get("cost_model_id"),
            "cost_model_version": row.get("cost_model_version"),
            "cost_status": row.get("cost_status"),
            "entry_quote_source": row.get("entry_quote_source"),
            "spread_source": row.get("spread_source"),
            "target_policy_id": row.get("target_policy_id"),
        }
        for row in rows
    ]
    canonical.sort(
        key=lambda row: (
            str(row["prediction_time"] or ""),
            str(row["decision_id"]),
            str(row["label_version"]),
        )
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_definition_hash(candidate: Mapping[str, object]) -> str:
    frozen = {
        key: candidate.get(key)
        for key in (
            "candidate_id",
            "scope",
            "key",
            "priority",
            "action_type",
            "proposed_change",
            "validation_ja",
            "guardrail_ja",
        )
    }
    encoded = json.dumps(
        json_safe(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prospective_lcb(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return mean - 1.645 * math.sqrt(variance / len(values))


def _prospective_dsr(
    values: Sequence[float],
    proposed_change: Mapping[str, object],
) -> tuple[float | None, int]:
    if len(values) < 3:
        return None, 0
    try:
        import numpy as np

        from fx_backtester.overfitting import deflated_sharpe_ratio, per_period_sharpe
    except ImportError:
        return None, 0
    raw_trials = proposed_change.get("trial_sharpes")
    trial_sharpes = (
        [
            float(value)
            for value in raw_trials
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ]
        if isinstance(raw_trials, Sequence) and not isinstance(raw_trials, str | bytes)
        else []
    )
    selected = np.asarray(values, dtype=float)
    if not trial_sharpes:
        trial_sharpes = [per_period_sharpe(selected)]
    try:
        result = deflated_sharpe_ratio(selected, trial_sharpes)
    except ValueError:
        return None, len(trial_sharpes)
    return float(result["dsr"]), len(trial_sharpes)


def _prospective_max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def _prospective_concentration(rows: Sequence[Mapping[str, object]]) -> float | None:
    if not rows:
        return None
    counts: dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "unknown")
        counts[symbol] = counts.get(symbol, 0) + 1
    return max(counts.values()) / len(rows)


def _empty_prospective_metrics() -> dict[str, object]:
    return {
        "schema": PROSPECTIVE_EVIDENCE_SCHEMA,
        "scoped_samples": 0,
        "quality_excluded_samples": 0,
        "raw_samples": 0,
        "net_label_samples": 0,
        "effective_samples": 0,
        "net_label_coverage": None,
        "overlap_ratio": None,
        "cluster_count": 0,
        "market_days": 0,
        "net_expectancy_r": None,
        "net_expectancy_lcb": None,
        "dsr": None,
        "dsr_trials": 0,
        "max_drawdown_r": None,
        "cost_stressed_expectancy_r": None,
        "max_symbol_concentration": None,
        "label_versions": [],
        "label_provenances": [],
        "cost_model_ids": [],
        "contract_consistent": False,
        "blocking_reasons": ["insufficient_effective_samples"],
    }


def _parse_aware_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def load_improvement_registry(path: str | Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != IMPROVEMENT_REGISTRY_SCHEMA:
        return {}
    return raw


def save_improvement_registry(registry: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(json_safe(registry), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def set_improvement_candidate_approval(
    registry: Mapping[str, object] | None,
    candidate_id: str,
    decision: str,
    *,
    actor: str = "manual",
    note: str = "",
    now: datetime | None = None,
) -> tuple[dict, dict]:
    """改善候補を人間承認/却下/再開する。

    approved は ready_for_review のみ、resumed は auto_paused のみ許可する。
    TP/SL候補は、期待Rが現行比で改善している証跡がある場合だけ昇格できる。
    """
    generated_time = _utc(now or datetime.now(UTC))
    generated_at = generated_time.isoformat()
    records = _registry_records(registry)
    events = _registry_events(registry)
    candidate_id = str(candidate_id)
    result = {
        "candidate_id": candidate_id,
        "decision": decision,
        "status": "not_found",
        "message_ja": "候補IDが見つかりません",
    }
    record = records.get(candidate_id)
    if record is None:
        return _registry_payload(records, generated_at), result

    stage = str(record.get("stage", ""))
    status = str(record.get("status", ""))
    if status != "active":
        result.update(
            {
                "status": "not_active",
                "message_ja": "active候補ではないため承認対象外です",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision == "approved" and stage != "ready_for_review":
        result.update(
            {
                "status": "not_ready",
                "message_ja": "ready_for_review到達前のため承認できません",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision == "resumed" and stage != "auto_paused":
        result.update(
            {
                "status": "not_paused",
                "message_ja": "auto_paused候補ではないため再開できません",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision not in {"approved", "rejected", "resumed"}:
        result.update(
            {
                "status": "invalid_decision",
                "message_ja": "decisionはapproved/rejected/resumedのみ有効です",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision in {"approved", "resumed"}:
        prospective_ok, prospective_reason = _prospective_approval_gate(
            record,
            now=generated_time,
        )
        if not prospective_ok:
            result.update(
                {
                    "status": "not_ready",
                    "message_ja": prospective_reason,
                }
            )
            return _registry_payload(records, generated_at), result
        improvement_ok, improvement_reason = _approval_improvement_gate(record)
        if not improvement_ok:
            result.update(
                {
                    "status": "not_improving",
                    "message_ja": improvement_reason,
                }
            )
            return _registry_payload(records, generated_at), result

    updated = dict(record)
    updated["stage"] = "approved" if decision == "resumed" else decision
    updated[f"{decision}_at"] = generated_at
    if decision == "approved":
        updated["approved_by"] = actor
        updated["approval_note"] = note
        updated.pop("rejected_at", None)
        updated.pop("rejected_by", None)
        updated.pop("rejection_note", None)
    elif decision == "resumed":
        updated["resumed_by"] = actor
        updated["resume_note"] = note
        updated["approved_by"] = actor
        updated["approval_note"] = note or str(updated.get("approval_note", ""))
        updated.pop("rejected_at", None)
        updated.pop("rejected_by", None)
        updated.pop("rejection_note", None)
    else:
        updated["rejected_by"] = actor
        updated["rejection_note"] = note
        updated.pop("approved_at", None)
        updated.pop("approved_by", None)
        updated.pop("approval_note", None)
        updated.pop("resumed_at", None)
        updated.pop("resumed_by", None)
        updated.pop("resume_note", None)
    records[candidate_id] = updated
    _append_registry_event(
        events,
        generated_at=generated_at,
        candidate_id=candidate_id,
        event_type=decision,
        from_stage=stage,
        to_stage=str(updated.get("stage", "")),
        actor=actor,
        note=note,
    )
    result.update(
        {
            "status": decision,
            "message_ja": {
                "approved": "候補を承認しました",
                "rejected": "候補を却下しました",
                "resumed": "自動停止中の候補を再開しました",
            }[decision],
        }
    )
    return _registry_payload(records, generated_at, _bounded_registry_events(events)), result


def build_monitoring_snapshot(
    summary: Mapping[str, object],
    *,
    registry: Mapping[str, object] | None = None,
    health_report: TradeOutcomeHealthReport | None = None,
    now: datetime | None = None,
) -> dict:
    """cron/dashboard向けの軽量JSON。"""
    generated_at = _utc(now or datetime.now(UTC)).isoformat()
    health_report = health_report or check_expectancy_health(summary)
    records = _registry_records(registry)
    active = [record for record in records.values() if record.get("status") == "active"]
    ready_for_review = [record for record in active if record.get("stage") == "ready_for_review"]
    approved = [record for record in active if record.get("stage") == "approved"]
    rejected = [record for record in active if record.get("stage") == "rejected"]
    auto_paused = [record for record in active if record.get("stage") == "auto_paused"]
    alerts = [
        {
            "type": "health",
            "severity": check.status,
            "message_ja": check.message,
            "check": check.name,
        }
        for check in health_report.checks
        if check.status in {STATUS_WARN, STATUS_FAIL}
    ]
    alerts.extend(
        {
            "type": "ready_for_review",
            "severity": "info",
            "candidate_id": str(record.get("candidate_id", "")),
            "message_ja": str(record.get("title_ja", "承認待ちの改善候補")),
        }
        for record in ready_for_review
    )
    alerts.extend(
        {
            "type": "auto_paused",
            "severity": "warn",
            "candidate_id": str(record.get("candidate_id", "")),
            "message_ja": str(record.get("auto_pause_reason_ja", "承認済みTP/SLを自動停止")),
        }
        for record in auto_paused
    )
    return {
        "schema": 2,
        "generated_at": generated_at,
        "status": health_report.status,
        "exit_code": health_report.exit_code,
        "health": health_report.to_dict(),
        "summary": summary,
        "registry": {
            "active_count": len(active),
            "ready_for_review_count": len(ready_for_review),
            "approved_count": len(approved),
            "rejected_count": len(rejected),
            "auto_paused_count": len(auto_paused),
            "resolved_count": sum(
                1 for record in records.values() if record.get("status") == "resolved"
            ),
            "ready_for_review": _monitor_records(ready_for_review),
            "approved": _monitor_records(approved),
            "rejected": _monitor_records(rejected),
            "auto_paused": _monitor_records(auto_paused),
        },
        "approved_policy_stats": approved_policy_stats(summary, registry=registry),
        "recent_events": _bounded_registry_events(_registry_events(registry), limit=20),
        "alerts": alerts,
    }


def approved_target_policies(
    registry: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
) -> list[ApprovedTargetPolicy]:
    policies: list[ApprovedTargetPolicy] = []
    current_time = _utc(now or datetime.now(UTC))
    for record in _registry_records(registry).values():
        if record.get("status") != "active" or record.get("stage") != "approved":
            continue
        if record.get("action_type") != "tp_sl_variant_paper_test":
            continue
        prospective_ok, _ = _prospective_approval_gate(record, now=current_time)
        if not prospective_ok:
            continue
        improvement_ok, _ = _approval_improvement_gate(record)
        if not improvement_ok:
            continue
        proposed = record.get("proposed_change")
        if not isinstance(proposed, Mapping):
            continue
        target1_r = _stat_float(proposed, "target1_r")
        target2_r = _stat_float(proposed, "target2_r")
        if (
            target1_r is None
            or target2_r is None
            or not math.isfinite(target1_r)
            or not math.isfinite(target2_r)
            or target1_r <= 0
            or target2_r <= target1_r
        ):
            continue
        policies.append(
            ApprovedTargetPolicy(
                candidate_id=str(record.get("candidate_id", "")),
                scope=str(proposed.get("scope", "overall")),
                key=str(proposed.get("key", "")),
                target1_r=round(target1_r, 4),
                target2_r=round(target2_r, 4),
                priority=str(record.get("priority", "")),
                reason_ja=str(record.get("title_ja", "")),
                approved_at=(str(record.get("approved_at")) if record.get("approved_at") else None),
            )
        )
    policies.sort(
        key=lambda policy: (
            -_policy_specificity(policy),
            str(policy.approved_at or ""),
            policy.candidate_id,
        )
    )
    return policies


def select_approved_target_policy(
    registry: Mapping[str, object] | None,
    symbol: str,
    direction: str,
    conviction: int,
    *,
    now: datetime | None = None,
) -> ApprovedTargetPolicy | None:
    for policy in approved_target_policies(registry, now=now):
        if _target_policy_matches(policy, symbol, direction, conviction):
            return policy
    return None


def approved_policy_stats(
    summary: Mapping[str, object],
    *,
    registry: Mapping[str, object] | None = None,
) -> list[dict]:
    by_policy = summary.get("by_target_policy")
    if not isinstance(by_policy, Mapping):
        return []
    records = _registry_records(registry)
    rows: list[dict] = []
    for candidate_id, stats in by_policy.items():
        if not isinstance(stats, Mapping):
            continue
        record = records.get(str(candidate_id), {})
        rows.append(
            {
                "candidate_id": str(candidate_id),
                "stage": str(record.get("stage", "")),
                "title_ja": str(record.get("title_ja", "")),
                "tradable": _stat_int(stats, "tradable"),
                "evaluated": _stat_int(stats, "evaluated"),
                "sample_ok": bool(stats.get("sample_ok")),
                "gross_expectancy_r": _stat_float(stats, "gross_expectancy_r"),
                "net_expectancy_r": _stat_float(stats, "net_expectancy_r"),
                "gross_profit_factor_r": _stat_float(stats, "gross_profit_factor_r"),
                "net_profit_factor_r": _stat_float(stats, "net_profit_factor_r"),
                "net_label_samples": _stat_int(stats, "net_label_samples"),
                "net_label_coverage": _stat_float(stats, "net_label_coverage"),
                "avg_path_quality": _stat_float(stats, "avg_path_quality"),
            }
        )
    rows.sort(key=lambda row: (-int(row["tradable"]), str(row["candidate_id"])))
    return rows


def auto_pause_underperforming_approved_policies(
    registry: Mapping[str, object] | None,
    summary: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> tuple[dict, list[dict]]:
    generated_time = _utc(now or datetime.now(UTC))
    generated_at = generated_time.isoformat()
    records = _registry_records(registry)
    events = _registry_events(registry)
    raw_by_policy = summary.get("by_target_policy")
    by_policy = raw_by_policy if isinstance(raw_by_policy, Mapping) else {}

    paused: list[dict] = []
    for candidate_id, record in list(records.items()):
        if record.get("status") != "active" or record.get("stage") != "approved":
            continue
        if record.get("action_type") != "tp_sl_variant_paper_test":
            continue
        prospective_ok, prospective_reason = _prospective_approval_gate(
            record,
            now=generated_time,
        )
        stats = by_policy.get(candidate_id)
        if not prospective_ok:
            reason = prospective_reason
            stats = stats if isinstance(stats, Mapping) else {}
        elif not isinstance(stats, Mapping):
            reason = "承認済みTP/SLのcanonical純R証拠が取得不能"
            stats = {}
        elif not bool(stats.get("sample_ok")) or _stat_int(stats, "net_label_samples") <= 0:
            reason = (
                "承認済みTP/SLのcanonical純Rサンプル不足"
                f"(n={_stat_int(stats, 'net_label_samples')})"
            )
        else:
            reason = _policy_pause_reason(stats)
        if not reason:
            continue
        updated = dict(record)
        updated["stage"] = "auto_paused"
        updated["auto_paused_at"] = generated_at
        updated["auto_pause_reason_ja"] = reason
        records[candidate_id] = updated
        _append_registry_event(
            events,
            generated_at=generated_at,
            candidate_id=candidate_id,
            event_type="auto_paused",
            from_stage=str(record.get("stage", "")),
            to_stage="auto_paused",
            details={
                "reason_ja": reason,
                "net_expectancy_r": _stat_float(stats, "net_expectancy_r"),
                "net_profit_factor_r": _stat_float(stats, "net_profit_factor_r"),
                "net_label_samples": _stat_int(stats, "net_label_samples"),
            },
        )
        paused.append(
            {
                "candidate_id": candidate_id,
                "reason_ja": reason,
                "net_expectancy_r": _stat_float(stats, "net_expectancy_r"),
                "net_profit_factor_r": _stat_float(stats, "net_profit_factor_r"),
                "net_label_samples": _stat_int(stats, "net_label_samples"),
            }
        )
    return _registry_payload(records, generated_at, _bounded_registry_events(events)), paused


def json_safe(value: object) -> object:
    """JSONレポート用にNaN/Infinityを標準JSONで扱える値へ落とす。"""
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def format_expectancy_report_ja(summary: Mapping[str, object], *, limit: int = 5) -> str:
    overall = summary.get("overall")
    if not isinstance(overall, Mapping) or _stat_int(overall, "evaluated") <= 0:
        return "トレード期待値監視: 対象なし"
    tradable = _stat_int(overall, "tradable")
    evaluated = _stat_int(overall, "evaluated")
    min_samples = _stat_int(overall, "min_samples")
    sample_status = "サンプルOK" if bool(overall.get("sample_ok")) else "サンプル不足"
    lines = [
        "トレード期待値監視(MFE/MAE/TP/SL): "
        f"canonical net期待R {_fmt_signed(_stat_float(overall, 'net_expectancy_r'), 'R')}"
        f" / net PF {_fmt_number(_stat_float(overall, 'net_profit_factor_r'))}"
        f" / gross診断 {_fmt_signed(_stat_float(overall, 'gross_expectancy_r'), 'R')}"
        f" / TP1 {_fmt_pct(_stat_float(overall, 'tp1_rate'))}"
        f" / TP2 {_fmt_pct(_stat_float(overall, 'tp2_rate'))}"
        f" / SL {_fmt_pct(_stat_float(overall, 'sl_rate'))}"
        f" / 平均MFE {_fmt_signed(_stat_float(overall, 'avg_mfe_r'), 'R')}"
        f" / 平均MAE {_fmt_number(_stat_float(overall, 'avg_mae_r'), 'R')}"
        f" (net n={_stat_int(overall, 'net_label_samples')}, gross n={tradable}/{evaluated}, "
        f"{sample_status}"
        + (f" {_stat_int(overall, 'effective_samples')}/{min_samples}" if min_samples else "")
        + ")"
    ]
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        lines.append(
            "経路データ品質: "
            f"scored={_stat_int(quality, 'scored')}/{_stat_int(quality, 'evaluated')}"
            f" / 低品質={_stat_int(quality, 'low_quality')}"
            f" / 平均品質{_fmt_pct(_stat_float(quality, 'avg_path_quality'))}"
        )
    findings = expectancy_findings(summary, limit=limit)
    if not findings:
        lines.append("改善候補: 期待値悪化セルなし")
    else:
        lines.append("改善候補:")
        lines.extend(f"・{finding['label']}: {finding['reason_ja']}" for finding in findings)
    return "\n".join(lines)


def counterfactual_outcome_count(summary: Mapping[str, object]) -> int:
    """summarize_expectancy結果に含まれる反実仮想アウトカム件数(品質フラグ集計から)。"""
    quality = summary.get("quality")
    if not isinstance(quality, Mapping):
        return 0
    flags = quality.get("flags")
    if not isinstance(flags, Mapping):
        return 0
    value = flags.get(COUNTERFACTUAL_QUALITY_FLAG, 0)
    return int(value) if isinstance(value, (int, float)) else 0


def format_guard_evidence_note_ja(summary: Mapping[str, object]) -> str:
    """期待値ガード根拠(実績+反実仮想)の1〜2行ノート。反実仮想ゼロなら空文字。

    ガード根拠が従来(実績のみ)と同一のときに毎回同じ注記を重ねないため、
    反実仮想が実際に混ざったときだけ表示する。
    """
    counterfactuals = counterfactual_outcome_count(summary)
    if counterfactuals <= 0:
        return ""
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        return ""
    tradable = _stat_int(overall, "tradable")
    min_samples = _stat_int(overall, "min_samples")
    return (
        "期待値ガード根拠: ガード見送り中のシャドー計画(反実仮想)"
        f"{counterfactuals}件を含めて更新 — 期待R "
        f"{_fmt_signed(_stat_float(overall, 'expectancy_r'), 'R')}"
        f"(n={tradable}/{min_samples})。"
        "推奨はガード判定に従い、根拠だけを毎時更新する"
    )


def format_improvement_candidates_ja(candidates: Sequence[TradeImprovementCandidate]) -> str:
    if not candidates:
        return "改善候補: なし"
    lines = ["改善候補(paper検証待ち):"]
    for candidate in candidates:
        lines.append(
            f"・[{candidate.priority}] {candidate.title_ja}: "
            f"{candidate.rationale_ja} / 検証: {candidate.validation_ja}"
        )
    return "\n".join(lines)


def format_improvement_registry_ja(registry: Mapping[str, object], *, limit: int = 5) -> str:
    records = _registry_records(registry)
    active = [record for record in records.values() if record.get("status") == "active"]
    resolved = [record for record in records.values() if record.get("status") == "resolved"]
    ready = [record for record in active if record.get("stage") == "ready_for_review"]
    approved = [record for record in active if record.get("stage") == "approved"]
    rejected = [record for record in active if record.get("stage") == "rejected"]
    auto_paused = [record for record in active if record.get("stage") == "auto_paused"]
    lines = [
        f"改善候補レジストリ: active={len(active)} / ready_for_review={len(ready)}"
        f" / approved={len(approved)} / auto_paused={len(auto_paused)}"
        f" / rejected={len(rejected)} / resolved={len(resolved)}"
    ]
    events = _registry_events(registry)
    if events:
        last = events[-1]
        lines.append(
            "監査イベント: "
            f"{len(events)}件 / latest={last.get('event_type')}:{last.get('candidate_id')}"
        )
    for record in sorted(
        active,
        key=lambda item: (
            -_record_prospective_effective_samples(item),
            str(item.get("candidate_id", "")),
        ),
    )[:limit]:
        metrics = record.get("prospective_metrics")
        effective_samples = (
            _stat_int(metrics, "effective_samples") if isinstance(metrics, Mapping) else 0
        )
        lines.append(
            f"・{record.get('title_ja', record.get('candidate_id'))}"
            f" (stage={record.get('stage')}, prospective effective={effective_samples}, "
            f"expiry={record.get('prospective_end') or '—'})"
        )
    return "\n".join(lines)


def format_variant_retest_report_ja(report: Mapping[str, object], *, limit: int = 5) -> str:
    baseline = report.get("baseline")
    if not isinstance(baseline, Mapping):
        return "TP/SL候補paper再採点: 対象なし"
    overall = baseline.get("overall")
    if not isinstance(overall, Mapping) or _stat_int(overall, "evaluated") <= 0:
        return "TP/SL候補paper再採点: 対象なし"
    lines = [
        "TP/SL候補paper再採点: "
        f"現行 canonical net期待R "
        f"{_fmt_signed(_stat_float(overall, 'net_expectancy_r'), 'R')}"
        f" / net PF {_fmt_number(_stat_float(overall, 'net_profit_factor_r'))}"
        f" / gross診断 {_fmt_signed(_stat_float(overall, 'gross_expectancy_r'), 'R')}"
        f" / net n={_stat_int(overall, 'net_label_samples')}"
        f" / gross n={_stat_int(overall, 'tradable')}/{_stat_int(overall, 'evaluated')}"
    ]
    best = report.get("best")
    if isinstance(best, Mapping):
        lines.append(
            "最有力候補: "
            f"TP1={_fmt_number(_stat_float(best, 'target1_r'), 'R')}"
            f" / TP2={_fmt_number(_stat_float(best, 'target2_r'), 'R')}"
            f" / 期待R {_fmt_signed(_stat_float(best, 'expectancy_r'), 'R')}"
            f" ({_fmt_signed(_stat_float(best, 'delta_expectancy_r'), 'R')})"
            f" / {best.get('reason_ja', '')}"
        )
    variants = report.get("variants")
    if not isinstance(variants, Sequence) or not variants:
        lines.append("候補: 比較可能なTP/SL候補なし")
        return "\n".join(lines)
    lines.append("候補上位:")
    for raw in [item for item in variants if isinstance(item, Mapping)][:limit]:
        lines.append(
            "・"
            f"TP1={_fmt_number(_stat_float(raw, 'target1_r'), 'R')}"
            f" / TP2={_fmt_number(_stat_float(raw, 'target2_r'), 'R')}"
            f" / 期待R {_fmt_signed(_stat_float(raw, 'expectancy_r'), 'R')}"
            f" / 差分 {_fmt_signed(_stat_float(raw, 'delta_expectancy_r'), 'R')}"
            f" / PF {_fmt_number(_stat_float(raw, 'profit_factor_r'))}"
            f" / {raw.get('recommendation', '')}"
        )
    cell_lines = _format_variant_cell_lines(report, limit=limit)
    if cell_lines:
        lines.append("セル別候補:")
        lines.extend(cell_lines)
    return "\n".join(lines)


def check_expectancy_health(
    summary: Mapping[str, object],
    *,
    require_sample_ok: bool = False,
) -> TradeOutcomeHealthReport:
    checks: list[TradeOutcomeHealthCheck] = []
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        return TradeOutcomeHealthReport(
            [TradeOutcomeHealthCheck("summary", STATUS_FAIL, "overall統計がありません")]
        )
    evaluated = _stat_int(overall, "evaluated")
    tradable = _stat_int(overall, "tradable")
    net_label_samples = _stat_int(overall, "net_label_samples")
    effective_samples = _stat_int(overall, "effective_samples")
    min_samples = _stat_int(overall, "min_samples")
    sample_ok = bool(overall.get("sample_ok"))
    expectancy_r = _stat_float(overall, "expectancy_r")
    if evaluated <= 0:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_WARN, "期待値監査対象がまだありません")
        )
    elif tradable <= 0:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_FAIL, "期待値計算に使える判断がありません")
        )
    elif net_label_samples <= 0:
        checks.append(
            TradeOutcomeHealthCheck(
                "sample",
                STATUS_FAIL,
                "canonical純Rラベルがなく期待値を判定できません",
                {"gross_samples": tradable, "net_label_samples": net_label_samples},
            )
        )
    elif not sample_ok:
        checks.append(
            TradeOutcomeHealthCheck(
                "sample",
                STATUS_FAIL if require_sample_ok else STATUS_WARN,
                "期待値サンプルが不足しています",
                {"effective_samples": effective_samples, "min_samples": min_samples},
            )
        )
    else:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_OK, "期待値サンプルは最低件数を満たしています")
        )
    if sample_ok and expectancy_r is not None and expectancy_r <= 0:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy", STATUS_FAIL, "全体期待Rが非正です", {"expectancy_r": expectancy_r}
            )
        )
    elif expectancy_r is None:
        checks.append(TradeOutcomeHealthCheck("expectancy", STATUS_WARN, "期待Rを計算できません"))
    else:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy",
                STATUS_OK if expectancy_r > 0 else STATUS_WARN,
                "期待Rを計算済み",
                {"expectancy_r": expectancy_r},
            )
        )
    quality = summary.get("quality")
    checks.append(_health_check_quality(quality if isinstance(quality, Mapping) else {}))
    blocking = [
        finding
        for finding in expectancy_findings(summary, limit=20)
        if finding["severity"] in {"block", "quality_block"}
    ]
    checks.append(
        TradeOutcomeHealthCheck(
            "blocked_cells",
            STATUS_FAIL if blocking else STATUS_OK,
            "期待値ガード対象セルがあります" if blocking else "期待値ガード対象セルはありません",
            {"count": len(blocking)},
        )
    )
    return TradeOutcomeHealthReport(checks)


def format_expectancy_health_ja(report: TradeOutcomeHealthReport) -> str:
    labels = {STATUS_OK: "OK", STATUS_WARN: "WARN", STATUS_FAIL: "FAIL"}
    lines = [f"トレード期待値ヘルスチェック: {labels[report.status]}"]
    for check in report.checks:
        detail = _format_details(check.details)
        suffix = f" ({detail})" if detail else ""
        lines.append(
            f"- [{labels.get(check.status, check.status.upper())}] {check.name}: {check.message}{suffix}"
        )
    return "\n".join(lines)


def _aggregate_by(
    outcomes: Sequence[TradeOutcome],
    key_func: Callable[[TradeOutcome], str],
    *,
    min_samples: int,
) -> dict[str, dict]:
    grouped: dict[str, list[TradeOutcome]] = {}
    for outcome in outcomes:
        key = key_func(outcome)
        if key:
            grouped.setdefault(key, []).append(outcome)
    return {
        key: aggregate_expectancy(group, min_samples=min_samples).to_dict()
        for key, group in sorted(grouped.items())
    }


def _matching_expectancy_findings(
    summary: Mapping[str, object],
    symbol: str,
    direction: str,
    conviction: int,
) -> list[dict]:
    normalized_symbol = symbol.upper().replace("/", "")
    normalized_direction = direction.lower()
    checks = [
        ("by_symbol_direction", "通貨ペア×方向", f"{normalized_symbol}:{normalized_direction}"),
        ("by_symbol", "通貨ペア", normalized_symbol),
        ("by_direction", "方向", normalized_direction),
        ("by_confidence", "確信度", _confidence_label_from_int(conviction)),
    ]
    findings: list[dict] = []
    for group_key, scope, key in checks:
        finding = _group_finding(summary, group_key, scope, key)
        if finding is not None:
            findings.append(finding)
    overall = summary.get("overall")
    if isinstance(overall, Mapping):
        finding = _expectancy_finding("全体", "", overall)
        if finding is not None:
            findings.append(finding)
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        finding = _quality_finding(quality)
        if finding is not None:
            findings.append(finding)
    return findings


def _group_finding(
    summary: Mapping[str, object],
    group_key: str,
    scope: str,
    key: str,
) -> dict | None:
    group = summary.get(group_key)
    if not isinstance(group, Mapping):
        return None
    stats = group.get(key)
    if not isinstance(stats, Mapping):
        return None
    return _expectancy_finding(scope, key, stats)


def _adjustment_from_finding(
    finding: Mapping[str, object],
    *,
    action: str,
    factor: float,
    block: bool,
    reason_ja: str,
) -> ExpectancyAdjustment:
    return ExpectancyAdjustment(
        action=action,
        factor=round(max(0.0, min(1.0, factor)), 4),
        block=block,
        reason_ja=reason_ja,
        matched_scope=str(finding.get("scope", "")),
        matched_key=str(finding.get("key", "")),
        severity=str(finding.get("severity", "")),
        tradable=_stat_int(finding, "tradable"),
        min_samples=_stat_int(finding, "min_samples"),
        expectancy_r=_stat_float(finding, "expectancy_r"),
        profit_factor_r=_stat_float(finding, "profit_factor_r"),
    )


def _finding_specificity(finding: Mapping[str, object]) -> int:
    scope = str(finding.get("scope", ""))
    return {
        "通貨ペア×方向": 5,
        "通貨ペア": 4,
        "方向": 3,
        "確信度": 2,
        "全体": 1,
        "データ品質": 0,
    }.get(scope, 0)


def _clean_r_candidates(values: Sequence[float]) -> list[float]:
    cleaned: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric) or numeric <= 0:
            continue
        cleaned.append(round(numeric, 4))
    return sorted(dict.fromkeys(cleaned))


def _variant_score(
    target1_r: float,
    target2_r: float,
    stats: Mapping[str, object],
    baseline: Mapping[str, object],
) -> TradeVariantScore:
    expectancy = _stat_float(stats, "net_expectancy_r")
    baseline_expectancy = _stat_float(baseline, "net_expectancy_r")
    profit_factor = _stat_float(stats, "net_profit_factor_r")
    baseline_pf = _stat_float(baseline, "net_profit_factor_r")
    delta_expectancy = (
        _round(expectancy - baseline_expectancy)
        if expectancy is not None and baseline_expectancy is not None
        else None
    )
    delta_pf = (
        _round(profit_factor - baseline_pf)
        if profit_factor is not None
        and baseline_pf is not None
        and math.isfinite(profit_factor)
        and math.isfinite(baseline_pf)
        else None
    )
    sample_ok = bool(stats.get("sample_ok"))
    recommendation, reason = _variant_recommendation(
        stats,
        baseline,
        delta_expectancy,
        profit_factor,
    )
    return TradeVariantScore(
        variant_id=f"tp1-{target1_r:g}-tp2-{target2_r:g}",
        target1_r=target1_r,
        target2_r=target2_r,
        tradable=_stat_int(stats, "tradable"),
        sample_ok=sample_ok,
        expectancy_r=expectancy,
        net_sharpe_per_period=_stat_float(stats, "net_sharpe_per_period"),
        profit_factor_r=profit_factor,
        gross_expectancy_r=_stat_float(stats, "gross_expectancy_r"),
        net_expectancy_r=expectancy,
        gross_profit_factor_r=_stat_float(stats, "gross_profit_factor_r"),
        net_profit_factor_r=profit_factor,
        net_label_samples=_stat_int(stats, "net_label_samples"),
        net_label_coverage=_stat_float(stats, "net_label_coverage"),
        effective_samples=_stat_int(stats, "effective_samples"),
        label_version=str(stats.get("label_version") or ""),
        label_provenance=str(stats.get("label_provenance") or ""),
        cost_model_id=str(stats.get("cost_model_id") or ""),
        tp1_rate=_stat_float(stats, "tp1_rate"),
        tp2_rate=_stat_float(stats, "tp2_rate"),
        sl_rate=_stat_float(stats, "sl_rate"),
        avg_mfe_r=_stat_float(stats, "avg_mfe_r"),
        avg_mae_r=_stat_float(stats, "avg_mae_r"),
        avg_path_quality=_stat_float(stats, "avg_path_quality"),
        delta_expectancy_r=delta_expectancy,
        delta_profit_factor_r=delta_pf,
        recommendation=recommendation,
        reason_ja=reason,
    )


def _variant_cell_reports(
    baseline_outcomes: Sequence[TradeOutcome],
    variant_outcomes: Sequence[tuple[float, float, Sequence[TradeOutcome]]],
    *,
    scopes: Sequence[str],
    min_samples: int,
    limit: int,
) -> dict[str, dict[str, dict]]:
    reports: dict[str, dict[str, dict]] = {}
    for scope in scopes:
        key_func = _cell_key_func(scope)
        if key_func is None:
            continue
        baseline_groups = _group_outcomes(baseline_outcomes, key_func)
        scoped: dict[str, dict] = {}
        for key, baseline_group in baseline_groups.items():
            baseline_stats = aggregate_expectancy(baseline_group, min_samples=min_samples).to_dict()
            if _stat_int(baseline_stats, "evaluated") <= 0:
                continue
            variants: list[TradeVariantScore] = []
            for tp1_r, tp2_r, outcomes in variant_outcomes:
                grouped = _group_outcomes(outcomes, key_func)
                stats = aggregate_expectancy(
                    grouped.get(key, []), min_samples=min_samples
                ).to_dict()
                variants.append(_variant_score(tp1_r, tp2_r, stats, baseline_stats))
            variants.sort(key=_variant_sort_key)
            best = next(
                (
                    variant
                    for variant in variants
                    if variant.recommendation in {"paper_test", "watch"}
                ),
                None,
            )
            scoped[key] = {
                "baseline": baseline_stats,
                "variants": [variant.to_dict() for variant in variants],
                "best": best.to_dict() if best is not None else None,
            }
        sorted_items = sorted(scoped.items(), key=lambda item: _cell_report_sort_key(item[1]))
        reports[scope] = dict(sorted_items[: max(0, limit)])
    return reports


def _format_variant_cell_lines(report: Mapping[str, object], *, limit: int) -> list[str]:
    cells = report.get("cells")
    if not isinstance(cells, Mapping):
        return []
    lines: list[str] = []
    for scope, grouped in cells.items():
        if not isinstance(grouped, Mapping):
            continue
        for key, cell_report in grouped.items():
            if not isinstance(cell_report, Mapping):
                continue
            best = cell_report.get("best")
            if not isinstance(best, Mapping) or best.get("recommendation") != "paper_test":
                continue
            label = _variant_scope_label(str(scope), str(key))
            lines.append(
                "・"
                f"{label}: TP1={_fmt_number(_stat_float(best, 'target1_r'), 'R')}"
                f" / TP2={_fmt_number(_stat_float(best, 'target2_r'), 'R')}"
                f" / 期待R {_fmt_signed(_stat_float(best, 'expectancy_r'), 'R')}"
                f" / 差分 {_fmt_signed(_stat_float(best, 'delta_expectancy_r'), 'R')}"
            )
            if len(lines) >= limit:
                return lines
    return lines


def _group_outcomes(
    outcomes: Sequence[TradeOutcome],
    key_func: Callable[[TradeOutcome], str],
) -> dict[str, list[TradeOutcome]]:
    grouped: dict[str, list[TradeOutcome]] = {}
    for outcome in outcomes:
        key = key_func(outcome)
        if key:
            grouped.setdefault(key, []).append(outcome)
    return grouped


def _cell_key_func(scope: str) -> Callable[[TradeOutcome], str] | None:
    if scope == "by_symbol_direction":
        return lambda outcome: f"{outcome.symbol}:{outcome.direction}"
    if scope == "by_symbol":
        return lambda outcome: outcome.symbol
    if scope == "by_direction":
        return lambda outcome: outcome.direction
    if scope == "by_confidence":
        return _confidence_label
    return None


def _cell_report_sort_key(report: Mapping[str, object]) -> tuple:
    best = report.get("best")
    baseline = report.get("baseline")
    if not isinstance(best, Mapping):
        best_rank = 9
        delta = None
    else:
        best_rank = {
            "paper_test": 0,
            "watch": 1,
            "sample_guard": 2,
            "reject": 3,
        }.get(str(best.get("recommendation")), 4)
        delta = _stat_float(best, "delta_expectancy_r")
    tradable = _stat_int(baseline, "tradable") if isinstance(baseline, Mapping) else 0
    return (best_rank, -_sort_number(delta), -tradable)


def _variant_recommendation(
    stats: Mapping[str, object],
    baseline: Mapping[str, object],
    delta_expectancy: float | None,
    profit_factor: float | None,
) -> tuple[str, str]:
    if not bool(stats.get("sample_ok")):
        return "sample_guard", "サンプル不足のため採用不可"
    expectancy = _stat_float(stats, "expectancy_r")
    if expectancy is None:
        return "reject", "期待Rを計算できないため採用不可"
    if expectancy <= 0:
        return "reject", "期待Rが非正のため採用不可"
    baseline_sample_ok = bool(baseline.get("sample_ok"))
    if (
        baseline_sample_ok
        and delta_expectancy is not None
        and delta_expectancy >= MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R
        and (profit_factor is None or profit_factor >= WEAK_PROFIT_FACTOR)
    ):
        return "paper_test", "現行より期待Rが改善。paper比較候補"
    if not baseline_sample_ok:
        return "watch", "現行サンプル不足。候補として監視"
    return "watch", "改善幅が小さいため監視継続"


def _variant_sort_key(variant: TradeVariantScore) -> tuple:
    recommendation_rank = {
        "paper_test": 0,
        "watch": 1,
        "sample_guard": 2,
        "reject": 3,
    }.get(variant.recommendation, 4)
    return (
        recommendation_rank,
        -_sort_number(variant.delta_expectancy_r),
        -_sort_number(variant.expectancy_r),
        -_sort_number(variant.profit_factor_r),
        variant.target1_r,
        variant.target2_r,
    )


def _sort_number(value: float | None) -> float:
    if value is None or math.isnan(value):
        return -1_000_000.0
    if math.isinf(value):
        return 1_000_000.0 if value > 0 else -1_000_000.0
    return value


def _expectancy_finding(scope: str, key: str, stats: Mapping[str, object]) -> dict | None:
    evaluated = _stat_int(stats, "evaluated")
    tradable = _stat_int(stats, "tradable")
    min_samples = _stat_int(stats, "min_samples")
    if evaluated <= 0:
        return None
    label = scope if not key else f"{scope} {key}"
    sample_text = f"n={tradable}" + (f"/{min_samples}" if min_samples else "")
    sample_ok = bool(stats.get("sample_ok"))
    expectancy_r = _stat_float(stats, "expectancy_r")
    profit_factor = _stat_float(stats, "profit_factor_r")
    avg_mfe = _stat_float(stats, "avg_mfe_r")
    avg_mae = _stat_float(stats, "avg_mae_r")
    if tradable <= 0:
        return _finding(
            scope,
            key,
            label,
            "quality_block",
            3,
            evaluated,
            tradable,
            min_samples,
            stats,
            "期待値評価に使えるサンプルがありません",
        )
    if sample_ok and expectancy_r is not None and expectancy_r <= 0:
        return _finding(
            scope,
            key,
            label,
            "block",
            0,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"期待R {_fmt_signed(expectancy_r, 'R')}が非正({sample_text})。新規判断は見送り優先",
        )
    if sample_ok and profit_factor is not None and profit_factor < WEAK_PROFIT_FACTOR:
        return _finding(
            scope,
            key,
            label,
            "weak",
            1,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"PF {_fmt_number(profit_factor)}が薄い({sample_text})。利確/損切り条件を再調整",
        )
    if sample_ok and avg_mfe is not None and avg_mae is not None and avg_mfe <= avg_mae:
        return _finding(
            scope,
            key,
            label,
            "weak",
            1,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"平均MFE {_fmt_signed(avg_mfe, 'R')} <= 平均MAE {_fmt_number(avg_mae, 'R')}({sample_text})",
        )
    if not sample_ok:
        return _finding(
            scope,
            key,
            label,
            "sample_guard",
            2,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"期待値サンプル不足({sample_text})。方向目線は参考扱い",
        )
    avg_quality = _stat_float(stats, "avg_path_quality")
    if avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        return _finding(
            scope,
            key,
            label,
            "quality_warn",
            3,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"平均経路品質{_fmt_pct(avg_quality)}が低く、TP/SL到達順の信頼度が不足",
        )
    return None


def _quality_finding(quality: Mapping[str, object]) -> dict | None:
    evaluated = _stat_int(quality, "evaluated")
    scored = _stat_int(quality, "scored")
    low_quality = _stat_int(quality, "low_quality")
    avg_quality = _stat_float(quality, "avg_path_quality")
    if evaluated <= 0:
        return None
    if scored <= 0:
        reason = "将来価格で採点できた判断がありません"
    elif low_quality / evaluated >= 0.5:
        reason = f"低品質サンプルが{low_quality}/{evaluated}件と多く、期待値判定の信頼度が不足"
    elif avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        reason = f"平均経路品質{_fmt_pct(avg_quality)}が低く、high/low付き経路の補強が必要"
    else:
        return None
    return _finding(
        "データ品質",
        "path",
        "データ品質 経路価格",
        "quality_warn",
        3,
        evaluated,
        scored,
        0,
        {},
        reason,
    )


def _candidate_from_finding(
    finding: Mapping[str, object], index: int
) -> TradeImprovementCandidate | None:
    severity = str(finding.get("severity", ""))
    scope = str(finding.get("scope", ""))
    key = str(finding.get("key", ""))
    label = str(finding.get("label", scope or "全体"))
    reason = str(finding.get("reason_ja", ""))
    candidate_id = _candidate_id(finding, index)
    if severity == "sample_guard":
        needed = max(0, _stat_int(finding, "min_samples") - _stat_int(finding, "tradable"))
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "low",
            "collect_samples",
            f"{label}のサンプル蓄積を優先",
            reason,
            {"execution_policy": "keep_guarded", "needed_samples": needed},
            f"有効サンプルをあと{needed}件以上追加し、期待R/PFを再評価",
            "サンプル不足の間はTP/SLや重みを最適化しない",
            dict(finding),
        )
    if severity in {"quality_warn", "quality_block"}:
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "medium",
            "improve_path_quality",
            f"{label}の経路データ品質を補強",
            reason,
            {"path_source": "use_high_low_ohlc_or_tick_bars"},
            "high/low付き経路でTP/SL到達順を再採点",
            "低品質経路ではTP/SL最適化を採用しない",
            dict(finding),
        )
    if severity == "block":
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "high",
            "expectancy_guard",
            f"{label}を見送り優先に戻す",
            reason,
            {"conviction_factor": EXPECTANCY_BLOCK_FACTOR, "execution_policy": "skip_new_entries"},
            "期待R>0、PF>=1.05、平均MFE>平均MAEを確認",
            "期待R非正セルはpaper検証で改善確認後に解除",
            dict(finding),
        )
    if severity == "weak":
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "medium",
            "tp_sl_entry_retest",
            f"{label}のTP/SL・エントリー条件を再検証",
            reason,
            {
                "tp1_r_candidates": _tp1_retest_candidates(finding),
                "entry_policy": "confirmation_only",
            },
            "候補TP1と確認型エントリーをpaper比較",
            "既存SL/TPを即時変更せずA/B検証してから昇格",
            dict(finding),
        )
    return None


def _variant_trial_sharpes(
    variants: Sequence[Mapping[str, object]],
) -> list[float]:
    return [
        value
        for variant in variants
        if (value := _stat_float(variant, "net_sharpe_per_period")) is not None
        and math.isfinite(value)
    ]


def _candidate_from_variant(
    variant: Mapping[str, object],
    baseline_overall: object,
    index: int,
    *,
    scope: str = "overall",
    key: str = "",
    trial_sharpes: Sequence[float] = (),
    trial_count: int = 0,
) -> TradeImprovementCandidate:
    baseline = baseline_overall if isinstance(baseline_overall, Mapping) else {}
    target1_r = _stat_float(variant, "target1_r")
    target2_r = _stat_float(variant, "target2_r")
    expectancy = _stat_float(variant, "net_expectancy_r")
    delta = _stat_float(variant, "delta_net_expectancy_r")
    profit_factor = _stat_float(variant, "net_profit_factor_r")
    variant_id = str(variant.get("variant_id") or f"variant-{index}")
    scope_label = _variant_scope_label(scope, key)
    title = (
        (f"{scope_label}の" if scope_label else "")
        + f"TP1={_fmt_number(target1_r, 'R')} / "
        + f"TP2={_fmt_number(target2_r, 'R')}をpaper検証"
    )
    rationale = (
        f"現行比で期待Rが{_fmt_signed(delta, 'R')}改善し、"
        f"候補期待Rは{_fmt_signed(expectancy, 'R')} / PF {_fmt_number(profit_factor)}"
    )
    return TradeImprovementCandidate(
        _variant_candidate_id(scope, key, variant_id),
        "TP/SL候補" if not scope_label else f"TP/SL候補 {scope_label}",
        key or variant_id,
        "medium",
        "tp_sl_variant_paper_test",
        title,
        rationale,
        {
            "target1_r": target1_r,
            "target2_r": target2_r,
            "execution_policy": "paper_ab_test",
            "baseline_net_expectancy_r": _stat_float(baseline, "net_expectancy_r"),
            "candidate_net_expectancy_r": expectancy,
            "delta_net_expectancy_r": delta,
            "label_version": str(variant.get("label_version") or ""),
            "label_provenance": str(variant.get("label_provenance") or ""),
            "cost_model_id": str(variant.get("cost_model_id") or ""),
            "net_label_samples": _stat_int(variant, "net_label_samples"),
            "net_label_coverage": _stat_float(variant, "net_label_coverage"),
            "scope": scope,
            "key": key,
            "min_expected_improvement_r": MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
            "trial_count": trial_count,
            "trial_sharpes": list(trial_sharpes),
        },
        "paper期間で期待Rが現行比+0.05R以上、PF>=1.05、最低サンプル数を維持することを確認",
        "分析用policyへの反映はready_for_review後も即時採用せず、人間承認と"
        "データ品質確認を必須にする",
        {"variant": dict(variant), "baseline_overall": dict(baseline), "scope": scope, "key": key},
    )


def _variant_has_canonical_net_evidence(variant: Mapping[str, object]) -> bool:
    return (
        variant.get("label_version") in PROMOTION_NET_LABEL_VERSIONS
        and variant.get("label_provenance") in PROMOTION_NET_LABEL_PROVENANCES
        and variant.get("cost_model_id") in KNOWN_EXECUTABLE_COST_MODEL_IDS
        and _stat_int(variant, "net_label_samples") >= MIN_EXPECTANCY_SAMPLES
        and math.isclose(
            _stat_float(variant, "net_label_coverage") or 0.0,
            1.0,
            abs_tol=1e-12,
        )
        and _stat_float(variant, "net_expectancy_r") is not None
        and _stat_float(variant, "delta_net_expectancy_r") is not None
    )


def _health_check_quality(quality: Mapping[str, object]) -> TradeOutcomeHealthCheck:
    evaluated = _stat_int(quality, "evaluated")
    scored = _stat_int(quality, "scored")
    low_quality = _stat_int(quality, "low_quality")
    avg_quality = _stat_float(quality, "avg_path_quality")
    details: dict[str, object] = {
        "evaluated": evaluated,
        "scored": scored,
        "low_quality": low_quality,
        "avg_path_quality": avg_quality,
    }
    if evaluated <= 0:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "経路品質の評価対象がありません", details
        )
    if scored <= 0:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_FAIL, "将来価格で採点できた判断がありません", details
        )
    if low_quality / evaluated >= 0.5 or (
        avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD
    ):
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "経路品質が低い状態です", details
        )
    return TradeOutcomeHealthCheck(
        "path_quality", STATUS_OK, "経路品質は監査可能な範囲です", details
    )


def _price_path_point(ts: datetime, entry: Mapping[str, object]) -> PricePathPoint | None:
    close = _float(entry.get("close"))
    if close is None:
        return None
    high = _float(entry.get("high"))
    low = _float(entry.get("low"))
    if high is None or low is None:
        return PricePathPoint(ts, close)
    scope = str(entry.get("ohlc_scope", "")).strip()
    if scope not in TRUSTED_POST_PREDICTION_OHLC_SCOPES:
        return PricePathPoint(ts, close, range_scope=scope, rejected_range=True)
    normalized_high = max(high, low, close)
    normalized_low = min(high, low, close)
    return PricePathPoint(ts, close, normalized_high, normalized_low, range_scope=scope)


def _target_policy_meta(entry: Mapping[str, object]) -> tuple[str | None, str, str]:
    raw = entry.get("target_policy")
    if not isinstance(raw, Mapping):
        return None, "", ""
    candidate_id = str(raw.get("candidate_id", "")).strip()
    if not candidate_id:
        return None, "", ""
    return candidate_id, str(raw.get("scope", "")), str(raw.get("key", ""))


def _future_path(
    series: Sequence[PricePathPoint],
    times: Sequence[datetime],
    ts: datetime,
    horizon_hours: float,
    tolerance_hours: float,
) -> list[PricePathPoint]:
    upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    open_hours = WeekendOpenHours(ts, upper)
    limit = horizon_hours + tolerance_hours
    future: list[PricePathPoint] = []
    for index in range(bisect_left(times, ts), len(series)):
        point = series[index]
        if point.ts <= ts:
            continue
        if point.ts > upper:
            break
        age = open_hours.age(point.ts)
        if age > limit:
            # age は point.ts に対して単調非減少(経過 - 休場重なりで、休場ぶんは
            # end の増加を超えて増えない)。上限を越えた時点で以降も必ず越えるため
            # 打ち切ってよい。upper は週末クローズ50時間ぶんの保守的padding込みで
            # 広く、ここで切らないと採点対象外の点まで毎回走査することになる。
            break
        if age > 0.0:
            future.append(point)
    return future


def _path_quality(
    ts: datetime,
    future: Sequence[PricePathPoint],
    horizon_hours: float,
    min_path_points: int,
) -> tuple[float, tuple[str, ...], str]:
    flags: list[str] = []
    points = len(future)
    coverage = _coverage(ts, future[-1].ts, horizon_hours) if future else 0.0
    point_ratio = min(1.0, points / POINTS_FOR_FULL_DENSITY)
    range_points = sum(1 for point in future if point.has_range)
    range_ratio = range_points / points if points else 0.0
    if any(point.rejected_range for point in future):
        flags.append("untrusted_forming_ohlc_ignored")
    if range_ratio <= 0:
        path_source = "close"
        flags.append("close_only_path")
        quality = min(CLOSE_ONLY_QUALITY_CAP, 0.65 * coverage + 0.35 * point_ratio)
    else:
        path_source = "ohlc" if range_ratio >= 0.8 else "mixed"
        if path_source == "mixed":
            flags.append("partial_high_low_path")
        cap = OHLC_QUALITY_CAP if path_source == "ohlc" else PARTIAL_OHLC_QUALITY_CAP
        quality = min(cap, 0.50 * coverage + 0.25 * point_ratio + 0.25 * range_ratio)
    if points < min_path_points:
        flags.append("insufficient_path_points")
    if coverage < MIN_PATH_COVERAGE:
        flags.append("short_path_coverage")
    if quality < MIN_PATH_QUALITY:
        flags.append("low_path_quality")
    return round(max(0.0, min(1.0, quality)), 4), tuple(flags), path_source


def _coverage(ts: datetime, last_ts: datetime, horizon_hours: float) -> float:
    if horizon_hours <= 0:
        return 0.0
    return max(0.0, min(1.0, open_hours_between(ts, last_ts) / horizon_hours))


def _touch(
    direction: str,
    point: PricePathPoint,
    stop: float | None,
    target1: float | None,
    target2: float | None,
) -> str:
    if stop is None or target1 is None:
        return "none"
    high = point.high if point.high is not None else point.close
    low = point.low if point.low is not None else point.close
    if direction == "long":
        stop_hit = low <= stop
        tp2_hit = target2 is not None and high >= target2
        tp1_hit = high >= target1
        if stop_hit and (tp1_hit or tp2_hit):
            return "ambiguous_sl_tp"
        if stop_hit:
            return "sl"
        if tp2_hit:
            return "tp2"
        if tp1_hit:
            return "tp1"
    else:
        stop_hit = high >= stop
        tp2_hit = target2 is not None and low <= target2
        tp1_hit = low <= target1
        if stop_hit and (tp1_hit or tp2_hit):
            return "ambiguous_sl_tp"
        if stop_hit:
            return "sl"
        if tp2_hit:
            return "tp2"
        if tp1_hit:
            return "tp1"
    return "none"


def _target_price(direction: str, entry: float, risk_distance: float, target_r: float) -> float:
    sign = 1.0 if direction == "long" else -1.0
    return entry + sign * risk_distance * target_r


def _target_r(
    direction: str,
    entry: float,
    risk_distance: float,
    target: float | None,
    *,
    default: float,
) -> float:
    if target is None or risk_distance <= 0:
        return default
    return max(0.0, _signed_move(direction, entry, target) / risk_distance)


def _favorable_move(direction: str, entry: float, point: PricePathPoint) -> float:
    if direction == "long":
        return (point.high if point.high is not None else point.close) - entry
    return entry - (point.low if point.low is not None else point.close)


def _adverse_move(direction: str, entry: float, point: PricePathPoint) -> float:
    if direction == "long":
        return entry - (point.low if point.low is not None else point.close)
    return (point.high if point.high is not None else point.close) - entry


def _realized_r(
    first_touch: str,
    terminal_r: float,
    target1_r: float,
    target2_r: float,
) -> float:
    if first_touch in {"sl", "ambiguous_sl_tp"}:
        return -1.0
    if first_touch == "tp2":
        return target2_r
    if first_touch == "tp1":
        return target1_r
    return terminal_r


def _signed_move(direction: str, entry: float, price: float) -> float:
    return price - entry if direction == "long" else entry - price


def _confidence_label(outcome: TradeOutcome) -> str:
    return _confidence_label_from_int(outcome.conviction)


def _confidence_label_from_int(conviction: int) -> str:
    for low, high in CONFIDENCE_BINS:
        if low <= conviction < high:
            return f"{low}-{high - 1}"
    return "unknown"


def _prospective_approval_gate(
    record: Mapping[str, object],
    *,
    now: datetime,
) -> tuple[bool, str]:
    if _stat_int(record, "prospective_schema") != PROSPECTIVE_EVIDENCE_SCHEMA:
        return False, "前向き検証schemaが不一致のため承認できません"
    created_at = _parse_aware_iso(record.get("candidate_created_at"))
    training_end = _parse_aware_iso(record.get("training_data_end"))
    start = _parse_aware_iso(record.get("prospective_start"))
    end = _parse_aware_iso(record.get("prospective_end"))
    if (
        created_at is None
        or training_end is None
        or start is None
        or end is None
        or created_at > start
        or training_end > start
        or end <= start
    ):
        return False, "前向き検証期間の来歴が不正なため承認できません"
    if now > end:
        return False, "前向き検証証拠が期限切れのため承認できません"
    last_evaluated = _parse_aware_iso(record.get("prospective_last_evaluated_at"))
    if last_evaluated is None or last_evaluated > now:
        return False, "前向き検証の評価時刻が不正なため承認できません"
    if record.get("candidate_definition_hash") != _candidate_definition_hash(record):
        return False, "候補定義が登録後に変更されたため承認できません"
    for key in (
        "candidate_definition_hash",
        "dataset_hash",
        "prospective_dataset_hash",
        "prospective_effective_dataset_hash",
    ):
        value = str(record.get(key) or "")
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            return False, f"{key}が不正なため承認できません"
    if not str(record.get("lockbox_id") or "").startswith("prospective-"):
        return False, "lockbox IDが不正なため承認できません"
    metrics = record.get("prospective_metrics")
    if not isinstance(metrics, Mapping):
        return False, "前向き検証指標がないため承認できません"
    if _stat_int(metrics, "schema") != PROSPECTIVE_EVIDENCE_SCHEMA:
        return False, "前向き検証指標schemaが不一致のため承認できません"
    effective_samples = _stat_int(metrics, "effective_samples")
    outcome_keys = record.get("prospective_outcome_keys")
    if (
        not isinstance(outcome_keys, list)
        or len(outcome_keys) != effective_samples
        or effective_samples < PROSPECTIVE_MIN_EFFECTIVE_SAMPLES
    ):
        return False, "前向き検証のeffective sampleが不足しています"
    coverage = _stat_float(metrics, "net_label_coverage")
    if coverage is None or coverage < PROSPECTIVE_MIN_NET_COVERAGE:
        return False, "前向き検証のcanonical net coverageが不足しています"
    lcb = _stat_float(metrics, "net_expectancy_lcb")
    if lcb is None or lcb <= 0:
        return False, "前向き検証のnet期待R片側信頼下限が非正です"
    dsr = _stat_float(metrics, "dsr")
    if dsr is None or dsr < PROSPECTIVE_MIN_DSR:
        return False, "前向き検証のDSRが基準未達です"
    max_drawdown = _stat_float(metrics, "max_drawdown_r")
    if max_drawdown is None or max_drawdown < -PROSPECTIVE_MAX_DRAWDOWN_R:
        return False, "前向き検証の最大ドローダウンが基準超過です"
    stressed = _stat_float(metrics, "cost_stressed_expectancy_r")
    if stressed is None or stressed <= 0:
        return False, "前向き検証のcost stress後期待Rが非正です"
    concentration = _stat_float(metrics, "max_symbol_concentration")
    proposed = record.get("proposed_change")
    proposed_change = proposed if isinstance(proposed, Mapping) else {}
    scope = str(proposed_change.get("scope") or record.get("scope") or "")
    if concentration is None or (
        scope not in {"by_symbol", "by_symbol_direction", "通貨ペア", "通貨ペア×方向"}
        and concentration > PROSPECTIVE_MAX_CONCENTRATION
    ):
        return False, "前向き検証の集中度が基準超過です"
    if metrics.get("contract_consistent") is not True:
        return False, "前向き検証のlabel/cost契約が混在しています"
    versions = metrics.get("label_versions")
    provenances = metrics.get("label_provenances")
    cost_models = metrics.get("cost_model_ids")
    if (
        versions != sorted(PROMOTION_NET_LABEL_VERSIONS)
        or provenances != sorted(PROMOTION_NET_LABEL_PROVENANCES)
        or not isinstance(cost_models, list)
        or len(cost_models) != 1
        or cost_models[0] not in KNOWN_EXECUTABLE_COST_MODEL_IDS
    ):
        return False, "前向き検証のcanonical label/cost schemaが不一致です"
    blockers = metrics.get("blocking_reasons")
    if not isinstance(blockers, list) or blockers:
        return False, "前向き検証に未解決の昇格阻害理由があります"
    return True, ""


def _approval_improvement_gate(record: Mapping[str, object]) -> tuple[bool, str]:
    if record.get("action_type") != "tp_sl_variant_paper_test":
        return True, ""
    proposed = record.get("proposed_change")
    if not isinstance(proposed, Mapping):
        return False, "TP/SL候補の改善根拠がないため昇格できません"
    label_version = str(proposed.get("label_version") or "")
    label_provenance = str(proposed.get("label_provenance") or "")
    cost_model_id = str(proposed.get("cost_model_id") or "")
    net_samples = _stat_int(proposed, "net_label_samples")
    net_coverage = _stat_float(proposed, "net_label_coverage")
    if (
        label_version not in PROMOTION_NET_LABEL_VERSIONS
        or label_provenance not in PROMOTION_NET_LABEL_PROVENANCES
        or cost_model_id not in KNOWN_EXECUTABLE_COST_MODEL_IDS
    ):
        return False, "TP/SL候補のcanonical純R来歴が不明なため昇格できません"
    if net_samples < MIN_EXPECTANCY_SAMPLES:
        return (
            False,
            f"TP/SL候補のcanonical純Rサンプル不足({net_samples}/{MIN_EXPECTANCY_SAMPLES}件)",
        )
    if net_coverage is None or not math.isclose(net_coverage, 1.0, abs_tol=1e-12):
        return False, "TP/SL候補のcanonical純R coverage不足のため昇格できません"
    delta = _stat_float(proposed, "delta_net_expectancy_r")
    candidate = _stat_float(proposed, "candidate_net_expectancy_r")
    min_delta = _stat_float(proposed, "min_expected_improvement_r")
    if min_delta is None:
        min_delta = MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R
    if delta is None:
        return False, "TP/SL候補の純R改善幅が未記録のため昇格できません"
    if delta < min_delta:
        return (
            False,
            f"TP/SL候補の純R改善幅が不足しています({delta:+.2f}R<{min_delta:+.2f}R)",
        )
    if candidate is None:
        return False, "TP/SL候補の候補純Rが未記録のため昇格できません"
    if candidate <= 0:
        return False, f"TP/SL候補の候補純Rが非正です({candidate:+.2f}R)"
    return True, ""


def _candidate_id(finding: Mapping[str, object], index: int) -> str:
    raw = f"{finding.get('scope', 'all')}:{finding.get('key', '') or 'overall'}:{finding.get('severity', 'unknown')}".lower()
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"trade-improvement-{normalized or f'candidate-{index}'}"


def _variant_candidate_id(scope: str, key: str, variant_id: str) -> str:
    raw = f"tp-sl-variant:{scope}:{key or 'overall'}:{variant_id}".lower()
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"trade-improvement-{normalized or 'tp-sl-variant'}"


def _variant_scope_label(scope: str, key: str) -> str:
    if scope == "overall" or not key:
        return ""
    labels = {
        "by_symbol_direction": "通貨ペア×方向",
        "by_symbol": "通貨ペア",
        "by_direction": "方向",
        "by_confidence": "確信度",
    }
    return f"{labels.get(scope, scope)} {key}"


def _registry_records(registry: Mapping[str, object] | None) -> dict[str, dict]:
    if not isinstance(registry, Mapping):
        return {}
    raw = registry.get("candidates")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _registry_events(registry: Mapping[str, object] | None) -> list[dict]:
    if not isinstance(registry, Mapping):
        return []
    raw = registry.get("events")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _append_registry_event(
    events: list[dict],
    *,
    generated_at: str,
    candidate_id: str,
    event_type: str,
    from_stage: str = "",
    to_stage: str = "",
    actor: str = "",
    note: str = "",
    details: Mapping[str, object] | None = None,
) -> None:
    event: dict[str, object] = {
        "ts": generated_at,
        "candidate_id": candidate_id,
        "event_type": event_type,
        "from_stage": from_stage,
        "to_stage": to_stage,
    }
    if actor:
        event["actor"] = actor
    if note:
        event["note"] = note
    if details:
        event["details"] = dict(details)
    events.append(event)


def _bounded_registry_events(
    events: Sequence[Mapping[str, object]], *, limit: int = 200
) -> list[dict]:
    return [dict(event) for event in list(events)[-max(0, limit) :]]


def _registry_payload(
    records: Mapping[str, Mapping[str, object]],
    generated_at: str,
    events: Sequence[Mapping[str, object]] | None = None,
) -> dict:
    payload = {
        "schema": IMPROVEMENT_REGISTRY_SCHEMA,
        "generated_at": generated_at,
        "active_count": sum(1 for record in records.values() if record.get("status") == "active"),
        "ready_for_review_count": sum(
            1 for record in records.values() if record.get("stage") == "ready_for_review"
        ),
        "approved_count": sum(
            1 for record in records.values() if record.get("stage") == "approved"
        ),
        "rejected_count": sum(
            1 for record in records.values() if record.get("stage") == "rejected"
        ),
        "auto_paused_count": sum(
            1 for record in records.values() if record.get("stage") == "auto_paused"
        ),
        "resolved_count": sum(
            1 for record in records.values() if record.get("status") == "resolved"
        ),
        "events": [dict(event) for event in events] if events is not None else [],
        "candidates": {str(key): dict(value) for key, value in records.items()},
    }
    contracts = {
        str(record.get("data_contract"))
        for record in records.values()
        if record.get("data_contract")
    }
    if len(contracts) == 1:
        payload["data_contract"] = contracts.pop()
    return payload


def _monitor_records(records: Sequence[Mapping[str, object]], *, limit: int = 20) -> list[dict]:
    sorted_records = sorted(
        records,
        key=lambda record: (
            -_record_prospective_effective_samples(record),
            str(record.get("priority", "")),
            str(record.get("candidate_id", "")),
        ),
    )
    output: list[dict] = []
    for record in sorted_records[: max(0, limit)]:
        proposed_change = record.get("proposed_change")
        prospective_metrics = record.get("prospective_metrics")
        output.append(
            {
                "candidate_id": str(record.get("candidate_id", "")),
                "stage": str(record.get("stage", "")),
                "priority": str(record.get("priority", "")),
                "action_type": str(record.get("action_type", "")),
                "title_ja": str(record.get("title_ja", "")),
                "scope": str(record.get("scope", "")),
                "key": str(record.get("key", "")),
                "seen_count": _stat_int(record, "seen_count"),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
                "candidate_created_at": record.get("candidate_created_at"),
                "candidate_definition_hash": record.get("candidate_definition_hash"),
                "training_data_end": record.get("training_data_end"),
                "prospective_start": record.get("prospective_start"),
                "prospective_end": record.get("prospective_end"),
                "lockbox_id": record.get("lockbox_id"),
                "dataset_hash": record.get("dataset_hash"),
                "prospective_dataset_hash": record.get("prospective_dataset_hash"),
                "prospective_effective_dataset_hash": record.get(
                    "prospective_effective_dataset_hash"
                ),
                "prospective_metrics": (
                    {str(key): value for key, value in prospective_metrics.items()}
                    if isinstance(prospective_metrics, Mapping)
                    else {}
                ),
                "approved_at": record.get("approved_at"),
                "approved_by": record.get("approved_by"),
                "auto_paused_at": record.get("auto_paused_at"),
                "auto_pause_reason_ja": record.get("auto_pause_reason_ja"),
                "resumed_at": record.get("resumed_at"),
                "resumed_by": record.get("resumed_by"),
                "rejected_at": record.get("rejected_at"),
                "rejected_by": record.get("rejected_by"),
                "proposed_change": (
                    dict(proposed_change) if isinstance(proposed_change, Mapping) else {}
                ),
            }
        )
    return output


def _record_prospective_effective_samples(record: Mapping[str, object]) -> int:
    metrics = record.get("prospective_metrics")
    return _stat_int(metrics, "effective_samples") if isinstance(metrics, Mapping) else 0


def _policy_specificity(policy: ApprovedTargetPolicy) -> int:
    return {
        "by_symbol_direction": 5,
        "by_symbol": 4,
        "by_direction": 3,
        "by_confidence": 2,
        "overall": 1,
    }.get(policy.scope, 0)


def _policy_pause_reason(stats: Mapping[str, object]) -> str:
    expectancy = _stat_float(stats, "net_expectancy_r")
    profit_factor = _stat_float(stats, "net_profit_factor_r")
    avg_quality = _stat_float(stats, "avg_path_quality")
    tradable = _stat_int(stats, "net_label_samples")
    if expectancy is not None and expectancy <= 0:
        return f"承認済みTP/SLの適用後期待Rが{_fmt_signed(expectancy, 'R')}に悪化(n={tradable})"
    if profit_factor is not None and profit_factor < 1.0:
        return f"承認済みTP/SLの適用後PFが{_fmt_number(profit_factor)}に悪化(n={tradable})"
    if avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        return (
            f"承認済みTP/SLの適用後サンプルの経路品質が{_fmt_pct(avg_quality)}と低い(n={tradable})"
        )
    return ""


def _target_policy_matches(
    policy: ApprovedTargetPolicy,
    symbol: str,
    direction: str,
    conviction: int,
) -> bool:
    symbol = symbol.upper()
    if direction not in {"long", "short"}:
        return False
    if policy.scope == "overall":
        return True
    if policy.scope == "by_symbol_direction":
        return policy.key == f"{symbol}:{direction}"
    if policy.scope == "by_symbol":
        return policy.key == symbol
    if policy.scope == "by_direction":
        return policy.key == direction
    if policy.scope == "by_confidence":
        return policy.key == _confidence_label_from_int(conviction)
    return False


def _finding(
    scope: str,
    key: str,
    label: str,
    severity: str,
    rank: int,
    evaluated: int,
    tradable: int,
    min_samples: int,
    stats: Mapping[str, object],
    reason_ja: str,
) -> dict:
    return {
        "scope": scope,
        "key": key,
        "label": label,
        "severity": severity,
        "rank": rank,
        "evaluated": evaluated,
        "tradable": tradable,
        "min_samples": min_samples,
        "sample_ok": bool(stats.get("sample_ok")),
        "expectancy_r": _stat_float(stats, "expectancy_r"),
        "profit_factor_r": _stat_float(stats, "profit_factor_r"),
        "avg_mfe_r": _stat_float(stats, "avg_mfe_r"),
        "avg_mae_r": _stat_float(stats, "avg_mae_r"),
        "avg_path_quality": _stat_float(stats, "avg_path_quality"),
        "reason_ja": reason_ja,
    }


def _tp1_retest_candidates(finding: Mapping[str, object]) -> list[float]:
    avg_mfe = _stat_float(finding, "avg_mfe_r")
    if avg_mfe is None or avg_mfe <= 0:
        return [0.8, 1.0]
    primary = max(0.5, min(1.2, round(avg_mfe * 0.8, 2)))
    return list(dict.fromkeys([primary, 1.0, 0.75 if primary < 0.9 else primary]))[:3]


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _int(value: object) -> int:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _sample_sharpe(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance <= 1e-24:
        return None
    return mean / math.sqrt(variance)


def _single_contract_value(
    records: Sequence[Mapping[str, object]],
    key: str,
) -> str:
    values = {str(record.get(key) or "") for record in records}
    return next(iter(values)) if len(values) == 1 else ""


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return value
    return round(value, digits)


def _stat_int(mapping: Mapping[str, object], key: str) -> int:
    return _int(mapping.get(key))


def _stat_float(mapping: Mapping[str, object], key: str) -> float | None:
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _fmt_number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return f"∞{suffix}"
    return f"{value:.2f}{suffix}"


def _fmt_signed(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return f"∞{suffix}"
    return f"{value:+.2f}{suffix}"


def _format_details(details: Mapping[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in details.items() if value is not None)
