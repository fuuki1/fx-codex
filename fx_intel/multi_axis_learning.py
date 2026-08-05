"""Research-only multi-axis learner for mature virtual-portfolio outcomes.

The learner is deliberately downstream of the immutable decision and simulated
outcome ledgers.  It cannot change a decision, a risk gate, or any broker state.
It uses train/tune/calibration/test only. The rolling fifth split is not called a
lockbox; a fixed one-time lockbox remains unavailable until separately committed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
import math
import statistics
from typing import Any, TypedDict

from fx_intel.gbm import (
    GradientBoostingClassifier,
    GradientBoostingRegressor,
    brier_score,
    log_loss,
    pinball_loss,
    platt_calibrate,
    rmse,
)
from fx_intel.virtual_portfolio_learning import (
    InvalidLearningDataset,
    build_challenger_manifest,
    prepare_temporal_learning_splits,
)

MULTI_AXIS_CONTRACT = "virtual-portfolio-multi-axis-shadow-v1"
MIN_AXIS_COVERAGE = 0.60
REQUIRED_INPUT_AXES = (
    "market_regime",
    "liquidity_proxy",
    "macro_surprise",
    "cross_asset_relationship",
    "portfolio_risk",
)
AXIS_DEFINITIONS: dict[str, dict[str, str]] = {
    "net_return_distribution": {
        "label_ja": "コスト控除後の収益分布",
        "role": "prediction_head",
        "source": "canonical realized_net_r; mean and p10/p50/p90 heads",
    },
    "market_regime": {
        "label_ja": "市場レジーム",
        "role": "prediction_time_feature",
        "source": "decision learning_dimensions with context decision_time",
    },
    "liquidity_proxy": {
        "label_ja": "流動性・公開気配proxy",
        "role": "prediction_time_feature",
        "source": "decision bid/ask with recomputed spread, pips, bps and quote age",
    },
    "macro_surprise": {
        "label_ja": "マクロサプライズ",
        "role": "prediction_time_feature",
        "source": "first-release actual minus pre-release forecast, standardized PIT",
    },
    "cross_asset_relationship": {
        "label_ja": "クロスアセット関係",
        "role": "prediction_time_feature",
        "source": "PIT VIX, USD index, US yields, curve and COT state",
    },
    "uncertainty": {
        "label_ja": "不確実性",
        "role": "prediction_head",
        "source": "separate calibration window and p10-p90 interval",
    },
    "execution_quality": {
        "label_ja": "執行品質",
        "role": "auxiliary_target",
        "source": "realized spread/slippage/commission/financing/conversion cost per planned R",
    },
    "portfolio_risk": {
        "label_ja": "ポートフォリオリスク",
        "role": "prediction_time_feature",
        "source": "event-time and knowledge-time frozen virtual portfolio state",
    },
}
_POST_OUTCOME_INPUT_TOKENS = (
    "realized_",
    "net_pnl",
    "gross_pnl",
    "close_reason",
    "label_",
    "maximum_favorable",
    "maximum_adverse",
    "mfe_",
    "mae_",
    "outcome",
    "future_return",
    "profit_loss",
)
_PREDICTION_FEATURE_ALLOWLIST = {
    "adx",
    "adx_1h",
    "atr",
    "atr_pct",
    "ma_gap_atr",
    "news_count",
    "rating_1d",
    "rating_4h",
    "rsi",
    "rsi_1h",
    "tf_agreement",
}
_CROSS_ASSET_KEYS = {
    "vix_level",
    "vix_change_5d_pct",
    "us10y_level_pct",
    "us10y_change_5d_bp",
    "us2y_level_pct",
    "us2y_change_5d_bp",
    "curve_2s10s_bp",
    "usd_index_level",
    "usd_index_change_5d_pct",
    "cot_base_net_ratio",
    "cot_quote_net_ratio",
    "cot_pair_diff",
    "macro_pair_score",
    "macro_pair_confidence",
}
_LEGACY_SIX_DECIMAL_MACRO_KEYS = frozenset(
    {
        "vix_change_5d_pct",
        "us10y_change_5d_bp",
        "us2y_change_5d_bp",
        "curve_2s10s_bp",
        "usd_index_change_5d_pct",
        "cot_pair_diff",
    }
)
_CROSS_ASSET_RANGES: dict[str, tuple[float, float]] = {
    "vix_level": (0.0, 500.0),
    "vix_change_5d_pct": (-100.0, 10_000.0),
    "us10y_level_pct": (-20.0, 100.0),
    "us10y_change_5d_bp": (-20_000.0, 20_000.0),
    "us2y_level_pct": (-20.0, 100.0),
    "us2y_change_5d_bp": (-20_000.0, 20_000.0),
    "curve_2s10s_bp": (-20_000.0, 20_000.0),
    "usd_index_level": (1.0, 1_000.0),
    "usd_index_change_5d_pct": (-100.0, 1_000.0),
    "cot_base_net_ratio": (-1.0, 1.0),
    "cot_quote_net_ratio": (-1.0, 1.0),
    "cot_pair_diff": (-2.0, 2.0),
    "macro_pair_score": (-1.0, 1.0),
    "macro_pair_confidence": (0.0, 1.0),
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidLearningDataset(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidLearningDataset(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _available_no_later_than(
    envelope: Mapping[str, object],
    prediction_time: datetime,
    name: str,
) -> None:
    event_time = _aware(envelope.get("event_time"), f"{name}.event_time")
    available = _aware(envelope.get("available_time"), f"{name}.available_time")
    ingested = _aware(envelope.get("ingested_time"), f"{name}.ingested_time")
    first_seen = _aware(envelope.get("first_seen_time"), f"{name}.first_seen_time")
    if event_time > prediction_time:
        raise InvalidLearningDataset(f"{name}.event_time follows prediction_time")
    if max(available, ingested, first_seen) > prediction_time:
        raise InvalidLearningDataset(f"{name} was not available at prediction_time")
    for field in ("source", "source_record_id"):
        if not str(envelope.get(field) or "").strip():
            raise InvalidLearningDataset(f"{name}.{field} is required")
    content_hash = str(envelope.get("content_hash") or "")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise InvalidLearningDataset(f"{name}.content_hash must be a lowercase SHA-256")


def _numeric_section(
    section: Mapping[str, object],
    *,
    prediction_time: datetime,
    section_name: str,
    require_value_envelopes: bool,
    legacy_six_decimal_envelope_keys: frozenset[str] = frozenset(),
) -> dict[str, float | None]:
    raw_features = _mapping(section.get("features"))
    masks = _mapping(section.get("feature_masks"))
    values = _mapping(section.get("values"))
    output: dict[str, float | None] = {}
    for raw_key, raw_value in raw_features.items():
        key = str(raw_key)
        available = int(bool(masks.get(key, 0)))
        number = _finite(raw_value)
        if available == 0:
            output[key] = None
            continue
        if number is None:
            raise InvalidLearningDataset(f"{section_name}.{key} is masked available but not finite")
        envelope = _mapping(values.get(key))
        if require_value_envelopes and not envelope:
            raise InvalidLearningDataset(f"{section_name}.{key} lacks PIT provenance")
        if envelope:
            _available_no_later_than(
                envelope,
                prediction_time,
                f"{section_name}.{key}",
            )
            envelope_value = _finite(envelope.get("value"))
            exact_match = envelope_value is not None and number == envelope_value
            legacy_rounded_match = (
                envelope_value is not None
                and key in legacy_six_decimal_envelope_keys
                and number == round(envelope_value, 6)
            )
            if not exact_match and not legacy_rounded_match:
                raise InvalidLearningDataset(
                    f"{section_name}.{key} does not match its PIT envelope value"
                )
            assert envelope_value is not None
            number = (
                round(envelope_value, 6)
                if key in legacy_six_decimal_envelope_keys
                else envelope_value
            )
        output[key] = number
    return output


def _iter_mapping_keys(value: object, path: str = "") -> Sequence[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            nested_path = f"{path}.{key}" if path else key
            found.append((key.lower(), nested_path))
            found.extend(_iter_mapping_keys(nested, nested_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, nested in enumerate(value):
            found.extend(_iter_mapping_keys(nested, f"{path}[{index}]"))
    return found


def _reject_post_outcome_features(row: Mapping[str, Any]) -> None:
    for section_name in (
        "features",
        "input_features",
        "input_feature_masks",
        "execution_snapshot",
        "input_context",
        "learning_dimensions",
        "portfolio_risk_snapshot",
    ):
        for key, path in _iter_mapping_keys(row.get(section_name), section_name):
            if any(token in key for token in _POST_OUTCOME_INPUT_TOKENS):
                raise InvalidLearningDataset(f"post-outcome field leaked into model inputs: {path}")


def _market_regime_features(
    row: Mapping[str, Any],
    context: Mapping[str, object],
    prediction_time: datetime,
) -> tuple[dict[str, float | None], bool]:
    if not context:
        return {}, False
    context_time = _aware(context.get("decision_time"), "input_context.decision_time")
    if context_time > prediction_time:
        raise InvalidLearningDataset("input context follows prediction_time")
    dimensions = _mapping(row.get("learning_dimensions")) or _mapping(
        context.get("learning_dimensions")
    )
    regime = str(dimensions.get("regime") or "unknown").strip().lower()
    session = str(dimensions.get("session_bucket") or "unknown").strip().lower()
    features: dict[str, float | None] = {}
    for name in ("risk_on", "risk_off", "neutral", "unknown"):
        features[f"regime__{name}"] = float(regime == name)
    for name in (
        "tokyo",
        "london",
        "new_york",
        "tokyo_london_overlap",
        "london_new_york_overlap",
        "off_session",
        "closed",
        "unknown",
    ):
        features[f"session__{name}"] = float(session == name)
    return features, regime not in {"", "unknown"} and session not in {"", "unknown"}


def _liquidity_features(
    context: Mapping[str, object],
    prediction_time: datetime,
    symbol: str,
) -> tuple[dict[str, float | None], bool]:
    liquidity = _mapping(context.get("liquidity"))
    if not liquidity:
        return {}, False
    if liquidity.get("schema_version") != "fx-liquidity-proxy-v1":
        raise InvalidLearningDataset("liquidity snapshot contract mismatch")
    quote = _mapping(liquidity.get("quote"))
    if not quote:
        return {}, False
    quote_available = _aware(
        quote.get("available_time"),
        "input_context.liquidity.quote.available_time",
    )
    quote_observed = _aware(
        quote.get("observed_at"),
        "input_context.liquidity.quote.observed_at",
    )
    quote_ingested = _aware(
        quote.get("ingested_time"),
        "input_context.liquidity.quote.ingested_time",
    )
    if not quote_observed <= quote_available <= quote_ingested <= prediction_time:
        raise InvalidLearningDataset("liquidity quote follows prediction_time")
    for field in ("source", "source_record_id"):
        if not str(quote.get(field) or "").strip():
            raise InvalidLearningDataset(f"liquidity quote {field} is required")
    content_hash = str(quote.get("content_hash") or "")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise InvalidLearningDataset("liquidity quote content_hash must be a SHA-256")
    bid = _finite(quote.get("bid"))
    ask = _finite(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        raise InvalidLearningDataset("liquidity quote bid/ask is invalid")
    raw_values = _numeric_section(
        liquidity,
        prediction_time=prediction_time,
        section_name="liquidity",
        require_value_envelopes=False,
    )
    spread = ask - bid
    mid = (ask + bid) / 2.0
    pip = 0.01 if symbol.upper().endswith("JPY") else 0.0001
    expected = {
        "spread_price": spread,
        "spread_pips": spread / pip,
        "spread_bps": spread / mid * 10_000.0,
        "quote_age_sec": (prediction_time - quote_available).total_seconds(),
    }
    features: dict[str, float | None] = {}
    for key, expected_value in expected.items():
        actual_value = raw_values.get(key)
        if actual_value is None:
            continue
        if not math.isclose(
            float(actual_value),
            expected_value,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise InvalidLearningDataset(f"liquidity.{key} does not match the decision quote")
        features[f"liquidity__{key}"] = float(actual_value)
    quality_status = str(quote.get("quality_status") or "").lower()
    return (
        features,
        "liquidity__spread_price" in features
        and quality_status in {"measured", "measured_not_tradable", "proxy"},
    )


def _macro_surprise_features(
    context: Mapping[str, object],
    prediction_time: datetime,
) -> tuple[dict[str, float | None], bool]:
    section = _mapping(context.get("macro_surprises"))
    if not section:
        return {}, False
    if section.get("revision_policy") != "first_release_only":
        raise InvalidLearningDataset("macro surprise must use first-release values")
    values = _numeric_section(
        section,
        prediction_time=prediction_time,
        section_name="macro_surprises",
        require_value_envelopes=True,
    )
    features = {f"macro_surprise__{key}": value for key, value in values.items()}
    available = any(value is not None for key, value in values.items() if key.endswith("_z"))
    return features, available


def _cross_asset_features(
    context: Mapping[str, object],
    prediction_time: datetime,
) -> tuple[dict[str, float | None], bool]:
    section = _mapping(context.get("macro"))
    if not section:
        return {}, False
    values = _numeric_section(
        section,
        prediction_time=prediction_time,
        section_name="macro",
        require_value_envelopes=True,
        legacy_six_decimal_envelope_keys=_LEGACY_SIX_DECIMAL_MACRO_KEYS,
    )
    for key, value in values.items():
        if value is None or key not in _CROSS_ASSET_RANGES:
            continue
        lower, upper = _CROSS_ASSET_RANGES[key]
        if not lower <= value <= upper:
            raise InvalidLearningDataset(f"macro.{key} is outside its plausible range")
    selected = {
        f"cross_asset__{key}": value for key, value in values.items() if key in _CROSS_ASSET_KEYS
    }
    available_count = sum(value is not None for value in selected.values())
    return selected, available_count >= 2


def _portfolio_risk_features(
    row: Mapping[str, Any],
    prediction_time: datetime,
) -> tuple[dict[str, float | None], bool]:
    snapshot = _mapping(row.get("portfolio_risk_snapshot"))
    if not snapshot:
        return {}, False
    if snapshot.get("contract") != "virtual-portfolio-risk-snapshot-v1":
        raise InvalidLearningDataset("portfolio risk snapshot contract mismatch")
    as_of = _aware(snapshot.get("as_of"), "portfolio_risk_snapshot.as_of")
    knowledge = _aware(
        snapshot.get("knowledge_as_of"),
        "portfolio_risk_snapshot.knowledge_as_of",
    )
    if max(as_of, knowledge) > prediction_time:
        raise InvalidLearningDataset("portfolio risk snapshot follows prediction_time")
    claimed = str(snapshot.get("snapshot_sha256") or "")
    unhashed = {str(key): value for key, value in snapshot.items() if key != "snapshot_sha256"}
    if claimed != _sha256(unhashed):
        raise InvalidLearningDataset("portfolio risk snapshot hash mismatch")
    policy_sha = str(snapshot.get("policy_sha256") or "")
    if len(policy_sha) != 64 or any(
        character not in "0123456789abcdef" for character in policy_sha
    ):
        raise InvalidLearningDataset("portfolio risk snapshot lacks policy provenance")
    source_record_ids = snapshot.get("source_record_ids")
    source_record_sha256 = snapshot.get("source_record_sha256")
    if (
        not isinstance(source_record_ids, Sequence)
        or isinstance(source_record_ids, (str, bytes))
        or not source_record_ids
        or not isinstance(source_record_sha256, Sequence)
        or isinstance(source_record_sha256, (str, bytes))
        or not source_record_sha256
    ):
        raise InvalidLearningDataset("portfolio risk snapshot lacks source provenance")
    for source_hash in source_record_sha256:
        value = str(source_hash)
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise InvalidLearningDataset(
                "portfolio risk source provenance contains a non-SHA value"
            )
    capital = _finite(snapshot.get("initial_capital_jpy"))
    if capital is None or capital <= 0:
        raise InvalidLearningDataset("portfolio initial capital must be positive")
    features = {
        "portfolio__cash_ratio": (
            cash / capital
            if (cash := _finite(snapshot.get("cash_balance_jpy"))) is not None
            else None
        ),
        "portfolio__cash_drawdown_ratio": _finite(snapshot.get("cash_drawdown_ratio")),
        "portfolio__open_position_count": _finite(snapshot.get("open_position_count")),
        "portfolio__open_planned_risk_ratio": _finite(snapshot.get("open_planned_risk_ratio")),
    }
    for raw_period, raw_value in _mapping(snapshot.get("period_pnl_jpy")).items():
        period = str(raw_period)
        features[f"portfolio__{period}_pnl_ratio"] = (
            number / capital if (number := _finite(raw_value)) is not None else None
        )
    for raw_period, raw_value in _mapping(snapshot.get("remaining_loss_budget_jpy")).items():
        period = str(raw_period)
        features[f"portfolio__{period}_remaining_loss_budget_ratio"] = (
            number / capital if (number := _finite(raw_value)) is not None else None
        )
    available = all(
        features.get(key) is not None
        for key in (
            "portfolio__cash_ratio",
            "portfolio__cash_drawdown_ratio",
            "portfolio__open_position_count",
            "portfolio__open_planned_risk_ratio",
        )
    )
    return features, available


def _prediction_signal_features(row: Mapping[str, Any]) -> dict[str, float | None]:
    features: dict[str, float | None] = {}
    for raw_key, raw_value in _mapping(row.get("features")).items():
        normalized_key = str(raw_key)
        if normalized_key not in _PREDICTION_FEATURE_ALLOWLIST:
            continue
        number = _finite(raw_value)
        if number is not None:
            features[f"signal__{normalized_key}"] = number
    direction = str(row.get("direction") or "unknown").lower()
    for name in ("long", "short", "unknown"):
        features[f"direction__{name}"] = float(direction == name)
    return features


def _execution_cost_r(row: Mapping[str, Any]) -> float:
    attribution = _mapping(row.get("attribution"))
    costs = [
        _finite(attribution.get(field))
        for field in (
            "spread_quote_cost_jpy",
            "slippage_cost_jpy",
            "commission_jpy",
            "financing_jpy",
            "conversion_cost_jpy",
        )
    ]
    if any(value is None for value in costs):
        raise InvalidLearningDataset("execution attribution contains a non-finite cost")
    total_cost = sum(value for value in costs if value is not None)
    planned_risk = float(row["planned_risk_jpy"])
    if planned_risk <= 0:
        raise InvalidLearningDataset("planned_risk_jpy must be positive")
    return total_cost / planned_risk


def _extract_row(row: Mapping[str, Any]) -> dict[str, Any]:
    _reject_post_outcome_features(row)
    prediction_time = row["_prediction_time"]
    if not isinstance(prediction_time, datetime):
        raise InvalidLearningDataset("validated prediction timestamp is unavailable")
    context = _mapping(row.get("input_context"))
    regime_features, regime_available = _market_regime_features(
        row,
        context,
        prediction_time,
    )
    liquidity_features, liquidity_available = _liquidity_features(
        context,
        prediction_time,
        str(row.get("symbol") or ""),
    )
    surprise_features, surprise_available = _macro_surprise_features(
        context,
        prediction_time,
    )
    cross_features, cross_available = _cross_asset_features(
        context,
        prediction_time,
    )
    portfolio_features, portfolio_available = _portfolio_risk_features(
        row,
        prediction_time,
    )
    flat = _prediction_signal_features(row)
    flat.update(regime_features)
    flat.update(liquidity_features)
    flat.update(surprise_features)
    flat.update(cross_features)
    flat.update(portfolio_features)
    return {
        "row": row,
        "features": flat,
        "axes": {
            "net_return_distribution": True,
            "market_regime": regime_available,
            "liquidity_proxy": liquidity_available,
            "macro_surprise": surprise_available,
            "cross_asset_relationship": cross_available,
            "uncertainty": True,
            "execution_quality": True,
            "portfolio_risk": portfolio_available,
        },
        "net_r": float(row["realized_net_r"]),
        "profitable": int(float(row["realized_net_r"]) > 0.0),
        "execution_cost_r": _execution_cost_r(row),
    }


def _axis_status(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, object]]:
    total = len(rows)
    result: dict[str, dict[str, object]] = {}
    for axis, definition in AXIS_DEFINITIONS.items():
        available = sum(bool(_mapping(row.get("axes")).get(axis)) for row in rows)
        coverage = available / total if total else 0.0
        required = axis in REQUIRED_INPUT_AXES
        result[axis] = {
            **definition,
            "available_rows": available,
            "development_rows": total,
            "coverage": round(coverage, 4),
            "minimum_coverage": MIN_AXIS_COVERAGE if required else None,
            "ready": (
                coverage >= MIN_AXIS_COVERAGE if required else total > 0 and available == total
            ),
        }
    result["liquidity_proxy"]["claim_boundary"] = "public/broker quote proxy; not dealer order flow"
    result["execution_quality"][
        "feature_policy"
    ] = "realized costs are auxiliary labels only, never prediction features"
    return result


def _feature_schema(
    train_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], dict[str, float]]:
    names = sorted({str(key) for row in train_rows for key in _mapping(row.get("features"))})
    medians: dict[str, float] = {}
    for name in names:
        values = [
            number
            for row in train_rows
            if (number := _finite(_mapping(row.get("features")).get(name))) is not None
        ]
        medians[name] = statistics.median(values) if values else 0.0
    return names, medians


def _vectorize(
    rows: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    medians: Mapping[str, float],
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for row in rows:
        raw = _mapping(row.get("features"))
        values: list[float] = []
        masks: list[float] = []
        for name in names:
            number = _finite(raw.get(name))
            values.append(float(medians[name]) if number is None else number)
            masks.append(float(number is None))
        vectors.append([*values, *masks])
    return vectors


class _TreeParameters(TypedDict):
    n_estimators: int
    learning_rate: float
    max_depth: int
    min_samples_leaf: int
    subsample: float
    feature_fraction: float
    reg_lambda: float
    early_stopping_rounds: int
    seed: int


def _tree_parameters(
    depth: int,
    learning_rate: float,
    seed: int,
) -> _TreeParameters:
    return {
        "n_estimators": 120,
        "learning_rate": learning_rate,
        "max_depth": depth,
        "min_samples_leaf": 5,
        "subsample": 0.8,
        "feature_fraction": 0.9,
        "reg_lambda": 1.0,
        "early_stopping_rounds": 20,
        "seed": seed,
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _empirical_quantile(values: Sequence[float], level: float) -> float:
    if not values or not 0 < level < 1:
        raise InvalidLearningDataset("empirical quantile requires values and 0 < level < 1")
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(level * len(ordered)) - 1))
    return ordered[index]


def _coverage_interval(successes: int, total: int) -> dict[str, float]:
    """Wilson interval used only as a descriptive dependent-data diagnostic."""

    if total <= 0:
        raise InvalidLearningDataset("coverage interval requires observations")
    z = 1.959963984540054
    estimate = successes / total
    denominator = 1 + z * z / total
    center = (estimate + z * z / (2 * total)) / denominator
    half = (
        z * math.sqrt(estimate * (1 - estimate) / total + z * z / (4 * total * total)) / denominator
    )
    return {
        "lower_95": max(0.0, center - half),
        "upper_95": min(1.0, center + half),
    }


def _interval_score(actual: float, lower: float, upper: float, alpha: float) -> float:
    score = upper - lower
    if actual < lower:
        score += (2.0 / alpha) * (lower - actual)
    elif actual > upper:
        score += (2.0 / alpha) * (actual - upper)
    return score


def summarize_multi_axis_readiness(
    learning_payload: Mapping[str, object],
    *,
    as_of: datetime,
    embargo_hours: int = 24,
) -> dict[str, object]:
    """Return coverage-only state for read-only monitoring; no model is fitted."""

    splits = prepare_temporal_learning_splits(
        learning_payload,
        as_of=as_of,
        embargo_hours=embargo_hours,
    )
    development = [
        _extract_row(row)
        for name in ("train", "tune", "calibration", "test")
        for row in splits[name]
    ]
    axes = _axis_status(development)
    unavailable = [axis for axis in REQUIRED_INPUT_AXES if axes[axis]["ready"] is not True]
    return {
        "contract": MULTI_AXIS_CONTRACT,
        "status": "axis_inputs_ready" if not unavailable else "collecting_axis_inputs",
        "development_row_count": len(development),
        "lockbox_row_count": 0,
        "rolling_holdout_row_count": len(splits["lockbox"]),
        "axes": axes,
        "unavailable_axes": unavailable,
        "research_only": True,
        "model_usable_for_decisions": False,
        "lockbox_access": "unavailable_no_fixed_lockbox_commitment",
    }


def build_multi_axis_shadow_model(
    learning_payload: Mapping[str, object],
    manifest: Mapping[str, object],
    *,
    as_of: datetime | None = None,
    seed: int = 20_260_731,
) -> dict[str, object]:
    """Train a research challenger or explain why its fail-closed gates remain shut."""

    raw_moment = as_of or datetime.now(UTC)
    if raw_moment.tzinfo is None or raw_moment.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    moment = raw_moment.astimezone(UTC)
    manifest_generated_at = _aware(
        manifest.get("generated_at"),
        "manifest.generated_at",
    )
    if manifest_generated_at > moment:
        raise InvalidLearningDataset("manifest was generated after artifact as_of")
    raw_embargo = _finite(manifest.get("embargo_hours"))
    embargo_hours = int(raw_embargo) if raw_embargo is not None else 24
    raw_minimum_rows = _finite(manifest.get("minimum_rows"))
    if raw_minimum_rows is None:
        raise InvalidLearningDataset("manifest minimum_rows is required")
    expected_manifest = build_challenger_manifest(
        learning_payload,
        as_of=manifest_generated_at,
        minimum_rows=int(raw_minimum_rows),
        embargo_hours=embargo_hours,
    )
    if _canonical_json(expected_manifest) != _canonical_json(manifest):
        raise InvalidLearningDataset(
            "manifest does not match the current dataset and temporal splits"
        )
    splits = prepare_temporal_learning_splits(
        learning_payload,
        as_of=manifest_generated_at,
        embargo_hours=embargo_hours,
    )
    extracted = {
        name: [_extract_row(row) for row in splits[name]]
        for name in ("train", "tune", "calibration", "test")
    }
    development = [
        row for name in ("train", "tune", "calibration", "test") for row in extracted[name]
    ]
    axes = _axis_status(development)
    training_axis_gate = _axis_status(extracted["train"])
    base: dict[str, object] = {
        "contract": MULTI_AXIS_CONTRACT,
        "generated_at": moment.isoformat(),
        "source_dataset_sha256": str(
            expected_manifest.get("ordered_dataset_sha256")
            or learning_payload.get("dataset_sha256")
            or ""
        ),
        "manifest_sha256": str(expected_manifest.get("manifest_sha256") or ""),
        "research_only": True,
        "promotion_eligible": False,
        "execution_target": "shadow_decision_only",
        "broker_connected": False,
        "lockbox_access": "unavailable_no_fixed_lockbox_commitment",
        "lockbox_isolation": "rolling_holdout_only_not_a_lockbox",
        "lockbox_row_count": 0,
        "rolling_holdout_row_count": len(splits["lockbox"]),
        "daily_target_used_as_feature": False,
        "daily_target_used_as_label": False,
        "components_used_as_features": False,
        "axes": axes,
        "training_axis_gate": training_axis_gate,
        "axis_gate_basis": "train_only",
        "model_usable_for_decisions": False,
        "automatic_policy_update": False,
    }
    unavailable_axes = [
        axis for axis in REQUIRED_INPUT_AXES if training_axis_gate[axis]["ready"] is not True
    ]
    if manifest.get("eligible_for_candidate_training") is not True:
        base.update(
            {
                "status": "insufficient_data",
                "reason": str(manifest.get("reason") or "candidate training gate is closed"),
                "unavailable_axes": unavailable_axes,
                "feature_schema": {},
                "models": {},
                "rolling_test_diagnostics": {},
            }
        )
        base["artifact_sha256"] = _sha256(base)
        return base
    if unavailable_axes:
        base.update(
            {
                "status": "insufficient_axis_coverage",
                "reason": ("判断時点PIT入力のcoverage不足: " + ", ".join(unavailable_axes)),
                "unavailable_axes": unavailable_axes,
                "feature_schema": {},
                "models": {},
                "rolling_test_diagnostics": {},
            }
        )
        base["artifact_sha256"] = _sha256(base)
        return base

    train = extracted["train"]
    tune = extracted["tune"]
    calibration = extracted["calibration"]
    test = extracted["test"]
    if any(not rows for rows in (train, tune, calibration, test)):
        raise InvalidLearningDataset("a development split is empty after purge/embargo")
    names, medians = _feature_schema(train)
    if not names:
        raise InvalidLearningDataset("multi-axis feature schema is empty")
    x_train = _vectorize(train, names, medians)
    x_tune = _vectorize(tune, names, medians)
    x_calibration = _vectorize(calibration, names, medians)
    x_test = _vectorize(test, names, medians)
    y_train_class = [int(row["profitable"]) for row in train]
    y_tune_class = [int(row["profitable"]) for row in tune]
    y_calibration_class = [int(row["profitable"]) for row in calibration]
    y_test_class = [int(row["profitable"]) for row in test]
    class_support = {
        name: {
            "negative": labels.count(0),
            "positive": labels.count(1),
        }
        for name, labels in (
            ("train", y_train_class),
            ("tune", y_tune_class),
            ("calibration", y_calibration_class),
            ("test", y_test_class),
        )
    }
    unsupported_splits = [
        name
        for name, counts in class_support.items()
        if min(counts["negative"], counts["positive"]) < 5
    ]
    if unsupported_splits:
        base.update(
            {
                "status": "insufficient_class_support",
                "reason": (
                    "binary probability/calibration evaluation requires at least "
                    "5 positive and 5 negative outcomes in every development split: "
                    + ", ".join(unsupported_splits)
                ),
                "unavailable_axes": [],
                "class_support": class_support,
                "feature_schema": {},
                "models": {},
                "rolling_test_diagnostics": {},
            }
        )
        base["artifact_sha256"] = _sha256(base)
        return base

    trial_grid = ((2, 0.05), (3, 0.03))
    classifier_trials: list[tuple[float, int, float, GradientBoostingClassifier]] = []
    mean_trials: list[tuple[float, int, float, GradientBoostingRegressor]] = []
    trial_ledger: list[dict[str, object]] = []
    tune_trade_ids = [str(row["row"]["trade_id"]) for row in tune]
    y_train_r = [float(row["net_r"]) for row in train]
    y_tune_r = [float(row["net_r"]) for row in tune]
    for trial_index, (depth, learning_rate) in enumerate(trial_grid):
        classifier_seed = seed + trial_index
        classifier_parameters = _tree_parameters(
            depth,
            learning_rate,
            classifier_seed,
        )
        classifier = GradientBoostingClassifier(**classifier_parameters).fit(
            x_train, y_train_class, x_tune, y_tune_class
        )
        tune_probability = classifier.predict_proba_many(x_tune)
        classifier_metric = log_loss(y_tune_class, tune_probability)
        classifier_trials.append(
            (
                classifier_metric,
                depth,
                learning_rate,
                classifier,
            )
        )
        trial_ledger.append(
            {
                "trial_id": f"classifier-{trial_index}",
                "head": "profit_probability",
                "status": "completed",
                "parameters": classifier_parameters,
                "model_sha256": _sha256(classifier.to_dict()),
                "tune_metric": {"log_loss": classifier_metric},
                "tune_trade_ids_sha256": _sha256(tune_trade_ids),
                "tune_predictions": tune_probability,
            }
        )
        mean_seed = seed + 100 + trial_index
        mean_parameters = _tree_parameters(
            depth,
            learning_rate,
            mean_seed,
        )
        mean_model = GradientBoostingRegressor(
            objective="squared",
            **mean_parameters,
        ).fit(x_train, y_train_r, x_tune, y_tune_r)
        mean_tune_predictions = mean_model.predict_many(x_tune)
        mean_metric = rmse(y_tune_r, mean_tune_predictions)
        mean_trials.append(
            (
                mean_metric,
                depth,
                learning_rate,
                mean_model,
            )
        )
        trial_ledger.append(
            {
                "trial_id": f"mean-net-r-{trial_index}",
                "head": "mean_net_r",
                "status": "completed",
                "parameters": mean_parameters,
                "model_sha256": _sha256(mean_model.to_dict()),
                "tune_metric": {"rmse": mean_metric},
                "tune_trade_ids_sha256": _sha256(tune_trade_ids),
                "tune_predictions": mean_tune_predictions,
            }
        )
    classifier_loss, classifier_depth, classifier_rate, classifier = min(
        classifier_trials,
        key=lambda item: item[0],
    )
    mean_loss, mean_depth, mean_rate, mean_model = min(
        mean_trials,
        key=lambda item: item[0],
    )

    calibration_margins = [classifier.predict_margin(row) for row in x_calibration]
    calibration_model = platt_calibrate(
        calibration_margins,
        y_calibration_class,
    )
    test_probabilities = [calibration_model.apply(classifier.predict_margin(row)) for row in x_test]
    calibration_base_rate = _mean(y_calibration_class)
    baseline_probabilities = [calibration_base_rate] * len(test)

    quantiles: dict[str, GradientBoostingRegressor] = {}
    quantile_levels = {"p10": 0.10, "p50": 0.50, "p90": 0.90}
    for offset, (name, quantile) in enumerate(quantile_levels.items()):
        quantile_parameters = _tree_parameters(
            mean_depth,
            mean_rate,
            seed + 200 + offset,
        )
        quantile_model = GradientBoostingRegressor(
            objective="quantile",
            quantile=quantile,
            **quantile_parameters,
        ).fit(x_train, y_train_r, x_tune, y_tune_r)
        quantiles[name] = quantile_model
        quantile_tune_predictions = quantile_model.predict_many(x_tune)
        trial_ledger.append(
            {
                "trial_id": f"net-r-{name}",
                "head": f"net_return_{name}",
                "status": "completed",
                "parameters": {
                    **quantile_parameters,
                    "objective": "quantile",
                    "quantile": quantile,
                },
                "model_sha256": _sha256(quantile_model.to_dict()),
                "tune_metric": {
                    "pinball": pinball_loss(
                        y_tune_r,
                        quantile_tune_predictions,
                        quantile,
                    )
                },
                "tune_trade_ids_sha256": _sha256(tune_trade_ids),
                "tune_predictions": quantile_tune_predictions,
            }
        )

    calibration_quantiles = {
        name: model.predict_many(x_calibration) for name, model in quantiles.items()
    }
    interval_alpha = 0.20
    conformity_scores: list[float] = []
    for p10, p90, actual in zip(
        calibration_quantiles["p10"],
        calibration_quantiles["p90"],
        [float(row["net_r"]) for row in calibration],
        strict=True,
    ):
        raw_lower, raw_upper = sorted((p10, p90))
        conformity_scores.append(max(raw_lower - actual, actual - raw_upper, 0.0))
    conformal_level = min(
        1.0,
        math.ceil((len(conformity_scores) + 1) * (1.0 - interval_alpha)) / len(conformity_scores),
    )
    conformal_adjustment = _empirical_quantile(
        conformity_scores,
        conformal_level,
    )

    execution_train = [float(row["execution_cost_r"]) for row in train]
    execution_tune = [float(row["execution_cost_r"]) for row in tune]
    execution_parameters = _tree_parameters(mean_depth, mean_rate, seed + 300)
    execution_model = GradientBoostingRegressor(
        objective="squared",
        **execution_parameters,
    ).fit(x_train, execution_train, x_tune, execution_tune)
    execution_tune_predictions = execution_model.predict_many(x_tune)
    trial_ledger.append(
        {
            "trial_id": "execution-cost-r",
            "head": "execution_cost_r",
            "status": "completed",
            "parameters": execution_parameters,
            "model_sha256": _sha256(execution_model.to_dict()),
            "tune_metric": {
                "rmse": rmse(execution_tune, execution_tune_predictions),
            },
            "tune_trade_ids_sha256": _sha256(tune_trade_ids),
            "tune_predictions": execution_tune_predictions,
        }
    )

    y_test_r = [float(row["net_r"]) for row in test]
    mean_predictions = mean_model.predict_many(x_test)
    quantile_predictions = {name: model.predict_many(x_test) for name, model in quantiles.items()}
    crossing_count = 0
    lower: list[float] = []
    median: list[float] = []
    upper: list[float] = []
    for p10, p50, p90 in zip(
        quantile_predictions["p10"],
        quantile_predictions["p50"],
        quantile_predictions["p90"],
        strict=True,
    ):
        crossing_count += int(not (p10 <= p50 <= p90))
        ordered = sorted((p10, p50, p90))
        lower.append(ordered[0])
        median.append(ordered[1])
        upper.append(ordered[2])
    calibrated_lower = [value - conformal_adjustment for value in lower]
    calibrated_upper = [value + conformal_adjustment for value in upper]
    coverage_hits = [
        int(low <= actual <= high)
        for low, actual, high in zip(
            calibrated_lower,
            y_test_r,
            calibrated_upper,
            strict=True,
        )
    ]
    interval_coverage = _mean(coverage_hits)
    interval_width = _mean(
        [high - low for low, high in zip(calibrated_lower, calibrated_upper, strict=True)]
    )
    interval_scores = [
        _interval_score(actual, low, high, interval_alpha)
        for actual, low, high in zip(
            y_test_r,
            calibrated_lower,
            calibrated_upper,
            strict=True,
        )
    ]
    baseline_quantiles = {
        name: _empirical_quantile(y_train_r, level) for name, level in quantile_levels.items()
    }
    baseline_lower = baseline_quantiles["p10"]
    baseline_upper = baseline_quantiles["p90"]
    baseline_coverage_hits = [
        int(baseline_lower <= actual <= baseline_upper) for actual in y_test_r
    ]
    execution_test = [float(row["execution_cost_r"]) for row in test]
    execution_predictions = execution_model.predict_many(x_test)
    rolling_test_diagnostics = {
        "profit_probability": {
            "brier": brier_score(y_test_class, test_probabilities),
            "baseline_brier": brier_score(y_test_class, baseline_probabilities),
            "log_loss": log_loss(y_test_class, test_probabilities),
            "baseline_log_loss": log_loss(y_test_class, baseline_probabilities),
            "calibration_base_rate": calibration_base_rate,
        },
        "net_return_distribution": {
            "mean_rmse": rmse(y_test_r, mean_predictions),
            "train_mean_baseline_rmse": rmse(
                y_test_r,
                [_mean(y_train_r)] * len(y_test_r),
            ),
            "pinball": {
                name: pinball_loss(
                    y_test_r,
                    quantile_predictions[name],
                    quantile,
                )
                for name, quantile in quantile_levels.items()
            },
            "train_empirical_quantile_baseline_pinball": {
                name: pinball_loss(
                    y_test_r,
                    [baseline_quantiles[name]] * len(y_test_r),
                    quantile,
                )
                for name, quantile in quantile_levels.items()
            },
            "p10_p90_interval_coverage": interval_coverage,
            "p10_p90_interval_coverage_wilson_95": _coverage_interval(
                sum(coverage_hits),
                len(coverage_hits),
            ),
            "p10_p90_mean_width_r": interval_width,
            "p10_p90_mean_interval_score": _mean(interval_scores),
            "train_empirical_interval_baseline": {
                "coverage": _mean(baseline_coverage_hits),
                "coverage_wilson_95": _coverage_interval(
                    sum(baseline_coverage_hits),
                    len(baseline_coverage_hits),
                ),
                "mean_width_r": baseline_upper - baseline_lower,
                "mean_interval_score": _mean(
                    [
                        _interval_score(
                            actual,
                            baseline_lower,
                            baseline_upper,
                            interval_alpha,
                        )
                        for actual in y_test_r
                    ]
                ),
            },
            "raw_quantile_crossing_rate": crossing_count / len(test),
            "serving_postprocess": "monotone_rearrangement",
            "interval_calibration": {
                "method": "split_conformalized_quantile_interval",
                "alpha": interval_alpha,
                "calibration_rows": len(calibration),
                "conformal_level": conformal_level,
                "adjustment_r": conformal_adjustment,
                "limitation": (
                    "marginal finite-sample coverage assumes exchangeability; "
                    "serial/cross-pair dependence remains a validation gate"
                ),
            },
        },
        "execution_quality": {
            "cost_r_rmse": rmse(execution_test, execution_predictions),
            "train_mean_baseline_rmse": rmse(
                execution_test,
                [_mean(execution_train)] * len(execution_test),
            ),
        },
    }
    model_payload = {
        "classifier": classifier.to_dict(),
        "calibration": {
            "profit_probability": {
                "method": "platt",
                "scale": calibration_model.scale,
                "offset": calibration_model.offset,
                "iterations": calibration_model.iterations,
            },
            "net_return_interval": {
                "method": "split_conformalized_quantile_interval",
                "alpha": interval_alpha,
                "conformal_level": conformal_level,
                "adjustment_r": conformal_adjustment,
            },
        },
        "mean_net_r": mean_model.to_dict(),
        "quantiles": {name: model.to_dict() for name, model in quantiles.items()},
        "execution_cost_r": execution_model.to_dict(),
    }
    base.update(
        {
            "status": "trained_shadow_candidate",
            "reason": (
                "8軸のresearch-only候補を学習済み。PBO/DSR/CPCV/lockbox/独立レビュー"
                "未完了のため判断には不使用"
            ),
            "unavailable_axes": [],
            "feature_schema": {
                "raw_feature_names": names,
                "model_feature_names": [
                    *names,
                    *(f"{name}__missing" for name in names),
                ],
                "train_only_medians": medians,
                "feature_count": len(names) * 2,
                "missing_policy": "train-median plus explicit missing indicator",
            },
            "selection": {
                "classifier": {
                    "tune_log_loss": classifier_loss,
                    "max_depth": classifier_depth,
                    "learning_rate": classifier_rate,
                },
                "mean_net_r": {
                    "tune_rmse": mean_loss,
                    "max_depth": mean_depth,
                    "learning_rate": mean_rate,
                },
                "selection_trial_count": len(trial_grid) * 2,
                "all_head_trial_count": len(trial_ledger),
                "selection_window": "tune_only",
            },
            "trial_ledger": trial_ledger,
            "class_support": class_support,
            "models": model_payload,
            "rolling_test_diagnostics": rolling_test_diagnostics,
            "evaluation_scope": (
                "rolling development test diagnostics; not a fixed final test "
                "and not promotion evidence"
            ),
            "fixed_test_access": "unavailable_no_fixed_test_commitment",
            "remaining_validation_gates": {
                "pbo": "unavailable_no_all-trial_return_matrix",
                "deflated_sharpe": "unavailable_no_all-trial_return_ledger",
                "cpcv": "unavailable",
                "cost_stress": "separate validation_evidence artifact required",
                "fixed_test": "unavailable_no_fixed_test_commitment",
                "lockbox": "unavailable_no_fixed_lockbox_commitment",
                "independent_review": "required",
            },
        }
    )
    base["artifact_sha256"] = _sha256(base)
    return base


__all__ = [
    "AXIS_DEFINITIONS",
    "MIN_AXIS_COVERAGE",
    "MULTI_AXIS_CONTRACT",
    "build_multi_axis_shadow_model",
    "summarize_multi_axis_readiness",
]
