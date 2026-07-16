"""Leakage-resistant temporal partitions for model and strategy validation.

Every split operates on both the prediction timestamp and the time at which its
label becomes complete.  Purging by row count alone is insufficient when labels
have different horizons, so a training observation is eligible only when its
entire label interval ends before the next evaluation interval begins.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from typing import Literal

import numpy as np
import pandas as pd

ValidationMode = Literal["anchored", "rolling"]


class TemporalLeakageError(ValueError):
    """Raised when a split cannot prove chronological isolation."""


@dataclass(frozen=True)
class PurgedWalkForwardConfig:
    train_size: int = 500
    test_size: int = 100
    step_size: int | None = None
    mode: ValidationMode = "anchored"
    purge: timedelta = timedelta(0)
    embargo: timedelta = timedelta(0)
    min_train_size: int | None = None

    def __post_init__(self) -> None:
        if self.train_size < 1 or self.test_size < 1:
            raise ValueError("train_size and test_size must be positive")
        step = self.step_size or self.test_size
        if step < self.test_size:
            raise ValueError("step_size must prevent overlapping test windows")
        if self.mode not in {"anchored", "rolling"}:
            raise ValueError("mode must be anchored or rolling")
        if self.purge < timedelta(0) or self.embargo < timedelta(0):
            raise ValueError("purge and embargo must be non-negative")
        if self.min_train_size is not None and self.min_train_size < 1:
            raise ValueError("min_train_size must be positive")


@dataclass(frozen=True)
class TemporalFold:
    fold: int
    train: tuple[int, ...]
    test: tuple[int, ...]
    purged: tuple[int, ...] = ()
    embargoed: tuple[int, ...] = ()
    test_groups: tuple[int, ...] = ()

    @property
    def train_size(self) -> int:
        return len(self.train)

    @property
    def test_size(self) -> int:
        return len(self.test)


def purged_walk_forward_splits(
    prediction_times: Sequence[object] | pd.DatetimeIndex,
    label_end_times: Sequence[object] | pd.DatetimeIndex,
    config: PurgedWalkForwardConfig | None = None,
) -> list[TemporalFold]:
    """Build anchored or rolling walk-forward folds with label-aware purging."""

    settings = config or PurgedWalkForwardConfig()
    prediction, label_end = _validated_intervals(prediction_times, label_end_times)
    n_rows = len(prediction)
    step = settings.step_size or settings.test_size
    minimum = settings.min_train_size or settings.train_size
    folds: list[TemporalFold] = []
    test_start_position = settings.train_size

    while test_start_position + settings.test_size <= n_rows:
        test_positions = tuple(range(test_start_position, test_start_position + settings.test_size))
        test_start = prediction[test_start_position]
        label_cutoff = test_start - settings.purge
        prediction_cutoff = test_start - settings.embargo
        candidates = np.arange(test_start_position, dtype=int)
        label_safe = label_end[:test_start_position] < label_cutoff
        embargo_safe = prediction[:test_start_position] < prediction_cutoff
        eligible = candidates[label_safe & embargo_safe]
        purged = tuple(int(value) for value in candidates[~label_safe])
        embargoed = tuple(int(value) for value in candidates[label_safe & ~embargo_safe])

        if len(eligible) >= minimum:
            if settings.mode == "rolling":
                selected = eligible[-settings.train_size :]
            else:
                selected = eligible
            fold = TemporalFold(
                fold=len(folds) + 1,
                train=tuple(int(value) for value in selected),
                test=test_positions,
                purged=purged,
                embargoed=embargoed,
            )
            assert_no_temporal_overlap(
                fold.train,
                fold.test,
                prediction,
                label_end,
                purge=settings.purge,
                embargo=settings.embargo,
            )
            folds.append(fold)
        test_start_position += step

    if not folds:
        raise TemporalLeakageError(
            "no valid walk-forward fold remains after label purging and embargo"
        )
    return folds


@dataclass(frozen=True)
class CombinatorialPurgedConfig:
    n_groups: int = 6
    n_test_groups: int = 2
    purge: timedelta = timedelta(0)
    embargo: timedelta = timedelta(0)
    min_train_size: int = 1

    def __post_init__(self) -> None:
        if self.n_groups < 4:
            raise ValueError("n_groups must be at least 4")
        if not 1 <= self.n_test_groups < self.n_groups:
            raise ValueError("n_test_groups must be between 1 and n_groups - 1")
        if self.purge < timedelta(0) or self.embargo < timedelta(0):
            raise ValueError("purge and embargo must be non-negative")
        if self.min_train_size < 1:
            raise ValueError("min_train_size must be positive")


def combinatorial_purged_splits(
    prediction_times: Sequence[object] | pd.DatetimeIndex,
    label_end_times: Sequence[object] | pd.DatetimeIndex,
    config: CombinatorialPurgedConfig | None = None,
) -> list[TemporalFold]:
    """Return deterministic CPCV-style folds over contiguous time groups.

    Training rows are removed when their label interval overlaps any selected
    test group. Rows immediately after a test interval are embargoed as well.
    """

    settings = config or CombinatorialPurgedConfig()
    prediction, label_end = _validated_intervals(prediction_times, label_end_times)
    if len(prediction) < settings.n_groups:
        raise ValueError("fewer observations than CPCV groups")
    groups = [
        np.asarray(group, dtype=int)
        for group in np.array_split(np.arange(len(prediction)), settings.n_groups)
    ]
    folds: list[TemporalFold] = []

    for selected_groups in combinations(range(settings.n_groups), settings.n_test_groups):
        test_arrays = [groups[group] for group in selected_groups]
        test = np.sort(np.concatenate(test_arrays))
        train_groups = [group for group in range(settings.n_groups) if group not in selected_groups]
        candidates = np.sort(np.concatenate([groups[group] for group in train_groups]))
        intervals = [
            (
                prediction[group[0]] - settings.purge,
                max(label_end[group]) + settings.purge,
            )
            for group in test_arrays
        ]
        keep: list[int] = []
        purged: list[int] = []
        embargoed: list[int] = []
        for position in candidates:
            sample_start = prediction[position]
            sample_end = label_end[position]
            overlaps = any(
                sample_start <= interval_end and sample_end >= interval_start
                for interval_start, interval_end in intervals
            )
            in_embargo = any(
                interval_end < sample_start <= interval_end + settings.embargo
                for _, interval_end in intervals
            )
            if overlaps:
                purged.append(int(position))
            elif in_embargo:
                embargoed.append(int(position))
            else:
                keep.append(int(position))
        if len(keep) < settings.min_train_size:
            raise TemporalLeakageError(
                f"CPCV groups {selected_groups} leave only {len(keep)} training rows"
            )
        folds.append(
            TemporalFold(
                fold=len(folds) + 1,
                train=tuple(keep),
                test=tuple(int(value) for value in test),
                purged=tuple(purged),
                embargoed=tuple(embargoed),
                test_groups=tuple(selected_groups),
            )
        )
    return folds


@dataclass(frozen=True)
class ModelPartitionConfig:
    train_fraction: float = 0.50
    tune_fraction: float = 0.15
    calibration_fraction: float = 0.10
    test_fraction: float = 0.15
    lockbox_fraction: float = 0.10
    purge: timedelta = timedelta(0)
    embargo: timedelta = timedelta(0)
    min_rows_per_partition: int = 1

    def __post_init__(self) -> None:
        fractions = (
            self.train_fraction,
            self.tune_fraction,
            self.calibration_fraction,
            self.test_fraction,
            self.lockbox_fraction,
        )
        if any(value <= 0 for value in fractions):
            raise ValueError("all partition fractions must be positive")
        if not np.isclose(sum(fractions), 1.0):
            raise ValueError("partition fractions must sum to 1")
        if self.purge < timedelta(0) or self.embargo < timedelta(0):
            raise ValueError("purge and embargo must be non-negative")
        if self.min_rows_per_partition < 1:
            raise ValueError("min_rows_per_partition must be positive")


class ModelPartitions:
    """Five-way temporal partition with one-time lockbox access.

    Hyperparameters use ``train``/``tune``; probability calibration uses only
    ``calibration``; ordinary final evaluation uses ``test``.  The final lockbox
    can be opened once only after the selection process has been declared complete.
    """

    def __init__(self, partitions: Mapping[str, tuple[int, ...]]) -> None:
        self.train = partitions["train"]
        self.tune = partitions["tune"]
        self.calibration = partitions["calibration"]
        self.test = partitions["test"]
        self._lockbox = partitions["lockbox"]
        self._lockbox_opened = False
        self._lockbox_purpose = ""

    @property
    def lockbox_size(self) -> int:
        return len(self._lockbox)

    @property
    def lockbox_opened(self) -> bool:
        return self._lockbox_opened

    @property
    def lockbox_commitment(self) -> str:
        """Commit to withheld row positions without exposing outcomes."""

        encoded = json.dumps(list(self._lockbox), separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def withheld_lockbox_positions(self) -> tuple[int, ...]:
        """Return positions solely so outcome columns can be withheld at rest.

        Positions and prediction inputs are not secret. ``open_lockbox`` remains
        the guard for attaching/evaluating the externally held outcomes.
        """

        return self._lockbox

    def open_lockbox(self, *, selection_complete: bool, purpose: str) -> tuple[int, ...]:
        if not selection_complete:
            raise TemporalLeakageError(
                "lockbox cannot be opened before model selection is complete"
            )
        if not purpose.strip():
            raise ValueError("lockbox purpose must be recorded")
        if self._lockbox_opened:
            raise TemporalLeakageError(f"lockbox was already opened for: {self._lockbox_purpose}")
        self._lockbox_opened = True
        self._lockbox_purpose = purpose.strip()
        return self._lockbox

    def audit(self) -> dict[str, object]:
        return {
            "train_rows": len(self.train),
            "tune_rows": len(self.tune),
            "calibration_rows": len(self.calibration),
            "test_rows": len(self.test),
            "lockbox_rows": len(self._lockbox),
            "lockbox_commitment_sha256": self.lockbox_commitment,
            "lockbox_opened": self._lockbox_opened,
            "lockbox_purpose": self._lockbox_purpose,
        }


def chronological_model_partitions(
    prediction_times: Sequence[object] | pd.DatetimeIndex,
    label_end_times: Sequence[object] | pd.DatetimeIndex,
    config: ModelPartitionConfig | None = None,
) -> ModelPartitions:
    """Create train/tune/calibration/test/lockbox slices in strict time order."""

    settings = config or ModelPartitionConfig()
    prediction, label_end = _validated_intervals(prediction_times, label_end_times)
    names = ("train", "tune", "calibration", "test", "lockbox")
    fractions = (
        settings.train_fraction,
        settings.tune_fraction,
        settings.calibration_fraction,
        settings.test_fraction,
        settings.lockbox_fraction,
    )
    cumulative = np.cumsum(fractions)
    boundaries = (
        [0]
        + [int(np.floor(len(prediction) * value)) for value in cumulative[:-1]]
        + [len(prediction)]
    )
    partitions: dict[str, tuple[int, ...]] = {}
    for offset, name in enumerate(names):
        start, stop = boundaries[offset], boundaries[offset + 1]
        raw = np.arange(start, stop, dtype=int)
        if name != "lockbox" and stop < len(prediction):
            next_start = prediction[stop]
            safe = (label_end[raw] < next_start - settings.purge) & (
                prediction[raw] < next_start - settings.embargo
            )
            raw = raw[safe]
        if len(raw) < settings.min_rows_per_partition:
            raise TemporalLeakageError(
                f"{name} has {len(raw)} rows after purge/embargo; "
                f"minimum is {settings.min_rows_per_partition}"
            )
        partitions[name] = tuple(int(value) for value in raw)

    for left, right in zip(names, names[1:]):
        assert_no_temporal_overlap(
            partitions[left],
            partitions[right],
            prediction,
            label_end,
            purge=settings.purge,
            embargo=settings.embargo,
        )
    return ModelPartitions(partitions)


def assert_no_temporal_overlap(
    train: Sequence[int],
    test: Sequence[int],
    prediction_times: Sequence[object] | pd.DatetimeIndex,
    label_end_times: Sequence[object] | pd.DatetimeIndex,
    *,
    purge: timedelta = timedelta(0),
    embargo: timedelta = timedelta(0),
) -> None:
    """Reject random/interleaved splits and any label window crossing the boundary."""

    prediction, label_end = _validated_intervals(prediction_times, label_end_times)
    if not train or not test:
        raise TemporalLeakageError("train and test indices must be non-empty")
    train_array = np.asarray(train, dtype=int)
    test_array = np.asarray(test, dtype=int)
    if train_array.min() < 0 or test_array.min() < 0:
        raise IndexError("split positions must be non-negative")
    if train_array.max() >= len(prediction) or test_array.max() >= len(prediction):
        raise IndexError("split position out of range")
    test_start = prediction[test_array].min()
    if prediction[train_array].max() >= test_start - embargo:
        raise TemporalLeakageError("training predictions are interleaved with the test interval")
    if label_end[train_array].max() >= test_start - purge:
        raise TemporalLeakageError("a training label overlaps the test interval")


def _validated_intervals(
    prediction_times: Sequence[object] | pd.DatetimeIndex,
    label_end_times: Sequence[object] | pd.DatetimeIndex,
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    try:
        prediction = pd.DatetimeIndex(prediction_times)
        label_end = pd.DatetimeIndex(label_end_times)
    except (TypeError, ValueError) as error:
        raise TemporalLeakageError("timestamps could not be parsed") from error
    if len(prediction) != len(label_end) or len(prediction) == 0:
        raise ValueError("prediction_times and label_end_times must have equal non-zero length")
    if prediction.tz is None or label_end.tz is None:
        raise TemporalLeakageError("all timestamps must be timezone-aware")
    prediction = prediction.tz_convert("UTC")
    label_end = label_end.tz_convert("UTC")
    if prediction.hasnans or label_end.hasnans:
        raise TemporalLeakageError("timestamps cannot contain NaT")
    if not prediction.is_monotonic_increasing or prediction.has_duplicates:
        raise TemporalLeakageError("prediction timestamps must be unique and monotonic")
    if bool((label_end < prediction).any()):
        raise TemporalLeakageError("label_end cannot precede prediction_time")
    return prediction, label_end
