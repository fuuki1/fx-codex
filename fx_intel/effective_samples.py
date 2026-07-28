"""Conservative effective-sample accounting for overlapping FX decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, UTC
import math
from typing import TypeVar
from zoneinfo import ZoneInfo

from .evaluation_labels import KNOWN_NET_LABEL_VERSIONS

DEFAULT_MINIMUM_GAP = timedelta(minutes=5)
DEFAULT_MIN_EFFECTIVE_SAMPLES = 20
FX_SESSION_TIMEZONE = ZoneInfo("America/New_York")
FX_SESSION_ROLLOVER = time(17, 0)
T = TypeVar("T")


class EffectiveSampleConflict(ValueError):
    """The same decision/label key carries different holding metadata."""


@dataclass(frozen=True)
class EffectiveSampleSummary:
    raw_samples: int
    unique_samples: int
    effective_samples: int
    overlap_ratio: float | None
    cluster_count: int
    market_days: int
    invalid_samples: int
    duplicate_samples: int
    minimum_gap_seconds: float
    min_samples: int
    sample_ok: bool
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    selected_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_samples": self.raw_samples,
            "unique_samples": self.unique_samples,
            "effective_samples": self.effective_samples,
            "overlap_ratio": self.overlap_ratio,
            "cluster_count": self.cluster_count,
            "market_days": self.market_days,
            "invalid_samples": self.invalid_samples,
            "duplicate_samples": self.duplicate_samples,
            "minimum_gap_seconds": self.minimum_gap_seconds,
            "min_samples": self.min_samples,
            "sample_ok": self.sample_ok,
            "invalid_reasons": dict(self.invalid_reasons),
            "selected_keys": list(self.selected_keys),
        }


@dataclass(frozen=True)
class _Observation:
    decision_id: str
    label_version: str
    symbol: str
    direction: str
    timeframe: str
    horizon_hours: float
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[str, str]:
        return self.decision_id, self.label_version

    @property
    def key_text(self) -> str:
        return f"{self.decision_id}+{self.label_version}"

    @property
    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.symbol,
            self.direction,
            self.timeframe,
            self.horizon_hours,
            self.start,
            self.end,
        )

    @property
    def exposures(self) -> dict[str, int]:
        base, quote = self.symbol[:3], self.symbol[3:]
        sign = 1 if self.direction == "long" else -1
        return {base: sign, quote: -sign}


def summarize_effective_samples(
    records: Sequence[object],
    *,
    min_samples: int = DEFAULT_MIN_EFFECTIVE_SAMPLES,
    minimum_gap: timedelta = DEFAULT_MINIMUM_GAP,
) -> EffectiveSampleSummary:
    """Count a deterministic, non-overlapping subset of canonical outcomes.

    Rows sharing the same symbol, or any currency exposure in either direction,
    are dependent while their holding intervals (plus ``minimum_gap``) overlap.
    Connected overlap components are reported separately from the greedy
    effective subset so chained dependence remains visible.
    """

    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    if minimum_gap.total_seconds() < 0:
        raise ValueError("minimum_gap must be nonnegative")

    invalid_reasons: dict[str, int] = {}
    observations: dict[tuple[str, str], _Observation] = {}
    duplicate_samples = 0
    for record in records:
        observation, reason = _observation(record)
        if reason is not None:
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
            continue
        assert observation is not None
        existing = observations.get(observation.key)
        if existing is None:
            observations[observation.key] = observation
            continue
        if existing.fingerprint != observation.fingerprint:
            raise EffectiveSampleConflict(
                "conflicting effective-sample interval for "
                f"{observation.decision_id} + {observation.label_version}"
            )
        duplicate_samples += 1

    ordered = sorted(
        observations.values(),
        key=lambda row: (row.start, row.end, row.decision_id, row.label_version),
    )
    selected: list[_Observation] = []
    active_selected: list[_Observation] = []
    for observation in ordered:
        active_selected = [
            row for row in active_selected if row.end + minimum_gap > observation.start
        ]
        if any(_dependent(row, observation, minimum_gap) for row in active_selected):
            continue
        selected.append(observation)
        active_selected.append(observation)

    cluster_count = _cluster_count(ordered, minimum_gap)
    unique_samples = len(ordered)
    effective_samples = len(selected)
    invalid_samples = sum(invalid_reasons.values())
    overlap_ratio = 1.0 - effective_samples / unique_samples if unique_samples > 0 else None
    market_days = len({_fx_market_day(row.start) for row in ordered})
    return EffectiveSampleSummary(
        raw_samples=len(records),
        unique_samples=unique_samples,
        effective_samples=effective_samples,
        overlap_ratio=overlap_ratio,
        cluster_count=cluster_count,
        market_days=market_days,
        invalid_samples=invalid_samples,
        duplicate_samples=duplicate_samples,
        minimum_gap_seconds=minimum_gap.total_seconds(),
        min_samples=min_samples,
        sample_ok=invalid_samples == 0 and effective_samples >= min_samples,
        invalid_reasons=dict(sorted(invalid_reasons.items())),
        selected_keys=tuple(row.key_text for row in selected),
    )


def select_effective_samples(
    records: Sequence[T],
    *,
    min_samples: int = DEFAULT_MIN_EFFECTIVE_SAMPLES,
    minimum_gap: timedelta = DEFAULT_MINIMUM_GAP,
) -> tuple[list[T], EffectiveSampleSummary]:
    """Return the deterministic effective subset and its full audit summary."""

    summary = summarize_effective_samples(
        records,
        min_samples=min_samples,
        minimum_gap=minimum_gap,
    )
    selected_keys = set(summary.selected_keys)
    first_by_key: dict[str, T] = {}
    for record in records:
        key = f"{_get(record, 'decision_id')}+{_get(record, 'label_version')}"
        if key in selected_keys and key not in first_by_key:
            first_by_key[key] = record
    return [first_by_key[key] for key in summary.selected_keys], summary


def _observation(record: object) -> tuple[_Observation | None, str | None]:
    decision_id = str(_get(record, "decision_id") or "").strip()
    if not decision_id:
        return None, "missing_decision_id"
    label_version = str(_get(record, "label_version") or "").strip()
    if not label_version:
        return None, "missing_label_version"
    if label_version not in KNOWN_NET_LABEL_VERSIONS:
        return None, "unknown_label_version"
    if _get(record, "net_label_eligible") is not True:
        return None, "ineligible_net_label"
    symbol = _normalized_symbol(_get(record, "symbol"))
    if symbol is None:
        return None, "invalid_symbol"
    direction = str(_get(record, "direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        return None, "invalid_direction"
    timeframe = str(_get(record, "timeframe") or "").strip()
    if not timeframe:
        return None, "missing_timeframe"
    horizon = _positive_number(_get(record, "horizon_hours"))
    if horizon is None:
        return None, "invalid_horizon_hours"
    start, reason = _aware_time(_get(record, "prediction_time"), "prediction_time")
    if reason is not None:
        return None, reason
    end, reason = _aware_time(_get(record, "holding_end_time"), "holding_end_time")
    if reason is not None:
        return None, reason
    assert start is not None and end is not None
    if end <= start:
        return None, "invalid_holding_interval"
    return (
        _Observation(
            decision_id=decision_id,
            label_version=label_version,
            symbol=symbol,
            direction=direction,
            timeframe=timeframe,
            horizon_hours=horizon,
            start=start,
            end=end,
        ),
        None,
    )


def _get(record: object, key: str) -> object:
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _normalized_symbol(value: object) -> str | None:
    symbol = str(value or "").upper()
    for separator in ("/", "_", "-"):
        symbol = symbol.replace(separator, "")
    return symbol if len(symbol) == 6 and symbol.isalpha() else None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _aware_time(value: object, name: str) -> tuple[datetime | None, str | None]:
    if value in (None, ""):
        return None, f"missing_{name}"
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None, f"invalid_{name}"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, f"naive_{name}"
    return parsed.astimezone(UTC), None


def _dependent(
    left: _Observation,
    right: _Observation,
    minimum_gap: timedelta,
) -> bool:
    if left.end + minimum_gap <= right.start or right.end + minimum_gap <= left.start:
        return False
    if left.symbol == right.symbol:
        return True
    # Sharing a currency is what creates mechanical correlation, regardless of
    # sign. EURUSD long and GBPUSD short both move with USD — inversely, but
    # not independently — so requiring equal signs would count them as two
    # independent samples and inflate the evidence behind every gate that
    # consumes this number. Direction stays available to callers as portfolio
    # risk metadata; it must not weaken the dependence test.
    right_exposures = right.exposures
    return any(currency in right_exposures for currency in left.exposures)


def _cluster_count(observations: Sequence[_Observation], minimum_gap: timedelta) -> int:
    if not observations:
        return 0
    parent = list(range(len(observations)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    active: list[int] = []
    for index, observation in enumerate(observations):
        active = [
            other for other in active if observations[other].end + minimum_gap > observation.start
        ]
        for other in active:
            if _dependent(observations[other], observation, minimum_gap):
                union(other, index)
        active.append(index)
    return len({find(index) for index in range(len(observations))})


def _fx_market_day(value: datetime) -> date:
    local = value.astimezone(FX_SESSION_TIMEZONE)
    if local.timetz().replace(tzinfo=None) >= FX_SESSION_ROLLOVER:
        return local.date() + timedelta(days=1)
    return local.date()
