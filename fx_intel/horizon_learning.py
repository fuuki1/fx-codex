"""Maturity scoring and symbol x horizon learning for design A.

Only ``horizon-pit-v1`` rows enter this path.  All calculations are stateless:
the append-only forecast journal and completed M5 bid/ask price series are the
source of truth on every run.
"""

from __future__ import annotations

import json
import math
import tempfile
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .horizon_journal import HORIZON_PIT_CONTRACT, is_pit_eligible_horizon_entry
from .horizons import HORIZON_BY_LABEL, HORIZON_SPECS, PRIOR_WEIGHTS, HorizonSpec
from .market import WEEKEND_CLOSURE, open_hours_between

SCHEMA_VERSION = 1
MODEL_DATA_CONTRACT = HORIZON_PIT_CONTRACT
CALIBRATION_MIN_SAMPLES = 50
BAND_BUCKET_MIN_SAMPLES = 40
BAND_HORIZON_MIN_SAMPLES = 20
WEIGHT_MIN_SAMPLES = 20
WEIGHT_SHRINK_HALFWAY = 40
COMPOSITE_BINS = ((-1.0, -0.5), (-0.5, -0.15), (-0.15, 0.15), (0.15, 0.5), (0.5, 1.0001))
PROMOTION_MIN_SAMPLES = {
    "5m": math.inf,
    "15m": 100,
    "30m": 100,
    "1h": 100,
    "3h": 100,
    "6h": 60,
    "12h": 60,
    "24h": 60,
    "3d": 40,
}


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class PricePoint:
    ts: datetime
    close: float
    high: float
    low: float


@dataclass
class ScoredHorizonForecast:
    symbol: str
    horizon: str
    ts: datetime
    direction: str
    composite: float
    move: float
    realized_class: str
    direction_outcome: str
    brier: float | None
    log_loss: float | None
    pinball_p10: float | None
    pinball_p50: float | None
    pinball_p90: float | None
    band_covered: bool | None
    range_ratio: float | None
    mfe: float | None
    mae: float | None
    net_r: float | None
    vol_bucket: str
    session: str
    shadow_only: bool
    features: dict[str, float | str] = field(default_factory=dict)


@dataclass
class HorizonScoreResult:
    scored: list[ScoredHorizonForecast] = field(default_factory=list)
    immature: int = 0
    unresolved: int = 0
    pit_ineligible: int = 0
    quality_violations: dict[tuple[str, str], list[datetime]] = field(default_factory=dict)


def _pinball(actual: float, predicted: float, quantile: float) -> float:
    residual = actual - predicted
    return quantile * residual if residual >= 0 else (quantile - 1.0) * residual


def _price_series(rows: Iterable[Mapping[str, object]]) -> dict[str, list[PricePoint]]:
    """Deduplicate M5 rows copied to four timeframe labels, preferring 15m."""
    selected: dict[tuple[str, datetime], tuple[int, PricePoint]] = {}
    for row in rows:
        ts = _parse_ts(row.get("available_time") or row.get("ts"))
        symbol = str(row.get("symbol", ""))
        close = _number(row.get("close"))
        if ts is None or not symbol or close is None:
            continue
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        point = PricePoint(
            ts, close, high if high is not None else close, low if low is not None else close
        )
        priority = 0 if row.get("timeframe") == "15m" else 1
        key = (symbol, ts)
        if key not in selected or priority < selected[key][0]:
            selected[key] = (priority, point)
    output: dict[str, list[PricePoint]] = defaultdict(list)
    for (symbol, _ts), (_priority, point) in selected.items():
        output[symbol].append(point)
    for points in output.values():
        points.sort(key=lambda point: point.ts)
    return dict(output)


def _future_point_and_path(
    points: Sequence[PricePoint], ts: datetime, spec: HorizonSpec
) -> tuple[PricePoint | None, list[PricePoint]]:
    stamps = [point.ts for point in points]
    lower = ts + timedelta(hours=max(0.0, spec.hours - spec.tolerance_hours))
    upper = ts + timedelta(hours=spec.hours + spec.tolerance_hours) + WEEKEND_CLOSURE
    best: tuple[float, PricePoint] | None = None
    start_index = bisect_left(stamps, ts)
    for point in points[bisect_left(stamps, lower) :]:
        if point.ts > upper:
            break
        age = open_hours_between(ts, point.ts)
        if spec.hours - spec.tolerance_hours <= age <= spec.hours + spec.tolerance_hours:
            gap = abs(age - spec.hours)
            if best is None or gap < best[0]:
                best = (gap, point)
    if best is None:
        return None, []
    path = [point for point in points[start_index:] if ts < point.ts <= best[1].ts]
    return best[1], path


def score_horizon_history(
    entries: Iterable[Mapping[str, object]],
    price_rows: Iterable[Mapping[str, object]],
    now: datetime | None = None,
    *,
    require_pit: bool = True,
) -> HorizonScoreResult:
    now = now or datetime.now(UTC)
    series = _price_series(price_rows)
    result = HorizonScoreResult()
    quality: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for entry in entries:
        horizon = str(entry.get("horizon", ""))
        symbol = str(entry.get("symbol", ""))
        ts = _parse_ts(entry.get("ts"))
        gates = entry.get("gates")
        if ts is not None and isinstance(gates, Mapping) and gates.get("freshness_ok") is not True:
            quality[(symbol, horizon)].append(ts)
        if require_pit and not is_pit_eligible_horizon_entry(entry):
            result.pit_ineligible += 1
            if ts is not None:
                quality[(symbol, horizon)].append(ts)
            continue
        spec = HORIZON_BY_LABEL.get(horizon)
        close = _number(entry.get("close"))
        if spec is None or ts is None or close is None or not symbol:
            result.unresolved += 1
            continue
        if open_hours_between(ts, now) < spec.hours + spec.tolerance_hours:
            result.immature += 1
            continue
        future, path = _future_point_and_path(series.get(symbol, []), ts, spec)
        if future is None:
            result.unresolved += 1
            continue
        move = future.close - close
        threshold = _number(entry.get("flat_threshold")) or 0.0
        realized = "up" if move > threshold else "down" if move < -threshold else "flat"
        direction = str(entry.get("direction", ""))
        if direction in {"long", "short"}:
            wanted = "up" if direction == "long" else "down"
            direction_outcome = (
                "flat" if realized == "flat" else "hit" if realized == wanted else "miss"
            )
        else:
            direction_outcome = "none"

        probabilities = [_number(entry.get(key)) for key in ("p_up", "p_down", "p_flat")]
        brier = logloss = None
        if all(value is not None for value in probabilities):
            probs = [float(value) for value in probabilities if value is not None]
            truth_index = {"up": 0, "down": 1, "flat": 2}[realized]
            brier = sum(
                (probability - float(index == truth_index)) ** 2
                for index, probability in enumerate(probs)
            )
            logloss = -math.log(max(1e-12, probs[truth_index]))

        p10, p50, p90 = (_number(entry.get(key)) for key in ("band_p10", "band_p50", "band_p90"))
        covered = p10 <= move <= p90 if p10 is not None and p90 is not None else None
        expected_range = _number(entry.get("expected_range"))
        range_ratio = (
            abs(move) / expected_range
            if expected_range is not None and expected_range > 0
            else None
        )
        signed_path = []
        if direction == "long":
            signed_path = [
                value for point in path for value in (point.high - close, point.low - close)
            ]
        elif direction == "short":
            signed_path = [
                value for point in path for value in (close - point.low, close - point.high)
            ]
        mfe = max(signed_path) if signed_path else None
        mae = min(signed_path) if signed_path else None
        atr_h = _number(entry.get("atr_h"))
        spread = _number(entry.get("spread")) or 0.0
        net_r = None
        if direction in {"long", "short"} and atr_h is not None and atr_h > 0:
            signed_move = move if direction == "long" else -move
            net_r = (signed_move - spread) / atr_h
        raw_features = entry.get("features")
        features = dict(raw_features) if isinstance(raw_features, Mapping) else {}
        result.scored.append(
            ScoredHorizonForecast(
                symbol=symbol,
                horizon=horizon,
                ts=ts,
                direction=direction,
                composite=_number(entry.get("composite")) or 0.0,
                move=move,
                realized_class=realized,
                direction_outcome=direction_outcome,
                brier=round(brier, 8) if brier is not None else None,
                log_loss=round(logloss, 8) if logloss is not None else None,
                pinball_p10=_pinball(move, p10, 0.1) if p10 is not None else None,
                pinball_p50=_pinball(move, p50, 0.5) if p50 is not None else None,
                pinball_p90=_pinball(move, p90, 0.9) if p90 is not None else None,
                band_covered=covered,
                range_ratio=range_ratio,
                mfe=mfe,
                mae=mae,
                net_r=net_r,
                vol_bucket=str(features.get("vol_bucket", "mid")),
                session=str(features.get("session", "unknown")),
                shadow_only=bool(entry.get("shadow_only")),
                features=features,
            )
        )
    result.quality_violations = dict(quality)
    return result


def thin_scored(
    items: Sequence[ScoredHorizonForecast], gap_hours: float
) -> list[ScoredHorizonForecast]:
    kept: list[ScoredHorizonForecast] = []
    last: dict[tuple[str, str], datetime] = {}
    for item in sorted(items, key=lambda row: (row.symbol, row.horizon, row.ts)):
        key = (item.symbol, item.horizon)
        if key in last and item.ts - last[key] < timedelta(hours=gap_hours):
            continue
        last[key] = item.ts
        kept.append(item)
    return kept


def _bin(value: float) -> int:
    for index, (low, high) in enumerate(COMPOSITE_BINS):
        if low <= value < high:
            return index
    return len(COMPOSITE_BINS) // 2


def _mean(values: Iterable[float | None]) -> float | None:
    materialized = [float(value) for value in values if value is not None and math.isfinite(value)]
    return sum(materialized) / len(materialized) if materialized else None


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(len(ordered) - 1, low + 1)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _band(values: Sequence[float], source: str) -> dict:
    return {
        "n": len(values),
        "p10": round(_quantile(values, 0.1), 8),
        "p50": round(_quantile(values, 0.5), 8),
        "p90": round(_quantile(values, 0.9), 8),
        "source": source,
    }


def _learn_weights(items: Sequence[ScoredHorizonForecast], horizon: str) -> dict[str, float] | None:
    prior = PRIOR_WEIGHTS[horizon]
    rates: dict[str, tuple[int, float]] = {}
    for key in (*PRIOR_WEIGHTS[horizon].keys(),):
        feature_key = "news_score" if key == "news" else f"rating_{key}"
        hits = total = 0
        for item in items:
            signal = _number(item.features.get(feature_key))
            if signal is None or signal == 0 or item.realized_class == "flat":
                continue
            total += 1
            hits += int((signal > 0) == (item.realized_class == "up"))
        rates[key] = (total, hits / total if total else 0.5)
    if any(total < WEIGHT_MIN_SAMPLES for total, _rate in rates.values()):
        return None
    raw = {key: prior[key] * max(0.25, rate) for key, (_n, rate) in rates.items()}
    total_raw = sum(raw.values())
    target = {key: value / total_raw for key, value in raw.items()}
    n = min(total for total, _rate in rates.values())
    shrink = n / (n + WEIGHT_SHRINK_HALFWAY)
    learned = {key: prior[key] * (1 - shrink) + target[key] * shrink for key in prior}
    normalizer = sum(learned.values())
    return {key: round(value / normalizer, 8) for key, value in learned.items()}


def _promotion(profile: dict, spec: HorizonSpec, violations: int) -> dict:
    required = PROMOTION_MIN_SAMPLES[spec.label]
    checks = {
        "sample": False if math.isinf(required) else profile["n_scored"] >= required,
        "brier": profile["mean_brier"] is not None
        and profile["climatology_brier"] is not None
        and profile["mean_brier"] <= profile["climatology_brier"],
        "coverage": profile["band_coverage"] is not None
        and 0.70 <= profile["band_coverage"] <= 0.90,
        "net_r": profile["mean_net_r"] is not None and profile["mean_net_r"] > 0,
        "quality_7d": violations == 0,
    }
    eligible = not spec.shadow_only and all(checks.values())
    remaining = None if math.isinf(required) else max(0, int(required - profile["n_scored"]))
    rate_per_market_day = max(1.0, 24.0 / spec.learn_thin_gap_hours)
    return {
        "stage": "adopted" if eligible else "shadow",
        "integration_eligible": eligible,
        "permanent_shadow": spec.shadow_only,
        "checks": checks,
        "required_n": None if math.isinf(required) else int(required),
        "remaining_n": remaining,
        "estimated_market_days_remaining": (
            round(remaining / rate_per_market_day, 1) if remaining is not None else None
        ),
    }


def derive_horizon_learning(
    result: HorizonScoreResult,
    now: datetime | None = None,
    *,
    specs: Sequence[HorizonSpec] = HORIZON_SPECS,
) -> dict:
    now = now or datetime.now(UTC)
    by_cell: dict[tuple[str, str], list[ScoredHorizonForecast]] = defaultdict(list)
    for item in result.scored:
        by_cell[(item.symbol, item.horizon)].append(item)
    profiles: dict[str, dict] = {}
    bands: dict[str, dict] = {}
    for (symbol, horizon), raw_items in sorted(by_cell.items()):
        spec = next((candidate for candidate in specs if candidate.label == horizon), None)
        if spec is None:
            continue
        items = thin_scored(raw_items, spec.learn_thin_gap_hours)
        counts = {
            label: sum(item.realized_class == label for item in items)
            for label in ("up", "down", "flat")
        }
        total = len(items)
        directional = [item for item in items if item.direction_outcome in {"hit", "miss", "flat"}]
        hits = sum(item.direction_outcome == "hit" for item in items)
        misses = sum(item.direction_outcome == "miss" for item in items)
        base = (
            {label: count / total for label, count in counts.items()}
            if total
            else {label: 0.0 for label in counts}
        )
        climatology = (
            sum(
                base[label] * sum((base[other] - float(other == label)) ** 2 for other in base)
                for label in base
            )
            if total
            else None
        )
        covered = [item.band_covered for item in items if item.band_covered is not None]
        calibration: list[dict] = []
        if total >= CALIBRATION_MIN_SAMPLES:
            bin_counts = {
                index: {label: 0 for label in counts} for index in range(len(COMPOSITE_BINS))
            }
            for item in items:
                bin_counts[_bin(item.composite)][item.realized_class] += 1
            for index, limits in enumerate(COMPOSITE_BINS):
                cell = bin_counts[index]
                cell_n = sum(cell.values())
                # Three pseudo-observations shrink sparse bins to the cell climatology.
                denominator = cell_n + 3.0
                probabilities = {
                    label: (cell[label] + 3.0 * base[label]) / denominator for label in counts
                }
                calibration.append(
                    {
                        "bin": list(limits),
                        "n": cell_n,
                        "p_up": round(probabilities["up"], 8),
                        "p_down": round(probabilities["down"], 8),
                        "p_flat": round(probabilities["flat"], 8),
                    }
                )
        profile = {
            "symbol": symbol,
            "horizon": horizon,
            "raw_n": len(raw_items),
            "n_scored": total,
            "n_directional": len(directional),
            "hits": hits,
            "misses": misses,
            "flat_directional": sum(item.direction_outcome == "flat" for item in items),
            "hit_rate": round(hits / (hits + misses), 6) if hits + misses else None,
            "class_counts": counts,
            "mean_brier": _mean(item.brier for item in items),
            "mean_log_loss": _mean(item.log_loss for item in items),
            "climatology_brier": climatology,
            "band_coverage": (
                sum(bool(value) for value in covered) / len(covered) if covered else None
            ),
            "mean_pinball_p10": _mean(item.pinball_p10 for item in items),
            "mean_pinball_p50": _mean(item.pinball_p50 for item in items),
            "mean_pinball_p90": _mean(item.pinball_p90 for item in items),
            "mean_range_ratio": _mean(item.range_ratio for item in items),
            "mean_mfe": _mean(item.mfe for item in items),
            "mean_mae": _mean(item.mae for item in items),
            "mean_net_r": _mean(item.net_r for item in items),
            "calibrated": bool(calibration),
            "calibration": calibration,
            "learned_weights": _learn_weights(items, horizon),
        }
        violations = sum(
            stamp >= now - timedelta(days=7)
            for stamp in result.quality_violations.get((symbol, horizon), [])
        )
        profile["quality_violations_7d"] = violations
        profile["promotion"] = _promotion(profile, spec, violations)
        profiles[f"{symbol}|{horizon}"] = profile

        grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
        for item in raw_items:
            grouped[(item.vol_bucket, item.session)].append(item.move)
        cell_bands = {
            f"{bucket}|{session}": _band(moves, "vol_session")
            for (bucket, session), moves in grouped.items()
            if len(moves) >= BAND_BUCKET_MIN_SAMPLES
        }
        all_moves = [item.move for item in raw_items]
        if len(all_moves) >= BAND_HORIZON_MIN_SAMPLES:
            cell_bands["__horizon__"] = _band(all_moves, "horizon_all")
        if cell_bands:
            bands[f"{symbol}|{horizon}"] = cell_bands
    return {
        "schema": SCHEMA_VERSION,
        "contract": MODEL_DATA_CONTRACT,
        "generated_at": now.isoformat(),
        "gbdt_review_gate": "approved_pre_a2",
        "scored_total": len(result.scored),
        "immature": result.immature,
        "unresolved": result.unresolved,
        "pit_ineligible": result.pit_ineligible,
        "profiles": profiles,
        "bands": bands,
    }


def save_horizon_learning(state: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        temp = Path(handle.name)
    temp.replace(target)


def load_horizon_learning(path: str | Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (
        payload
        if isinstance(payload, dict)
        and payload.get("schema") == SCHEMA_VERSION
        and payload.get("contract") == MODEL_DATA_CONTRACT
        else None
    )


def make_profile_lookup(state: Mapping[str, object] | None):
    def lookup(symbol: str, horizon: str) -> tuple[Mapping[str, float] | None, float]:
        profiles = state.get("profiles") if isinstance(state, Mapping) else None
        profile = profiles.get(f"{symbol}|{horizon}") if isinstance(profiles, Mapping) else None
        if not isinstance(profile, Mapping):
            return None, 1.0
        weights = profile.get("learned_weights")
        hit_rate = _number(profile.get("hit_rate"))
        n = int(profile.get("n_directional", 0) or 0)
        factor = min(1.0, max(0.6, hit_rate / 0.5)) if hit_rate is not None and n >= 8 else 1.0
        return (dict(weights) if isinstance(weights, Mapping) else None), factor

    return lookup


def make_band_provider(state: Mapping[str, object] | None):
    def provider(symbol: str, horizon: str, bucket: str, session: str):
        bands = state.get("bands") if isinstance(state, Mapping) else None
        cell = bands.get(f"{symbol}|{horizon}") if isinstance(bands, Mapping) else None
        band = (
            (cell.get(f"{bucket}|{session}") or cell.get("__horizon__"))
            if isinstance(cell, Mapping)
            else None
        )
        if not isinstance(band, Mapping):
            return None
        p10 = _number(band.get("p10"))
        p50 = _number(band.get("p50"))
        p90 = _number(band.get("p90"))
        if p10 is None or p50 is None or p90 is None:
            return None
        return (p10, p50, p90, str(band.get("source", "learned")))

    return provider


def make_calibration_provider(state: Mapping[str, object] | None):
    def provider(symbol: str, horizon: str, composite: float):
        profiles = state.get("profiles") if isinstance(state, Mapping) else None
        profile = profiles.get(f"{symbol}|{horizon}") if isinstance(profiles, Mapping) else None
        calibration = (
            profile.get("calibration")
            if isinstance(profile, Mapping) and profile.get("calibrated")
            else None
        )
        if not isinstance(calibration, list) or _bin(composite) >= len(calibration):
            return None
        row = calibration[_bin(composite)]
        if not isinstance(row, Mapping):
            return None
        p_up = _number(row.get("p_up"))
        p_down = _number(row.get("p_down"))
        p_flat = _number(row.get("p_flat"))
        if p_up is None or p_down is None or p_flat is None:
            return None
        return (p_up, p_down, p_flat)

    return provider
