"""Canonical full-cost net-R labels for the analysis-only virtual portfolio.

The virtual portfolio records simulated fills, never broker executions.  This
adapter turns its immutable entry/close evidence into ``net-r-v3`` without
claiming that delayed Dukascopy history was executable in real time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from .evaluation_labels import (
    FULL_COST_NET_LABEL_VERSION,
    VIRTUAL_PORTFOLIO_COST_STATUS,
    VIRTUAL_PORTFOLIO_LABEL_PROVENANCE,
    canonical_net_label_contract_flags,
)
from .canonical_outcome_store import (
    load_canonical_outcomes,
    verify_canonical_outcome_store,
)
from .trade_outcome import CanonicalTradeOutcome

VIRTUAL_LEARNING_CONTRACT = "virtual-portfolio-learning-pit-v3"
HISTORICAL_NONTRADABLE_FLAG = "historical_download_not_tradable"
_TIMEFRAME_HOURS = {"15m": 0.25, "1h": 1.0}


class InvalidVirtualNetLabel(ValueError):
    """A closed virtual trade cannot produce an admissible canonical label."""


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidVirtualNetLabel(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidVirtualNetLabel(f"{name} must be finite")
    return number


def _integer(value: object, name: str) -> int:
    number = _number(value, name)
    integer = int(number)
    if number != integer:
        raise InvalidVirtualNetLabel(f"{name} must be an integer")
    return integer


def _text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidVirtualNetLabel(f"{name} is required")
    return text


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidVirtualNetLabel(f"{name} must be an object")
    return value


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise InvalidVirtualNetLabel(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidVirtualNetLabel(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_outcome_from_virtual_row(row: Mapping[str, object]) -> CanonicalTradeOutcome:
    """Build one fail-closed ``net-r-v3`` outcome from an immutable learning row."""

    prediction = _aware(row.get("prediction_time"), "prediction_time")
    simulated_entry = _aware(row.get("simulated_entry_time"), "simulated_entry_time")
    simulated_entry_available = _aware(
        row.get("simulated_entry_available_time"),
        "simulated_entry_available_time",
    )
    holding_end = _aware(row.get("label_end_time"), "label_end_time")
    available = _aware(row.get("label_available_time"), "label_available_time")
    ingested = _aware(row.get("label_ingested_time"), "label_ingested_time")
    if not prediction <= simulated_entry <= simulated_entry_available <= available:
        raise InvalidVirtualNetLabel("entry knowledge interval is reversed or immature")
    if not simulated_entry < holding_end <= available <= ingested:
        raise InvalidVirtualNetLabel("label knowledge interval is reversed or immature")

    direction = _text(row.get("direction"), "direction").lower()
    if direction not in {"long", "short"}:
        raise InvalidVirtualNetLabel("direction must be long or short")
    timeframe = _text(row.get("timeframe"), "timeframe").lower()
    horizon_hours = _TIMEFRAME_HOURS.get(timeframe)
    if horizon_hours is None:
        raise InvalidVirtualNetLabel("only 15m/1h capacity trades can receive net-r-v3")

    entry_bid = _number(row.get("entry_bid"), "entry_bid")
    entry_ask = _number(row.get("entry_ask"), "entry_ask")
    entry_mid = _number(row.get("entry_mid"), "entry_mid")
    entry_executable = _number(row.get("entry_executable"), "entry_executable")
    exit_bid = _number(row.get("exit_bid"), "exit_bid")
    exit_ask = _number(row.get("exit_ask"), "exit_ask")
    exit_mid = _number(row.get("exit_mid"), "exit_mid")
    exit_executable = _number(row.get("exit_executable"), "exit_executable")
    if not (0 < entry_bid <= entry_ask and 0 < exit_bid <= exit_ask):
        raise InvalidVirtualNetLabel("entry/exit bid-ask quotes must be positive and ordered")
    if not math.isclose(entry_mid, (entry_bid + entry_ask) / 2.0, abs_tol=1e-12):
        raise InvalidVirtualNetLabel("entry_mid does not reconcile with bid/ask")
    if not math.isclose(exit_mid, (exit_bid + exit_ask) / 2.0, abs_tol=1e-12):
        raise InvalidVirtualNetLabel("exit_mid does not reconcile with bid/ask")
    expected_entry = entry_ask if direction == "long" else entry_bid
    expected_exit = exit_bid if direction == "long" else exit_ask
    if not math.isclose(entry_executable, expected_entry, abs_tol=1e-12):
        raise InvalidVirtualNetLabel("entry executable quote uses the wrong bid/ask side")
    if not math.isclose(exit_executable, expected_exit, abs_tol=1e-12):
        raise InvalidVirtualNetLabel("exit executable quote uses the wrong bid/ask side")
    units = _number(row.get("units"), "units")
    entry_quote_to_jpy_rate = _number(
        row.get("entry_quote_to_jpy_rate"),
        "entry_quote_to_jpy_rate",
    )
    exit_quote_to_jpy_rate = _number(
        row.get("exit_quote_to_jpy_rate"),
        "exit_quote_to_jpy_rate",
    )
    if units <= 0 or entry_quote_to_jpy_rate <= 0 or exit_quote_to_jpy_rate <= 0:
        raise InvalidVirtualNetLabel("units and quote-to-JPY rates must be positive")
    entry_conversion_source = _text(
        row.get("entry_quote_conversion_source"),
        "entry_quote_conversion_source",
    )
    exit_conversion_source = _text(
        row.get("exit_quote_conversion_source"),
        "exit_quote_conversion_source",
    )

    stop_price = _number(row.get("stop_price"), "stop_price")
    target_price = _number(row.get("target_price"), "target_price")
    risk_distance = float(abs(Decimal(str(entry_executable)) - Decimal(str(stop_price))))
    if risk_distance <= 0:
        raise InvalidVirtualNetLabel("planned risk distance must be positive")
    payoff_move = (
        target_price - entry_executable if direction == "long" else entry_executable - target_price
    )

    planned_risk_jpy = _integer(row.get("planned_risk_jpy"), "planned_risk_jpy")
    if planned_risk_jpy <= 0:
        raise InvalidVirtualNetLabel("planned_risk_jpy must be positive")
    attribution = _mapping(row.get("attribution"), "attribution")
    gross_jpy = _integer(attribution.get("gross_market_pnl_jpy"), "gross_market_pnl_jpy")
    spread_jpy = _integer(attribution.get("spread_quote_cost_jpy"), "spread_quote_cost_jpy")
    slippage_jpy = _integer(attribution.get("slippage_cost_jpy"), "slippage_cost_jpy")
    commission_jpy = _integer(attribution.get("commission_jpy"), "commission_jpy")
    financing_jpy = _integer(attribution.get("financing_jpy"), "financing_jpy")
    conversion_jpy = _integer(attribution.get("conversion_cost_jpy"), "conversion_cost_jpy")
    net_jpy = _integer(row.get("net_pnl_jpy"), "net_pnl_jpy")
    costs = (spread_jpy, slippage_jpy, commission_jpy, financing_jpy, conversion_jpy)
    if any(value < 0 for value in costs):
        raise InvalidVirtualNetLabel("canonical cost components cannot be negative")
    executable_jpy = gross_jpy - spread_jpy
    if net_jpy != executable_jpy - sum(costs[1:]):
        raise InvalidVirtualNetLabel("full-cost JPY attribution does not reconcile")
    stored_net_r = _number(row.get("realized_net_r"), "realized_net_r")
    if not math.isclose(stored_net_r, net_jpy / planned_risk_jpy, abs_tol=1e-12):
        raise InvalidVirtualNetLabel("stored realized_net_r does not reconcile")

    entry_quote = _mapping(row.get("entry_quote"), "entry_quote")
    exit_quote = _mapping(row.get("exit_quote"), "exit_quote")
    entry_source = _text(entry_quote.get("source"), "entry quote source")
    exit_source = _text(exit_quote.get("source"), "exit quote source")
    entry_source_id = _text(entry_quote.get("source_record_id"), "entry source_record_id")
    exit_source_id = _text(exit_quote.get("source_record_id"), "exit source_record_id")
    entry_quote_bid = _number(entry_quote.get("bid"), "entry quote bid")
    entry_quote_ask = _number(entry_quote.get("ask"), "entry quote ask")
    exit_quote_bid = _number(exit_quote.get("bid"), "exit quote bid")
    exit_quote_ask = _number(exit_quote.get("ask"), "exit quote ask")
    if not (
        math.isclose(entry_quote_bid, entry_bid, abs_tol=1e-12)
        and math.isclose(entry_quote_ask, entry_ask, abs_tol=1e-12)
        and math.isclose(exit_quote_bid, exit_bid, abs_tol=1e-12)
        and math.isclose(exit_quote_ask, exit_ask, abs_tol=1e-12)
    ):
        raise InvalidVirtualNetLabel("quote JSON bid/ask does not match immutable fill columns")
    entry_event = _aware(entry_quote.get("event_time"), "entry quote event_time")
    entry_available = _aware(entry_quote.get("available_time"), "entry quote available_time")
    entry_ingested = _aware(entry_quote.get("ingested_time"), "entry quote ingested_time")
    exit_event = _aware(exit_quote.get("event_time"), "exit quote event_time")
    exit_available = _aware(exit_quote.get("available_time"), "exit quote available_time")
    exit_ingested = _aware(exit_quote.get("ingested_time"), "exit quote ingested_time")
    if entry_event > simulated_entry or exit_event > holding_end:
        raise InvalidVirtualNetLabel("quote event time is after the simulated fill")
    if not (
        entry_event <= entry_available <= entry_ingested <= simulated_entry_available
        and exit_event <= exit_available <= exit_ingested <= available
    ):
        raise InvalidVirtualNetLabel("quote knowledge times violate the label as-of boundary")
    entry_revision = _text(entry_quote.get("revision_id"), "entry quote revision_id")
    exit_revision = _text(exit_quote.get("revision_id"), "exit quote revision_id")
    cost_contract = _mapping(attribution.get("cost_contract"), "cost_contract")
    if cost_contract.get("costs_complete") is not True:
        raise InvalidVirtualNetLabel("explicit complete cost evidence is required")
    cost_model_id = _text(cost_contract.get("cost_model_id"), "cost_model_id")
    cost_model_version = _text(cost_contract.get("cost_model_version"), "cost_model_version")
    cost_source = _text(cost_contract.get("cost_source"), "cost_source")
    observation_mode = _text(
        attribution.get("execution_observation_mode"),
        "execution_observation_mode",
    )
    if observation_mode not in {
        "delayed_historical_bid_ask_replay",
        "contemporaneous_quote",
    }:
        raise InvalidVirtualNetLabel("unsupported execution observation mode")
    quality_flags: tuple[str, ...] = ()
    if observation_mode == "delayed_historical_bid_ask_replay":
        if entry_event != simulated_entry or exit_event != holding_end:
            raise InvalidVirtualNetLabel(
                "historical replay quote event must equal the simulated fill time"
            )
        replay = _mapping(attribution.get("replay_evidence"), "replay_evidence")
        if (
            replay.get("contract") != "virtual-portfolio-delayed-bid-ask-replay-v1"
            or _integer(replay.get("path_tick_count_to_exit"), "path_tick_count_to_exit") <= 0
            or not _text(replay.get("path_source_ids_sha256"), "path_source_ids_sha256")
        ):
            raise InvalidVirtualNetLabel("delayed replay path evidence is incomplete")
        quality_flags = (HISTORICAL_NONTRADABLE_FLAG,)

    denominator = float(planned_risk_jpy)
    gross_r = gross_jpy / denominator
    spread_r = spread_jpy / denominator
    quote_r = executable_jpy / denominator
    slippage_r = slippage_jpy / denominator
    commission_r = commission_jpy / denominator
    financing_r = financing_jpy / denominator
    conversion_r = conversion_jpy / denominator
    additional_r = slippage_r + commission_r + financing_r + conversion_r
    net_r = net_jpy / denominator
    execution_cost_r = gross_r - net_r
    model_prefix = f"{cost_model_id}:{cost_model_version}"
    outcome = CanonicalTradeOutcome(
        decision_id=_text(row.get("decision_id"), "decision_id"),
        label_version=FULL_COST_NET_LABEL_VERSION,
        label_provenance=VIRTUAL_PORTFOLIO_LABEL_PROVENANCE,
        symbol=_text(row.get("symbol"), "symbol").upper(),
        mode="offline_simulation",
        timeframe=timeframe,
        horizon_hours=horizon_hours,
        direction=direction,
        prediction_time=prediction.isoformat(),
        holding_end_time=holding_end.isoformat(),
        label_available_time=available.isoformat(),
        label_ingested_time=ingested.isoformat(),
        first_touch=_text(row.get("close_reason"), "close_reason"),
        first_touch_ts=holding_end.isoformat(),
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        entry_executable=entry_executable,
        planned_risk_distance=risk_distance,
        planned_risk_jpy=planned_risk_jpy,
        units=units,
        entry_quote_to_jpy_rate=entry_quote_to_jpy_rate,
        entry_quote_conversion_source=entry_conversion_source,
        exit_bid=exit_bid,
        exit_ask=exit_ask,
        exit_executable=exit_executable,
        exit_mid=exit_mid,
        exit_quote_to_jpy_rate=exit_quote_to_jpy_rate,
        exit_quote_conversion_source=exit_conversion_source,
        gross_realized_r=gross_r,
        quote_realized_r=quote_r,
        planned_payoff_r=payoff_move / risk_distance,
        slippage_r=slippage_r,
        commission_r=commission_r,
        financing_r=financing_r,
        conversion_r=conversion_r,
        entry_spread_r=(entry_ask - entry_bid) / risk_distance,
        realized_spread_cost_r=spread_r,
        additional_cost_r=additional_r,
        execution_cost_r=execution_cost_r,
        realized_net_r=net_r,
        gross_market_pnl_jpy=gross_jpy,
        executable_pnl_jpy=executable_jpy,
        spread_quote_cost_jpy=spread_jpy,
        slippage_cost_jpy=slippage_jpy,
        commission_jpy=commission_jpy,
        financing_jpy=financing_jpy,
        conversion_cost_jpy=conversion_jpy,
        net_pnl_jpy=net_jpy,
        cost_model_id=cost_model_id,
        cost_model_version=cost_model_version,
        cost_status=VIRTUAL_PORTFOLIO_COST_STATUS,
        entry_quote_source=entry_source,
        spread_source=f"{entry_source}+{exit_source}",
        slippage_model_id=f"{model_prefix}:slippage",
        commission_model_id=f"{model_prefix}:commission",
        conversion_model_id=f"{model_prefix}:conversion",
        cost_source=cost_source,
        exit_quote_source=exit_source,
        execution_observation_mode=observation_mode,
        entry_source_record_id=entry_source_id,
        exit_source_record_id=exit_source_id,
        entry_quote_event_time=entry_event.isoformat(),
        entry_quote_available_time=entry_available.isoformat(),
        entry_quote_ingested_time=entry_ingested.isoformat(),
        entry_quote_revision_id=entry_revision,
        exit_quote_event_time=exit_event.isoformat(),
        exit_quote_available_time=exit_available.isoformat(),
        exit_quote_ingested_time=exit_ingested.isoformat(),
        exit_quote_revision_id=exit_revision,
        decision_record_sha256=_text(row.get("decision_record_sha256"), "decision_record_sha256"),
        open_record_sha256=_text(row.get("open_record_sha256"), "open_record_sha256"),
        open_observation_sha256=_text(
            row.get("open_observation_sha256"),
            "open_observation_sha256",
        ),
        close_record_sha256=_text(row.get("close_record_sha256"), "close_record_sha256"),
        close_observation_sha256=_text(
            row.get("close_observation_sha256"),
            "close_observation_sha256",
        ),
        entry_knowledge_basis=_text(
            row.get("simulated_entry_knowledge_basis"),
            "simulated_entry_knowledge_basis",
        ),
        label_knowledge_basis=_text(
            row.get("label_knowledge_basis"),
            "label_knowledge_basis",
        ),
        costs_complete=True,
        research_only=True,
        promotion_eligible=False,
        cost_quality_flags=quality_flags,
        net_label_eligible=True,
        path_quality=1.0,
        path_source=observation_mode,
        quality_flags=quality_flags,
    )
    contract_flags = canonical_net_label_contract_flags(outcome.to_dict())
    if contract_flags:
        raise InvalidVirtualNetLabel(
            f"canonical net-R contract violation: {','.join(contract_flags)}"
        )
    return outcome


def canonical_outcome_sha256(outcome: CanonicalTradeOutcome) -> str:
    """Return the stable hash used to bind a learning row to its label."""

    return _sha256(outcome.to_dict())


def canonical_label_summary(
    rows: Sequence[Mapping[str, object]],
    *,
    eligible_rows: int | None = None,
) -> dict[str, object]:
    """Summarize already validated v3 rows without recomputing their accounting."""

    labels = [row.get("canonical_outcome") for row in rows]
    valid = [
        label
        for label in labels
        if isinstance(label, Mapping) and not canonical_net_label_contract_flags(label)
    ]
    values = [float(label["realized_net_r"]) for label in valid]
    gross_values = [float(label["gross_realized_r"]) for label in valid]
    total = len(rows) if eligible_rows is None else eligible_rows
    if total < len(rows) or total < 0:
        raise InvalidVirtualNetLabel("eligible row denominator is inconsistent")
    return {
        "contract": "virtual-portfolio-canonical-net-r-summary-v1",
        "eligible_rows": total,
        "net_label_samples": len(valid),
        "net_label_coverage": len(valid) / total if total else None,
        "gross_expectancy_r": sum(gross_values) / len(gross_values) if gross_values else None,
        "net_expectancy_r": sum(values) / len(values) if values else None,
        "cumulative_net_r": sum(values) if values else None,
        "label_version": FULL_COST_NET_LABEL_VERSION,
        "label_provenance": VIRTUAL_PORTFOLIO_LABEL_PROVENANCE,
        "research_only": True,
        "promotion_eligible": False,
    }


def canonical_store_connection_summary(
    rows: Sequence[Mapping[str, object]],
    *,
    eligible_rows: int,
    store_path: str | Path,
) -> dict[str, object]:
    """Prove generated v3 labels are byte-equivalent to the verified JSONL store."""

    generated = canonical_label_summary(rows, eligible_rows=eligible_rows)
    expected: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in rows:
        payload = row.get("canonical_outcome")
        if not isinstance(payload, Mapping):
            raise InvalidVirtualNetLabel("canonical_outcome must be an object")
        if canonical_net_label_contract_flags(payload):
            raise InvalidVirtualNetLabel("generated canonical outcome is contract-invalid")
        if row.get("canonical_outcome_sha256") != _sha256(payload):
            raise InvalidVirtualNetLabel("generated canonical outcome hash mismatch")
        key = (
            _text(payload.get("decision_id"), "canonical decision_id"),
            _text(payload.get("label_version"), "canonical label_version"),
        )
        if key in expected:
            raise InvalidVirtualNetLabel("duplicate generated canonical outcome key")
        expected[key] = payload

    target = Path(store_path).resolve()
    if eligible_rows == 0:
        return {
            **generated,
            "contract": "virtual-portfolio-canonical-connection-v1",
            "connected_rows": 0,
            "generated_label_samples": 0,
            "net_label_samples": 0,
            "net_label_coverage": None,
            "status": "no_eligible_rows",
            "store_path": str(target),
        }

    audit = verify_canonical_outcome_store(target)
    persisted = {
        outcome.outcome_key: outcome.to_dict() for outcome in load_canonical_outcomes(target)
    }
    connected = sum(1 for key, payload in expected.items() if persisted.get(key) == dict(payload))
    coverage = connected / eligible_rows
    complete = (
        len(rows) == eligible_rows and len(expected) == eligible_rows and connected == eligible_rows
    )
    return {
        **generated,
        "contract": "virtual-portfolio-canonical-connection-v1",
        "connected_rows": connected,
        "generated_label_samples": len(expected),
        "net_label_samples": connected,
        "net_label_coverage": coverage,
        "status": "connected" if complete else "invalid_or_unavailable",
        "store_path": str(target),
        "store_entry_count": audit.entry_count,
        "store_sha256": audit.store_sha256,
        "canonical_export_sha256": audit.canonical_export_sha256,
        "gross_expectancy_r": generated["gross_expectancy_r"] if complete else None,
        "net_expectancy_r": generated["net_expectancy_r"] if complete else None,
        "cumulative_net_r": generated["cumulative_net_r"] if complete else None,
    }


__all__ = [
    "HISTORICAL_NONTRADABLE_FLAG",
    "InvalidVirtualNetLabel",
    "VIRTUAL_LEARNING_CONTRACT",
    "canonical_label_summary",
    "canonical_outcome_from_virtual_row",
    "canonical_outcome_sha256",
    "canonical_store_connection_summary",
]
