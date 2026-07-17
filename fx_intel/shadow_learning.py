"""Immutable counterfactual predictions and shadow-only learning summaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, UTC

SHADOW_SCHEMA_VERSION = 1
SHADOW_DEADBAND = 0.05
SHADOW_LABEL_PROVENANCE = "shadow_counterfactual_quote_model"
SHADOW_THIN_HOURS = 4.0


def prediction_draft(
    producer: str,
    score: float,
    *,
    stage: str = "shadow",
    producer_version: str = "score-v1",
) -> dict[str, object]:
    return {
        "producer": producer,
        "producer_version": producer_version,
        "stage_at_prediction": stage,
        "score": round(max(-1.0, min(1.0, float(score))), 4),
    }


def build_shadow_predictions(
    drafts: Sequence[Mapping[str, object]],
    *,
    close: float | None,
    atr: float | None,
    entry_bid: float | None,
    entry_ask: float | None,
    quote_observed_at: str | None,
    cost_model_id: str,
    slippage_r: float,
    commission_r: float,
    atr_multiple: float,
    production_threshold: float,
    horizon_hours: float,
    blocked_by: Sequence[str],
    market_open: bool,
    learning_dimensions: Mapping[str, object],
) -> list[dict[str, object]]:
    """Freeze producer scores and hypothetical levels before outcomes exist."""

    output: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw in drafts:
        producer = str(raw.get("producer", "")).strip()
        version = str(raw.get("producer_version", "score-v1")).strip() or "score-v1"
        score = _number(raw.get("score"))
        if not producer or score is None or (producer, version) in seen:
            continue
        seen.add((producer, version))
        abstained = abs(score) < SHADOW_DEADBAND
        direction = "neutral" if abstained else ("long" if score > 0 else "short")
        prediction_blocks = list(dict.fromkeys(str(reason) for reason in blocked_by if reason))
        if (
            abs(score) < production_threshold
            and "below_production_threshold" not in prediction_blocks
        ):
            prediction_blocks.append("below_production_threshold")
        risk_distance = (
            float(atr) * atr_multiple
            if isinstance(atr, (int, float)) and atr > 0 and atr_multiple > 0
            else None
        )
        stop = target1 = target2 = None
        if direction in ("long", "short") and close is not None and risk_distance is not None:
            sign = 1.0 if direction == "long" else -1.0
            stop = close - sign * risk_distance
            target1 = close + sign * risk_distance
            target2 = close + sign * risk_distance * 2.0
        direction_eligible = (
            market_open
            and direction in ("long", "short")
            and close is not None
            and risk_distance is not None
        )
        net_input_eligible = (
            direction_eligible
            and entry_bid is not None
            and entry_ask is not None
            and bool(quote_observed_at)
            and bool(cost_model_id)
        )
        missing: list[str] = []
        if not market_open:
            missing.append("market_closed")
        if abstained:
            missing.append("shadow_abstained")
        if close is None:
            missing.append("missing_entry")
        if risk_distance is None:
            missing.append("missing_atr")
        if entry_bid is None or entry_ask is None:
            missing.append("missing_entry_quote")
        output.append(
            {
                "schema": SHADOW_SCHEMA_VERSION,
                "producer": producer,
                "producer_version": version,
                "stage_at_prediction": str(raw.get("stage_at_prediction", "shadow")),
                "score": round(score, 4),
                "direction": direction,
                "abstained": abstained,
                "shadow_deadband": SHADOW_DEADBAND,
                "production_threshold": production_threshold,
                "horizon_hours": horizon_hours,
                "blocked_by": prediction_blocks,
                "eligible_for_scoring": direction_eligible,
                "eligible_for_production_training": False,
                "net_label_input_eligible": net_input_eligible,
                "missing_reasons": missing,
                "close": close,
                "atr": atr,
                "entry_bid": entry_bid,
                "entry_ask": entry_ask,
                "quote_observed_at": quote_observed_at,
                "cost_model_id": cost_model_id,
                "slippage_r": slippage_r,
                "commission_r": commission_r,
                "planned_risk_distance": risk_distance,
                "stop": stop,
                "target1": target1,
                "target2": target2,
                "target_policy": {"policy_id": "shadow-default-atr-v1"},
                "learning_dimensions": dict(learning_dimensions),
            }
        )
    return output


def assign_prediction_ids(predictions: object, decision_id: str) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    rows = predictions if isinstance(predictions, (list, tuple)) else []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        producer = str(row.get("producer", "unknown"))
        version = str(row.get("producer_version", "score-v1"))
        row["prediction_id"] = f"{decision_id}:{producer}:{version}"
        output.append(row)
    return output


def summarize_shadow_outcomes(
    outcomes: Sequence[Mapping[str, object]],
    *,
    predictions: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Build producer/session/regime diagnostics from shadow-only observations."""

    producer_names = {str(row.get("producer", "unknown")) for row in [*predictions, *outcomes]}
    by_producer: dict[str, object] = {}
    for producer in sorted(producer_names):
        all_producer_outcomes = [row for row in outcomes if str(row.get("producer")) == producer]
        all_producer_predictions = [
            row for row in predictions if str(row.get("producer")) == producer
        ]
        versions = sorted(
            {
                str(row.get("producer_version", "unknown") or "unknown")
                for row in [*all_producer_predictions, *all_producer_outcomes]
            }
        )
        latest_row = max(
            [*all_producer_predictions, *all_producer_outcomes],
            key=lambda row: str(row.get("ts", "")),
            default={},
        )
        active_version = str(latest_row.get("producer_version", "unknown") or "unknown")
        producer_outcomes = [
            row
            for row in all_producer_outcomes
            if str(row.get("producer_version", "unknown") or "unknown") == active_version
        ]
        producer_predictions = [
            row
            for row in all_producer_predictions
            if str(row.get("producer_version", "unknown") or "unknown") == active_version
        ]
        thinned = _thin(producer_outcomes)
        by_producer[producer] = {
            **_stats(thinned, predictions=producer_predictions),
            "active_version": active_version,
            "producer_versions": versions,
            "by_version": {
                version: _stats(
                    _thin(
                        [
                            row
                            for row in all_producer_outcomes
                            if str(row.get("producer_version", "unknown") or "unknown") == version
                        ]
                    ),
                    predictions=[
                        row
                        for row in all_producer_predictions
                        if str(row.get("producer_version", "unknown") or "unknown") == version
                    ],
                )
                for version in versions
            },
            "by_timeframe": _group_field_stats(thinned, "timeframe"),
            "by_session": _group_stats(thinned, "session_bucket"),
            "by_regime": _group_stats(thinned, "regime"),
        }
    return {
        "schema": 1,
        "predictions": len(predictions),
        "outcomes": len(outcomes),
        "by_producer": by_producer,
    }


def build_learning_observations(
    final_outcomes: Sequence[Mapping[str, object]],
    shadow_outcomes: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for kind, rows in (
        ("final_decision", final_outcomes),
        ("shadow_hypothesis", shadow_outcomes),
    ):
        for raw in rows:
            identity = (
                raw.get("prediction_id") if kind == "shadow_hypothesis" else raw.get("decision_id")
            )
            if not identity:
                continue
            row = dict(raw)
            row["observation_id"] = f"{identity}:{raw.get('label_version', 'unknown')}"
            row["prediction_kind"] = kind
            row["training_role"] = (
                "shadow_only" if kind == "shadow_hypothesis" else "production_history"
            )
            observations.append(row)
    return observations


def summarize_outcome_dimensions(
    outcomes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Aggregate final outcomes by marginal session/regime dimensions."""

    result: dict[str, object] = {}
    for dimension in ("session_bucket", "regime"):
        grouped: dict[tuple[str, str], list[Mapping[str, object]]] = {}
        for row in outcomes:
            dimensions = row.get("learning_dimensions")
            bucket = (
                str(dimensions.get(dimension, "unknown"))
                if isinstance(dimensions, Mapping)
                else "unknown"
            )
            direction = str(row.get("direction", "unknown"))
            grouped.setdefault((bucket, direction), []).append(row)
        dimension_result: dict[str, object] = {}
        for (bucket, direction), rows in sorted(grouped.items()):
            dimension_result.setdefault(bucket, {})[direction] = _stats(_thin(rows))  # type: ignore[index]
        result[dimension] = dimension_result
    return result


def _group_stats(rows: Sequence[Mapping[str, object]], dimension: str) -> dict[str, object]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        dimensions = row.get("learning_dimensions")
        bucket = (
            str(dimensions.get(dimension, "unknown"))
            if isinstance(dimensions, Mapping)
            else "unknown"
        )
        grouped.setdefault(bucket, []).append(row)
    return {bucket: _stats(group) for bucket, group in sorted(grouped.items())}


def _group_field_stats(rows: Sequence[Mapping[str, object]], field: str) -> dict[str, object]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(field, "unknown") or "unknown"), []).append(row)
    return {bucket: _stats(group) for bucket, group in sorted(grouped.items())}


def _stats(
    rows: Sequence[Mapping[str, object]],
    *,
    predictions: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    directional = [row for row in rows if row.get("direction_outcome") in ("hit", "miss")]
    if directional:
        hits = sum(1 for row in directional if row.get("direction_outcome") == "hit")
    else:
        directional = [row for row in rows if _number(row.get("realized_r")) is not None]
        hits = sum(1 for row in directional if (_number(row.get("realized_r")) or 0.0) > 0)
    net_values = [
        value
        for row in rows
        if (value := _number(row.get("realized_net_r"))) is not None
        and bool(row.get("net_label_eligible", False))
    ]
    abstained = sum(1 for row in predictions if bool(row.get("abstained", False)))
    return {
        "raw": len(rows),
        "effective": len(directional),
        "hits": hits,
        "hit_rate": round(hits / len(directional), 4) if directional else None,
        "net_labels": len(net_values),
        "net_label_coverage": round(len(net_values) / len(rows), 4) if rows else 0.0,
        "net_expectancy_r": round(sum(net_values) / len(net_values), 4) if net_values else None,
        "cumulative_net_r": round(sum(net_values), 4) if net_values else None,
        "predictions": len(predictions),
        "abstained": abstained,
    }


def _thin(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    parsed: list[tuple[datetime, Mapping[str, object]]] = []
    for row in rows:
        try:
            ts = datetime.fromisoformat(str(row.get("ts", "")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        parsed.append((ts, row))
    last: dict[str, datetime] = {}
    output: list[Mapping[str, object]] = []
    for ts, row in sorted(parsed, key=lambda item: item[0]):
        symbol = str(row.get("symbol", ""))
        previous = last.get(symbol)
        if previous is not None and ts - previous < timedelta(hours=SHADOW_THIN_HOURS):
            continue
        last[symbol] = ts
        output.append(row)
    return output


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
