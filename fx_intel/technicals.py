"""TradingViewマルチタイムフレームのテクニカル集約。

tv_discord_notify.py と同じく tradingview_ta のスキャナーAPIを使い、
複数時間足のレーティング・主要指標を1ペア単位に集約する。
上位足ほど重みを付けた「テクニカル整合スコア」(-1.0〜+1.0)を計算し、
briefing の複合スコアの入力にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tradingview_ta import get_multiple_analysis

DEFAULT_EXCHANGE = "OANDA"
DEFAULT_SCREENER = "forex"
DEFAULT_INTERVALS = ("15m", "1h", "4h", "1d")

# 上位足ほど重い(合計1.0)
INTERVAL_WEIGHTS = {"15m": 0.15, "1h": 0.30, "4h": 0.30, "1d": 0.25}

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
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    adx: float | None = None
    atr: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None

    @property
    def recommendation_ja(self) -> str:
        return RECOMMENDATION_JA.get(self.recommendation, self.recommendation)

    @property
    def score(self) -> float:
        return RECOMMENDATION_SCORE.get(self.recommendation, 0.0)


@dataclass
class PairTechnicals:
    symbol: str
    views: dict[str, IntervalView] = field(default_factory=dict)
    fast_window: int = 20
    slow_window: int = 100

    def alignment_score(self) -> float:
        """時間足の重み付きレーティング平均(-1.0〜+1.0)。"""
        total_weight = 0.0
        total = 0.0
        for interval, view in self.views.items():
            weight = INTERVAL_WEIGHTS.get(interval, 0.1)
            total += view.score * weight
            total_weight += weight
        if total_weight == 0:
            return 0.0
        return round(total / total_weight, 3)

    def ma_side(self, interval: str = "1h") -> str | None:
        """自作MAクロス戦略と同じ目線判定(long/short/None)。"""
        view = self.views.get(interval)
        if view is None or view.sma_fast is None or view.sma_slow is None:
            return None
        if view.sma_fast > view.sma_slow:
            return "long"
        if view.sma_fast < view.sma_slow:
            return "short"
        return None

    def close(self, interval: str = "1h") -> float | None:
        view = self.views.get(interval)
        return view.close if view else None

    def atr(self, interval: str = "1h") -> float | None:
        view = self.views.get(interval)
        return view.atr if view else None


def _to_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def build_interval_view(
    interval: str, summary: dict, indicators: dict, fast: int, slow: int
) -> IntervalView:
    return IntervalView(
        interval=interval,
        recommendation=str(summary.get("RECOMMENDATION", "NEUTRAL")),
        buy=int(summary.get("BUY", 0)),
        sell=int(summary.get("SELL", 0)),
        neutral=int(summary.get("NEUTRAL", 0)),
        close=_to_float(indicators.get("close")),
        rsi=_to_float(indicators.get("RSI")),
        macd=_to_float(indicators.get("MACD.macd")),
        macd_signal=_to_float(indicators.get("MACD.signal")),
        adx=_to_float(indicators.get("ADX")),
        atr=_to_float(indicators.get("ATR")),
        sma_fast=_to_float(indicators.get(f"SMA{fast}")),
        sma_slow=_to_float(indicators.get(f"SMA{slow}")),
    )


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
        symbol: PairTechnicals(
            symbol=symbol, fast_window=fast_window, slow_window=slow_window
        )
        for symbol in cleaned
    }
    warnings: list[str] = []

    for interval in intervals:
        try:
            analysis = get_multiple_analysis(
                screener=screener, interval=interval, symbols=qualified
            )
        except Exception as error:  # noqa: BLE001 - 外部API起因
            warnings.append(f"TradingView {interval} 取得失敗: {error}")
            continue
        for symbol in cleaned:
            entry = analysis.get(f"{exchange}:{symbol}")
            if entry is None:
                warnings.append(f"TradingView {interval} {symbol}: データなし")
                continue
            result[symbol].views[interval] = build_interval_view(
                interval, entry.summary, entry.indicators, fast_window, slow_window
            )
    return result, warnings
