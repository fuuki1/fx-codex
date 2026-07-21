"""2020–2025 bid/askチャート専用の分離shadow学習。

運用判断ログへ履歴行を混ぜず、価格だけから作れる特徴量と将来quote経路を
固定期間で学習・検証する。歴史quoteはbroker約定ではないため、生成モデルは
常にshadowでありlive昇格根拠にはしない。
"""

from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .gbm import GradientBoostingClassifier, brier_score, log_loss

PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
TIMEFRAME_RULES = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}
MAX_LABEL_GAPS = {
    "15m": pd.Timedelta(minutes=45),
    "1h": pd.Timedelta(hours=3),
    "4h": pd.Timedelta(hours=12),
    # 日足は週末クローズを跨ぐ次営業日ラベルだけ許容する。
    "1d": pd.Timedelta(hours=72),
}
FEATURE_NAMES = (
    "return_1",
    "return_4",
    "return_16",
    "ema_gap_atr",
    "rsi_14",
    "atr_pct",
    "volatility_16",
    "range_atr",
    "body_atr",
    "spread_atr",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
)
TRAIN_START = pd.Timestamp("2020-01-01", tz="UTC")
TRAIN_END = pd.Timestamp("2023-12-31T23:59:59", tz="UTC")
VALID_START = pd.Timestamp("2024-01-01", tz="UTC")
VALID_END = pd.Timestamp("2024-12-31T23:59:59", tz="UTC")
TEST_START = pd.Timestamp("2025-01-01", tz="UTC")
TEST_END = pd.Timestamp("2025-12-31T23:59:59", tz="UTC")


def load_pair_bars(root: str | Path, pair: str) -> pd.DataFrame:
    paths = sorted((Path(root) / pair).glob(f"{pair}_*_m5_bidask.csv.gz"))
    if not paths:
        raise FileNotFoundError(f"historical bid/ask files not found: {pair}")
    frames = [pd.read_csv(path, parse_dates=["timestamp"]) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")


def resample_bid_ask(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    indexed = frame.set_index("timestamp").sort_index()
    aggregation: dict[str, str] = {}
    for side in ("bid", "ask"):
        aggregation.update(
            {
                f"{side}_open": "first",
                f"{side}_high": "max",
                f"{side}_low": "min",
                f"{side}_close": "last",
            }
        )
    bars = (
        indexed.resample(TIMEFRAME_RULES[timeframe], closed="right", label="right")
        .agg(aggregation)
        .dropna()
    )
    for field in ("open", "high", "low", "close"):
        bars[field] = (bars[f"bid_{field}"] + bars[f"ask_{field}"]) / 2.0
    bars["spread"] = bars["ask_close"] - bars["bid_close"]
    return bars


def build_labeled_frame(
    bars: pd.DataFrame, *, max_label_gap: pd.Timedelta | None = None
) -> pd.DataFrame:
    out = bars.copy()
    previous_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - previous_close).abs(),
            (out["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(14, min_periods=14).mean()
    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    out["return_1"] = out["close"].pct_change(1)
    out["return_4"] = out["close"].pct_change(4)
    out["return_16"] = out["close"].pct_change(16)
    out["ema_gap_atr"] = (
        out["close"].ewm(span=12, adjust=False).mean()
        - out["close"].ewm(span=26, adjust=False).mean()
    ) / atr
    rs = gain / loss.replace(0.0, np.nan)
    rsi = 1.0 - 1.0 / (1.0 + rs)
    rsi = rsi.mask((loss == 0.0) & (gain > 0.0), 1.0)
    rsi = rsi.mask((loss == 0.0) & (gain == 0.0), 0.5)
    out["rsi_14"] = rsi
    out["atr_pct"] = atr / out["close"]
    out["volatility_16"] = out["return_1"].rolling(16, min_periods=16).std()
    out["range_atr"] = (out["high"] - out["low"]) / atr
    out["body_atr"] = (out["close"] - out["open"]) / atr
    out["spread_atr"] = out["spread"] / atr
    hour = out.index.hour + out.index.minute / 60.0
    dow = out.index.dayofweek
    out["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    out["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    out["dow_sin"] = np.sin(2.0 * np.pi * dow / 7.0)
    out["dow_cos"] = np.cos(2.0 * np.pi * dow / 7.0)
    out["label_end_time"] = out.index.to_series().shift(-1)
    out["label_gap"] = out["label_end_time"] - out.index.to_series()
    out["future_bid_close"] = out["bid_close"].shift(-1)
    out["future_ask_close"] = out["ask_close"].shift(-1)
    out["future_spread"] = out["future_ask_close"] - out["future_bid_close"]
    out["long_quote_r"] = (out["future_bid_close"] - out["ask_close"]) / atr
    out["short_quote_r"] = (out["bid_close"] - out["future_ask_close"]) / atr
    out["up_label"] = (out["long_quote_r"] > out["short_quote_r"]).astype(int)
    out["atr"] = atr
    out = out.replace([np.inf, -np.inf], np.nan).dropna(
        subset=[*FEATURE_NAMES, "label_end_time", "long_quote_r", "short_quote_r", "atr"]
    )
    valid = (out["spread"] > 0.0) & (out["future_spread"] > 0.0)
    if max_label_gap is not None:
        valid &= out["label_gap"] <= max_label_gap
    return out.loc[valid]


def _partition(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    # 特徴量の時刻だけでなく、答えが確定する時刻も同じ窓に閉じ込める。
    # これにより2023年末の行が2024年を、2024年末の行が2025年を参照しない。
    return frame.loc[
        (frame.index >= start)
        & (frame.index <= end)
        & (frame["label_end_time"] >= start)
        & (frame["label_end_time"] <= end)
    ]


def _sample(frame: pd.DataFrame, maximum: int) -> pd.DataFrame:
    if len(frame) <= maximum:
        return frame
    positions = np.linspace(0, len(frame) - 1, maximum, dtype=int)
    return frame.iloc[np.unique(positions)]


def _medians(frame: pd.DataFrame) -> dict[str, float]:
    return {name: float(frame[name].median()) for name in FEATURE_NAMES}


def _matrix(frame: pd.DataFrame, medians: Mapping[str, float]) -> list[list[float]]:
    values = frame.loc[:, FEATURE_NAMES].fillna(dict(medians)).to_numpy()
    return [[float(value) for value in row] for row in values]


def _quote_metrics(frame: pd.DataFrame, probabilities: Sequence[float]) -> dict[str, object]:
    selected: list[float] = []
    for probability, (_, row) in zip(probabilities, frame.iterrows()):
        if probability >= 0.55:
            selected.append(float(row["long_quote_r"]))
        elif probability <= 0.45:
            selected.append(float(row["short_quote_r"]))
    wins = [value for value in selected if value > 0]
    losses = [value for value in selected if value < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else None
    return {
        "selected": len(selected),
        "coverage": round(len(selected) / len(frame), 4) if len(frame) else 0.0,
        "mean_quote_r": round(float(np.mean(selected)), 4) if selected else None,
        "cumulative_quote_r": round(float(np.sum(selected)), 4) if selected else None,
        "profit_factor_quote": round(float(profit_factor), 4) if profit_factor else None,
        "cost_status": "spread_measured_commission_slippage_missing",
        "canonical_pure_r": False,
    }


def train_cell(pair: str, timeframe: str, bars: pd.DataFrame) -> dict[str, object]:
    labeled = build_labeled_frame(
        resample_bid_ask(bars, timeframe), max_label_gap=MAX_LABEL_GAPS[timeframe]
    )
    train = _sample(_partition(labeled, TRAIN_START, TRAIN_END), 12000)
    valid = _sample(_partition(labeled, VALID_START, VALID_END), 4000)
    test = _sample(_partition(labeled, TEST_START, TEST_END), 4000)
    if min(len(train), len(valid), len(test)) < 100:
        raise ValueError(f"insufficient fixed-window samples: {pair} {timeframe}")
    medians = _medians(train)
    x_train, x_valid, x_test = (_matrix(partition, medians) for partition in (train, valid, test))
    y_train = [int(value) for value in train["up_label"]]
    y_valid = [int(value) for value in valid["up_label"]]
    y_test = [int(value) for value in test["up_label"]]
    model = GradientBoostingClassifier(
        n_estimators=80,
        learning_rate=0.04,
        max_depth=3,
        min_samples_leaf=80,
        subsample=0.8,
        feature_fraction=0.9,
        max_bins=24,
        early_stopping_rounds=10,
        seed=7,
    ).fit(x_train, y_train, x_valid, y_valid)
    probabilities = model.predict_proba_many(x_test)
    test_brier = brier_score(y_test, probabilities)
    base_rate = sum(y_train) / len(y_train)
    baseline = brier_score(y_test, [base_rate] * len(y_test))
    accuracy = sum(
        (probability >= 0.5) == bool(label) for probability, label in zip(probabilities, y_test)
    ) / len(y_test)
    importance_total = sum(model.feature_importance_.values()) or 1.0
    importance = [
        {"feature": FEATURE_NAMES[index], "importance": round(value / importance_total, 6)}
        for index, value in sorted(
            model.feature_importance_.items(), key=lambda item: item[1], reverse=True
        )
    ]
    return {
        "pair": pair,
        "timeframe": timeframe,
        "stage": "shadow",
        "promotion_admissible": False,
        "features": list(FEATURE_NAMES),
        "medians": medians,
        "model": model.to_dict(),
        "importance": importance,
        "samples": {"train": len(train), "valid": len(valid), "test": len(test)},
        "metrics": {
            "test_accuracy": round(accuracy, 4),
            "test_brier": round(test_brier, 6),
            "baseline_brier": round(baseline, 6),
            "test_logloss": round(log_loss(y_test, probabilities), 6),
            "beats_baseline": test_brier < baseline,
            **_quote_metrics(test, probabilities),
        },
    }


def train_historical_models(
    bars_root: str | Path,
    *,
    pairs: Sequence[str] = PAIRS,
    timeframes: Sequence[str] = tuple(TIMEFRAME_RULES),
) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    for pair in pairs:
        bars = load_pair_bars(bars_root, pair)
        for timeframe in timeframes:
            cells.append(train_cell(pair, timeframe, bars))
    return {
        "schema_version": 1,
        "trained_at": datetime.now(UTC).isoformat(),
        "mode": "historical_chart_shadow",
        "stage": "shadow",
        "source_contract": "histdata-bid-m1-plus-ask-tick-v1",
        "data_windows": {
            "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
            "validation": [VALID_START.isoformat(), VALID_END.isoformat()],
            "lockbox_test": [TEST_START.isoformat(), TEST_END.isoformat()],
            "forward": "2026-live-only",
        },
        "operational_log_mixed": False,
        "promotion_admissible": False,
        "canonical_pure_r": False,
        "canonical_pure_r_reason": "historical spread is measured but broker commission/slippage is unavailable",
        "cells": cells,
    }


def save_artifact(payload: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
