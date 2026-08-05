"""Synchronized liquidation valuation and shadow V2 risk evaluation.

This module consumes only persisted offline-simulation rows.  It does not
collect prices, contact a broker, create orders, or mutate any external
account.  The risk result is shadow decision support until prospective parity
and independent review are complete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, cast
from zoneinfo import ZoneInfo

from fx_intel.virtual_portfolio import VirtualPortfolioStore
from fx_intel.virtual_portfolio_v2 import (
    PortfolioValuationRequest,
    PositionLiquidationValuation,
)

VALUATION_CONSUMER_CONTRACT = "virtual-portfolio-valuation-consumer-v1"
RISK_GATE_CONTRACT = "virtual-portfolio-risk-gate-v2-shadow"
TOKYO = ZoneInfo("Asia/Tokyo")
_CONVERSION_RECORD_RE = re.compile(r"^(dukascopy:USDJPY:(?P<event_time>.+):[^:;]+);")


class VirtualValuationError(RuntimeError):
    """A synchronized liquidation valuation input is invalid."""


@dataclass(frozen=True)
class LiquidationReservePolicy:
    """Pre-registered modeled exit reserves and valuation quality limits."""

    policy_id: str = "virtual-liquidation-reserve-policy-v1"
    slippage_r_bps: int = 200
    commission_r_bps: int = 100
    financing_r_bps_per_started_day: int = 100
    conversion_r_bps: int = 50
    low_cost_multiplier_bps: int = 20_000
    high_cost_multiplier_bps: int = 7_500
    gap_buffer_r_bps: int = 2_500
    max_source_age_seconds: int = 300
    max_cross_position_skew_seconds: int = 60
    max_portfolio_heat_bps: int = 200

    def validate(self) -> None:
        if not self.policy_id.strip():
            raise VirtualValuationError("liquidation reserve policy_id is required")
        for name, value in asdict(self).items():
            if name == "policy_id":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VirtualValuationError(f"{name} must be a non-negative integer")
        if self.low_cost_multiplier_bps < 10_000:
            raise VirtualValuationError("low valuation must reserve at least base costs")
        if self.high_cost_multiplier_bps > 10_000:
            raise VirtualValuationError("high valuation cannot reserve more than base")
        if self.max_source_age_seconds <= 0:
            raise VirtualValuationError("max_source_age_seconds must be positive")
        if self.max_cross_position_skew_seconds < 0:
            raise VirtualValuationError("max_cross_position_skew_seconds cannot be negative")
        if self.max_portfolio_heat_bps <= 0 or self.max_portfolio_heat_bps > 10_000:
            raise VirtualValuationError("max_portfolio_heat_bps must be within 1..10000")

    @property
    def sha256(self) -> str:
        self.validate()
        return _sha256_json(asdict(self))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VirtualValuationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _to_ns(value: datetime) -> int:
    return int(_utc(value, "timestamp").timestamp() * 1_000_000_000)


def _ceil_nonnegative_seconds(value: timedelta, name: str) -> int:
    """Conservatively convert a non-negative duration to whole seconds."""

    microseconds = value.days * 86_400 * 1_000_000 + value.seconds * 1_000_000 + value.microseconds
    if microseconds < 0:
        raise VirtualValuationError(f"{name} cannot be negative")
    return (microseconds + 999_999) // 1_000_000


def _modeled_reserve(
    *,
    planned_risk_jpy: int,
    symbol: str,
    open_time: datetime,
    cutoff: datetime,
    policy: LiquidationReservePolicy,
) -> dict[str, object]:
    if planned_risk_jpy <= 0:
        raise VirtualValuationError("planned risk must be positive")
    if cutoff < open_time:
        raise VirtualValuationError("valuation cutoff cannot precede trade open")

    def amount(bps: int) -> int:
        return math.ceil(planned_risk_jpy * bps / 10_000)

    started_days = max(1, math.ceil((cutoff - open_time).total_seconds() / 86_400))
    components = {
        "slippage_cost_jpy": amount(policy.slippage_r_bps),
        "commission_jpy": amount(policy.commission_r_bps),
        "financing_jpy": amount(policy.financing_r_bps_per_started_day * started_days),
        "conversion_cost_jpy": (0 if symbol[3:] == "JPY" else amount(policy.conversion_r_bps)),
    }
    base = sum(components.values())
    low = math.ceil(base * policy.low_cost_multiplier_bps / 10_000)
    high = math.ceil(base * policy.high_cost_multiplier_bps / 10_000)
    return {
        "evidence_kind": "modeled",
        "started_financing_days": started_days,
        "components_base_jpy": components,
        "reserve_base_jpy": base,
        "reserve_low_jpy": low,
        "reserve_high_jpy": high,
        "gap_buffer_jpy": amount(policy.gap_buffer_r_bps),
    }


def _conversion_evidence(
    *,
    symbol: str,
    conversion_source: str,
) -> tuple[str | None, datetime | None]:
    if symbol[3:] == "JPY":
        return None, None
    matched = _CONVERSION_RECORD_RE.match(conversion_source)
    if matched is None:
        return None, None
    record_id = matched.group(1)
    try:
        event_time = datetime.fromisoformat(matched.group("event_time"))
    except ValueError:
        return None, None
    return record_id, _utc(event_time, "conversion source event_time")


def append_synchronized_valuation_from_marks(
    store: VirtualPortfolioStore,
    *,
    valuation_cutoff: datetime,
    observed_at: datetime,
    reserve_policy: LiquidationReservePolicy | None = None,
) -> dict[str, object]:
    """Append one all-position valuation from immutable persisted marks."""

    if store.writer_id is None:
        raise VirtualValuationError("valuation consumer requires the portfolio writer")
    policy = reserve_policy or LiquidationReservePolicy()
    policy.validate()
    cutoff = _utc(valuation_cutoff, "valuation cutoff")
    observed = _utc(observed_at, "valuation observed_at")
    if cutoff > observed:
        raise VirtualValuationError("valuation cutoff cannot follow observed_at")

    open_rows = store.connection.execute(
        """
        SELECT o.*
        FROM simulated_trade_opens AS o
        JOIN simulated_trade_open_observations AS ok ON ok.trade_id = o.trade_id
        LEFT JOIN simulated_trade_close_observations AS ck
          ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
        LEFT JOIN simulated_trade_closes AS c
          ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
         AND ck.trade_id IS NOT NULL
        WHERE o.open_time_ns <= ?
          AND ok.open_known_at_ns <= ?
          AND c.trade_id IS NULL
        ORDER BY o.trade_id
        """,
        (
            _to_ns(observed),
            _to_ns(cutoff),
            _to_ns(cutoff),
            _to_ns(observed),
        ),
    ).fetchall()
    positions: list[PositionLiquidationValuation] = []
    warnings: list[str] = []
    mark_identities: list[dict[str, object]] = []
    planned_stop_loss_jpy = 0
    gap_buffer_jpy = 0
    low_exit_reserve_jpy = 0
    source_times: list[datetime] = []

    for opened in open_rows:
        trade_id = str(opened["trade_id"])
        mark = store.connection.execute(
            """
            SELECT *
            FROM simulated_position_marks
            WHERE trade_id = ? AND mark_time_ns <= ? AND observed_at_ns <= ?
            ORDER BY mark_time_ns DESC, mark_id DESC
            LIMIT 1
            """,
            (trade_id, _to_ns(cutoff), _to_ns(observed)),
        ).fetchone()
        if mark is None:
            warnings.append(f"missing_position_mark:{trade_id}")
            continue
        mark_time = _from_ns(int(mark["mark_time_ns"]))
        mark_observed = _from_ns(int(mark["observed_at_ns"]))
        try:
            quote_payload = json.loads(str(mark["quote_json"]))
        except (json.JSONDecodeError, TypeError):
            warnings.append(f"invalid_position_mark_quote_json:{trade_id}")
            continue
        if not isinstance(quote_payload, dict):
            warnings.append(f"invalid_position_mark_quote_type:{trade_id}")
            continue
        try:
            price_event_time = _utc(
                datetime.fromisoformat(str(quote_payload["event_time"])),
                "position price event_time",
            )
        except (KeyError, ValueError, VirtualValuationError):
            warnings.append(f"invalid_price_event_time:{trade_id}")
            continue
        if price_event_time > mark_time:
            warnings.append(f"price_event_after_mark_time:{trade_id}")
            continue
        if cutoff < price_event_time:
            warnings.append(f"future_position_mark:{trade_id}")
            continue
        source_age = _ceil_nonnegative_seconds(
            cutoff - price_event_time,
            "position source age",
        )
        if source_age > policy.max_source_age_seconds:
            warnings.append(f"stale_position_mark:{trade_id}:{source_age}s")
            continue

        symbol = str(opened["symbol"])
        conversion_record_id, conversion_time = _conversion_evidence(
            symbol=symbol,
            conversion_source=str(mark["quote_conversion_source"]),
        )
        price_record_id = str(quote_payload.get("source_record_id") or "").strip()
        if not price_record_id:
            warnings.append(f"missing_price_record_id:{trade_id}")
            continue
        quote_ids = [price_record_id]
        bundle_source_time = price_event_time
        if symbol[3:] != "JPY":
            if conversion_record_id is None or conversion_time is None:
                warnings.append(f"missing_conversion_event_evidence:{trade_id}")
                continue
            if conversion_time > cutoff:
                warnings.append(f"future_conversion_event:{trade_id}")
                continue
            quote_ids.append(conversion_record_id)
            bundle_source_time = min(bundle_source_time, conversion_time)
            source_age = max(
                source_age,
                _ceil_nonnegative_seconds(
                    cutoff - conversion_time,
                    "conversion source age",
                ),
            )
        if source_age > policy.max_source_age_seconds:
            warnings.append(f"stale_source_bundle:{trade_id}:{source_age}s")
            continue

        planned_risk = int(opened["planned_risk_jpy"])
        reserve = _modeled_reserve(
            planned_risk_jpy=planned_risk,
            symbol=symbol,
            open_time=_from_ns(int(opened["open_time_ns"])),
            cutoff=cutoff,
            policy=policy,
        )
        planned_stop_loss_jpy += planned_risk
        gap_buffer_jpy += cast(int, reserve["gap_buffer_jpy"])
        low_exit_reserve_jpy += cast(int, reserve["reserve_low_jpy"])
        source_times.append(bundle_source_time)
        mark_identities.append(
            {
                "trade_id": trade_id,
                "mark_id": str(mark["mark_id"]),
                "mark_sha256": str(mark["record_sha256"]),
                "source_event_time": bundle_source_time.isoformat(),
            }
        )
        positions.append(
            PositionLiquidationValuation(
                trade_id=trade_id,
                valuation_cutoff=cutoff,
                observed_at=observed,
                source_event_time=bundle_source_time,
                mid_unrealized_pnl_jpy=int(mark["gross_unrealized_pnl_jpy"]),
                executable_unrealized_pnl_jpy=int(mark["executable_unrealized_pnl_jpy"]),
                exit_cost_reserve_base_jpy=cast(int, reserve["reserve_base_jpy"]),
                exit_cost_reserve_low_jpy=cast(int, reserve["reserve_low_jpy"]),
                exit_cost_reserve_high_jpy=cast(int, reserve["reserve_high_jpy"]),
                evidence_kind="modeled",
                quote_ids=tuple(quote_ids),
                valuation_model_sha256=policy.sha256,
                max_source_age_seconds=source_age,
                detail={
                    "mark_id": str(mark["mark_id"]),
                    "mark_observed_at": mark_observed.isoformat(),
                    "quote_conversion_source": str(mark["quote_conversion_source"]),
                    "reserve": reserve,
                },
            )
        )

    skew = (
        _ceil_nonnegative_seconds(
            max(source_times) - min(source_times),
            "cross-position source skew",
        )
        if source_times
        else 0
    )
    if skew > policy.max_cross_position_skew_seconds:
        warnings.append(f"cross_position_skew_exceeded:{skew}s")
    quality_complete = (
        len(positions) == len(open_rows) and skew <= policy.max_cross_position_skew_seconds
    )
    portfolio_heat_jpy = planned_stop_loss_jpy + gap_buffer_jpy + low_exit_reserve_jpy
    identity = {
        "contract": VALUATION_CONSUMER_CONTRACT,
        "valuation_cutoff": cutoff.isoformat(),
        "observed_at": observed.isoformat(),
        "policy_sha256": policy.sha256,
        "mark_identities": mark_identities,
        "warnings": sorted(warnings),
    }
    valuation_id = f"valuation-{_sha256_json(identity)[:24]}"
    existing = store.connection.execute(
        """
        SELECT recorded_at_ns
        FROM portfolio_liquidation_valuations
        WHERE valuation_id = ?
        """,
        (valuation_id,),
    ).fetchone()
    recorded_at = (
        _from_ns(int(existing["recorded_at_ns"])) if existing is not None else datetime.now(UTC)
    )
    if observed > recorded_at:
        raise VirtualValuationError("valuation observed_at cannot be in the future")
    request = PortfolioValuationRequest(
        valuation_id=valuation_id,
        valuation_cutoff=cutoff,
        observed_at=observed,
        recorded_at=recorded_at,
        positions=tuple(positions),
        policy_sha256=store.policy(
            as_of=cutoff,
            known_at=observed,
        ).policy_sha256,
        valuation_model_sha256=policy.sha256,
        writer_id=store.writer_id,
        detail={
            "consumer_contract": VALUATION_CONSUMER_CONTRACT,
            "reserve_policy": asdict(policy),
            "reserve_policy_sha256": policy.sha256,
            "warnings": sorted(warnings),
            "planned_stop_loss_jpy": planned_stop_loss_jpy,
            "gap_buffer_jpy": gap_buffer_jpy,
            "low_exit_reserve_jpy": low_exit_reserve_jpy,
            "portfolio_heat_jpy": portfolio_heat_jpy,
            "quality_complete": quality_complete,
        },
        quality_complete=quality_complete,
    )
    result = store.record_synchronized_liquidation_valuation(request)
    return {
        "contract": VALUATION_CONSUMER_CONTRACT,
        "valuation_id": valuation_id,
        "quality_complete": quality_complete,
        "warnings": sorted(warnings),
        "expected_position_count": len(open_rows),
        "valued_position_count": len(positions),
        "cross_position_skew_seconds": skew,
        "portfolio_heat_jpy": portfolio_heat_jpy,
        "valuation": result,
    }


def _period_start(now: datetime, period: str) -> datetime:
    local = _utc(now, "risk as_of").astimezone(TOKYO)
    if period == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise VirtualValuationError(f"unknown risk period: {period}")
    return start.astimezone(UTC)


def _select_period_baseline(
    store: VirtualPortfolioStore,
    *,
    period: str,
    period_start: datetime,
    moment: datetime,
    known_at: datetime,
    portfolio_created: datetime,
    current_cutoff: datetime,
    current_liquidation_hwm_low_jpy: int | None,
    initial_capital_jpy: int,
    valuation_model_sha256: str,
    policy: LiquidationReservePolicy,
) -> tuple[Any | None, dict[str, object], list[str]]:
    """Select one PIT-visible, boundary-fresh, model-continuous baseline."""

    start = _utc(period_start, "period start")
    created = _utc(portfolio_created, "portfolio created")
    as_of = _utc(moment, "baseline as_of")
    knowledge = _utc(known_at, "baseline known_at")
    current = _utc(current_cutoff, "current valuation cutoff")
    inception_period = created >= start
    if inception_period:
        selection_mode = "portfolio_inception_equity"
        reference = created
        baseline_equity = max(
            initial_capital_jpy,
            current_liquidation_hwm_low_jpy or initial_capital_jpy,
        )
        baseline: Any | None = {
            "liquidation_equity_low_jpy": baseline_equity,
        }
    else:
        selection_mode = "period_boundary"
        reference = start
        baseline = store.connection.execute(
            """
            SELECT *
            FROM portfolio_liquidation_valuations
            WHERE valuation_status = 'complete'
              AND valuation_cutoff_ns <= ?
              AND observed_at_ns <= ?
              AND recorded_at_ns <= ?
            ORDER BY valuation_cutoff_ns DESC, valuation_id DESC
            LIMIT 1
            """,
            (_to_ns(start), _to_ns(start), _to_ns(knowledge)),
        ).fetchone()

    evidence: dict[str, object] = {
        "selection_mode": selection_mode,
        "reference_time": reference.isoformat(),
        "knowledge_as_of": knowledge.isoformat(),
        "source_kind": (
            "initial_capital_and_liquidation_hwm"
            if inception_period
            else "synchronized_liquidation_valuation"
        ),
        "valuation_id": None,
        "valuation_cutoff": None,
        "observed_at": None,
        "boundary_distance_seconds": None,
        "observation_distance_seconds": None,
        "effective_source_age_seconds": None,
        "valuation_model_sha256": None,
        "model_continuity": False,
        "status": "unavailable",
    }
    baseline_reasons: list[str] = []
    if baseline is None:
        baseline_reasons.append(f"{period}_liquidation_baseline_unavailable")
        return None, evidence, baseline_reasons

    if inception_period:
        model_discontinuity_count = int(
            store.connection.execute(
                """
                SELECT COUNT(*)
                FROM portfolio_liquidation_valuations
                WHERE valuation_status = 'complete'
                  AND valuation_cutoff_ns >= ?
                  AND valuation_cutoff_ns <= ?
                  AND observed_at_ns <= ?
                  AND recorded_at_ns <= ?
                  AND valuation_model_sha256 != ?
                """,
                (
                    _to_ns(created),
                    _to_ns(current),
                    _to_ns(as_of),
                    _to_ns(knowledge),
                    valuation_model_sha256,
                ),
            ).fetchone()[0]
        )
        evidence.update(
            {
                "boundary_distance_seconds": 0,
                "observation_distance_seconds": 0,
                "recorded_source_age_seconds": 0,
                "effective_source_age_seconds": 0,
                "cross_position_skew_seconds": 0,
                "valuation_model_sha256": valuation_model_sha256,
                "model_continuity": model_discontinuity_count == 0,
                "baseline_equity_low_jpy": baseline_equity,
                "initial_capital_jpy": initial_capital_jpy,
            }
        )
        if model_discontinuity_count:
            baseline_reasons.append(f"{period}_liquidation_baseline_model_discontinuity")
        evidence["status"] = "eligible" if not baseline_reasons else "rejected"
        return baseline, evidence, baseline_reasons

    cutoff = _from_ns(int(baseline["valuation_cutoff_ns"]))
    observed = _from_ns(int(baseline["observed_at_ns"]))
    recorded_source_age = baseline["max_source_age_seconds"]
    skew = baseline["cross_position_skew_seconds"]
    boundary_distance = _ceil_nonnegative_seconds(
        (cutoff - reference) if inception_period else (reference - cutoff),
        f"{period} baseline boundary distance",
    )
    observation_distance = _ceil_nonnegative_seconds(
        (observed - reference) if inception_period else (reference - observed),
        f"{period} baseline observation distance",
    )
    effective_source_age = (
        int(recorded_source_age) + boundary_distance if recorded_source_age is not None else None
    )
    baseline_model = str(baseline["valuation_model_sha256"])
    model_discontinuity_count = int(
        store.connection.execute(
            """
            SELECT COUNT(*)
            FROM portfolio_liquidation_valuations
            WHERE valuation_status = 'complete'
              AND valuation_cutoff_ns >= ?
              AND valuation_cutoff_ns <= ?
              AND observed_at_ns <= ?
              AND recorded_at_ns <= ?
              AND valuation_model_sha256 != ?
            """,
            (
                int(baseline["valuation_cutoff_ns"]),
                _to_ns(current),
                _to_ns(as_of),
                _to_ns(knowledge),
                valuation_model_sha256,
            ),
        ).fetchone()[0]
    )
    evidence.update(
        {
            "valuation_id": str(baseline["valuation_id"]),
            "valuation_cutoff": cutoff.isoformat(),
            "observed_at": observed.isoformat(),
            "boundary_distance_seconds": boundary_distance,
            "observation_distance_seconds": observation_distance,
            "recorded_source_age_seconds": (
                int(recorded_source_age) if recorded_source_age is not None else None
            ),
            "effective_source_age_seconds": effective_source_age,
            "cross_position_skew_seconds": int(skew) if skew is not None else None,
            "valuation_model_sha256": baseline_model,
            "model_continuity": model_discontinuity_count == 0,
            "baseline_equity_low_jpy": int(baseline["liquidation_equity_low_jpy"]),
        }
    )
    if boundary_distance > policy.max_source_age_seconds:
        baseline_reasons.append(f"{period}_liquidation_baseline_boundary_stale")
    if observation_distance > policy.max_source_age_seconds:
        baseline_reasons.append(f"{period}_liquidation_baseline_observation_stale")
    if effective_source_age is None:
        baseline_reasons.append(f"{period}_liquidation_baseline_source_age_unavailable")
    elif effective_source_age > policy.max_source_age_seconds:
        baseline_reasons.append(f"{period}_liquidation_baseline_source_stale")
    if skew is None:
        baseline_reasons.append(f"{period}_liquidation_baseline_skew_unavailable")
    elif int(skew) > policy.max_cross_position_skew_seconds:
        baseline_reasons.append(f"{period}_liquidation_baseline_skew_exceeded")
    if baseline_model != valuation_model_sha256 or model_discontinuity_count:
        baseline_reasons.append(f"{period}_liquidation_baseline_model_discontinuity")
    if baseline["liquidation_equity_low_jpy"] is None:
        baseline_reasons.append(f"{period}_liquidation_baseline_equity_unavailable")
    evidence["status"] = "eligible" if not baseline_reasons else "rejected"
    return baseline, evidence, baseline_reasons


def evaluate_shadow_risk_gate_v2(
    store: VirtualPortfolioStore,
    *,
    as_of: datetime,
    known_at: datetime | None = None,
    reserve_policy: LiquidationReservePolicy | None = None,
) -> dict[str, object]:
    """Evaluate, but never enforce, the liquidation-low safety contract."""

    policy = reserve_policy or LiquidationReservePolicy()
    policy.validate()
    moment = _utc(as_of, "risk as_of")
    knowledge = _utc(known_at or as_of, "risk known_at")
    if knowledge < moment:
        raise VirtualValuationError("risk known_at cannot precede as_of")
    portfolio_policy = store.policy(as_of=moment, known_at=knowledge)
    audit = store.audit_virtual_portfolio_v2()
    reasons: list[str] = []
    if audit["passed"] is not True:
        reasons.append("v2_integrity_audit_failed")
    legacy_risk_gate_reason = store._risk_gate_reason(
        moment,
        portfolio_policy,
        known_at=knowledge,
    )
    if legacy_risk_gate_reason is not None:
        reasons.append("legacy_risk_gate_blocked")
    unresolved_candidate_gates = (
        "currency_concentration_gate_unavailable",
        "gross_leverage_gate_unavailable",
        "spread_anomaly_gate_unavailable",
        "candidate_safety_learning_gate_unavailable",
    )
    reasons.extend(unresolved_candidate_gates)
    row = store.connection.execute(
        """
        SELECT *
        FROM portfolio_liquidation_valuations
        WHERE valuation_cutoff_ns <= ?
          AND observed_at_ns <= ?
          AND recorded_at_ns <= ?
        ORDER BY valuation_cutoff_ns DESC, valuation_id DESC
        LIMIT 1
        """,
        (_to_ns(moment), _to_ns(knowledge), _to_ns(knowledge)),
    ).fetchone()
    if row is None:
        reasons.append("synchronized_liquidation_valuation_unavailable")
        return {
            "contract": RISK_GATE_CONTRACT,
            "enforcement_mode": "shadow_only",
            "can_open": False,
            "reasons": reasons,
            "as_of": moment.isoformat(),
            "knowledge_as_of": knowledge.isoformat(),
            "risk_equity_jpy": None,
            "portfolio_heat_jpy": None,
            "next_trade_risk_jpy": None,
            "period_losses_jpy": {},
        }
    status = str(row["valuation_status"])
    if status != "complete":
        reasons.append(f"valuation_status:{status}")
    valuation_model_sha = str(row["valuation_model_sha256"])
    if valuation_model_sha != policy.sha256:
        reasons.append("valuation_model_changed")
    cutoff = _from_ns(int(row["valuation_cutoff_ns"]))
    valuation_age = _ceil_nonnegative_seconds(
        moment - cutoff,
        "valuation age",
    )
    if valuation_age > policy.max_source_age_seconds:
        reasons.append(f"valuation_stale:{valuation_age}s")
    recorded_source_age = (
        int(row["max_source_age_seconds"]) if row["max_source_age_seconds"] is not None else None
    )
    effective_source_age = (
        recorded_source_age + valuation_age
        if recorded_source_age is not None and valuation_age >= 0
        else None
    )
    if effective_source_age is None:
        reasons.append("source_bundle_age_unavailable")
    elif effective_source_age > policy.max_source_age_seconds:
        reasons.append(f"source_bundle_stale:{effective_source_age}s")
    skew = int(row["cross_position_skew_seconds"] or 0)
    if skew > policy.max_cross_position_skew_seconds:
        reasons.append(f"cross_position_skew_exceeded:{skew}s")
    current_position_ids = tuple(
        str(current["trade_id"])
        for current in store.connection.execute(
            """
            SELECT o.trade_id
            FROM simulated_trade_opens AS o
            JOIN simulated_trade_open_observations AS ok ON ok.trade_id = o.trade_id
            LEFT JOIN simulated_trade_close_observations AS ck
              ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
            LEFT JOIN simulated_trade_closes AS c
              ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
             AND ck.trade_id IS NOT NULL
            WHERE o.open_time_ns <= ?
              AND ok.open_known_at_ns <= ?
              AND c.trade_id IS NULL
            ORDER BY o.trade_id
            """,
            (
                _to_ns(knowledge),
                _to_ns(moment),
                _to_ns(moment),
                _to_ns(knowledge),
            ),
        ).fetchall()
    )
    valued_position_ids = tuple(
        str(valued["trade_id"])
        for valued in store.connection.execute(
            """
            SELECT trade_id
            FROM position_liquidation_valuations
            WHERE portfolio_valuation_id = ?
            ORDER BY trade_id
            """,
            (str(row["valuation_id"]),),
        ).fetchall()
    )
    if current_position_ids != valued_position_ids:
        reasons.append("valuation_position_set_changed")
    valuation_cash = int(row["cash_jpy"])
    current_cash = store.cash_balance_jpy(as_of=moment, known_at=knowledge)
    if current_cash != valuation_cash:
        reasons.append("valuation_cash_state_changed")
    valuation_event = store.connection.execute(
        """
        SELECT event_seq
        FROM portfolio_event_log
        WHERE event_id = ?
        """,
        (str(row["event_id"]),),
    ).fetchone()
    current_event = store.connection.execute(
        """
        SELECT event_seq
        FROM portfolio_event_log
        WHERE effective_time_ns <= ? AND observed_at_ns <= ? AND recorded_at_ns <= ?
        ORDER BY event_seq DESC
        LIMIT 1
        """,
        (_to_ns(moment), _to_ns(knowledge), _to_ns(knowledge)),
    ).fetchone()
    valuation_event_seq = int(valuation_event["event_seq"]) if valuation_event is not None else None
    current_event_seq = int(current_event["event_seq"]) if current_event is not None else None
    if valuation_event_seq is None or current_event_seq != valuation_event_seq:
        reasons.append("valuation_event_head_changed")
    risk_equity = (
        int(row["liquidation_equity_low_jpy"])
        if row["liquidation_equity_low_jpy"] is not None
        else None
    )
    if risk_equity is None:
        reasons.append("liquidation_equity_low_unavailable")
    canonical_valuation = store.connection.execute(
        """
        SELECT payload_json
        FROM portfolio_event_log
        WHERE event_id = ?
        """,
        (str(row["event_id"]),),
    ).fetchone()
    try:
        canonical_payload = (
            json.loads(str(canonical_valuation["payload_json"]))
            if canonical_valuation is not None
            else {}
        )
    except (json.JSONDecodeError, TypeError):
        canonical_payload = {}
        reasons.append("canonical_valuation_payload_invalid")
    if not isinstance(canonical_payload, dict):
        canonical_payload = {}
        reasons.append("canonical_valuation_payload_invalid")
    detail_raw = canonical_payload.get("detail")
    detail = detail_raw if isinstance(detail_raw, dict) else {}
    if not isinstance(detail_raw, dict):
        reasons.append("canonical_valuation_detail_invalid")
    heat_raw = detail.get("portfolio_heat_jpy")
    portfolio_heat = int(heat_raw) if isinstance(heat_raw, int) else None
    if portfolio_heat is None:
        reasons.append("portfolio_heat_unavailable")
    heat_limit = portfolio_policy.initial_capital_jpy * policy.max_portfolio_heat_bps // 10_000
    if portfolio_heat is not None and portfolio_heat >= heat_limit:
        reasons.append("portfolio_heat_limit_reached")
    hard_drawdown_limit = (
        portfolio_policy.initial_capital_jpy * portfolio_policy.hard_drawdown_bps // 10_000
    )
    low_drawdown = (
        int(row["liquidation_drawdown_low_jpy"])
        if row["liquidation_drawdown_low_jpy"] is not None
        else None
    )
    if low_drawdown is None:
        reasons.append("liquidation_drawdown_unavailable")
    elif low_drawdown >= hard_drawdown_limit:
        reasons.append("hard_liquidation_drawdown_limit_reached")

    period_losses: dict[str, int | None] = {}
    portfolio_created = _from_ns(
        int(
            store.connection.execute("SELECT MIN(created_at_ns) FROM portfolio_config").fetchone()[
                0
            ]
        )
    )
    period_limits = (
        ("day", portfolio_policy.daily_loss_limit_bps),
        ("week", portfolio_policy.weekly_loss_limit_bps),
        ("month", portfolio_policy.monthly_loss_limit_bps),
    )
    period_baselines: dict[str, dict[str, object]] = {}
    for period, limit_bps in period_limits:
        start = _period_start(moment, period)
        baseline, baseline_evidence, baseline_reasons = _select_period_baseline(
            store,
            period=period,
            period_start=start,
            moment=moment,
            known_at=knowledge,
            portfolio_created=portfolio_created,
            current_cutoff=cutoff,
            current_liquidation_hwm_low_jpy=(
                int(row["liquidation_hwm_low_jpy"])
                if row["liquidation_hwm_low_jpy"] is not None
                else None
            ),
            initial_capital_jpy=portfolio_policy.initial_capital_jpy,
            valuation_model_sha256=valuation_model_sha,
            policy=policy,
        )
        period_baselines[period] = baseline_evidence
        reasons.extend(baseline_reasons)
        if baseline is None or baseline_reasons:
            period_losses[period] = None
            continue
        if risk_equity is None:
            period_losses[period] = None
            continue
        loss = int(baseline["liquidation_equity_low_jpy"]) - risk_equity
        period_losses[period] = loss
        limit = portfolio_policy.initial_capital_jpy * limit_bps // 10_000
        if loss >= limit:
            reasons.append(f"{period}_liquidation_loss_limit_reached")

    next_trade_risk = (
        max(0, risk_equity * portfolio_policy.risk_per_trade_bps // 10_000)
        if risk_equity is not None
        else None
    )
    return {
        "contract": RISK_GATE_CONTRACT,
        "enforcement_mode": "shadow_only",
        "can_open": not reasons,
        "reasons": reasons,
        "as_of": moment.isoformat(),
        "knowledge_as_of": knowledge.isoformat(),
        "valuation_id": str(row["valuation_id"]),
        "valuation_model_sha256": valuation_model_sha,
        "current_valuation_model_sha256": policy.sha256,
        "valuation_cutoff": cutoff.isoformat(),
        "valuation_age_seconds": valuation_age,
        "recorded_source_age_seconds": recorded_source_age,
        "effective_source_age_seconds": effective_source_age,
        "cross_position_skew_seconds": skew,
        "valuation_position_count": len(valued_position_ids),
        "current_position_count": len(current_position_ids),
        "valuation_cash_jpy": valuation_cash,
        "current_cash_jpy": current_cash,
        "valuation_event_seq": valuation_event_seq,
        "current_event_seq": current_event_seq,
        "legacy_risk_gate_reason": legacy_risk_gate_reason,
        "unresolved_candidate_gates": list(unresolved_candidate_gates),
        "risk_equity_jpy": risk_equity,
        "portfolio_heat_jpy": portfolio_heat,
        "portfolio_heat_limit_jpy": heat_limit,
        "liquidation_drawdown_low_jpy": low_drawdown,
        "hard_drawdown_limit_jpy": hard_drawdown_limit,
        "period_losses_jpy": period_losses,
        "period_baselines": period_baselines,
        "next_trade_risk_jpy": next_trade_risk,
        "integrity_event_head_sha256": audit["event_head_sha256"],
    }


__all__ = [
    "LiquidationReservePolicy",
    "RISK_GATE_CONTRACT",
    "VALUATION_CONSUMER_CONTRACT",
    "VirtualValuationError",
    "append_synchronized_valuation_from_marks",
    "evaluate_shadow_risk_gate_v2",
]
