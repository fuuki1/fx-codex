"""Cost-aware, volatility-adjusted triple-barrier labels for FX research.

Labels are deliberately separate from features.  A prediction made on bar ``t``
defaults to entry at bar ``t+1`` open, matching the backtest engine's next-open
execution contract.  Intrabar TP/SL ambiguity is explicit and defaults to the
conservative stop-first policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Literal

import numpy as np
import pandas as pd

SameBarPolicy = Literal["stop_first", "take_profit_first", "unresolved"]


@dataclass(frozen=True)
class TripleBarrierConfig:
    take_profit_vol_multiple: float = 2.0
    stop_vol_multiple: float = 1.0
    entry_lag_bars: int = 1
    same_bar_policy: SameBarPolicy = "stop_first"
    cost_r: float = 0.0

    def __post_init__(self) -> None:
        if self.take_profit_vol_multiple <= 0 or not isfinite(self.take_profit_vol_multiple):
            raise ValueError("take_profit_vol_multiple must be positive and finite")
        if self.stop_vol_multiple <= 0 or not isfinite(self.stop_vol_multiple):
            raise ValueError("stop_vol_multiple must be positive and finite")
        if self.entry_lag_bars < 0:
            raise ValueError("entry_lag_bars must be >= 0")
        if self.same_bar_policy not in {"stop_first", "take_profit_first", "unresolved"}:
            raise ValueError("unsupported same_bar_policy")
        if self.cost_r < 0 or not isfinite(self.cost_r):
            raise ValueError("cost_r must be finite and >= 0")


@dataclass(frozen=True)
class TripleBarrierLabel:
    horizon: str
    prediction_time: Any
    entry_time: Any | None
    label_end_time: Any | None
    direction: int
    entry_reference: str
    entry_price: float | None
    volatility: float
    upper_barrier: float | None
    lower_barrier: float | None
    time_barrier: Any | None
    first_touch: str
    exit_time: Any | None
    exit_price: float | None
    bars_to_exit: int
    mfe: float
    mae: float
    mfe_r: float
    mae_r: float
    realized_return: float | None
    gross_r: float | None
    cost_r: float
    net_r: float | None
    ambiguous_intrabar: bool = False
    data_quality_flags: tuple[str, ...] = ()

    @property
    def label_up(self) -> int | None:
        if self.exit_price is None or self.entry_price is None:
            return None
        return int(self.exit_price > self.entry_price)

    @property
    def tradable_label(self) -> int | None:
        return None if self.net_r is None else int(self.net_r > 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def triple_barrier_label(
    data: pd.DataFrame,
    *,
    prediction_position: int,
    direction: int,
    volatility: float,
    max_horizon_bars: int,
    horizon: str = "",
    config: TripleBarrierConfig | None = None,
) -> TripleBarrierLabel:
    """Label one prediction using future OHLC without leaking it into features."""

    settings = config or TripleBarrierConfig()
    _validate_input(data, prediction_position, direction, volatility, max_horizon_bars)
    index = data.index
    prediction_time = index[prediction_position]
    entry_position = prediction_position + settings.entry_lag_bars
    if entry_position >= len(data):
        return _unavailable_label(
            horizon,
            prediction_time,
            direction,
            volatility,
            settings,
            "entry_bar_unavailable",
        )

    entry_reference = "next_open" if settings.entry_lag_bars > 0 else "prediction_close"
    entry_column = "open" if settings.entry_lag_bars > 0 else "close"
    entry_price = float(data.iloc[entry_position][entry_column])
    if not isfinite(entry_price) or entry_price <= 0:
        return _unavailable_label(
            horizon,
            prediction_time,
            direction,
            volatility,
            settings,
            "invalid_entry_price",
        )

    profit_distance = volatility * settings.take_profit_vol_multiple
    risk_distance = volatility * settings.stop_vol_multiple
    upper_barrier = entry_price + (profit_distance if direction == 1 else risk_distance)
    lower_barrier = entry_price - (risk_distance if direction == 1 else profit_distance)
    profit_barrier = upper_barrier if direction == 1 else lower_barrier
    stop_barrier = lower_barrier if direction == 1 else upper_barrier

    start_position = entry_position if settings.entry_lag_bars > 0 else entry_position + 1
    last_position = min(start_position + max_horizon_bars - 1, len(data) - 1)
    time_barrier = index[last_position] if last_position >= start_position else None
    if start_position > last_position:
        return TripleBarrierLabel(
            horizon=horizon,
            prediction_time=prediction_time,
            entry_time=index[entry_position],
            label_end_time=None,
            direction=direction,
            entry_reference=entry_reference,
            entry_price=entry_price,
            volatility=volatility,
            upper_barrier=upper_barrier,
            lower_barrier=lower_barrier,
            time_barrier=time_barrier,
            first_touch="none",
            exit_time=None,
            exit_price=None,
            bars_to_exit=0,
            mfe=0.0,
            mae=0.0,
            mfe_r=0.0,
            mae_r=0.0,
            realized_return=None,
            gross_r=None,
            cost_r=settings.cost_r,
            net_r=None,
            data_quality_flags=("label_horizon_unavailable",),
        )

    mfe = 0.0
    mae = 0.0
    for position in range(start_position, last_position + 1):
        row = data.iloc[position]
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        when = index[position]

        gap_stop = open_price <= stop_barrier if direction == 1 else open_price >= stop_barrier
        gap_profit = (
            open_price >= profit_barrier if direction == 1 else open_price <= profit_barrier
        )
        if gap_stop:
            favorable, adverse = _moves(direction, entry_price, open_price, open_price)
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            return _completed_label(
                horizon,
                prediction_time,
                index[entry_position],
                when,
                direction,
                entry_reference,
                entry_price,
                volatility,
                upper_barrier,
                lower_barrier,
                time_barrier,
                "sl_gap",
                open_price,
                position - entry_position + 1,
                mfe,
                mae,
                risk_distance,
                settings.cost_r,
            )
        if gap_profit:
            favorable, adverse = _moves(direction, entry_price, profit_barrier, profit_barrier)
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            return _completed_label(
                horizon,
                prediction_time,
                index[entry_position],
                when,
                direction,
                entry_reference,
                entry_price,
                volatility,
                upper_barrier,
                lower_barrier,
                time_barrier,
                "tp_gap",
                profit_barrier,
                position - entry_position + 1,
                mfe,
                mae,
                risk_distance,
                settings.cost_r,
            )

        stop_hit = low <= stop_barrier if direction == 1 else high >= stop_barrier
        profit_hit = high >= profit_barrier if direction == 1 else low <= profit_barrier
        if stop_hit and profit_hit:
            if settings.same_bar_policy == "unresolved":
                mfe = max(mfe, profit_distance)
                mae = max(mae, risk_distance)
                return TripleBarrierLabel(
                    horizon=horizon,
                    prediction_time=prediction_time,
                    entry_time=index[entry_position],
                    label_end_time=when,
                    direction=direction,
                    entry_reference=entry_reference,
                    entry_price=entry_price,
                    volatility=volatility,
                    upper_barrier=upper_barrier,
                    lower_barrier=lower_barrier,
                    time_barrier=time_barrier,
                    first_touch="ambiguous",
                    exit_time=when,
                    exit_price=None,
                    bars_to_exit=position - entry_position + 1,
                    mfe=mfe,
                    mae=mae,
                    mfe_r=mfe / risk_distance,
                    mae_r=mae / risk_distance,
                    realized_return=None,
                    gross_r=None,
                    cost_r=settings.cost_r,
                    net_r=None,
                    ambiguous_intrabar=True,
                    data_quality_flags=("ambiguous_intrabar_touch",),
                )
            stop_first = settings.same_bar_policy == "stop_first"
            exit_price = stop_barrier if stop_first else profit_barrier
            first_touch = "sl" if stop_first else "tp"
            if stop_first:
                mae = max(mae, risk_distance)
            else:
                mfe = max(mfe, profit_distance)
                _, bar_adverse = _moves(direction, entry_price, high, low)
                mae = max(mae, bar_adverse)
            result = _completed_label(
                horizon,
                prediction_time,
                index[entry_position],
                when,
                direction,
                entry_reference,
                entry_price,
                volatility,
                upper_barrier,
                lower_barrier,
                time_barrier,
                first_touch,
                exit_price,
                position - entry_position + 1,
                mfe,
                mae,
                risk_distance,
                settings.cost_r,
                ambiguous=True,
                flags=("ambiguous_intrabar_touch", "same_bar_policy_" + settings.same_bar_policy),
            )
            return result
        if stop_hit:
            mae = max(mae, risk_distance)
            return _completed_label(
                horizon,
                prediction_time,
                index[entry_position],
                when,
                direction,
                entry_reference,
                entry_price,
                volatility,
                upper_barrier,
                lower_barrier,
                time_barrier,
                "sl",
                stop_barrier,
                position - entry_position + 1,
                mfe,
                mae,
                risk_distance,
                settings.cost_r,
            )
        if profit_hit:
            mfe = max(mfe, profit_distance)
            _, bar_adverse = _moves(direction, entry_price, high, low)
            mae = max(mae, bar_adverse)
            return _completed_label(
                horizon,
                prediction_time,
                index[entry_position],
                when,
                direction,
                entry_reference,
                entry_price,
                volatility,
                upper_barrier,
                lower_barrier,
                time_barrier,
                "tp",
                profit_barrier,
                position - entry_position + 1,
                mfe,
                mae,
                risk_distance,
                settings.cost_r,
            )

        favorable, adverse = _moves(direction, entry_price, high, low)
        mfe = max(mfe, favorable)
        mae = max(mae, adverse)

    terminal = data.iloc[last_position]
    exit_price = float(terminal["close"])
    return _completed_label(
        horizon,
        prediction_time,
        index[entry_position],
        index[last_position],
        direction,
        entry_reference,
        entry_price,
        volatility,
        upper_barrier,
        lower_barrier,
        time_barrier,
        "timeout",
        exit_price,
        last_position - entry_position + 1,
        mfe,
        mae,
        risk_distance,
        settings.cost_r,
    )


def multi_horizon_triple_barrier(
    data: pd.DataFrame,
    *,
    prediction_positions: Sequence[int],
    directions: Sequence[int] | int,
    volatility: pd.Series | Sequence[float] | float,
    horizons_bars: Mapping[str, int],
    config: TripleBarrierConfig | None = None,
) -> pd.DataFrame:
    """Generate auditable labels for multiple strategy-aligned horizons."""

    direction_values = (
        [int(directions)] * len(prediction_positions)
        if isinstance(directions, int)
        else list(directions)
    )
    if len(direction_values) != len(prediction_positions):
        raise ValueError("directions length must match prediction_positions")
    rows: list[dict[str, Any]] = []
    for offset, prediction_position in enumerate(prediction_positions):
        sigma = _volatility_at(volatility, prediction_position)
        for name, bars in horizons_bars.items():
            label = triple_barrier_label(
                data,
                prediction_position=prediction_position,
                direction=direction_values[offset],
                volatility=sigma,
                max_horizon_bars=int(bars),
                horizon=str(name),
                config=config,
            )
            rows.append(label.to_dict())
    return pd.DataFrame(rows)


def _validate_input(
    data: pd.DataFrame,
    prediction_position: int,
    direction: int,
    volatility: float,
    max_horizon_bars: int,
) -> None:
    missing = {"open", "high", "low", "close"} - set(data.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    if prediction_position < 0 or prediction_position >= len(data):
        raise IndexError("prediction_position out of range")
    if direction not in (-1, 1):
        raise ValueError("direction must be +1 or -1")
    if not isfinite(volatility) or volatility <= 0:
        raise ValueError("volatility must be positive and finite")
    if max_horizon_bars < 1:
        raise ValueError("max_horizon_bars must be >= 1")
    if not isinstance(data.index, pd.DatetimeIndex):
        raise ValueError("data index must be a DatetimeIndex")
    if data.index.tz is None:
        raise ValueError("data index must be timezone-aware")
    if not data.index.is_monotonic_increasing or data.index.has_duplicates:
        raise ValueError("data index must be unique and monotonic")
    numeric = data[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if bool(numeric.isna().any().any()) or not bool(np.isfinite(numeric.to_numpy()).all()):
        raise ValueError("OHLC must be finite")
    if bool((numeric <= 0).any().any()):
        raise ValueError("OHLC must be positive")


def _completed_label(
    horizon: str,
    prediction_time: Any,
    entry_time: Any,
    exit_time: Any,
    direction: int,
    entry_reference: str,
    entry_price: float,
    volatility: float,
    upper_barrier: float,
    lower_barrier: float,
    time_barrier: Any,
    first_touch: str,
    exit_price: float,
    bars_to_exit: int,
    mfe: float,
    mae: float,
    risk_distance: float,
    cost_r: float,
    *,
    ambiguous: bool = False,
    flags: tuple[str, ...] = (),
) -> TripleBarrierLabel:
    gross_r = direction * (exit_price - entry_price) / risk_distance
    return TripleBarrierLabel(
        horizon=horizon,
        prediction_time=prediction_time,
        entry_time=entry_time,
        label_end_time=exit_time,
        direction=direction,
        entry_reference=entry_reference,
        entry_price=entry_price,
        volatility=volatility,
        upper_barrier=upper_barrier,
        lower_barrier=lower_barrier,
        time_barrier=time_barrier,
        first_touch=first_touch,
        exit_time=exit_time,
        exit_price=exit_price,
        bars_to_exit=bars_to_exit,
        mfe=mfe,
        mae=mae,
        mfe_r=mfe / risk_distance,
        mae_r=mae / risk_distance,
        realized_return=direction * (exit_price - entry_price) / entry_price,
        gross_r=gross_r,
        cost_r=cost_r,
        net_r=gross_r - cost_r,
        ambiguous_intrabar=ambiguous,
        data_quality_flags=flags,
    )


def _unavailable_label(
    horizon: str,
    prediction_time: Any,
    direction: int,
    volatility: float,
    config: TripleBarrierConfig,
    flag: str,
) -> TripleBarrierLabel:
    return TripleBarrierLabel(
        horizon=horizon,
        prediction_time=prediction_time,
        entry_time=None,
        label_end_time=None,
        direction=direction,
        entry_reference="next_open" if config.entry_lag_bars else "prediction_close",
        entry_price=None,
        volatility=volatility,
        upper_barrier=None,
        lower_barrier=None,
        time_barrier=None,
        first_touch="none",
        exit_time=None,
        exit_price=None,
        bars_to_exit=0,
        mfe=0.0,
        mae=0.0,
        mfe_r=0.0,
        mae_r=0.0,
        realized_return=None,
        gross_r=None,
        cost_r=config.cost_r,
        net_r=None,
        data_quality_flags=(flag,),
    )


def _moves(direction: int, entry: float, high: float, low: float) -> tuple[float, float]:
    if direction == 1:
        return max(0.0, high - entry), max(0.0, entry - low)
    return max(0.0, entry - low), max(0.0, high - entry)


def _volatility_at(
    volatility: pd.Series | Sequence[float] | float,
    position: int,
) -> float:
    if isinstance(volatility, pd.Series):
        return float(volatility.iloc[position])
    if isinstance(volatility, (int, float)):
        return float(volatility)
    return float(volatility[position])
