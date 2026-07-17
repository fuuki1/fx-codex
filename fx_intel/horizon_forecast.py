"""Pure generator for the design-A symbol x horizon forecast track."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .briefing import (
    CALENDAR_UNKNOWN_CONVICTION_CAP,
    DIRECTION_THRESHOLD,
    MIN_QUALITY_FOR_DIRECTION,
    NEWS_FULL_COVERAGE_COUNT,
    QUALITY_CALENDAR_WEIGHT,
    QUALITY_NEWS_WEIGHT,
    QUALITY_TECH_WEIGHT,
    STANDBY_CONVICTION_CAP,
    _clip,
    _event_warnings,
)
from .calendar import RiskWindow, symbol_currencies
from .horizons import (
    ANALYSIS_TIMEFRAMES,
    HORIZON_SPECS,
    PRIOR_WEIGHTS,
    HorizonSpec,
    atr_for_horizon,
    flat_threshold,
    vol_bucket,
)
from .market import is_market_open
from .market_session import classify_market_session
from .news import NewsItem
from .sentiment import CurrencySentiment, pair_bias
from .technicals import PairTechnicals

GENERATOR_VERSION = "hf-1"
_DEFAULT_BAND_ATR = (-0.8, 0.0, 0.8)
_BASE_PROBS = (0.375, 0.375, 0.25)

ProfileLookup = Callable[[str, str], tuple[Mapping[str, float] | None, float]]
BandProvider = Callable[[str, str, str, str], tuple[float, float, float, str] | None]
CalibrationProvider = Callable[[str, str, float], tuple[float, float, float] | None]


@dataclass
class HorizonForecast:
    symbol: str
    horizon: str
    horizon_hours: float
    shadow_only: bool
    direction: str
    composite: float
    conviction: int
    p_up: float
    p_down: float
    p_flat: float
    calibrated: bool
    close: float | None
    atr_h: float | None
    spread: float | None
    flat_threshold: float
    band_p10: float | None
    band_p50: float | None
    band_p90: float | None
    band_source: str
    expected_range: float | None
    data_quality: float
    weights: dict[str, float] = field(default_factory=dict)
    features: dict[str, float | str] = field(default_factory=dict)
    feature_masks: dict[str, int] = field(default_factory=dict)
    gates: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    input_context_id: str = ""
    generator_version: str = GENERATOR_VERSION


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _context_parts(context: Mapping[str, object] | None) -> tuple[dict, dict, dict]:
    if not isinstance(context, Mapping):
        return {}, {}, {}
    macro = context.get("macro")
    liquidity = context.get("liquidity")
    dimensions = context.get("learning_dimensions")
    return (
        dict(macro) if isinstance(macro, Mapping) else {},
        dict(liquidity) if isinstance(liquidity, Mapping) else {},
        dict(dimensions) if isinstance(dimensions, Mapping) else {},
    )


def _freshness_from_context(context: Mapping[str, object] | None) -> tuple[bool, str]:
    """Fail closed on a missing, future, stale, or invalid decision quote."""
    _macro, liquidity, _dimensions = _context_parts(context)
    reasons = liquidity.get("reason_codes")
    reason_set = {str(value) for value in reasons} if isinstance(reasons, list) else set()
    status = str(liquidity.get("status", "unknown"))
    blocking = {"missing_quote", "missing_quote_time", "future_quote", "stale_quote"}
    if status == "invalid" or reason_set & blocking:
        return False, ",".join(sorted(reason_set & blocking)) or status
    quote = liquidity.get("quote")
    if not isinstance(quote, Mapping):
        return False, "missing_quote"
    return True, ""


def _spread_from_context(context: Mapping[str, object] | None) -> float | None:
    _macro, liquidity, _dimensions = _context_parts(context)
    features = liquidity.get("features")
    if isinstance(features, Mapping):
        return _number(features.get("spread_price"))
    return None


def _uncalibrated_probabilities(composite: float) -> tuple[float, float, float]:
    """Shrink a directional tilt toward the three-class prior until n >= 50."""
    p_up = max(0.05, min(0.90, _BASE_PROBS[0] + 0.25 * composite))
    p_down = max(0.05, min(0.90, _BASE_PROBS[1] - 0.25 * composite))
    p_flat = max(0.05, 1.0 - p_up - p_down)
    total = p_up + p_down + p_flat
    return tuple(round(value / total, 6) for value in (p_up, p_down, p_flat))  # type: ignore[return-value]


def _composite(
    tech: PairTechnicals, news_score: float, weights: Mapping[str, float]
) -> tuple[float, float, dict[str, float]]:
    available = {
        timeframe: tech.views[timeframe].score
        for timeframe in ANALYSIS_TIMEFRAMES
        if timeframe in tech.views
    }
    expected_tech = sum(weights.get(timeframe, 0.0) for timeframe in ANALYSIS_TIMEFRAMES)
    covered = sum(weights.get(timeframe, 0.0) for timeframe in available)
    coverage = covered / expected_tech if expected_tech > 0 else 0.0
    total = covered + weights.get("news", 0.0)
    if total <= 0:
        return 0.0, coverage, {}
    used: dict[str, float] = {}
    score = 0.0
    for timeframe, rating in available.items():
        weight = weights.get(timeframe, 0.0) / total
        used[f"w_{timeframe}"] = round(weight, 6)
        score += weight * rating
    news_weight = weights.get("news", 0.0) / total
    used["w_news"] = round(news_weight, 6)
    return _clip(score + news_weight * news_score), coverage, used


def _base_features(
    tech: PairTechnicals,
    news_score: float,
    news_count: int,
    now: datetime,
    context: Mapping[str, object] | None,
) -> tuple[dict[str, float | str], dict[str, int], float | None, str]:
    anchor = tech.views.get("1h") or tech.views.get("15m") or tech.views.get("4h")
    atr_pct = (
        anchor.atr / anchor.close * 100
        if anchor is not None and anchor.atr is not None and anchor.close
        else None
    )
    _macro, _liquidity, dimensions = _context_parts(context)
    session = str(dimensions.get("session_bucket") or classify_market_session(now)[0])
    features: dict[str, float | str] = {
        "news_score": round(news_score, 6),
        "news_count": float(news_count),
        "session": session,
        "vol_bucket": vol_bucket(atr_pct),
    }
    masks: dict[str, int] = {}
    for timeframe in ANALYSIS_TIMEFRAMES:
        view = tech.views.get(timeframe)
        if view is not None:
            features[f"rating_{timeframe}"] = round(view.score, 6)
    if anchor is not None:
        if anchor.rsi is not None:
            features["rsi"] = round(anchor.rsi, 2)
        if anchor.adx is not None:
            features["adx"] = round(anchor.adx, 2)
        if atr_pct is not None:
            features["atr_pct"] = round(atr_pct, 6)
        if (
            anchor.sma_fast is not None
            and anchor.sma_slow is not None
            and anchor.atr is not None
            and anchor.atr > 0
        ):
            features["ma_gap_atr"] = round((anchor.sma_fast - anchor.sma_slow) / anchor.atr, 6)
    return features, masks, atr_pct, session


def _add_macro_features(
    features: dict[str, float | str],
    masks: dict[str, int],
    context: Mapping[str, object] | None,
) -> None:
    macro, _liquidity, _dimensions = _context_parts(context)
    raw_features = macro.get("features")
    raw_masks = macro.get("feature_masks")
    if not isinstance(raw_features, Mapping):
        return
    for key in (
        "macro_pair_score",
        "macro_pair_confidence",
        "vix_level",
        "vix_change_5d_pct",
        "usd_index_change_5d_pct",
        "us10y_change_5d_bp",
        "curve_2s10s_bp",
        "cot_pair_diff",
    ):
        value = _number(raw_features.get(key))
        features[f"macro_{key}"] = round(value, 6) if value is not None else 0.0
        masks[f"macro_{key}"] = (
            int(raw_masks.get(key, 0)) if isinstance(raw_masks, Mapping) else int(value is not None)
        )


def build_horizon_forecasts(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping[str, CurrencySentiment],
    windows: Sequence[RiskWindow],
    news_items: Sequence[NewsItem],
    macro_features: Mapping[str, object] | None = None,
    now: datetime | None = None,
    *,
    calendar_ok: bool = True,
    operational_data_ok: bool | None = None,
    operational_data_reason: str = "",
    spread: float | None = None,
    specs: Sequence[HorizonSpec] = HORIZON_SPECS,
    profile_lookup: ProfileLookup | None = None,
    band_provider: BandProvider | None = None,
    calibration_provider: CalibrationProvider | None = None,
) -> list[HorizonForecast]:
    """Generate all nine horizons without network access or external writes."""
    now = now or datetime.now(UTC)
    base, quote = symbol_currencies(symbol)
    news_score = pair_bias(base, quote, currency_scores)
    relevant = [item for item in news_items if base in item.currencies or quote in item.currencies]
    news_coverage = min(1.0, len(relevant) / NEWS_FULL_COVERAGE_COUNT)
    base_features, base_masks, _atr_pct, session = _base_features(
        tech, news_score, len(relevant), now, macro_features
    )
    anchor = tech.views.get("1h") or tech.views.get("15m") or tech.views.get("4h")
    close = anchor.close if anchor is not None else None
    view_atrs = {
        timeframe: float(view.atr)
        for timeframe, view in tech.views.items()
        if view.atr is not None and view.atr > 0
    }
    if spread is None:
        spread = _spread_from_context(macro_features)
    if operational_data_ok is None:
        operational_data_ok, detected_reason = _freshness_from_context(macro_features)
        operational_data_reason = operational_data_reason or detected_reason
    event_window, event_warnings = _event_warnings(windows, now)
    market_open = is_market_open(now)

    forecasts: list[HorizonForecast] = []
    for spec in specs:
        weights: Mapping[str, float] = PRIOR_WEIGHTS[spec.label]
        conviction_factor = 1.0
        if profile_lookup is not None:
            learned, conviction_factor = profile_lookup(symbol, spec.label)
            if learned:
                weights = learned
        composite, tech_coverage, used_weights = _composite(tech, news_score, weights)
        quality = round(
            QUALITY_TECH_WEIGHT * tech_coverage
            + QUALITY_NEWS_WEIGHT * news_coverage
            + QUALITY_CALENDAR_WEIGHT * float(calendar_ok),
            3,
        )
        conviction = min(
            100, round(abs(composite) * 100 * quality * max(0.0, min(1.0, conviction_factor)))
        )
        warnings = list(event_warnings)
        if not calendar_ok:
            conviction = min(conviction, CALENDAR_UNKNOWN_CONVICTION_CAP)
        if not operational_data_ok:
            direction = "neutral"
            conviction = 0
            warnings.append(f"data freshness gate: {operational_data_reason or 'unverified'}")
        elif not market_open:
            direction = "closed"
            conviction = 0
        elif event_window:
            direction = "standby"
            conviction = min(conviction, STANDBY_CONVICTION_CAP)
        elif tech_coverage <= 0 or quality < MIN_QUALITY_FOR_DIRECTION:
            direction = "neutral"
        elif composite >= DIRECTION_THRESHOLD:
            direction = "long"
        elif composite <= -DIRECTION_THRESHOLD:
            direction = "short"
        else:
            direction = "neutral"

        calibrated = False
        probabilities = (
            calibration_provider(symbol, spec.label, composite)
            if calibration_provider is not None
            else None
        )
        if probabilities is not None and all(_number(value) is not None for value in probabilities):
            total = sum(probabilities)
            if total > 0:
                probabilities = (
                    probabilities[0] / total,
                    probabilities[1] / total,
                    probabilities[2] / total,
                )
                calibrated = True
            else:
                probabilities = None
        if probabilities is None:
            probabilities = _uncalibrated_probabilities(composite)

        atr_h = atr_for_horizon(view_atrs, spec.hours)
        threshold = flat_threshold(atr_h, spread)
        bucket = str(base_features["vol_bucket"])
        band = band_provider(symbol, spec.label, bucket, session) if band_provider else None
        if band is None and atr_h is not None:
            band = (
                _DEFAULT_BAND_ATR[0] * atr_h,
                _DEFAULT_BAND_ATR[1] * atr_h,
                _DEFAULT_BAND_ATR[2] * atr_h,
                "atr_default",
            )
        if band is None:
            p10 = p50 = p90 = None
            band_source = "unavailable"
            expected_range = None
        else:
            p10, p50, p90, band_source = band
            expected_range = p90 - p10

        features = dict(base_features)
        masks = dict(base_masks)
        if spec.hours >= 6.0:
            _add_macro_features(features, masks, macro_features)
        forecasts.append(
            HorizonForecast(
                symbol=symbol,
                horizon=spec.label,
                horizon_hours=spec.hours,
                shadow_only=spec.shadow_only,
                direction=direction,
                composite=round(composite, 6),
                conviction=conviction,
                p_up=round(probabilities[0], 6),
                p_down=round(probabilities[1], 6),
                p_flat=round(probabilities[2], 6),
                calibrated=calibrated,
                close=close,
                atr_h=round(atr_h, 8) if atr_h is not None else None,
                spread=round(spread, 8) if spread is not None else None,
                flat_threshold=round(threshold, 8),
                band_p10=round(p10, 8) if p10 is not None else None,
                band_p50=round(p50, 8) if p50 is not None else None,
                band_p90=round(p90, 8) if p90 is not None else None,
                band_source=band_source,
                expected_range=round(expected_range, 8) if expected_range is not None else None,
                data_quality=quality,
                weights=used_weights,
                features=features,
                feature_masks=masks,
                gates={
                    "freshness_ok": bool(operational_data_ok),
                    "event_window": event_window,
                    "market_open": market_open,
                    "calendar_ok": calendar_ok,
                },
                warnings=warnings,
                input_context_id=str((macro_features or {}).get("context_id", "")),
            )
        )
    return forecasts
