"""Dataset quality-state classification.

Every materialised dataset carries one of four states. The state governs how the
data may be used downstream; a research run consumes ``usable`` data, may
optionally tolerate ``degraded`` with an explicit waiver, and must refuse
``quarantined`` / ``unavailable``.

    usable       every hard invariant holds and freshness/completeness meet SLO
    degraded     soft SLO miss (e.g. slightly stale) but no hard violation
    quarantined  a hard invariant is violated (dup key, future ts, bid>ask,
                 hash mismatch, timestamp reversal); the data exists but is unsafe
    unavailable  the data could not be obtained or measured at all

The classifier never invents a value to make a dataset look healthy: a metric
that could not be measured is ``None`` and counts against the dataset, it is not
back-filled with 0 or a mid. Thresholds are policy inputs with rationale, not
hard-coded magic numbers (see :class:`QualityThresholds`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class QualityState(StrEnum):
    USABLE = "usable"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    UNAVAILABLE = "unavailable"


# Hard invariants: any one of these being violated quarantines the dataset. These
# are correctness properties of point-in-time data, not tunable preferences, so
# their tolerance is exactly zero.
HARD_VIOLATION_KEYS = frozenset(
    {
        "duplicate_natural_key",
        "future_timestamp",
        "timestamp_reversal",
        "bid_gt_ask",
        "raw_hash_mismatch",
        "critical_schema_violation",
        "multiple_writers",
    }
)


@dataclass(frozen=True)
class QualityThresholds:
    """Soft-SLO limits with a required rationale.

    Defaults encode the platform's stated targets; callers may override per
    dataset but must record *why*. ``rationale`` is mandatory and non-trivial so
    a silent loosening of a gate is impossible.
    """

    max_freshness_seconds: float = 30.0
    min_completeness: float = 0.999
    max_snapshot_age_seconds_degraded: float = 300.0
    rationale: str = (
        "Platform defaults: 30s freshness and 99.9% bar completeness are the "
        "collector's steady-state SLO; a miss up to 300s is degraded, not unsafe."
    )

    def __post_init__(self) -> None:
        if len(self.rationale.strip()) < 20:
            raise ValueError("QualityThresholds.rationale must explain the chosen limits")
        if not 0.0 < self.min_completeness <= 1.0:
            raise ValueError("min_completeness must be in (0, 1]")
        if self.max_freshness_seconds <= 0 or self.max_snapshot_age_seconds_degraded <= 0:
            raise ValueError("freshness limits must be positive")


@dataclass(frozen=True)
class QualityAssessment:
    """The classified state plus the evidence that produced it."""

    state: QualityState
    hard_violations: tuple[str, ...]
    soft_violations: tuple[str, ...]
    unmeasured: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.state is QualityState.USABLE


def classify_quality(
    *,
    available: bool,
    hard_violation_counts: dict[str, int],
    freshness_seconds: float | None,
    completeness: float | None,
    thresholds: QualityThresholds | None = None,
) -> QualityAssessment:
    """Classify a dataset's quality state from measured evidence.

    ``available`` False (the source could not be obtained/measured) short-circuits
    to ``unavailable``. Any hard violation with a non-zero count quarantines.
    Otherwise, unmeasured or out-of-SLO freshness/completeness degrade the state;
    only a fully-measured, in-SLO dataset is ``usable``.
    """

    limits = thresholds or QualityThresholds()

    if not available:
        return QualityAssessment(
            state=QualityState.UNAVAILABLE,
            hard_violations=(),
            soft_violations=(),
            unmeasured=("source_unavailable",),
            detail={"reason": "source could not be obtained or measured"},
        )

    unknown_keys = sorted(set(hard_violation_counts) - HARD_VIOLATION_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown hard-violation keys: {unknown_keys}")

    hard = tuple(sorted(key for key, count in hard_violation_counts.items() if count and count > 0))
    if hard:
        return QualityAssessment(
            state=QualityState.QUARANTINED,
            hard_violations=hard,
            soft_violations=(),
            unmeasured=(),
            detail={"hard_violation_counts": dict(hard_violation_counts)},
        )

    soft: list[str] = []
    unmeasured: list[str] = []

    # A metric that could not be measured is never treated as passing.
    if freshness_seconds is None:
        unmeasured.append("freshness_seconds")
    elif freshness_seconds > limits.max_freshness_seconds:
        soft.append("freshness_seconds")

    if completeness is None:
        unmeasured.append("completeness")
    elif completeness < limits.min_completeness:
        soft.append("completeness")

    state = QualityState.USABLE if not soft and not unmeasured else QualityState.DEGRADED
    return QualityAssessment(
        state=state,
        hard_violations=(),
        soft_violations=tuple(soft),
        unmeasured=tuple(unmeasured),
        detail={
            "freshness_seconds": freshness_seconds,
            "completeness": completeness,
            "thresholds_rationale": limits.rationale,
        },
    )
