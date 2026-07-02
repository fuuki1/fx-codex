from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from fx_backtester.indicators import average_true_range, rsi, sma
from fx_backtester.strategies.base import Strategy


@dataclass
class AILogisticStrategy(Strategy):
    """Rolling logistic model that predicts next-bar direction from market features."""

    min_train_bars: int = 300
    retrain_interval: int = 24
    prediction_horizon: int = 1
    learning_rate: float = 0.08
    epochs: int = 160
    l2: float = 0.001
    long_threshold: float = 0.55
    short_threshold: float = 0.45
    min_abs_forward_return: float = 0.0
    fast_window: int = 12
    slow_window: int = 48
    momentum_window: int = 12
    volatility_window: int = 24
    rsi_window: int = 14
    atr_window: int = 14
    stop_atr_multiple: float = 2.0

    @property
    def name(self) -> str:
        return "ai_logistic"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        self._validate_params()
        features = self._features(data)
        forward_return = data["close"].astype(float).shift(-self.prediction_horizon) / data[
            "close"
        ].astype(float) - 1
        labels = (forward_return > self.min_abs_forward_return).astype(float)
        valid_train = features.notna().all(axis=1) & forward_return.notna()

        target = pd.Series(0, index=data.index, dtype=int)
        probability_up = pd.Series(np.nan, index=data.index, dtype=float)
        train_rows = pd.Series(0, index=data.index, dtype=int)
        model_ready = pd.Series(False, index=data.index, dtype=bool)

        weights: np.ndarray | None = None
        mean: pd.Series | None = None
        std: pd.Series | None = None
        last_train_position: int | None = None

        for position, timestamp in enumerate(data.index):
            if not bool(features.iloc[position].notna().all()):
                continue

            train_end = position - self.prediction_horizon + 1
            if train_end <= 0:
                continue
            train_mask = valid_train.iloc[:train_end]
            train_count = int(train_mask.sum())
            train_rows.at[timestamp] = train_count
            if train_count < self.min_train_bars:
                continue

            should_retrain = (
                weights is None
                or last_train_position is None
                or position - last_train_position >= self.retrain_interval
            )
            if should_retrain:
                train_index = train_mask.index[train_mask]
                train_features = features.loc[train_index]
                train_labels = labels.loc[train_index]
                fitted = _fit_logistic(
                    train_features,
                    train_labels,
                    learning_rate=self.learning_rate,
                    epochs=self.epochs,
                    l2=self.l2,
                )
                if fitted is None:
                    continue
                weights, mean, std = fitted
                last_train_position = position

            if weights is None or mean is None or std is None:
                continue
            transformed = (features.loc[[timestamp]] - mean) / std
            transformed = _filled_finite_frame(transformed)
            x = np.concatenate(([1.0], transformed.iloc[0].to_numpy(dtype=float)))
            probability = float(_sigmoid(np.array([x @ weights]))[0])
            probability_up.at[timestamp] = probability
            model_ready.at[timestamp] = True
            if probability >= self.long_threshold:
                target.at[timestamp] = 1
            elif probability <= self.short_threshold:
                target.at[timestamp] = -1

        atr = average_true_range(data, self.atr_window)
        stop_distance = atr * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": target,
                    "stop_distance": stop_distance,
                    "ai_probability_up": probability_up,
                    "ai_edge": probability_up - 0.5,
                    "ai_train_rows": train_rows,
                    "ai_model_ready": model_ready,
                },
                index=data.index,
            ),
        )

    def _features(self, data: pd.DataFrame) -> pd.DataFrame:
        close = data["close"].astype(float)
        open_ = data["open"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        returns = close.pct_change()
        atr = average_true_range(data, self.atr_window)
        high_low = (high - low).replace(0, pd.NA)

        output = pd.DataFrame(index=data.index)
        for lag in (1, 2, 3, 6, 12):
            output[f"return_lag_{lag}"] = returns.shift(lag)
        output["momentum"] = close / close.shift(self.momentum_window) - 1
        output["fast_slow_gap"] = sma(close, self.fast_window) / sma(close, self.slow_window) - 1
        output["volatility"] = returns.rolling(
            self.volatility_window,
            min_periods=self.volatility_window,
        ).std()
        output["atr_pct"] = atr / close
        output["rsi_scaled"] = (rsi(close, self.rsi_window) - 50) / 50
        output["candle_body_pct"] = (close - open_) / open_.replace(0, pd.NA)
        output["range_pct"] = (high - low) / close.replace(0, pd.NA)
        output["close_location"] = ((close - low) / high_low) - 0.5

        if "spread_pips" in data.columns:
            spread_column = "spread_pips"
        elif "spread_price" in data.columns:
            spread_column = "spread_price"
        else:
            spread_column = "spread"
        if spread_column in data.columns:
            spread = data[spread_column].astype(float)
            median = spread.rolling(
                self.volatility_window,
                min_periods=max(3, min(self.volatility_window, 12)),
            ).median()
            output["spread_ratio"] = spread / median.replace(0, pd.NA)
        else:
            output["spread_ratio"] = 1.0
        return _finite_frame(output)

    def _validate_params(self) -> None:
        if self.min_train_bars <= 20:
            raise ValueError("min_train_bars must be > 20")
        if self.retrain_interval <= 0:
            raise ValueError("retrain_interval must be positive")
        if self.prediction_horizon <= 0:
            raise ValueError("prediction_horizon must be positive")
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if self.epochs <= 0:
            raise ValueError("epochs must be positive")
        if self.l2 < 0:
            raise ValueError("l2 must be >= 0")
        if not 0 < self.short_threshold < 0.5 < self.long_threshold < 1:
            raise ValueError("thresholds must satisfy 0 < short < 0.5 < long < 1")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        for name, value in (
            ("momentum_window", self.momentum_window),
            ("volatility_window", self.volatility_window),
            ("rsi_window", self.rsi_window),
            ("atr_window", self.atr_window),
        ):
            if value <= 1:
                raise ValueError(f"{name} must be > 1")
        if self.stop_atr_multiple <= 0:
            raise ValueError("stop_atr_multiple must be positive")


def _fit_logistic(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    learning_rate: float,
    epochs: int,
    l2: float,
) -> tuple[np.ndarray, pd.Series, pd.Series] | None:
    y = labels.astype(float)
    if y.nunique(dropna=True) < 2:
        return None

    mean = features.mean()
    std = features.std(ddof=0).astype(float).mask(lambda values: values == 0, 1.0)
    x_frame = (features - mean) / std
    x_frame = _filled_finite_frame(x_frame)
    x = x_frame.to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(x)), x])
    weights = np.zeros(x.shape[1], dtype=float)
    y_array = y.to_numpy(dtype=float)

    for _ in range(epochs):
        probabilities = _sigmoid(x @ weights)
        gradient = x.T @ (probabilities - y_array) / len(y_array)
        regularization = l2 * weights
        regularization[0] = 0.0
        weights -= learning_rate * (gradient + regularization)
    return weights, mean, std


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35, 35)
    return 1 / (1 + np.exp(-clipped))


def _finite_frame(frame: pd.DataFrame) -> pd.DataFrame:
    numeric = frame.apply(pd.to_numeric, errors="coerce").astype(float)
    return numeric.mask(~np.isfinite(numeric), np.nan)


def _filled_finite_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return _finite_frame(frame).fillna(0.0)
