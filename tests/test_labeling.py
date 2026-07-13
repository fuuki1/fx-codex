from __future__ import annotations

import pandas as pd
import pytest

from fx_backtester.labeling import (
    TripleBarrierConfig,
    multi_horizon_triple_barrier,
    triple_barrier_label,
)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close"],
        index=pd.date_range("2024-01-01T00:00:00Z", periods=len(rows), freq="h"),
    )


def test_next_open_entry_takes_profit_and_cost_is_net_of_label() -> None:
    data = _bars([(99.0, 100.0, 98.0, 99.5), (100.0, 102.2, 99.7, 102.0)])

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=1,
        config=TripleBarrierConfig(cost_r=0.25),
    )

    assert label.entry_reference == "next_open"
    assert label.entry_time == data.index[1]
    assert label.first_touch == "tp"
    assert label.exit_price == pytest.approx(102.0)
    assert label.gross_r == pytest.approx(2.0)
    assert label.net_r == pytest.approx(1.75)
    assert label.mfe_r == pytest.approx(2.0)


def test_unknown_cost_preserves_gross_label_but_abstains_from_tradable_label() -> None:
    data = _bars([(99.0, 100.0, 98.0, 99.5), (100.0, 102.2, 99.7, 102.0)])

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=1,
    )

    assert label.first_touch == "tp"
    assert label.gross_r == pytest.approx(2.0)
    assert label.cost_r is None
    assert label.net_r is None
    assert label.tradable_label is None
    assert "cost_unavailable" in label.data_quality_flags


def test_stop_first_is_conservative_when_both_barriers_touch() -> None:
    data = _bars([(99.0, 100.0, 98.0, 99.5), (100.0, 102.2, 98.8, 100.5)])

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=1,
    )

    assert label.first_touch == "sl"
    assert label.exit_price == pytest.approx(99.0)
    assert label.gross_r == pytest.approx(-1.0)
    assert label.ambiguous_intrabar
    assert "same_bar_policy_stop_first" in label.data_quality_flags


def test_ambiguous_policy_can_abstain_from_creating_a_label() -> None:
    data = _bars([(99.0, 100.0, 98.0, 99.5), (100.0, 102.2, 98.8, 100.5)])

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=1,
        config=TripleBarrierConfig(same_bar_policy="unresolved"),
    )

    assert label.first_touch == "ambiguous"
    assert label.net_r is None
    assert label.tradable_label is None
    assert label.mfe_r == pytest.approx(2.0)
    assert label.mae_r == pytest.approx(1.0)


def test_optimistic_take_profit_first_policy_is_not_available() -> None:
    with pytest.raises(ValueError, match="unsupported same_bar_policy"):
        TripleBarrierConfig(same_bar_policy="take_profit_first")  # type: ignore[arg-type]


def test_gap_through_stop_uses_worse_open_price() -> None:
    data = _bars(
        [
            (99.0, 100.0, 98.0, 99.5),
            (100.0, 100.5, 99.5, 100.1),
            (98.5, 99.0, 98.0, 98.8),
        ]
    )

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=2,
    )

    assert label.first_touch == "sl_gap"
    assert label.exit_price == pytest.approx(98.5)
    assert label.gross_r == pytest.approx(-1.5)


def test_timeout_and_short_direction_are_direction_aware() -> None:
    data = _bars(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.4, 99.6, 99.8),
            (99.8, 100.1, 99.2, 99.5),
        ]
    )

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=-1,
        volatility=1.0,
        max_horizon_bars=2,
    )

    assert label.first_touch == "timeout"
    assert label.exit_price == pytest.approx(99.5)
    assert label.gross_r == pytest.approx(0.5)
    assert label.mfe == pytest.approx(0.8)
    assert label.mae == pytest.approx(0.4)


def test_mfe_and_label_end_stop_at_first_touch() -> None:
    data = _bars(
        [
            (99.0, 100.0, 98.0, 99.5),
            (100.0, 102.1, 99.8, 102.0),
            (102.0, 110.0, 101.0, 109.0),
        ]
    )

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=2,
    )

    assert label.exit_time == data.index[1]
    assert label.label_end_time == data.index[1]
    assert label.mfe_r == pytest.approx(2.0)


def test_entry_at_prediction_close_observes_the_next_full_bar() -> None:
    data = _bars([(100.0, 100.2, 99.8, 100.0), (100.0, 102.1, 99.9, 102.0)])

    label = triple_barrier_label(
        data,
        prediction_position=0,
        direction=1,
        volatility=1.0,
        max_horizon_bars=1,
        config=TripleBarrierConfig(entry_lag_bars=0),
    )

    assert label.entry_reference == "prediction_close"
    assert label.entry_time == data.index[0]
    assert label.exit_time == data.index[1]
    assert label.first_touch == "tp"


def test_multi_horizon_output_carries_end_time_for_purging() -> None:
    data = _bars(
        [
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.5, 99.5, 100.1),
            (100.1, 100.6, 99.6, 100.2),
        ]
    )

    labels = multi_horizon_triple_barrier(
        data,
        prediction_positions=[0],
        directions=1,
        volatility=1.0,
        horizons_bars={"fast": 1, "slow": 2},
    )

    assert labels["horizon"].tolist() == ["fast", "slow"]
    assert labels["label_end_time"].tolist() == [data.index[1], data.index[2]]


def test_labels_reject_naive_unsorted_or_nonpositive_data() -> None:
    data = _bars([(100.0, 101.0, 99.0, 100.0), (100.0, 101.0, 99.0, 100.0)])
    naive = data.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        triple_barrier_label(
            naive,
            prediction_position=0,
            direction=1,
            volatility=1.0,
            max_horizon_bars=1,
        )

    bad = data.copy()
    bad.iloc[1, bad.columns.get_loc("open")] = 0.0
    with pytest.raises(ValueError, match="positive"):
        triple_barrier_label(
            bad,
            prediction_position=0,
            direction=1,
            volatility=1.0,
            max_horizon_bars=1,
        )


@pytest.mark.parametrize(
    "bad_row",
    [
        (100.0, 99.0, 98.0, 98.5),  # high below open
        (100.0, 101.0, 100.5, 100.2),  # low above close
        (100.0, 99.0, 101.0, 100.0),  # high below low
    ],
)
def test_labels_reject_impossible_ohlc_relationships(
    bad_row: tuple[float, float, float, float],
) -> None:
    data = _bars([(100.0, 101.0, 99.0, 100.0), bad_row])

    with pytest.raises(ValueError, match="relationship is impossible"):
        triple_barrier_label(
            data,
            prediction_position=0,
            direction=1,
            volatility=1.0,
            max_horizon_bars=1,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"directions": True, "volatility": 1.0, "horizons_bars": {"fast": 1}},
        {"directions": 1, "volatility": True, "horizons_bars": {"fast": 1}},
        {"directions": 1, "volatility": 1.0, "horizons_bars": {"fast": True}},
    ],
)
def test_multi_horizon_rejects_boolean_values_before_numeric_coercion(
    kwargs: dict[str, object],
) -> None:
    data = _bars([(99.0, 100.0, 98.0, 99.5), (100.0, 102.2, 99.7, 102.0)])

    with pytest.raises(ValueError, match="boolean"):
        multi_horizon_triple_barrier(
            data,
            prediction_positions=[0],
            **kwargs,  # type: ignore[arg-type]
        )
