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
