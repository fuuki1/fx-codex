"""Build historical timeframe-learning datasets from OHLC chart history.

The live timeframe AI learns from two JSONL streams:

- timeframe decisions, shaped like ``logs/briefing_tf_journal.jsonl``
- close-price snapshots, shaped like ``logs/briefing_tf_prices.jsonl``

This module creates the same artifacts from historical OHLC CSV files without
calling TradingView. It is intentionally conservative: all higher timeframe
features are taken from bars that are already closed at the decision timestamp.
"""

from __future__ import annotations

import argparse
import json
import shutil
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

import pandas as pd

from fx_backtester.data import load_price_csv
from fx_backtester.indicators import average_true_range, sma

from .calendar import symbol_currencies
from .journal import DEFAULT_ATR_FRACTION
from .news import NewsItem
from .sentiment import CurrencySentiment
from .technicals import IntervalView, PairTechnicals
from .tf_learning import (
    BASELINE_MIN_LIVE_EVALUATED,
    DEFAULT_TIMEFRAMES,
    derive_timeframe_learning,
    evaluate_timeframe_history,
)
from .tf_learning import save_timeframe_learning
from .timeframe import PRIMARY_HORIZON_HOURS, build_timeframe_plan

TIMEFRAME_RULES: dict[str, str] = {
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}

TIMEFRAME_DELTAS: dict[str, pd.Timedelta] = {
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
}

DEFAULT_OUTPUT_DIR = Path("research_pack/ai_backfill")


@dataclass(frozen=True)
class HistoricalBackfillConfig:
    data_paths: tuple[Path, ...]
    output_dir: Path = DEFAULT_OUTPUT_DIR
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    start: str | None = None
    end: str | None = None
    input_timezone: str | None = None
    timestamp_mode: str = "start"  # start = bar-open timestamps, close = already closed bars
    fast_window: int = 20
    slow_window: int = 100
    atr_window: int = 14
    adx_window: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_multiple: float = 2.5
    currency_score_csv: Path | None = None
    sentiment_ttl_hours: float = 168.0
    install_baseline_path: Path | None = None
    baseline_min_evaluated: int = BASELINE_MIN_LIVE_EVALUATED


@dataclass(frozen=True)
class BackfillResult:
    output_dir: Path
    price_rows: int
    journal_rows: int
    symbols: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    artifacts: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class _ScoreSnapshot:
    timestamp: datetime
    currency: str
    score: float
    headline_count: int = 0
    confidence: float | None = None


class CurrencyScoreProvider:
    """Point-in-time lookup for historical currency news/macro scores.

    CSV schema:
      timestamp,currency,score[,headline_count,confidence]

    Each timestamp must describe what was known at that time. Lookup uses only
    the latest row at or before the decision timestamp, bounded by ``ttl``.
    """

    def __init__(self, snapshots: Iterable[_ScoreSnapshot], ttl: timedelta) -> None:
        self._ttl = ttl
        grouped: dict[str, list[_ScoreSnapshot]] = {}
        for snapshot in snapshots:
            grouped.setdefault(snapshot.currency.upper(), []).append(snapshot)
        self._snapshots = {
            currency: sorted(rows, key=lambda row: row.timestamp)
            for currency, rows in grouped.items()
        }
        self._times = {
            currency: [row.timestamp for row in rows] for currency, rows in self._snapshots.items()
        }

    @classmethod
    def from_csv(cls, path: str | Path, ttl_hours: float = 168.0) -> CurrencyScoreProvider:
        frame = pd.read_csv(path)
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        required = {"timestamp", "currency", "score"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
        snapshots: list[_ScoreSnapshot] = []
        for row in frame.to_dict("records"):
            timestamp = _to_utc_datetime(pd.Timestamp(row["timestamp"]))
            headline_count = int(row.get("headline_count") or 0)
            raw_confidence = row.get("confidence")
            confidence = (
                float(raw_confidence)
                if raw_confidence is not None and not pd.isna(raw_confidence)
                else None
            )
            snapshots.append(
                _ScoreSnapshot(
                    timestamp=timestamp,
                    currency=str(row["currency"]).upper().strip(),
                    score=max(-1.0, min(1.0, float(row["score"]))),
                    headline_count=max(0, headline_count),
                    confidence=confidence,
                )
            )
        return cls(snapshots, ttl=timedelta(hours=ttl_hours))

    def currencies_at(self, when: datetime) -> dict[str, CurrencySentiment]:
        output: dict[str, CurrencySentiment] = {}
        for currency, rows in self._snapshots.items():
            row = self._latest(currency, when)
            if row is None:
                continue
            output[currency] = CurrencySentiment(
                currency=currency,
                score=row.score,
                headline_count=row.headline_count,
                confidence=row.confidence,
            )
        return output

    def news_items_at(self, when: datetime, currencies: Sequence[str]) -> list[NewsItem]:
        items: list[NewsItem] = []
        for currency in currencies:
            row = self._latest(currency.upper(), when)
            if row is None:
                continue
            for index in range(min(row.headline_count, 5)):
                items.append(
                    NewsItem(
                        title=f"historical score {currency} #{index + 1}",
                        source="historical-score",
                        link="",
                        published=row.timestamp,
                        currencies=(currency.upper(),),
                    )
                )
        return items

    def _latest(self, currency: str, when: datetime) -> _ScoreSnapshot | None:
        rows = self._snapshots.get(currency)
        times = self._times.get(currency)
        if not rows or not times:
            return None
        index = bisect_right(times, when) - 1
        if index < 0:
            return None
        candidate = rows[index]
        if when - candidate.timestamp > self._ttl:
            return None
        return candidate


def run_backfill(config: HistoricalBackfillConfig) -> BackfillResult:
    warnings: list[str] = []
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_data = _load_price_data(config)
    dated_data = _filter_date_range(raw_data, config.start, config.end)
    score_provider = (
        CurrencyScoreProvider.from_csv(config.currency_score_csv, config.sentiment_ttl_hours)
        if config.currency_score_csv is not None
        else None
    )

    bars_by_symbol_tf: dict[str, dict[str, pd.DataFrame]] = {}
    views_by_symbol_tf: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, frame in dated_data.items():
        source_delta = _infer_delta(frame.index)
        bars_by_symbol_tf[symbol] = {}
        views_by_symbol_tf[symbol] = {}
        for timeframe in config.timeframes:
            target_delta = TIMEFRAME_DELTAS[timeframe]
            if source_delta is not None and source_delta > target_delta:
                warnings.append(
                    f"{symbol} {timeframe}: skipped because source frequency "
                    f"{source_delta} is coarser than target {target_delta}"
                )
                continue
            bars = _resample_ohlc(frame, timeframe)
            if bars.empty:
                warnings.append(f"{symbol} {timeframe}: no bars after resampling")
                continue
            bars_by_symbol_tf[symbol][timeframe] = bars
            views_by_symbol_tf[symbol][timeframe] = _build_indicator_frame(bars, config)

    price_rows = _build_price_rows(bars_by_symbol_tf)
    journal_rows = _build_journal_rows(
        bars_by_symbol_tf=bars_by_symbol_tf,
        views_by_symbol_tf=views_by_symbol_tf,
        config=config,
        score_provider=score_provider,
    )

    prices_path = output_dir / "historical_tf_prices.jsonl"
    journal_path = output_dir / "historical_tf_journal.jsonl"
    learning_path = output_dir / "historical_tf_learning.json"
    quality_path = output_dir / "quality_report.csv"
    manifest_path = output_dir / "manifest.json"

    _write_jsonl(prices_path, price_rows)
    _write_jsonl(journal_path, journal_rows)

    combined = journal_rows + price_rows
    learning = derive_timeframe_learning(combined)
    save_timeframe_learning(learning, learning_path)

    quality = _quality_report(combined, bars_by_symbol_tf, config.timeframes)
    quality.to_csv(quality_path, index=False)

    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "data_paths": [str(path) for path in config.data_paths],
        "start": config.start,
        "end": config.end,
        "timestamp_mode": config.timestamp_mode,
        "timeframes": list(config.timeframes),
        "fast_window": config.fast_window,
        "slow_window": config.slow_window,
        "atr_window": config.atr_window,
        "currency_score_csv": str(config.currency_score_csv) if config.currency_score_csv else None,
        "price_rows": len(price_rows),
        "journal_rows": len(journal_rows),
        "symbols": sorted(bars_by_symbol_tf),
        "warnings": warnings,
        "artifacts": {
            "prices": str(prices_path),
            "journal": str(journal_path),
            "learning": str(learning_path),
            "quality": str(quality_path),
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts = {
        "prices": prices_path,
        "journal": journal_path,
        "learning": learning_path,
        "quality": quality_path,
        "manifest": manifest_path,
    }
    if config.install_baseline_path is not None:
        install_info = _install_baseline(
            learning_path=learning_path,
            baseline_path=config.install_baseline_path,
            quality=quality,
            min_evaluated=config.baseline_min_evaluated,
            manifest_path=manifest_path,
        )
        manifest["baseline_install"] = install_info
        manifest["artifacts"]["baseline"] = str(config.install_baseline_path)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts["baseline"] = config.install_baseline_path

    return BackfillResult(
        output_dir=output_dir,
        price_rows=len(price_rows),
        journal_rows=len(journal_rows),
        symbols=tuple(sorted(bars_by_symbol_tf)),
        warnings=tuple(warnings),
        artifacts=artifacts,
    )


def _load_price_data(config: HistoricalBackfillConfig) -> dict[str, pd.DataFrame]:
    loaded: dict[str, pd.DataFrame] = {}
    for path in config.data_paths:
        for symbol, frame in load_price_csv(path, timezone=config.input_timezone).items():
            normalized = _normalize_time_index(frame, config.timestamp_mode)
            if symbol in loaded:
                loaded[symbol] = pd.concat([loaded[symbol], normalized]).sort_index()
                if loaded[symbol].index.has_duplicates:
                    raise ValueError(f"{symbol} has duplicate timestamps across input files")
            else:
                loaded[symbol] = normalized
    if not loaded:
        raise ValueError("No price data loaded")
    return loaded


def _filter_date_range(
    data: Mapping[str, pd.DataFrame], start: str | None, end: str | None
) -> dict[str, pd.DataFrame]:
    start_ts = _parse_bound(start, is_end=False)
    end_ts = _parse_bound(end, is_end=True)
    output: dict[str, pd.DataFrame] = {}
    for symbol, frame in data.items():
        selected = frame
        if start_ts is not None:
            selected = selected[selected.index >= start_ts]
        if end_ts is not None:
            selected = selected[selected.index <= end_ts]
        output[symbol] = selected.copy()
    if all(frame.empty for frame in output.values()):
        raise ValueError("date range removed all price data")
    return output


def _parse_bound(value: str | None, *, is_end: bool) -> pd.Timestamp | None:
    if value is None:
        return None
    raw = str(value).strip()
    timestamp = pd.Timestamp(raw)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    if is_end and len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        timestamp = timestamp + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return timestamp


def _normalize_time_index(frame: pd.DataFrame, timestamp_mode: str) -> pd.DataFrame:
    if timestamp_mode not in {"start", "close"}:
        raise ValueError("timestamp_mode must be 'start' or 'close'")
    output = frame.copy().sort_index()
    output.index = _to_utc_index(output.index)
    if timestamp_mode == "start":
        delta = _infer_delta(output.index)
        if delta is not None:
            output.index = output.index + delta
    return output


def _to_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    converted = pd.DatetimeIndex(pd.to_datetime(index))
    if converted.tz is None:
        return converted.tz_localize("UTC")
    return converted.tz_convert("UTC")


def _to_utc_datetime(timestamp: pd.Timestamp) -> datetime:
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime()


def _infer_delta(index: pd.Index) -> pd.Timedelta | None:
    if len(index) < 2:
        return None
    diffs = pd.Series(pd.DatetimeIndex(index).sort_values()).diff().dropna()
    if diffs.empty:
        return None
    return pd.Timedelta(diffs.median())


def _resample_ohlc(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = TIMEFRAME_RULES[timeframe]
    aggregations: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for optional, method in {"volume": "sum", "spread_price": "mean", "spread": "mean"}.items():
        if optional in frame.columns:
            aggregations[optional] = method
    bars = (
        frame.sort_index()
        .resample(rule, label="right", closed="right")
        .agg(aggregations)
        .dropna(subset=["open", "high", "low", "close"])
    )
    return bars[["open", "high", "low", "close"]]


def _build_indicator_frame(bars: pd.DataFrame, config: HistoricalBackfillConfig) -> pd.DataFrame:
    close = bars["close"]
    atr = average_true_range(bars, window=config.atr_window)
    rsi_values = _rsi(close, window=14)
    sma_fast = sma(close, config.fast_window)
    sma_slow = sma(close, config.slow_window)
    macd_line = (
        close.ewm(span=config.macd_fast, adjust=False).mean()
        - close.ewm(span=config.macd_slow, adjust=False).mean()
    )
    macd_signal = macd_line.ewm(span=config.macd_signal, adjust=False).mean()
    adx = _adx(bars, window=config.adx_window)

    frame = pd.DataFrame(
        {
            "close": close,
            "atr": atr,
            "rsi": rsi_values,
            "sma_fast": sma_fast,
            "sma_slow": sma_slow,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "adx": adx,
        },
        index=bars.index,
    )
    frame["recommendation"] = frame.apply(_recommendation, axis=1)
    return frame


def _adx(bars: pd.DataFrame, window: int = 14) -> pd.Series:
    high = bars["high"]
    low = bars["low"]
    close = bars["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
    return dx.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def _rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    average_loss = losses.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    relative_strength = average_gain / average_loss.replace(0, pd.NA)
    values = 100 - (100 / (1 + relative_strength))
    values = values.mask((average_loss == 0) & (average_gain > 0), 100.0)
    values = values.mask((average_gain == 0) & (average_loss > 0), 0.0)
    values = values.mask((average_gain == 0) & (average_loss == 0), 50.0)
    return values


def _recommendation(row: pd.Series) -> str:
    required = ("close", "atr", "rsi", "sma_fast", "sma_slow", "macd", "macd_signal", "adx")
    if any(pd.isna(row.get(key)) for key in required) or float(row["atr"]) <= 0:
        return "NEUTRAL"
    atr = float(row["atr"])
    trend = _clip((float(row["sma_fast"]) - float(row["sma_slow"])) / (2.0 * atr))
    price_location = _clip((float(row["close"]) - float(row["sma_fast"])) / (1.5 * atr))
    momentum = _clip((float(row["macd"]) - float(row["macd_signal"])) / max(atr, 1e-12))
    rsi_score = _clip((float(row["rsi"]) - 50.0) / 25.0)
    adx_factor = min(max(float(row["adx"]) / 25.0, 0.25), 1.25)
    score = _clip(0.35 * trend + 0.25 * price_location + 0.20 * momentum + 0.20 * rsi_score)
    score = _clip(score * adx_factor)
    if score >= 0.65:
        return "STRONG_BUY"
    if score >= 0.15:
        return "BUY"
    if score <= -0.65:
        return "STRONG_SELL"
    if score <= -0.15:
        return "SELL"
    return "NEUTRAL"


def _clip(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _build_price_rows(bars_by_symbol_tf: Mapping[str, Mapping[str, pd.DataFrame]]) -> list[dict]:
    rows: list[dict] = []
    for symbol, by_timeframe in sorted(bars_by_symbol_tf.items()):
        for timeframe, bars in sorted(by_timeframe.items()):
            for timestamp, bar in bars.iterrows():
                rows.append(
                    {
                        "ts": _to_utc_datetime(pd.Timestamp(timestamp)).isoformat(),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "close": round(float(bar["close"]), _price_decimals(symbol)),
                    }
                )
    rows.sort(key=lambda row: (row["ts"], row["symbol"], row["timeframe"]))
    return rows


def _build_journal_rows(
    *,
    bars_by_symbol_tf: Mapping[str, Mapping[str, pd.DataFrame]],
    views_by_symbol_tf: Mapping[str, Mapping[str, pd.DataFrame]],
    config: HistoricalBackfillConfig,
    score_provider: CurrencyScoreProvider | None,
) -> list[dict]:
    rows: list[dict] = []
    for symbol, by_timeframe in sorted(bars_by_symbol_tf.items()):
        for timeframe in config.timeframes:
            bars = by_timeframe.get(timeframe)
            if bars is None:
                continue
            for timestamp in bars.index:
                when = _to_utc_datetime(pd.Timestamp(timestamp))
                tech = _pair_technicals_at(
                    symbol,
                    views_by_symbol_tf.get(symbol, {}),
                    pd.Timestamp(timestamp),
                    config.fast_window,
                    config.slow_window,
                )
                currency_scores: dict[str, CurrencySentiment] = {}
                news_items: list[NewsItem] = []
                if score_provider is not None:
                    currency_scores = score_provider.currencies_at(when)
                    news_items = score_provider.news_items_at(when, symbol_currencies(symbol))
                plan = build_timeframe_plan(
                    symbol=symbol,
                    timeframe=timeframe,
                    tech=tech,
                    currency_scores=currency_scores,
                    windows=[],
                    news_items=news_items,
                    now=when,
                    atr_multiple=config.atr_multiple,
                    calendar_ok=True,
                )
                if plan.close is None or plan.atr is None:
                    continue
                rows.append(_plan_to_entry(plan, when))
    rows.sort(key=lambda row: (row["ts"], row["symbol"], row["timeframe"]))
    return rows


def _pair_technicals_at(
    symbol: str,
    views_by_timeframe: Mapping[str, pd.DataFrame],
    timestamp: pd.Timestamp,
    fast_window: int,
    slow_window: int,
) -> PairTechnicals:
    tech = PairTechnicals(symbol=symbol, fast_window=fast_window, slow_window=slow_window)
    for timeframe, indicators in views_by_timeframe.items():
        index = indicators.index.searchsorted(timestamp, side="right") - 1
        if index < 0:
            continue
        row = indicators.iloc[index]
        if str(row.get("recommendation", "NEUTRAL")) == "NEUTRAL" and any(
            pd.isna(row.get(key)) for key in ("atr", "rsi", "sma_fast", "sma_slow")
        ):
            continue
        tech.views[timeframe] = IntervalView(
            interval=timeframe,
            recommendation=str(row["recommendation"]),
            buy=_vote_count(str(row["recommendation"]), "buy"),
            sell=_vote_count(str(row["recommendation"]), "sell"),
            neutral=_vote_count(str(row["recommendation"]), "neutral"),
            close=_none_if_na(row.get("close")),
            rsi=_none_if_na(row.get("rsi")),
            macd=_none_if_na(row.get("macd")),
            macd_signal=_none_if_na(row.get("macd_signal")),
            adx=_none_if_na(row.get("adx")),
            atr=_none_if_na(row.get("atr")),
            sma_fast=_none_if_na(row.get("sma_fast")),
            sma_slow=_none_if_na(row.get("sma_slow")),
        )
    return tech


def _vote_count(recommendation: str, side: str) -> int:
    table = {
        "STRONG_BUY": {"buy": 16, "sell": 3, "neutral": 7},
        "BUY": {"buy": 12, "sell": 5, "neutral": 9},
        "NEUTRAL": {"buy": 8, "sell": 8, "neutral": 10},
        "SELL": {"buy": 5, "sell": 12, "neutral": 9},
        "STRONG_SELL": {"buy": 3, "sell": 16, "neutral": 7},
    }
    return table.get(recommendation, table["NEUTRAL"])[side]


def _none_if_na(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _plan_to_entry(plan: Any, when: datetime) -> dict:
    return {
        "ts": when.isoformat(),
        "symbol": plan.symbol,
        "timeframe": plan.timeframe,
        "horizon_hours": plan.horizon_hours,
        "direction": plan.direction,
        "conviction": plan.conviction,
        "composite": plan.composite,
        "tech_score": plan.tf_score,
        "news_score": plan.news_score,
        "close": round(float(plan.close), _price_decimals(plan.symbol)) if plan.close else None,
        "atr": plan.atr,
        "rsi": plan.rsi,
        "adx": plan.adx,
        "stop": plan.stop,
        "target1": plan.target1,
        "target2": plan.target2,
        "data_quality": plan.data_quality,
        "features": plan.features,
        "components": plan.components,
        "source": "historical_chart_backfill",
    }


def _quality_report(
    entries: Sequence[dict],
    bars_by_symbol_tf: Mapping[str, Mapping[str, pd.DataFrame]],
    timeframes: Sequence[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, by_timeframe in sorted(bars_by_symbol_tf.items()):
        for timeframe in timeframes:
            bars = by_timeframe.get(timeframe)
            calls = evaluate_timeframe_history(entries, timeframe)
            symbol_calls = [call for call in calls if call.symbol == symbol]
            scored = [call for call in symbol_calls if call.outcome in {"hit", "miss"}]
            hits = sum(1 for call in scored if call.outcome == "hit")
            flats = sum(1 for call in symbol_calls if call.outcome == "flat")
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars": 0 if bars is None else len(bars),
                    "start": None if bars is None or bars.empty else bars.index.min().isoformat(),
                    "end": None if bars is None or bars.empty else bars.index.max().isoformat(),
                    "primary_horizon_hours": PRIMARY_HORIZON_HOURS.get(timeframe),
                    "evaluated": len(scored),
                    "hits": hits,
                    "hit_rate": hits / len(scored) if scored else None,
                    "flat": flats,
                    "atr_fraction": DEFAULT_ATR_FRACTION,
                }
            )
    return pd.DataFrame(rows)


def _install_baseline(
    *,
    learning_path: Path,
    baseline_path: Path,
    quality: pd.DataFrame,
    min_evaluated: int,
    manifest_path: Path,
) -> dict[str, Any]:
    failures = _baseline_quality_failures(quality, min_evaluated)
    if failures:
        shown = "; ".join(failures[:8])
        remainder = "" if len(failures) <= 8 else f"; ... (+{len(failures) - 8})"
        raise ValueError(f"baseline quality gate failed: {shown}{remainder}")

    try:
        payload = json.loads(learning_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read generated learning artifact: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("generated learning artifact is not a JSON object")

    target = Path(baseline_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = target.with_name(f"{target.name}.bak-{stamp}")
        shutil.copy2(target, backup_path)

    payload["baseline"] = {
        "source": "historical_chart_backfill",
        "installed_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "min_evaluated_per_cell": min_evaluated,
    }
    tmp_path = target.with_name(f"{target.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(target)

    return {
        "path": str(target),
        "backup_path": str(backup_path) if backup_path is not None else None,
        "min_evaluated_per_cell": min_evaluated,
    }


def _baseline_quality_failures(quality: pd.DataFrame, min_evaluated: int) -> list[str]:
    failures: list[str] = []
    for row in quality.to_dict("records"):
        symbol = str(row.get("symbol", ""))
        timeframe = str(row.get("timeframe", ""))
        bars = int(row.get("bars") or 0)
        evaluated = int(row.get("evaluated") or 0)
        label = f"{symbol} {timeframe}".strip()
        if bars <= 0:
            failures.append(f"{label}: no bars")
            continue
        if evaluated < min_evaluated:
            failures.append(f"{label}: evaluated {evaluated} < {min_evaluated}")
    return failures


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _price_decimals(symbol: str) -> int:
    return 3 if "JPY" in symbol.upper() else 5


def _parse_args(argv: Sequence[str] | None = None) -> HistoricalBackfillConfig:
    parser = argparse.ArgumentParser(
        description="Build historical timeframe AI learning artifacts from OHLC CSV files"
    )
    parser.add_argument("--data", nargs="+", required=True, help="OHLC CSV file(s)")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--timeframe", dest="timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES)
    )
    parser.add_argument("--start-date", dest="start")
    parser.add_argument("--end-date", dest="end")
    parser.add_argument("--input-timezone")
    parser.add_argument("--timestamp-mode", choices=["start", "close"], default="start")
    parser.add_argument("--fast-window", type=int, default=20)
    parser.add_argument("--slow-window", type=int, default=100)
    parser.add_argument("--atr-window", type=int, default=14)
    parser.add_argument("--adx-window", type=int, default=14)
    parser.add_argument("--atr-multiple", type=float, default=2.5)
    parser.add_argument("--currency-score-csv")
    parser.add_argument("--sentiment-ttl-hours", type=float, default=168.0)
    parser.add_argument(
        "--install-baseline",
        help="品質ゲート通過後、このパスへ時間足別履歴ベースラインを設置する",
    )
    parser.add_argument(
        "--baseline-min-evaluated",
        type=int,
        default=BASELINE_MIN_LIVE_EVALUATED,
        help="baseline install に必要な symbol×timeframe ごとの最低採点件数",
    )
    args = parser.parse_args(argv)
    unknown = sorted(set(args.timeframes) - set(TIMEFRAME_RULES))
    if unknown:
        parser.error(f"unknown timeframe(s): {', '.join(unknown)}")
    if args.baseline_min_evaluated < 0:
        parser.error("--baseline-min-evaluated must be >= 0")
    return HistoricalBackfillConfig(
        data_paths=tuple(Path(path) for path in args.data),
        output_dir=Path(args.output_dir),
        timeframes=tuple(args.timeframes),
        start=args.start,
        end=args.end,
        input_timezone=args.input_timezone,
        timestamp_mode=args.timestamp_mode,
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        atr_window=args.atr_window,
        adx_window=args.adx_window,
        atr_multiple=args.atr_multiple,
        currency_score_csv=Path(args.currency_score_csv) if args.currency_score_csv else None,
        sentiment_ttl_hours=args.sentiment_ttl_hours,
        install_baseline_path=Path(args.install_baseline) if args.install_baseline else None,
        baseline_min_evaluated=args.baseline_min_evaluated,
    )


def main(argv: Sequence[str] | None = None) -> int:
    result = run_backfill(_parse_args(argv))
    print(
        json.dumps(
            {
                "output_dir": str(result.output_dir),
                "price_rows": result.price_rows,
                "journal_rows": result.journal_rows,
                "symbols": list(result.symbols),
                "warnings": list(result.warnings),
                "artifacts": {key: str(value) for key, value in result.artifacts.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
