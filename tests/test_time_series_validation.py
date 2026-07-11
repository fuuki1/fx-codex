from __future__ import annotations

from datetime import timedelta
from math import comb

import pandas as pd
import pytest

from fx_backtester.time_series_validation import (
    CombinatorialPurgedConfig,
    ModelPartitionConfig,
    PurgedWalkForwardConfig,
    TemporalLeakageError,
    assert_no_temporal_overlap,
    chronological_model_partitions,
    combinatorial_purged_splits,
    purged_walk_forward_splits,
)


def _intervals(rows: int = 80, horizon_hours: int = 2) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    prediction = pd.date_range("2024-01-01T00:00:00Z", periods=rows, freq="h")
    return prediction, prediction + pd.Timedelta(hours=horizon_hours)


def test_rolling_walk_forward_purges_overlapping_labels_and_has_disjoint_tests() -> None:
    prediction, label_end = _intervals()
    folds = purged_walk_forward_splits(
        prediction,
        label_end,
        PurgedWalkForwardConfig(train_size=12, test_size=6, mode="rolling"),
    )

    assert folds
    for fold in folds:
        assert len(fold.train) == 12
        assert max(label_end[list(fold.train)]) < min(prediction[list(fold.test)])
        assert set(fold.train).isdisjoint(fold.test)
    for previous, current in zip(folds, folds[1:]):
        assert max(previous.test) < min(current.test)


def test_anchored_walk_forward_grows_training_history_and_applies_embargo() -> None:
    prediction, label_end = _intervals(horizon_hours=1)
    folds = purged_walk_forward_splits(
        prediction,
        label_end,
        PurgedWalkForwardConfig(
            train_size=10,
            test_size=5,
            mode="anchored",
            embargo=timedelta(hours=2),
        ),
    )

    assert len(folds[1].train) > len(folds[0].train)
    for fold in folds:
        boundary = min(prediction[list(fold.test)])
        assert max(prediction[list(fold.train)]) < boundary - timedelta(hours=2)


def test_random_or_label_overlapping_split_is_rejected() -> None:
    prediction, label_end = _intervals(rows=10, horizon_hours=2)
    with pytest.raises(TemporalLeakageError, match="interleaved"):
        assert_no_temporal_overlap([0, 2, 4], [3, 5], prediction, label_end)
    with pytest.raises(TemporalLeakageError, match="label overlaps"):
        assert_no_temporal_overlap([0, 1, 2], [3, 4], prediction, label_end)


def test_cpcv_is_deterministic_and_purges_every_selected_test_group() -> None:
    prediction, label_end = _intervals(rows=60, horizon_hours=2)
    config = CombinatorialPurgedConfig(n_groups=6, n_test_groups=2, embargo=timedelta(hours=1))

    first = combinatorial_purged_splits(prediction, label_end, config)
    second = combinatorial_purged_splits(prediction, label_end, config)

    assert first == second
    assert len(first) == comb(6, 2)
    for fold in first:
        assert set(fold.train).isdisjoint(fold.test)
        assert set(fold.purged).isdisjoint(fold.train)
        assert len(fold.test_groups) == 2


def test_five_way_partition_separates_calibration_test_and_lockbox() -> None:
    prediction, label_end = _intervals(rows=120, horizon_hours=1)
    partitions = chronological_model_partitions(
        prediction,
        label_end,
        ModelPartitionConfig(min_rows_per_partition=5),
    )

    ordered = [
        partitions.train,
        partitions.tune,
        partitions.calibration,
        partitions.test,
    ]
    for left, right in zip(ordered, ordered[1:]):
        assert max(left) < min(right)
        assert max(label_end[list(left)]) < min(prediction[list(right)])
    assert partitions.lockbox_size == 12
    assert not partitions.lockbox_opened
    assert len(partitions.lockbox_commitment) == 64
    assert partitions.audit()["lockbox_commitment_sha256"] == partitions.lockbox_commitment
    assert len(partitions.withheld_lockbox_positions) == partitions.lockbox_size
    assert not partitions.lockbox_opened


def test_lockbox_requires_selection_complete_and_can_be_opened_only_once() -> None:
    prediction, label_end = _intervals(rows=120, horizon_hours=1)
    partitions = chronological_model_partitions(prediction, label_end)

    with pytest.raises(TemporalLeakageError, match="selection is complete"):
        partitions.open_lockbox(selection_complete=False, purpose="premature")
    lockbox = partitions.open_lockbox(
        selection_complete=True, purpose="single final governance evaluation"
    )
    assert len(lockbox) == partitions.lockbox_size
    assert partitions.lockbox_opened
    with pytest.raises(TemporalLeakageError, match="already opened"):
        partitions.open_lockbox(selection_complete=True, purpose="peek again")


def test_temporal_validation_rejects_naive_and_unsorted_timestamps() -> None:
    prediction, label_end = _intervals(rows=20)
    with pytest.raises(TemporalLeakageError, match="timezone-aware"):
        purged_walk_forward_splits(
            prediction.tz_localize(None),
            label_end.tz_localize(None),
            PurgedWalkForwardConfig(train_size=5, test_size=2),
        )
    with pytest.raises(TemporalLeakageError, match="unique and monotonic"):
        purged_walk_forward_splits(
            prediction[::-1],
            label_end[::-1],
            PurgedWalkForwardConfig(train_size=5, test_size=2),
        )
