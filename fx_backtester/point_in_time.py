"""Point-in-time records, backward as-of joins, and fail-closed price QA.

This module is deliberately independent from model code.  It defines the boundary
that data must cross before it can be used by research, calibration, or a trading
decision.  All internal timestamps are timezone-aware UTC and joins are performed
on ``available_time`` rather than the date an observation describes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd


class PointInTimeError(ValueError):
    """Raised when data cannot be proven available at the prediction time."""


def utc_datetime(value: object, *, field_name: str) -> datetime:
    """Return ``value`` as UTC, rejecting naive timestamps.

    Silently guessing the timezone of a naive timestamp makes a historical replay
    non-reproducible, especially around DST transitions, so callers must localise at
    the source boundary before constructing a record.
    """

    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise PointInTimeError(f"{field_name} is not a valid timestamp") from error
    if timestamp.tzinfo is None:
        raise PointInTimeError(f"{field_name} must be timezone-aware")
    return timestamp.tz_convert("UTC").to_pydatetime()


def canonical_content_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 for a JSON-compatible payload."""

    encoded = json.dumps(
        _json_ready(dict(payload)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PointInTimeRecord:
    """Canonical bitemporal envelope for an immutable raw observation.

    ``event_time`` is what the observation describes. ``available_time`` is the
    first instant this system could actually use it, so it is normalized to the
    latest of declared availability, publication, revision, ingestion, and
    validation completion.
    ``ingested_time`` records when this system received it. Scheduled events may
    have an event time in the future, so no ordering is imposed between event and
    availability time.
    """

    event_time: datetime
    available_time: datetime
    ingested_time: datetime
    source: str
    source_record_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    published_time: datetime | None = None
    source_time: datetime | None = None
    revision_time: datetime | None = None
    validated_time: datetime | None = None
    schema_version: int = 1
    content_hash: str = ""
    run_id: str = ""
    writer_id: str = ""
    model_version: str = ""
    data_quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("event_time", "available_time", "ingested_time"):
            if getattr(self, name) is None:
                raise PointInTimeError(f"{name} must be non-null")
        for name in (
            "event_time",
            "available_time",
            "ingested_time",
            "published_time",
            "source_time",
            "revision_time",
            "validated_time",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, field_name=name))
        for name in ("source", "source_record_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PointInTimeError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        for name in ("run_id", "writer_id", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise PointInTimeError(f"{name} must be a string")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise PointInTimeError("schema_version must be an integer")
        if self.schema_version < 1:
            raise PointInTimeError("schema_version must be >= 1")
        effective_availability = max(
            timestamp
            for timestamp in (
                self.available_time,
                self.ingested_time,
                self.published_time,
                self.revision_time,
                self.validated_time,
            )
            if timestamp is not None
        )
        availability_was_raised = effective_availability != self.available_time
        object.__setattr__(self, "available_time", effective_availability)
        canonical_payload = _json_ready(self.payload)
        if not isinstance(canonical_payload, dict):
            raise PointInTimeError("payload must be a JSON object")
        expected_digest = canonical_content_hash(canonical_payload)
        if self.content_hash and self.content_hash != expected_digest:
            raise PointInTimeError("content_hash does not match canonical payload")
        digest = expected_digest
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PointInTimeError("content_hash must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "payload", _freeze_json(canonical_payload))
        object.__setattr__(self, "content_hash", digest)
        if not isinstance(self.data_quality_flags, (list, tuple)) or any(
            not isinstance(flag, str) for flag in self.data_quality_flags
        ):
            raise PointInTimeError("data_quality_flags must contain only strings")
        normalized_flags = {flag.strip() for flag in self.data_quality_flags if flag.strip()}
        if availability_was_raised:
            normalized_flags.add("availability_normalized_to_actual_use")
        object.__setattr__(self, "data_quality_flags", tuple(sorted(normalized_flags)))

    def to_dict(self) -> dict[str, Any]:
        return _json_ready({item.name: getattr(self, item.name) for item in fields(self)})


def point_in_time_asof_join(
    observations: pd.DataFrame,
    features: pd.DataFrame,
    *,
    prediction_time: str = "prediction_time",
    available_time: str = "available_time",
    by: str | Sequence[str] | None = None,
    tolerance: pd.Timedelta | timedelta | None = None,
    suffixes: tuple[str, str] = ("", "_feature"),
) -> pd.DataFrame:
    """Backward as-of join that proves every feature was already available.

    The output is restored to the original observation order.  Naive timestamps,
    missing keys, and any impossible future match are rejected rather than coerced.
    """

    if prediction_time not in observations:
        raise PointInTimeError(f"missing observation column: {prediction_time}")
    if available_time not in features:
        raise PointInTimeError(f"missing feature column: {available_time}")
    by_columns = [by] if isinstance(by, str) else list(by or [])
    for column in by_columns:
        if column not in observations or column not in features:
            raise PointInTimeError(f"as-of key missing from one side: {column}")

    left = observations.copy()
    right = features.copy()
    left[prediction_time] = _utc_series(left[prediction_time], prediction_time)
    right[available_time] = _utc_series(right[available_time], available_time)
    # Raw feature tables sometimes carry the source's nominal release time in
    # available_time. Actual use cannot precede our ingestion (or a later revision),
    # so normalize before sorting/joining rather than trusting that declaration.
    for boundary in ("published_time", "ingested_time", "revision_time", "validated_time"):
        if boundary in right:
            right[boundary] = _utc_series(right[boundary], boundary)
            right[available_time] = right[[available_time, boundary]].max(axis=1)
    feature_key = [*by_columns, available_time]
    if bool(right.duplicated(feature_key, keep=False).any()):
        raise PointInTimeError(f"ambiguous duplicate feature availability key: {feature_key}")
    left["__pit_order"] = np.arange(len(left), dtype=int)

    left_sort = [prediction_time, *by_columns]
    right_sort = [available_time, *by_columns]
    left = left.sort_values(left_sort, kind="stable")
    right = right.sort_values(right_sort, kind="stable")
    joined = pd.merge_asof(
        left,
        right,
        left_on=prediction_time,
        right_on=available_time,
        by=by_columns or None,
        direction="backward",
        allow_exact_matches=True,
        tolerance=tolerance,
        suffixes=suffixes,
    )
    matched = joined[available_time].notna()
    if bool((joined.loc[matched, available_time] > joined.loc[matched, prediction_time]).any()):
        raise PointInTimeError("future feature matched by as-of join")
    return joined.sort_values("__pit_order").drop(columns="__pit_order").reset_index(drop=True)


@dataclass(frozen=True)
class PriceQualityThresholds:
    expected_frequency: str | None = None
    max_missing_pct: float = 0.005
    max_age: timedelta | None = None
    max_future_skew: timedelta = timedelta(seconds=5)
    max_spread_multiple: float = 5.0
    max_unchanged_run: int | None = None
    require_bid_ask: bool = False
    require_point_in_time_metadata: bool = False
    exclude_weekends_from_gap_check: bool = True
    source_disagreement_pct: float | None = None


@dataclass(frozen=True)
class PriceQualityReport:
    metrics: Mapping[str, Any]
    flags: tuple[str, ...]
    critical_flags: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.critical_flags

    @property
    def action(self) -> str:
        return "allow" if self.passed else "abstain"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "action": self.action,
            "metrics": _json_ready(dict(self.metrics)),
            "flags": list(self.flags),
            "critical_flags": list(self.critical_flags),
        }


def evaluate_price_quality(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
    thresholds: PriceQualityThresholds | None = None,
    expected_index: pd.DatetimeIndex | None = None,
) -> PriceQualityReport:
    """Evaluate execution-relevant price integrity and return a fail-closed veto.

    A critical flag means the data must not drive a prediction or order.  Missing
    bid/ask is advisory for research bars unless ``require_bid_ask`` is enabled.
    """

    limits = thresholds or PriceQualityThresholds()
    flags: list[str] = []
    critical: list[str] = []
    metrics: dict[str, Any] = {"rows": int(len(frame))}

    def add(flag: str, *, is_critical: bool = False) -> None:
        if flag not in flags:
            flags.append(flag)
        if is_critical and flag not in critical:
            critical.append(flag)

    if frame.empty:
        add("empty_price_data", is_critical=True)
        return PriceQualityReport(metrics, tuple(flags), tuple(critical))
    if not isinstance(frame.index, pd.DatetimeIndex):
        add("timestamp_index_required", is_critical=True)
        return PriceQualityReport(metrics, tuple(flags), tuple(critical))
    if frame.index.tz is None:
        add("timezone_naive", is_critical=True)
        return PriceQualityReport(metrics, tuple(flags), tuple(critical))

    index = frame.index.tz_convert("UTC")
    duplicate_count = int(index.duplicated().sum())
    metrics["duplicate_count"] = duplicate_count
    metrics["monotonic"] = bool(index.is_monotonic_increasing)
    metrics["start"] = index.min()
    metrics["end"] = index.max()
    if duplicate_count:
        add("duplicate_timestamp", is_critical=True)
    if not index.is_monotonic_increasing:
        add("timestamp_not_monotonic", is_critical=True)

    required = {"open", "high", "low", "close"}
    missing_columns = sorted(required - set(frame.columns))
    metrics["missing_ohlc_columns"] = missing_columns
    if missing_columns:
        add("missing_ohlc", is_critical=True)
    else:
        ohlc = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        null_ohlc = int(ohlc.isna().sum().sum())
        non_finite = int((~np.isfinite(ohlc.to_numpy(dtype=float))).sum())
        non_positive = int((ohlc <= 0).sum().sum())
        invalid = int(
            (
                (ohlc["high"] < ohlc["low"])
                | (ohlc["open"] > ohlc["high"])
                | (ohlc["open"] < ohlc["low"])
                | (ohlc["close"] > ohlc["high"])
                | (ohlc["close"] < ohlc["low"])
            ).sum()
        )
        metrics.update(
            null_ohlc_count=null_ohlc,
            non_finite_ohlc_count=non_finite,
            non_positive_price_count=non_positive,
            invalid_ohlc_count=invalid,
        )
        if null_ohlc or non_finite:
            add("invalid_numeric_price", is_critical=True)
        if non_positive:
            add("non_positive_price", is_critical=True)
        if invalid:
            add("invalid_ohlc", is_critical=True)

        if limits.max_unchanged_run is not None and limits.max_unchanged_run > 0:
            close = ohlc["close"]
            groups = close.ne(close.shift()).cumsum()
            max_run = int(close.groupby(groups).size().max())
            metrics["max_unchanged_close_run"] = max_run
            if max_run > limits.max_unchanged_run:
                add("stale_quote_run", is_critical=True)

    now_utc = utc_datetime(now or datetime.now(UTC), field_name="now")
    future_count = int((index > pd.Timestamp(now_utc + limits.max_future_skew)).sum())
    metrics["future_timestamp_count"] = future_count
    if future_count:
        add("future_timestamp", is_critical=True)
    if limits.max_age is not None:
        age_seconds = max(0.0, (now_utc - index.max().to_pydatetime()).total_seconds())
        metrics["age_seconds"] = age_seconds
        if age_seconds > limits.max_age.total_seconds():
            add("stale_data", is_critical=True)

    if expected_index is None and limits.expected_frequency:
        expected_index = pd.date_range(index.min(), index.max(), freq=limits.expected_frequency)
        if limits.exclude_weekends_from_gap_check:
            expected_index = expected_index[expected_index.dayofweek < 5]
    if expected_index is not None:
        if expected_index.tz is None:
            raise PointInTimeError("expected_index must be timezone-aware")
        expected_utc = expected_index.tz_convert("UTC")
        actual_unique = pd.DatetimeIndex(index.unique())
        missing_bars = int(len(expected_utc.difference(actual_unique)))
        missing_pct = missing_bars / len(expected_utc) if len(expected_utc) else 0.0
        metrics.update(expected_bars=int(len(expected_utc)), missing_bars=missing_bars)
        metrics["missing_pct"] = float(missing_pct)
        if missing_pct > limits.max_missing_pct:
            add("missing_bar_rate", is_critical=True)

    bid_ask_available = {"bid", "ask"}.issubset(frame.columns)
    metrics["bid_ask_available"] = bid_ask_available
    if bid_ask_available:
        bid = pd.to_numeric(frame["bid"], errors="coerce")
        ask = pd.to_numeric(frame["ask"], errors="coerce")
        crossed = int((bid > ask).sum())
        invalid_quote = int((bid.isna() | ask.isna() | (bid <= 0) | (ask <= 0)).sum())
        metrics.update(crossed_quote_count=crossed, invalid_quote_count=invalid_quote)
        if crossed:
            add("bid_above_ask", is_critical=True)
        if invalid_quote:
            add("invalid_bid_ask", is_critical=True)
        spread_values = ask - bid
    else:
        spread_values = _spread_series(frame)
        add("bid_ask_unavailable", is_critical=limits.require_bid_ask)

    if spread_values is not None:
        spread_values = pd.to_numeric(spread_values, errors="coerce")
        invalid_spread = int((spread_values.isna() | (spread_values <= 0)).sum())
        metrics["invalid_spread_count"] = invalid_spread
        if invalid_spread:
            add("invalid_spread", is_critical=True)
        positive = spread_values[spread_values > 0]
        if not positive.empty:
            median = float(positive.median())
            abnormal = int((positive > median * limits.max_spread_multiple).sum())
            metrics.update(median_spread=median, abnormal_spread_count=abnormal)
            if abnormal:
                add(
                    "abnormal_spread",
                    is_critical=limits.require_bid_ask or limits.require_point_in_time_metadata,
                )
    elif limits.require_bid_ask:
        add("spread_unavailable", is_critical=True)

    if "volume" in frame:
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        negative_volume = int((volume < 0).sum())
        metrics["negative_volume_count"] = negative_volume
        if negative_volume:
            add("negative_volume", is_critical=True)

    if (
        limits.source_disagreement_pct is not None
        and "reference_close" in frame
        and "close" in frame
    ):
        close = pd.to_numeric(frame["close"], errors="coerce")
        reference = pd.to_numeric(frame["reference_close"], errors="coerce")
        relative = (close - reference).abs() / reference.abs().replace(0, np.nan)
        disagreement = int((relative > limits.source_disagreement_pct).sum())
        metrics["source_disagreement_count"] = disagreement
        if disagreement:
            add("source_disagreement", is_critical=True)

    if limits.require_point_in_time_metadata:
        metadata = {
            "event_time",
            "available_time",
            "ingested_time",
            "source",
            "source_record_id",
            "schema_version",
            "content_hash",
            "run_id",
            "writer_id",
            "data_quality_flags",
        }
        missing_metadata = sorted(metadata - set(frame.columns))
        metrics["missing_metadata_columns"] = missing_metadata
        if missing_metadata:
            add("point_in_time_metadata_missing", is_critical=True)
        else:
            required_non_null = sorted(metadata)
            null_metadata = {
                column: int(frame[column].isna().sum())
                for column in required_non_null
                if bool(frame[column].isna().any())
            }
            metrics["null_metadata_counts"] = null_metadata
            if null_metadata:
                add("point_in_time_metadata_null", is_critical=True)

            timestamp_columns = ["event_time", "available_time", "ingested_time"]
            for optional in ("published_time", "revision_time", "validated_time"):
                if optional in frame:
                    timestamp_columns.append(optional)
            parsed_timestamps: dict[str, pd.Series] = {}
            for column in timestamp_columns:
                try:
                    parsed_timestamps[column] = _utc_series(frame[column], column)
                except (TypeError, ValueError, PointInTimeError):
                    add("point_in_time_timestamp_invalid", is_critical=True)
            if {"available_time", "ingested_time"} <= parsed_timestamps.keys():
                if bool(
                    (parsed_timestamps["available_time"] < parsed_timestamps["ingested_time"]).any()
                ):
                    add("available_before_ingestion", is_critical=True)
            for boundary in ("published_time", "revision_time", "validated_time"):
                if {"available_time", boundary} <= parsed_timestamps.keys() and bool(
                    (parsed_timestamps["available_time"] < parsed_timestamps[boundary]).any()
                ):
                    add(f"available_before_{boundary}", is_critical=True)

            future_boundary = pd.Timestamp(now_utc + limits.max_future_skew)
            future_metadata_counts = {
                column: int((parsed_timestamps[column] > future_boundary).sum())
                for column in ("available_time", "ingested_time", "validated_time")
                if column in parsed_timestamps
                and bool((parsed_timestamps[column] > future_boundary).any())
            }
            metrics["future_metadata_counts"] = future_metadata_counts
            if future_metadata_counts:
                add("point_in_time_future_metadata", is_critical=True)

            identity_columns = ("source", "source_record_id", "run_id", "writer_id")
            empty_identity = {
                column: int(frame[column].astype(str).str.strip().eq("").sum())
                for column in identity_columns
                if bool(frame[column].astype(str).str.strip().eq("").any())
            }
            metrics["empty_identity_counts"] = empty_identity
            if empty_identity:
                add("point_in_time_identity_empty", is_critical=True)
            duplicate_source_keys = frame.duplicated(["source", "source_record_id"], keep=False)
            metrics["duplicate_source_record_key_count"] = int(duplicate_source_keys.sum())
            if bool(duplicate_source_keys.any()):
                add("duplicate_source_record_id", is_critical=True)

            valid_schema = frame["schema_version"].map(
                lambda value: isinstance(value, (int, np.integer))
                and not isinstance(value, (bool, np.bool_))
                and int(value) >= 1
            )
            metrics["invalid_schema_version_count"] = int((~valid_schema).sum())
            if not bool(valid_schema.all()):
                add("invalid_schema_version", is_critical=True)

            valid_flags = frame["data_quality_flags"].map(
                lambda value: isinstance(value, (list, tuple))
                and all(isinstance(flag, str) for flag in value)
            )
            metrics["invalid_data_quality_flags_count"] = int((~valid_flags).sum())
            if not bool(valid_flags.all()):
                add("invalid_data_quality_flags", is_critical=True)

            hashes = frame["content_hash"].astype(str)
            valid_hashes = hashes.str.fullmatch(r"[0-9a-f]{64}", na=False)
            metrics["invalid_content_hash_count"] = int((~valid_hashes).sum())
            if not bool(valid_hashes.all()):
                add("invalid_content_hash", is_critical=True)

    return PriceQualityReport(metrics, tuple(flags), tuple(critical))


def _utc_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(series, errors="raise", utc=False)
    if not isinstance(parsed.dtype, pd.DatetimeTZDtype):
        raise PointInTimeError(f"{name} must contain timezone-aware timestamps")
    return parsed.dt.tz_convert("UTC")


def _spread_series(frame: pd.DataFrame) -> pd.Series | None:
    for column in ("spread_price", "spread_pips", "spread"):
        if column in frame:
            return frame[column]
    return None


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise PointInTimeError("JSON object keys must be strings")
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, set):
        raise PointInTimeError("sets are unordered and cannot be serialized canonically")
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return utc_datetime(value, field_name="timestamp").isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and not np.isfinite(value):
        raise PointInTimeError("non-finite values cannot be hashed or serialized")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PointInTimeError(f"unsupported JSON value type: {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value
