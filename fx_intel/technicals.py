"""TradingViewマルチタイムフレームのテクニカル集約。

tv_discord_notify.py と同じく tradingview_ta のスキャナーAPIを使い、
複数時間足のレーティング・主要指標を1ペア単位に集約する。
上位足ほど重みを付けた「テクニカル整合スコア」(-1.0〜+1.0)を計算し、
briefing の複合スコアの入力にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence

from fx_intel.tv_scanner import ScannerError, get_multiple_analysis

DEFAULT_EXCHANGE = "OANDA"
DEFAULT_SCREENER = "forex"
DEFAULT_INTERVALS = ("15m", "1h", "4h", "1d")

# 一時的な取得失敗(429/ネットワーク/サーバエラー)の再試行・バックオフは
# 管理版スキャナークライアント(fx_intel.tv_scanner)が担当する。ここでは
# ScannerError を「一時障害」として分類し、恒久的な空dataと区別するだけ。

# tradingview_ta の既定91指標にATRは含まれないため、明示的に追加リクエストする。
# ATRが無いとSL/TP計算・学習の小動き除外・ATR換算期待値がすべて機能しない
ADDITIONAL_INDICATORS = ("ATR",)

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
    # 一時的な取得失敗(ネットワーク/429/HTTP/非JSON)が起きた時間足。
    # 恒久的な「データなし」(空dataでの None)とは区別し、全滅時の終了コード判定に使う。
    transient_failures: list[str] = field(default_factory=list)

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

    def coverage(self, intervals: Sequence[str] = DEFAULT_INTERVALS) -> float:
        """取得できた時間足の重み合計 / 期待する重み合計(0.0〜1.0)。

        briefing のデータ品質判定に使う。1.0なら全時間足が揃っている。
        """
        expected = sum(INTERVAL_WEIGHTS.get(i, 0.1) for i in intervals)
        if expected == 0:
            return 0.0
        got = sum(INTERVAL_WEIGHTS.get(i, 0.1) for i in intervals if i in self.views)
        return round(got / expected, 3)

    def missing_intervals(self, intervals: Sequence[str] = DEFAULT_INTERVALS) -> list[str]:
        return [i for i in intervals if i not in self.views]

    def agreement_ratio(self) -> float | None:
        """時間足レーティングの向きがどれだけ揃っているか(0.0〜1.0)。

        重み付き平均(alignment_score)と同符号のレーティングを出している
        時間足の割合。中立(スコア0)の時間足は「揃っていない」側に数える。
        全体が中立(平均0)や未取得なら判定不能でNone。
        学習ジャーナルの特徴量「tf_agreement」に使う。
        """
        if not self.views:
            return None
        overall = self.alignment_score()
        if overall == 0:
            return None
        agree = sum(
            1
            for view in self.views.values()
            if view.score != 0 and (view.score > 0) == (overall > 0)
        )
        return round(agree / len(self.views), 3)

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

    def price_snapshot(self, interval: str = "1h") -> dict[str, float] | None:
        view = self.views.get(interval)
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
        }
        return {key: value for key, value in snapshot.items() if value is not None}

    def atr(self, interval: str = "1h") -> float | None:
        view = self.views.get(interval)
        return view.atr if view else None


def _to_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


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
    return abs(ask - bid)


def build_interval_view(
    interval: str, summary: dict, indicators: dict, fast: int, slow: int
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
        symbol: PairTechnicals(symbol=symbol, fast_window=fast_window, slow_window=slow_window)
        for symbol in cleaned
    }
    warnings: list[str] = []

    for interval in intervals:
        try:
            analysis = get_multiple_analysis(
                screener=screener,
                interval=interval,
                symbols=qualified,
                additional_indicators=list(ADDITIONAL_INDICATORS),
            )
        except ScannerError as error:
            # 一時障害(429/ネットワーク/HTTP/非JSON)。クライアントが上限付きで
            # 再試行し尽くしたうえでの失敗。全銘柄をこの足で「一時失敗」に記録する。
            warnings.append(f"TradingView {interval} 取得失敗(一時障害): {error}")
            for symbol in cleaned:
                result[symbol].transient_failures.append(interval)
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
