"""Append-only journal enforcing the ``horizon-pit-v1`` data contract."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .horizon_forecast import HorizonForecast
from .horizons import HORIZON_BY_LABEL

HORIZON_PIT_CONTRACT = "horizon-pit-v1"
SCHEMA_VERSION = 1


class HorizonPointInTimeError(ValueError):
    pass


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HorizonPointInTimeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _prediction_id(symbol: str, horizon: str, prediction_time: datetime) -> str:
    cycle = prediction_time.replace(
        minute=prediction_time.minute - prediction_time.minute % 5,
        second=0,
        microsecond=0,
    )
    raw = f"{HORIZON_PIT_CONTRACT}|{symbol}|{horizon}|{cycle.isoformat()}"
    return "hf:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row(
    forecast: HorizonForecast,
    prediction_time: datetime,
    source_cutoff: datetime,
    feature_time: datetime,
) -> dict:
    spec = HORIZON_BY_LABEL.get(forecast.horizon)
    if spec is None or not math.isclose(spec.hours, forecast.horizon_hours, abs_tol=1e-9):
        raise HorizonPointInTimeError(f"unknown or inconsistent horizon: {forecast.horizon}")
    probabilities = (forecast.p_up, forecast.p_down, forecast.p_flat)
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities):
        raise HorizonPointInTimeError("probabilities must be finite values in [0,1]")
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-5):
        raise HorizonPointInTimeError("p_up + p_down + p_flat must equal 1")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": HORIZON_PIT_CONTRACT,
        "prediction_id": _prediction_id(forecast.symbol, forecast.horizon, prediction_time),
        "ts": prediction_time.isoformat(),
        "prediction_time": prediction_time.isoformat(),
        "source_cutoff": source_cutoff.isoformat(),
        "max_feature_available_time": feature_time.isoformat(),
        "pit_eligible": True,
        "track_stage": "shadow",
        "symbol": forecast.symbol,
        "horizon": forecast.horizon,
        "horizon_hours": forecast.horizon_hours,
        "shadow_only": forecast.shadow_only,
        "direction": forecast.direction,
        "composite": forecast.composite,
        "conviction": forecast.conviction,
        "p_up": forecast.p_up,
        "p_down": forecast.p_down,
        "p_flat": forecast.p_flat,
        "calibrated": forecast.calibrated,
        "close": forecast.close,
        "atr_h": forecast.atr_h,
        "spread": forecast.spread,
        "flat_threshold": forecast.flat_threshold,
        "band_p10": forecast.band_p10,
        "band_p50": forecast.band_p50,
        "band_p90": forecast.band_p90,
        "band_source": forecast.band_source,
        "expected_range": forecast.expected_range,
        "data_quality": forecast.data_quality,
        "features": forecast.features,
        "feature_masks": forecast.feature_masks,
        "gates": forecast.gates,
        "weights": forecast.weights,
        "warnings": forecast.warnings,
        "input_context_id": forecast.input_context_id,
        "generator_version": forecast.generator_version,
    }


def append_horizon_forecasts(
    path: str | Path,
    forecasts: Sequence[HorizonForecast],
    *,
    prediction_time: datetime,
    source_cutoff: datetime,
    max_feature_available_time: datetime,
) -> int:
    """Validate the complete batch before writing any row."""
    prediction = _aware(prediction_time, "prediction_time")
    source = _aware(source_cutoff, "source_cutoff")
    feature = _aware(max_feature_available_time, "max_feature_available_time")
    if not source <= feature <= prediction:
        raise HorizonPointInTimeError(
            "PIT order violation: source_cutoff <= max_feature_available_time <= prediction_time"
        )
    rows = [_row(forecast, prediction, source, feature) for forecast in forecasts]
    ids = [row["prediction_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise HorizonPointInTimeError("duplicate symbol x horizon rows in one cycle")
    if not rows:
        return 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Only the recent tail is needed: a duplicate writer would collide in
        # the current five-minute cycle.  Avoid rereading a multi-GB archive.
        recent_ids = _recent_prediction_ids(target)
        if recent_ids & set(ids):
            raise HorizonPointInTimeError("duplicate five-minute forecast cycle")
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    return len(rows)


def read_horizon_entries(path: str | Path) -> Iterator[dict]:
    try:
        handle = Path(path).open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _recent_prediction_ids(path: Path, limit: int = 100) -> set[str]:
    """Read only enough bytes from the tail to cover recent cycle IDs."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks: list[bytes] = []
            newlines = 0
            while position > 0 and newlines <= limit:
                size = min(65536, position)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError:
        return set()
    ids: set[str] = set()
    for line in b"".join(reversed(chunks)).splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and row.get("prediction_id"):
            ids.add(str(row["prediction_id"]))
    return ids


def is_pit_eligible_horizon_entry(entry: Mapping[str, object]) -> bool:
    if entry.get("contract") != HORIZON_PIT_CONTRACT or entry.get("pit_eligible") is not True:
        return False
    source = _parse(entry.get("source_cutoff"))
    feature = _parse(entry.get("max_feature_available_time"))
    prediction = _parse(entry.get("prediction_time"))
    recorded = _parse(entry.get("ts"))
    if None in (source, feature, prediction, recorded):
        return False
    assert source is not None and feature is not None and prediction is not None and recorded
    return recorded == prediction and source <= feature <= prediction
