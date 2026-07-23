"""重複する判断を独立サンプルとして数えない共通契約の反証テスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel.effective_samples import (
    EffectiveSampleConflict,
    select_effective_samples,
    summarize_effective_samples,
)
from fx_intel.evaluation_labels import NET_LABEL_VERSION

START = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


def _row(
    index: int,
    start: datetime,
    *,
    symbol: str = "USDJPY",
    direction: str = "long",
    timeframe: str = "1h",
    horizon_hours: float = 1.0,
) -> dict[str, object]:
    return {
        "decision_id": f"d-{index}",
        "label_version": NET_LABEL_VERSION,
        "symbol": symbol,
        "direction": direction,
        "timeframe": timeframe,
        "horizon_hours": horizon_hours,
        "prediction_time": start.isoformat(),
        "holding_end_time": (start + timedelta(hours=horizon_hours)).isoformat(),
        "net_label_eligible": True,
    }


def test_raw_twenty_can_be_only_five_effective_samples() -> None:
    rows = []
    for batch in range(5):
        batch_start = START + timedelta(hours=batch * 2)
        rows.extend(
            _row(batch * 4 + offset, batch_start + timedelta(minutes=offset * 5))
            for offset in range(4)
        )

    summary = summarize_effective_samples(rows, min_samples=6)

    assert summary.raw_samples == 20
    assert summary.effective_samples == 5
    assert summary.overlap_ratio == pytest.approx(0.75)
    assert summary.sample_ok is False
    assert (
        summarize_effective_samples(list(reversed(rows)), min_samples=6).selected_keys
        == summary.selected_keys
    )


def test_five_minute_predictions_with_one_hour_horizon_are_not_twelve_samples() -> None:
    rows = [_row(index, START + timedelta(minutes=index * 5)) for index in range(12)]

    summary = summarize_effective_samples(rows, min_samples=2)

    assert summary.raw_samples == 12
    assert summary.effective_samples == 1
    assert summary.cluster_count == 1
    assert summary.sample_ok is False


def test_separate_market_days_increase_effective_samples() -> None:
    rows = [_row(index, START + timedelta(days=index)) for index in range(3)]

    summary = summarize_effective_samples(rows, min_samples=3)

    assert summary.effective_samples == 3
    assert summary.market_days == 3
    assert summary.sample_ok is True


def test_simultaneous_usd_factor_is_not_counted_once_per_pair() -> None:
    rows = [
        _row(1, START, symbol="EURUSD", direction="long"),
        _row(2, START, symbol="GBPUSD", direction="long"),
        _row(3, START, symbol="USDJPY", direction="short"),
    ]

    summary = summarize_effective_samples(rows, min_samples=2)

    assert summary.raw_samples == 3
    assert summary.effective_samples == 1
    assert summary.cluster_count == 1
    assert summary.sample_ok is False


def test_missing_or_naive_holding_times_fail_closed() -> None:
    missing = _row(1, START)
    missing.pop("holding_end_time")
    naive = _row(2, START + timedelta(hours=2))
    naive["prediction_time"] = "2026-07-06T10:00:00"
    unknown = _row(3, START + timedelta(hours=4))
    unknown["label_version"] = "unknown-net-label"

    summary = summarize_effective_samples([missing, naive, unknown], min_samples=1)

    assert summary.invalid_samples == 3
    assert summary.effective_samples == 0
    assert summary.sample_ok is False
    assert summary.invalid_reasons == {
        "missing_holding_end_time": 1,
        "naive_prediction_time": 1,
        "unknown_label_version": 1,
    }


def test_identical_replay_is_deduplicated_but_conflict_is_hard_error() -> None:
    row = _row(1, START)
    replay = dict(row)
    summary = summarize_effective_samples([row, replay], min_samples=1)

    assert summary.raw_samples == 2
    assert summary.unique_samples == 1
    assert summary.duplicate_samples == 1
    assert summary.effective_samples == 1

    conflicting = dict(row)
    conflicting["holding_end_time"] = (START + timedelta(hours=2)).isoformat()
    with pytest.raises(EffectiveSampleConflict, match=f"d-1.*{NET_LABEL_VERSION}"):
        summarize_effective_samples([row, conflicting], min_samples=1)


def test_selector_returns_the_same_deterministic_subset_as_summary_keys() -> None:
    rows = [_row(index, START + timedelta(minutes=5 * index)) for index in range(12)]

    selected, summary = select_effective_samples(list(reversed(rows)), min_samples=2)

    assert len(selected) == summary.effective_samples == 1
    assert [f"{row['decision_id']}+{row['label_version']}" for row in selected] == list(
        summary.selected_keys
    )
