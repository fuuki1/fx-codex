"""TradingViewマルチタイムフレームのテクニカル集約。

tv_discord_notify.py と同じく tradingview_ta のスキャナーAPIを使い、
複数時間足のレーティング・主要指標を1ペア単位に集約する。
上位足ほど重みを付けた「テクニカル整合スコア」(-1.0〜+1.0)を計算し、
briefing の複合スコアの入力にする。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Sequence

import requests
from tradingview_ta.main import TradingView, calculate

DEFAULT_EXCHANGE = "OANDA"
DEFAULT_SCREENER = "forex"
DEFAULT_INTERVALS = ("15m", "1h", "4h", "1d")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = PROJECT_ROOT / "logs" / "technical_cache.json"

# スキャナーAPIは一時的に失敗することがあるため時間足ごとに再試行する
FETCH_ATTEMPTS = 3
FETCH_RETRY_WAIT_SECONDS = 1.0
SCANNER_TIMEOUT_SECONDS = 12.0
TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com"
TRADINGVIEW_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# tradingview_ta の既定91指標にATRは含まれないため、明示的に追加リクエストする。
# ATRが無いとSL/TP計算・学習の小動き除外・ATR換算期待値がすべて機能しない
ADDITIONAL_INDICATORS = ("ATR",)

# 既定はFXのOANDAだが、金/暗号資産/一部クロスはTradingViewのscreenerが異なる。
# ここで正しい市場へ振り分けないと、TradingViewはHTTP 200でも data=[] を返す。
ROUTE_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "XAUUSD": ("cfd", "OANDA", "XAUUSD"),
    "XAGUSD": ("cfd", "OANDA", "XAGUSD"),
    "BTCUSD": ("crypto", "COINBASE", "BTCUSD"),
    "BTCUSDT": ("crypto", "BINANCE", "BTCUSDT"),
    "ETHUSD": ("crypto", "COINBASE", "ETHUSD"),
    "ETHUSDT": ("crypto", "BINANCE", "ETHUSDT"),
    "CNHJPY": ("forex", "FX_IDC", "CNHJPY"),
    "USDCNH": ("forex", "FX_IDC", "USDCNH"),
}

# キャッシュは一時的な429/空応答を埋めるための短命フォールバック。
# 短期足ほど古い値の危険が大きいので、時間足別に許容時間を変える。
CACHE_MAX_AGE_SECONDS = {
    "15m": 30 * 60,
    "1h": 2 * 60 * 60,
    "4h": 8 * 60 * 60,
    "1d": 48 * 60 * 60,
}

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
class SymbolRoute:
    requested_symbol: str
    screener: str
    exchange: str
    symbol: str

    @property
    def qualified(self) -> str:
        return f"{self.exchange}:{self.symbol}"


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


def _clean_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").strip()


def _route_for_symbol(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
) -> SymbolRoute:
    cleaned = _clean_symbol(symbol)
    if exchange == DEFAULT_EXCHANGE and screener == DEFAULT_SCREENER:
        override = ROUTE_OVERRIDES.get(cleaned)
        if override is not None:
            route_screener, route_exchange, route_symbol = override
            return SymbolRoute(cleaned, route_screener, route_exchange, route_symbol)
    return SymbolRoute(cleaned, screener, exchange, cleaned)


def _indicators_for(fast_window: int, slow_window: int) -> list[str]:
    indicators = TradingView.indicators.copy()
    for indicator in ADDITIONAL_INDICATORS + (f"SMA{fast_window}", f"SMA{slow_window}"):
        if indicator not in indicators:
            indicators.append(indicator)
    return indicators


def _request_analysis_group(
    screener: str,
    interval: str,
    routes: Sequence[SymbolRoute],
    indicators_key: Sequence[str],
) -> dict[str, Any | None]:
    tickers = [route.qualified for route in routes]
    data = TradingView.data(tickers, interval, list(indicators_key))
    response = requests.post(
        f"{TRADINGVIEW_SCAN_URL}/{screener.lower()}/scan",
        json=data,
        headers={
            "User-Agent": TRADINGVIEW_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=SCANNER_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise RuntimeError("HTTP 429 rate limited by TradingView scanner")
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} from TradingView scanner")
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"non-JSON TradingView response ({len(response.content)} bytes)"
        ) from error
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("TradingView scanner response missing data list")

    final: dict[str, Any | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        qualified = str(row.get("s", "")).upper()
        values = row.get("d")
        if not qualified or not isinstance(values, list):
            continue
        indicators = {
            indicators_key[index]: values[index] if index < len(values) else None
            for index in range(len(indicators_key))
        }
        try:
            row_exchange, row_symbol = qualified.split(":", 1)
        except ValueError:
            continue
        final[qualified] = calculate(
            indicators=indicators,
            indicators_key=list(indicators_key),
            screener=screener,
            symbol=row_symbol,
            exchange=row_exchange,
            interval=interval,
        )

    for ticker in tickers:
        final.setdefault(ticker.upper(), None)
    return final


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"entries": {}}
    return payload


def _save_cache(path: Path | None, cache: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # テクニカル取得自体を優先し、キャッシュ保存失敗では落とさない。
        return


def _cache_key(symbol: str, interval: str) -> str:
    return f"{symbol}|{interval}"


def _age_label(age_seconds: float) -> str:
    minutes = max(0, round(age_seconds / 60))
    if minutes < 60:
        return f"{minutes}分前"
    hours = age_seconds / 3600
    return f"{hours:.1f}時間前"


def _cache_view(
    cache: dict[str, Any],
    symbol: str,
    interval: str,
    fast_window: int,
    slow_window: int,
    now: datetime,
) -> tuple[IntervalView, str] | None:
    raw = cache.get("entries", {}).get(_cache_key(symbol, interval))
    if not isinstance(raw, dict):
        return None
    try:
        fetched_at = datetime.fromisoformat(str(raw.get("fetched_at", "")))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    age = (now - fetched_at.astimezone(UTC)).total_seconds()
    max_age = CACHE_MAX_AGE_SECONDS.get(interval, 2 * 60 * 60)
    if age < 0 or age > max_age:
        return None
    summary = raw.get("summary")
    indicators = raw.get("indicators")
    if not isinstance(summary, dict) or not isinstance(indicators, dict):
        return None
    return build_interval_view(interval, summary, indicators, fast_window, slow_window), _age_label(
        age
    )


def _update_cache(
    cache: dict[str, Any],
    symbol: str,
    interval: str,
    entry: Any,
    route: SymbolRoute,
    now: datetime,
) -> None:
    entries = cache.setdefault("entries", {})
    if not isinstance(entries, dict):
        cache["entries"] = entries = {}
    entries[_cache_key(symbol, interval)] = {
        "fetched_at": now.isoformat(),
        "route": {
            "screener": route.screener,
            "exchange": route.exchange,
            "symbol": route.symbol,
        },
        "summary": dict(entry.summary),
        "indicators": dict(entry.indicators),
    }


def _group_routes(routes: Sequence[SymbolRoute]) -> dict[str, list[SymbolRoute]]:
    grouped: dict[str, list[SymbolRoute]] = {}
    for route in routes:
        grouped.setdefault(route.screener, []).append(route)
    return grouped


def fetch_pair_technicals(
    symbols: Sequence[str],
    intervals: Sequence[str] = DEFAULT_INTERVALS,
    fast_window: int = 20,
    slow_window: int = 100,
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
    cache_path: Path | None = DEFAULT_CACHE_PATH,
) -> tuple[dict[str, PairTechnicals], list[str]]:
    """ペアごとのマルチタイムフレーム分析を取得する。

    戻り値は ({symbol: PairTechnicals}, 警告一覧)。
    """
    routes = [_route_for_symbol(symbol, exchange=exchange, screener=screener) for symbol in symbols]
    cleaned = [route.requested_symbol for route in routes]
    result = {
        symbol: PairTechnicals(symbol=symbol, fast_window=fast_window, slow_window=slow_window)
        for symbol in cleaned
    }
    warnings: list[str] = []
    indicators_key = _indicators_for(fast_window, slow_window)
    cache = _load_cache(cache_path)
    cache_dirty = False
    now = datetime.now(UTC)

    for interval in intervals:
        for route_group in _group_routes(routes).values():
            analysis = None
            last_error: Exception | None = None
            for attempt in range(FETCH_ATTEMPTS):
                try:
                    analysis = _request_analysis_group(
                        route_group[0].screener, interval, route_group, indicators_key
                    )
                    break
                except Exception as error:  # noqa: BLE001 - 外部API起因
                    last_error = error
                    if attempt + 1 < FETCH_ATTEMPTS:
                        time.sleep(FETCH_RETRY_WAIT_SECONDS * (attempt + 1))
            if analysis is None:
                for route in route_group:
                    cached = _cache_view(
                        cache, route.requested_symbol, interval, fast_window, slow_window, now
                    )
                    if cached is not None:
                        result[route.requested_symbol].views[interval] = cached[0]
                        warnings.append(
                            f"TradingView {interval} {route.requested_symbol}: "
                            f"取得失敗({last_error}) — 直近成功キャッシュ({cached[1]})を使用"
                        )
                    else:
                        warnings.append(
                            f"TradingView {interval} {route.requested_symbol} "
                            f"取得失敗(再試行{FETCH_ATTEMPTS}回): {last_error}"
                        )
                continue
            for route in route_group:
                entry = analysis.get(route.qualified.upper())
                if entry is None:
                    cached = _cache_view(
                        cache, route.requested_symbol, interval, fast_window, slow_window, now
                    )
                    if cached is not None:
                        result[route.requested_symbol].views[interval] = cached[0]
                        warnings.append(
                            f"TradingView {interval} {route.requested_symbol}: "
                            f"データなし — 直近成功キャッシュ({cached[1]})を使用"
                        )
                    else:
                        warnings.append(
                            f"TradingView {interval} {route.requested_symbol}: データなし"
                        )
                    continue
                _update_cache(cache, route.requested_symbol, interval, entry, route, now)
                cache_dirty = True
                result[route.requested_symbol].views[interval] = build_interval_view(
                    interval, entry.summary, entry.indicators, fast_window, slow_window
                )
    if cache_dirty:
        _save_cache(cache_path, cache)
    return result, warnings
