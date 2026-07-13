from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fx_backtester.point_in_time import (
    PointInTimeError,
    PointInTimeRecord,
    PriceQualityThresholds,
    canonical_content_hash,
    evaluate_price_quality,
    point_in_time_asof_join,
    utc_datetime,
)


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def _prices(index: pd.DatetimeIndex) -> pd.DataFrame:
    close = [100.0 + offset * 0.1 for offset in range(len(index))]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "bid": [value - 0.01 for value in close],
            "ask": [value + 0.01 for value in close],
        },
        index=index,
    )


def test_point_in_time_record_is_immutable_and_hash_is_canonical() -> None:
    available = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    first = canonical_content_hash({"b": 2, "a": 1})
    second = canonical_content_hash({"a": 1, "b": 2})

    record = PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        published_time=available,
        available_time=available,
        ingested_time=available + timedelta(seconds=2),
        source="fred",
        source_record_id="series:observation:vintage",
        payload={"b": 2, "a": 1},
    )

    assert first == second == record.content_hash
    assert record.to_dict()["available_time"] == "2024-01-02T15:00:02+00:00"
    assert "availability_normalized_to_actual_use" in record.data_quality_flags
    with pytest.raises((AttributeError, TypeError)):
        record.source = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.payload["a"] = 99  # type: ignore[index]


def test_point_in_time_record_rejects_forged_hash_and_deep_copies_payload() -> None:
    payload = {"nested": {"values": [1, 2]}}
    record = PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        available_time=datetime(2024, 1, 2, tzinfo=UTC),
        ingested_time=datetime(2024, 1, 2, tzinfo=UTC),
        source="test",
        source_record_id="1",
        payload=payload,
    )
    payload["nested"]["values"].append(3)
    assert record.to_dict()["payload"] == {"nested": {"values": [1, 2]}}

    with pytest.raises(PointInTimeError, match="does not match"):
        PointInTimeRecord(
            event_time=datetime(2024, 1, 1, tzinfo=UTC),
            available_time=datetime(2024, 1, 2, tzinfo=UTC),
            ingested_time=datetime(2024, 1, 2, tzinfo=UTC),
            source="test",
            source_record_id="2",
            payload={"value": 1},
            content_hash="0" * 64,
        )


def test_naive_timestamp_is_rejected_and_availability_never_precedes_actual_use() -> None:
    with pytest.raises(PointInTimeError, match="timezone-aware"):
        utc_datetime(datetime(2024, 1, 1), field_name="event_time")

    record = PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        available_time=datetime(2024, 1, 1, tzinfo=UTC),
        ingested_time=datetime(2024, 1, 2, tzinfo=UTC),
        revision_time=datetime(2024, 1, 3, tzinfo=UTC),
        source="test",
        source_record_id="1",
    )
    assert record.available_time == datetime(2024, 1, 3, tzinfo=UTC)


def test_source_time_is_a_causal_availability_boundary() -> None:
    declared_available = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    source_time = declared_available + timedelta(hours=1)
    record = PointInTimeRecord(
        event_time=declared_available,
        available_time=declared_available,
        ingested_time=declared_available,
        source_time=source_time,
        source="test",
        source_record_id="source-lagged",
    )

    assert record.available_time == source_time
    assert "availability_normalized_to_actual_use" in record.data_quality_flags


def test_backward_asof_join_cannot_match_before_source_time() -> None:
    declared_available = _utc("2024-01-01 10:00")
    source_time = _utc("2024-01-01 11:00")
    observations = pd.DataFrame(
        {"prediction_time": [_utc("2024-01-01 10:30"), _utc("2024-01-01 11:00")]}
    )
    features = pd.DataFrame(
        {
            "available_time": [declared_available],
            "ingested_time": [declared_available],
            "source_time": [source_time],
            "value": [99.0],
        }
    )

    joined = point_in_time_asof_join(observations, features)

    assert pd.isna(joined.loc[0, "value"])
    assert joined.loc[1, "value"] == 99.0
    assert joined.loc[1, "available_time"] == source_time


def test_explicit_dst_offset_converts_without_guessing() -> None:
    first_ambiguous_hour = utc_datetime(
        pd.Timestamp("2024-11-03T01:30:00-04:00"), field_name="event_time"
    )
    second_ambiguous_hour = utc_datetime(
        pd.Timestamp("2024-11-03T01:30:00-05:00"), field_name="event_time"
    )

    assert first_ambiguous_hour == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    assert second_ambiguous_hour == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)


def test_backward_asof_join_never_uses_future_value_and_preserves_order() -> None:
    observations = pd.DataFrame(
        {
            "prediction_time": [
                _utc("2024-01-01 09:05"),
                _utc("2024-01-01 09:01"),
                _utc("2024-01-01 09:05"),
            ],
            "symbol": ["EURUSD", "EURUSD", "USDJPY"],
        }
    )
    features = pd.DataFrame(
        {
            "available_time": [
                _utc("2024-01-01 09:00"),
                _utc("2024-01-01 09:06"),
                _utc("2024-01-01 09:04"),
            ],
            "symbol": ["EURUSD", "EURUSD", "USDJPY"],
            "value": [1.0, 99.0, 3.0],
        }
    )

    joined = point_in_time_asof_join(observations, features, by="symbol")

    assert joined["value"].tolist() == [1.0, 1.0, 3.0]
    assert joined["prediction_time"].tolist() == observations["prediction_time"].tolist()
    assert bool((joined["available_time"] <= joined["prediction_time"]).all())


def test_backward_asof_join_leaves_future_only_feature_unmatched() -> None:
    observations = pd.DataFrame({"prediction_time": [_utc("2024-01-01 09:00")]})
    features = pd.DataFrame({"available_time": [_utc("2024-01-01 09:01")], "value": [1.0]})

    joined = point_in_time_asof_join(observations, features)

    assert pd.isna(joined.loc[0, "value"])


def test_backward_asof_join_uses_actual_ingestion_not_nominal_release() -> None:
    observations = pd.DataFrame(
        {"prediction_time": [_utc("2024-01-01 15:30"), _utc("2024-01-01 16:01")]}
    )
    features = pd.DataFrame(
        {
            "available_time": [_utc("2024-01-01 15:00")],
            "published_time": [_utc("2024-01-01 15:00")],
            "ingested_time": [_utc("2024-01-01 16:00")],
            "value": [7.0],
        }
    )

    joined = point_in_time_asof_join(observations, features)

    assert pd.isna(joined.loc[0, "value"])
    assert joined.loc[1, "value"] == 7.0
    assert joined.loc[1, "available_time"] == _utc("2024-01-01 16:00")


def test_backward_asof_join_rejects_ambiguous_duplicate_feature_keys() -> None:
    observations = pd.DataFrame(
        {"prediction_time": [_utc("2024-01-01 09:01")], "symbol": ["EURUSD"]}
    )
    features = pd.DataFrame(
        {
            "available_time": [_utc("2024-01-01 09:00"), _utc("2024-01-01 09:00")],
            "symbol": ["EURUSD", "EURUSD"],
            "value": [1.0, 2.0],
        }
    )

    with pytest.raises(PointInTimeError, match="ambiguous duplicate as-of keys"):
        point_in_time_asof_join(observations, features, by="symbol")


def test_clean_price_data_passes_strict_quality_gate() -> None:
    index = pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="h")
    report = evaluate_price_quality(
        _prices(index),
        now=datetime(2024, 1, 1, 3, 1, tzinfo=UTC),
        thresholds=PriceQualityThresholds(
            expected_frequency="1h",
            max_age=timedelta(minutes=5),
            require_bid_ask=True,
        ),
    )

    assert report.passed
    assert report.action == "allow"
    assert report.critical_flags == ()


def test_bad_ohlc_duplicates_and_future_timestamps_fail_closed() -> None:
    index = pd.DatetimeIndex([_utc("2024-01-01 00:00"), _utc("2024-01-01 00:00")])
    frame = _prices(index)
    frame.iloc[0, frame.columns.get_loc("high")] = 99.0

    report = evaluate_price_quality(
        frame,
        now=datetime(2023, 12, 31, 23, 0, tzinfo=UTC),
        thresholds=PriceQualityThresholds(require_bid_ask=True),
    )

    assert not report.passed
    assert {"duplicate_timestamp", "invalid_ohlc", "future_timestamp"}.issubset(
        report.critical_flags
    )


def test_missing_bar_staleness_and_required_quotes_fail_closed() -> None:
    complete = pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="h")
    frame = _prices(complete.delete(2)).drop(columns=["bid", "ask"])

    report = evaluate_price_quality(
        frame,
        now=datetime(2024, 1, 1, 5, 0, tzinfo=UTC),
        thresholds=PriceQualityThresholds(
            expected_frequency="1h",
            max_missing_pct=0.0,
            max_age=timedelta(minutes=30),
            require_bid_ask=True,
        ),
    )

    assert not report.passed
    assert {"missing_bar_rate", "stale_data", "bid_ask_unavailable"}.issubset(report.critical_flags)


def test_missing_close_with_reference_data_reports_schema_failure_not_exception() -> None:
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="h")
    frame = _prices(index).drop(columns="close")
    frame["reference_close"] = [100.0, 100.1]

    report = evaluate_price_quality(
        frame,
        now=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        thresholds=PriceQualityThresholds(source_disagreement_pct=0.01),
    )

    assert "missing_ohlc" in report.critical_flags


def test_strict_pit_quality_rejects_null_identity_time_and_hash_metadata() -> None:
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="h")
    frame = _prices(index)
    for column in (
        "event_time",
        "available_time",
        "ingested_time",
        "source",
        "source_record_id",
        "schema_version",
        "content_hash",
        "run_id",
        "writer_id",
    ):
        frame[column] = None
    frame["data_quality_flags"] = [[], []]

    report = evaluate_price_quality(
        frame,
        now=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        thresholds=PriceQualityThresholds(require_point_in_time_metadata=True),
    )

    assert not report.passed
    assert "point_in_time_metadata_null" in report.critical_flags
    assert "point_in_time_timestamp_invalid" in report.critical_flags
    assert "invalid_content_hash" in report.critical_flags


def test_strict_pit_quality_recomputes_payload_hash_and_rejects_forgery() -> None:
    now = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
    index = pd.date_range("2024-01-01T00:00:00Z", periods=1, freq="h")
    frame = _prices(index)
    for column in ("event_time", "available_time", "ingested_time"):
        frame[column] = index
    frame["source"] = "test"
    frame["source_record_id"] = "test:1"
    frame["schema_version"] = 1
    frame["payload"] = pd.Series([{"close": 100.0}], index=index, dtype=object)
    frame["content_hash"] = "0" * 64
    frame["run_id"] = "run-1"
    frame["writer_id"] = "writer-1"
    frame["data_quality_flags"] = [[]]

    report = evaluate_price_quality(
        frame,
        now=now,
        thresholds=PriceQualityThresholds(require_point_in_time_metadata=True),
    )

    assert not report.passed
    assert "content_hash_mismatch" in report.critical_flags
    assert report.metrics["content_hash_mismatch_count"] == 1


def _strict_pit_frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    frame = _prices(index)
    payloads: list[dict[str, float]] = []
    for _, row in frame.iterrows():
        payloads.append(
            {field: float(row[field]) for field in ("open", "high", "low", "close", "bid", "ask")}
        )
    for column in ("event_time", "available_time", "ingested_time"):
        frame[column] = index
    frame["source"] = "test"
    frame["source_record_id"] = [f"test:{position}" for position in range(len(frame))]
    frame["schema_version"] = 1
    frame["payload"] = pd.Series(payloads, index=index, dtype=object)
    frame["content_hash"] = [canonical_content_hash(payload) for payload in payloads]
    frame["run_id"] = "run-1"
    frame["writer_id"] = "writer-1"
    frame["data_quality_flags"] = pd.Series([[] for _ in range(len(frame))], index=index)
    return frame


def test_strict_pit_quality_binds_materialized_prices_to_hashed_payload() -> None:
    now = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
    index = pd.date_range("2024-01-01T00:00:00Z", periods=1, freq="h")
    frame = _strict_pit_frame(index)

    clean = evaluate_price_quality(
        frame,
        now=now,
        thresholds=PriceQualityThresholds(require_point_in_time_metadata=True),
    )
    assert clean.passed

    frame.loc[index[0], "close"] = 100.1
    tampered = evaluate_price_quality(
        frame,
        now=now,
        thresholds=PriceQualityThresholds(require_point_in_time_metadata=True),
    )

    assert not tampered.passed
    assert "payload_projection_mismatch" in tampered.critical_flags
    assert tampered.metrics["payload_projection_mismatch_count"] == 1


def test_strict_pit_quality_rejects_unhashed_materialized_projection() -> None:
    now = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
    index = pd.date_range("2024-01-01T00:00:00Z", periods=1, freq="h")
    frame = _strict_pit_frame(index)
    payload = dict(frame.iloc[0]["payload"])
    payload.pop("bid")
    frame.at[index[0], "payload"] = payload
    frame.at[index[0], "content_hash"] = canonical_content_hash(payload)

    report = evaluate_price_quality(
        frame,
        now=now,
        thresholds=PriceQualityThresholds(require_point_in_time_metadata=True),
    )

    assert not report.passed
    assert "payload_projection_missing" in report.critical_flags
    assert report.metrics["payload_projection_missing_count"] == 1


@pytest.mark.parametrize(
    "timestamp_column",
    [
        "event_time",
        "available_time",
        "ingested_time",
        "published_time",
        "source_time",
        "revision_time",
    ],
)
def test_strict_pit_quality_rejects_future_metadata_timestamps(
    timestamp_column: str,
) -> None:
    now = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
    index = pd.date_range("2024-01-01T00:00:00Z", periods=1, freq="h")
    frame = _prices(index)
    for column in (
        "event_time",
        "available_time",
        "ingested_time",
        "published_time",
        "source_time",
        "revision_time",
    ):
        frame[column] = index
    frame[timestamp_column] = [now + timedelta(seconds=6)]
    frame["source"] = "test"
    frame["source_record_id"] = "test:1"
    frame["schema_version"] = 1
    frame["content_hash"] = "a" * 64
    frame["run_id"] = "run-1"
    frame["writer_id"] = "writer-1"
    frame["data_quality_flags"] = [[]]

    report = evaluate_price_quality(
        frame,
        now=now,
        thresholds=PriceQualityThresholds(
            max_future_skew=timedelta(seconds=5),
            require_point_in_time_metadata=True,
        ),
    )

    assert not report.passed
    assert "future_point_in_time_timestamp" in report.critical_flags
    assert report.metrics["future_point_in_time_timestamp_counts"][timestamp_column] == 1
