"""TradingViewマルチタイムフレームのテクニカル集約。

tv_discord_notify.py と同じく tradingview_ta のスキャナーAPIを使い、
複数時間足のレーティング・主要指標を1ペア単位に集約する。
上位足ほど重みを付けた「テクニカル整合スコア」(-1.0〜+1.0)を計算し、
briefing の複合スコアの入力にする。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from collections.abc import Sequence

from tradingview_ta import get_multiple_analysis

DEFAULT_EXCHANGE = "OANDA"
DEFAULT_SCREENER = "forex"
DEFAULT_INTERVALS = ("15m", "1h", "4h", "1d")

# スキャナーAPIは一時的に失敗することがあるため時間足ごとに再試行する
FETCH_ATTEMPTS = 2
FETCH_RETRY_WAIT_SECONDS = 1.0

# tradingview_ta の既定91指標にATRは含まれないため、明示的に追加リクエストする。
# ATRが無いとSL/TP計算・学習の小動き除外・ATR換算期待値がすべて機能しない
ADDITIONAL_INDICATORS = ("ATR",)

# 上位足ほど重い(合計1.0)
INTERVAL_WEIGHTS = {"15m": 0.15, "1h": 0.30, "4h": 0.30, "1d": 0.25}

# An authoritative provider timestamp, when one is supplied, must be recent
# enough for the timeframe it represents. Missing provider provenance remains an
# explicit warning because tradingview_ta does not consistently expose it; a
# declared-but-stale timestamp is stronger evidence and is therefore critical.
MAX_AUTHORITATIVE_SOURCE_AGE = {
    "15m": timedelta(minutes=45),
    "1h": timedelta(hours=2),
    "4h": timedelta(hours=8),
    "1d": timedelta(hours=36),
}

RECOMMENDATION_SCORE = {
    "STRONG_BUY": 1.0,
    "BUY": 0.5,
    "NEUTRAL": 0.0,
    "SELL": -0.5,
    "STRONG_SELL": -1.0,
}

RECOMMENDATION_JA = {
    "STRONG_BUY": "強い買い",
    "BUY": "買い",
    "NEUTRAL": "中立",
    "SELL": "売り",
    "STRONG_SELL": "強い売り",
}


@dataclass(frozen=True)
class IntervalView:
    interval: str
    recommendation: str
    buy: int
    sell: int
    neutral: int
    close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    adx: float | None = None
    atr: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    source_time: datetime | None = None
    available_time: datetime | None = None
    ingested_time: datetime | None = None
    source_record_id: str | None = None
    provenance_required: bool = False

    @property
    def recommendation_ja(self) -> str:
        return RECOMMENDATION_JA.get(self.recommendation, self.recommendation)

    @property
    def score(self) -> float:
        return RECOMMENDATION_SCORE.get(self.recommendation, 0.0)

    @property
    def quality_issues(self) -> tuple[str, ...]:
        """Critical source-boundary violations that make this view unusable."""

        issues: list[str] = []
        if self.recommendation not in RECOMMENDATION_SCORE:
            issues.append("unknown_recommendation")
        if any(value < 0 for value in (self.buy, self.sell, self.neutral)):
            issues.append("negative_recommendation_count")
        if self.close is None:
            issues.append("missing_close")
        elif not isfinite(self.close) or self.close <= 0:
            issues.append("invalid_close")

        prices = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }
        for name, value in prices.items():
            if value is not None and (not isfinite(value) or value <= 0):
                issue = f"invalid_{name}"
                if issue not in issues:
                    issues.append(issue)
        if self.high is not None and self.low is not None and self.high < self.low:
            issues.append("high_below_low")
        if self.high is not None:
            for name in ("open", "close"):
                value = prices[name]
                if value is not None and value > self.high:
                    issues.append(f"{name}_above_high")
        if self.low is not None:
            for name in ("open", "close"):
                value = prices[name]
                if value is not None and value < self.low:
                    issues.append(f"{name}_below_low")

        if self.bid is not None and (not isfinite(self.bid) or self.bid <= 0):
            issues.append("invalid_bid")
        if self.ask is not None and (not isfinite(self.ask) or self.ask <= 0):
            issues.append("invalid_ask")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            issues.append("crossed_quote")
        if self.spread is not None and (not isfinite(self.spread) or self.spread <= 0):
            issues.append("invalid_spread")
        if self.provenance_required:
            if self.available_time is None or self.ingested_time is None:
                issues.append("acquisition_time_unavailable")
            if (
                self.source_time is not None
                and self.available_time is not None
                and self.source_time > self.available_time
            ):
                issues.append("source_time_after_availability")
            if self.source_time is not None and self.available_time is not None:
                age_limit = MAX_AUTHORITATIVE_SOURCE_AGE.get(self.interval, timedelta(hours=2))
                if self.available_time - self.source_time > age_limit:
                    issues.append("authoritative_source_stale")
        return tuple(dict.fromkeys(issues))

    @property
    def provenance_warnings(self) -> tuple[str, ...]:
        """Non-fatal provenance gaps retained in snapshots and operator output."""

        if not self.provenance_required:
            return ()
        warnings: list[str] = []
        if self.source_time is None:
            warnings.append("source_time_unavailable")
        if not self.source_record_id:
            warnings.append("source_record_id_unavailable")
        return tuple(warnings)


@dataclass
class PairTechnicals:
    symbol: str
    views: dict[str, IntervalView] = field(default_factory=dict)
    fast_window: int = 20
    slow_window: int = 100
    quality_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def usable_view(self, interval: str) -> IntervalView | None:
        view = self.views.get(interval)
        if view is None or view.quality_issues:
            return None
        return view

    def critical_quality_issues(self) -> dict[str, tuple[str, ...]]:
        issues = dict(self.quality_errors)
        for interval, view in self.views.items():
            if view.quality_issues:
                issues[interval] = view.quality_issues
        return issues

    def provenance_warnings(self) -> dict[str, tuple[str, ...]]:
        return {
            interval: view.provenance_warnings
            for interval, view in self.views.items()
            if view.provenance_warnings
        }

    def alignment_score(self) -> float:
        """時間足の重み付きレーティング平均(-1.0〜+1.0)。"""
        total_weight = 0.0
        total = 0.0
        for interval in self.views:
            view = self.usable_view(interval)
            if view is None:
                continue
            weight = INTERVAL_WEIGHTS.get(interval, 0.1)
            total += view.score * weight
            total_weight += weight
        if total_weight == 0:
            return 0.0
        return round(total / total_weight, 3)

    def coverage(self, intervals: Sequence[str] = DEFAULT_INTERVALS) -> float:
        """取得できた時間足の重み合計 / 期待する重み合計(0.0〜1.0)。

        briefing のデータ品質判定に使う。1.0なら全時間足が揃っている。
        """
        expected = sum(INTERVAL_WEIGHTS.get(i, 0.1) for i in intervals)
        if expected == 0:
            return 0.0
        got = sum(INTERVAL_WEIGHTS.get(i, 0.1) for i in intervals if self.usable_view(i))
        return round(got / expected, 3)

    def missing_intervals(self, intervals: Sequence[str] = DEFAULT_INTERVALS) -> list[str]:
        return [i for i in intervals if self.usable_view(i) is None]

    def agreement_ratio(self) -> float | None:
        """時間足レーティングの向きがどれだけ揃っているか(0.0〜1.0)。

        重み付き平均(alignment_score)と同符号のレーティングを出している
        時間足の割合。中立(スコア0)の時間足は「揃っていない」側に数える。
        全体が中立(平均0)や未取得なら判定不能でNone。
        学習ジャーナルの特徴量「tf_agreement」に使う。
        """
        usable = [view for interval in self.views if (view := self.usable_view(interval))]
        if not usable:
            return None
        overall = self.alignment_score()
        if overall == 0:
            return None
        agree = sum(1 for view in usable if view.score != 0 and (view.score > 0) == (overall > 0))
        return round(agree / len(usable), 3)

    def ma_side(self, interval: str = "1h") -> str | None:
        """自作MAクロス戦略と同じ目線判定(long/short/None)。"""
        view = self.usable_view(interval)
        if view is None or view.sma_fast is None or view.sma_slow is None:
            return None
        if view.sma_fast > view.sma_slow:
            return "long"
        if view.sma_fast < view.sma_slow:
            return "short"
        return None

    def close(self, interval: str = "1h") -> float | None:
        view = self.usable_view(interval)
        return view.close if view else None

    def price_snapshot(self, interval: str = "1h") -> dict[str, object] | None:
        view = self.usable_view(interval)
        if view is None or view.close is None:
            return None
        snapshot = {
            "close": view.close,
            "open": view.open,
            "high": view.high,
            "low": view.low,
            "bid": view.bid,
            "ask": view.ask,
            "spread": view.spread,
            "source_time": view.source_time,
            "available_time": view.available_time,
            "ingested_time": view.ingested_time,
            "source_record_id": view.source_record_id,
            "data_quality_flags": list(view.provenance_warnings),
        }
        return {key: value for key, value in snapshot.items() if value is not None}

    def atr(self, interval: str = "1h") -> float | None:
        view = self.usable_view(interval)
        return view.atr if view else None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _indicator_float(indicators: dict, *keys: str) -> float | None:
    for key in keys:
        value = _to_float(indicators.get(key))
        if value is not None:
            return value
    return None


def _spread(bid: float | None, ask: float | None, fallback: float | None) -> float | None:
    if fallback is not None:
        return fallback
    if bid is None or ask is None:
        return None
    return ask - bid


def build_interval_view(
    interval: str,
    summary: dict,
    indicators: dict,
    fast: int,
    slow: int,
    *,
    source_time: datetime | None = None,
    acquired_at: datetime | None = None,
    source_record_id: str | None = None,
    provenance_required: bool = False,
) -> IntervalView:
    bid = _indicator_float(indicators, "bid", "Bid")
    ask = _indicator_float(indicators, "ask", "Ask")
    return IntervalView(
        interval=interval,
        recommendation=str(summary.get("RECOMMENDATION", "NEUTRAL")),
        buy=int(summary.get("BUY", 0)),
        sell=int(summary.get("SELL", 0)),
        neutral=int(summary.get("NEUTRAL", 0)),
        close=_indicator_float(indicators, "close", "Close"),
        open=_indicator_float(indicators, "open", "Open"),
        high=_indicator_float(indicators, "high", "High"),
        low=_indicator_float(indicators, "low", "Low"),
        bid=bid,
        ask=ask,
        spread=_spread(bid, ask, _indicator_float(indicators, "spread", "Spread")),
        rsi=_to_float(indicators.get("RSI")),
        macd=_to_float(indicators.get("MACD.macd")),
        macd_signal=_to_float(indicators.get("MACD.signal")),
        adx=_to_float(indicators.get("ADX")),
        atr=_to_float(indicators.get("ATR")),
        sma_fast=_to_float(indicators.get(f"SMA{fast}")),
        sma_slow=_to_float(indicators.get(f"SMA{slow}")),
        source_time=_aware_datetime_or_none(source_time),
        available_time=_aware_datetime_or_none(acquired_at),
        ingested_time=_aware_datetime_or_none(acquired_at),
        source_record_id=source_record_id,
        provenance_required=provenance_required,
    )


def _aware_datetime_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def fetch_pair_technicals(
    symbols: Sequence[str],
    intervals: Sequence[str] = DEFAULT_INTERVALS,
    fast_window: int = 20,
    slow_window: int = 100,
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
) -> tuple[dict[str, PairTechnicals], list[str]]:
    """ペアごとのマルチタイムフレーム分析を取得する。

    戻り値は ({symbol: PairTechnicals}, 警告一覧)。
    """
    cleaned = [s.upper().replace("/", "") for s in symbols]
    qualified = [f"{exchange}:{s}" for s in cleaned]
    result = {
        symbol: PairTechnicals(symbol=symbol, fast_window=fast_window, slow_window=slow_window)
        for symbol in cleaned
    }
    warnings: list[str] = []

    for interval in intervals:
        analysis = None
        last_error: Exception | None = None
        for attempt in range(FETCH_ATTEMPTS):
            try:
                analysis = get_multiple_analysis(
                    screener=screener,
                    interval=interval,
                    symbols=qualified,
                    additional_indicators=list(ADDITIONAL_INDICATORS),
                )
                break
            except Exception as error:  # noqa: BLE001 - 外部API起因
                last_error = error
                if attempt + 1 < FETCH_ATTEMPTS:
                    time.sleep(FETCH_RETRY_WAIT_SECONDS)
        if analysis is None:
            warnings.append(
                f"TradingView {interval} 取得失敗(再試行{FETCH_ATTEMPTS}回): {last_error}"
            )
            continue
        acquired_at = datetime.now(UTC)
        for symbol in cleaned:
            entry = analysis.get(f"{exchange}:{symbol}")
            if entry is None:
                warnings.append(f"TradingView {interval} {symbol}: データなし")
                continue
            source_time = _aware_datetime_or_none(
                getattr(entry, "time", None)
                or entry.indicators.get("source_time")
                or entry.indicators.get("timestamp")
            )
            source_record_id = (
                str(
                    entry.indicators.get("source_record_id")
                    or entry.indicators.get("record_id")
                    or ""
                ).strip()
                or None
            )
            view = build_interval_view(
                interval,
                entry.summary,
                entry.indicators,
                fast_window,
                slow_window,
                source_time=source_time,
                acquired_at=acquired_at,
                source_record_id=source_record_id,
                provenance_required=True,
            )
            if view.quality_issues:
                result[symbol].quality_errors[interval] = view.quality_issues
                warnings.append(
                    f"TradingView {interval} {symbol}: 品質違反 " + ",".join(view.quality_issues)
                )
                continue
            result[symbol].views[interval] = view
            if view.provenance_warnings:
                warnings.append(
                    f"TradingView {interval} {symbol}: 出所警告 "
                    + ",".join(view.provenance_warnings)
                )
    return result, warnings
