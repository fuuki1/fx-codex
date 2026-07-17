"""symbol×ホライズンの予測を生成する純ロジック(設計A §3)。

分析時間足(15m/1h/4h/1d)のレーティングとニュースを、ホライズン別のprior重み
(学習後はセル別の学習重み)で合成し、各ホライズンに独立した1予測を出す。
ゲート(鮮度fail-closed・イベント警戒窓・週末・データ品質)は時間足別判断と
同一の設計を全ホライズンへ適用する。

確率(v1)は較正前の暫定値: 固定プライア(上昇0.375/下落0.375/横ばい0.25)を
複合スコアで傾けただけの値で、calibrated=False を必ず付ける。セルの採点済み
サンプルが50件に達したら horizon_learning の較正テーブルが置換する(A2)。

価格帯(v1)は band_provider(状態条件付き経験分位点)が無い間、ATR_hの既定倍率で
埋めて band_source="atr_default" を記録する。「モデルの予言」ではないことを
出力自身が申告する設計。

ネットワークアクセスなし。テストから直接検証できる。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, UTC

from .briefing import _clip, _event_warnings
from .briefing import (
    CALENDAR_UNKNOWN_CONVICTION_CAP,
    MIN_QUALITY_FOR_DIRECTION,
    NEWS_FULL_COVERAGE_COUNT,
    QUALITY_CALENDAR_WEIGHT,
    QUALITY_NEWS_WEIGHT,
    QUALITY_TECH_WEIGHT,
    STANDBY_CONVICTION_CAP,
    DIRECTION_THRESHOLD,
)
from .calendar import RiskWindow, symbol_currencies
from .horizons import (
    ANALYSIS_TIMEFRAMES,
    HORIZON_SPECS,
    HorizonSpec,
    PRIOR_WEIGHTS,
    atr_for_horizon,
    flat_threshold,
    session_label,
    vol_bucket,
)
from .market import is_market_open
from .news import NewsItem
from .sentiment import CurrencySentiment, pair_bias
from .technicals import PairTechnicals

GENERATOR_VERSION = "hf-1"

# v1確率の固定プライアと傾き(較正前の暫定値。設計docに根拠を明記)
_P_UP_BASE = 0.375
_P_DOWN_BASE = 0.375
_P_TILT = 0.25
_P_MIN = 0.05
_P_MAX = 0.90

# band_provider不在時の既定帯(ATR_h倍率)。経験分位点ではない旨をsourceで申告する。
_DEFAULT_BAND_ATR = (-0.8, 0.0, 0.8)

# (symbol, horizon_label) -> (weights上書き dict | None, conviction_factor)
ProfileLookup = Callable[[str, str], tuple[Mapping[str, float] | None, float]]
# (symbol, horizon_label, vol_bucket, session) -> (p10, p50, p90, source) | None
BandProvider = Callable[[str, str, str, str], tuple[float, float, float, str] | None]
# (symbol, horizon_label, composite) -> (p_up, p_down, p_flat) | None
CalibrationProvider = Callable[[str, str, float], tuple[float, float, float] | None]


@dataclass
class HorizonForecast:
    """1 (symbol, horizon) ぶんの予測。journal 1行に対応する。"""

    symbol: str
    horizon: str
    horizon_hours: float
    shadow_only: bool
    direction: str  # long / short / neutral / standby / closed
    composite: float
    conviction: int
    p_up: float
    p_down: float
    p_flat: float
    calibrated: bool
    close: float | None
    atr_h: float | None
    spread: float | None
    flat_threshold: float
    band_p10: float | None
    band_p50: float | None
    band_p90: float | None
    band_source: str
    expected_range: float | None
    data_quality: float
    weights: dict[str, float] = field(default_factory=dict)
    features: dict[str, float | str] = field(default_factory=dict)
    gates: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    generator_version: str = GENERATOR_VERSION


def _uncalibrated_probabilities(composite: float) -> tuple[float, float, float]:
    """固定プライア+線形傾きの暫定確率。simplex(和=1, 各>=0.05)を保証する。"""
    p_up = min(_P_MAX, max(_P_MIN, _P_UP_BASE + _P_TILT * composite))
    p_down = min(_P_MAX, max(_P_MIN, _P_DOWN_BASE - _P_TILT * composite))
    p_flat = 1.0 - p_up - p_down
    if p_flat < _P_MIN:
        # flatの下限を守るため、up/downを比例縮小する
        excess = _P_MIN - p_flat
        total = p_up + p_down
        p_up -= excess * (p_up / total)
        p_down -= excess * (p_down / total)
        p_flat = _P_MIN
    return round(p_up, 4), round(p_down, 4), round(p_flat, 4)


def _horizon_composite(
    tech: PairTechnicals,
    news_score: float,
    weights: Mapping[str, float],
) -> tuple[float, float, dict[str, float]]:
    """ホライズン重みで時間足レーティング+ニュースを合成する。

    欠測時間足の重みは除外して再正規化する。戻り値は
    (複合スコア, テクニカルカバレッジ0-1, 実際に使った正規化済み重み)。
    """
    available: dict[str, float] = {}
    for tf in ANALYSIS_TIMEFRAMES:
        view = tech.views.get(tf)
        if view is not None:
            available[tf] = view.score
    tf_weight_total = sum(weights.get(tf, 0.0) for tf in ANALYSIS_TIMEFRAMES)
    covered_weight = sum(weights.get(tf, 0.0) for tf in available)
    tech_cov = covered_weight / tf_weight_total if tf_weight_total > 0 else 0.0

    used_weights: dict[str, float] = {}
    total = sum(weights.get(tf, 0.0) for tf in available) + weights.get("news", 0.0)
    if total <= 0:
        return 0.0, tech_cov, {}
    score = 0.0
    for tf, rating in available.items():
        w = weights.get(tf, 0.0) / total
        used_weights[f"w_{tf}"] = round(w, 4)
        score += w * rating
    w_news = weights.get("news", 0.0) / total
    used_weights["w_news"] = round(w_news, 4)
    score += w_news * news_score
    return _clip(score), tech_cov, used_weights


def build_horizon_forecasts(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping[str, CurrencySentiment],
    windows: Sequence[RiskWindow],
    news_items: Sequence[NewsItem],
    now: datetime | None = None,
    *,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    spread: float | None = None,
    specs: Sequence[HorizonSpec] = HORIZON_SPECS,
    profile_lookup: ProfileLookup | None = None,
    band_provider: BandProvider | None = None,
    calibration_provider: CalibrationProvider | None = None,
) -> list[HorizonForecast]:
    """1ペアぶんの全ホライズン予測を組み立てる(9本: 本番8+5m shadow)。"""
    now = now or datetime.now(UTC)
    base, quote = symbol_currencies(symbol)
    news_score = pair_bias(base, quote, currency_scores)

    def _relevance(item: NewsItem) -> int:
        return (base in item.currencies) + (quote in item.currencies)

    relevant_items = [item for item in news_items if _relevance(item) > 0]
    news_cov = min(1.0, len(relevant_items) / NEWS_FULL_COVERAGE_COUNT)

    anchor = tech.views.get("1h") or tech.views.get("4h") or tech.views.get("15m")
    close = anchor.close if anchor is not None else None
    atr_pct = None
    if anchor is not None and anchor.atr is not None and anchor.close:
        atr_pct = anchor.atr / anchor.close * 100
    bucket = vol_bucket(atr_pct)
    session = session_label(now)
    view_atrs = {
        tf: view.atr
        for tf, view in tech.views.items()
        if view is not None and view.atr is not None and view.atr > 0
    }

    base_features: dict[str, float | str] = {
        "news_score": round(news_score, 4),
        "news_count": float(len(relevant_items)),
        "session": session,
        "vol_bucket": bucket,
    }
    for tf in ANALYSIS_TIMEFRAMES:
        view = tech.views.get(tf)
        if view is not None:
            base_features[f"rating_{tf}"] = round(view.score, 4)
    if anchor is not None:
        if anchor.rsi is not None:
            base_features["rsi"] = round(anchor.rsi, 2)
        if anchor.adx is not None:
            base_features["adx"] = round(anchor.adx, 2)
        if atr_pct is not None:
            base_features["atr_pct"] = round(atr_pct, 4)
        if (
            anchor.sma_fast is not None
            and anchor.sma_slow is not None
            and anchor.atr is not None
            and anchor.atr > 0
        ):
            base_features["ma_gap_atr"] = round((anchor.sma_fast - anchor.sma_slow) / anchor.atr, 3)

    in_event_window, event_warnings = _event_warnings(windows, now)
    market_open = is_market_open(now)

    forecasts: list[HorizonForecast] = []
    for spec in specs:
        weights: Mapping[str, float] = PRIOR_WEIGHTS[spec.label]
        conviction_factor = 1.0
        if profile_lookup is not None:
            learned_weights, conviction_factor = profile_lookup(symbol, spec.label)
            if learned_weights is not None:
                weights = learned_weights
            conviction_factor = max(0.0, min(1.0, conviction_factor))

        composite, tech_cov, used_weights = _horizon_composite(tech, news_score, weights)
        quality = round(
            QUALITY_TECH_WEIGHT * tech_cov
            + QUALITY_NEWS_WEIGHT * news_cov
            + QUALITY_CALENDAR_WEIGHT * (1.0 if calendar_ok else 0.0),
            3,
        )
        conviction = min(100, round(abs(composite) * 100 * quality))
        warnings = list(event_warnings)

        if conviction_factor < 1.0:
            conviction = round(conviction * conviction_factor)
            warnings.append(
                f"📉 学習調整: {spec.label}ホライズンの過去成績により確信度を"
                f"×{conviction_factor:.2f}に減衰"
            )
        if not calendar_ok:
            conviction = min(conviction, CALENDAR_UNKNOWN_CONVICTION_CAP)

        if not operational_data_ok:
            direction = "neutral"
            conviction = 0
            warnings.append(
                "⛔ 運用データ鮮度ゲート: "
                + (operational_data_reason or "正常性を証明できないため新規判断を停止")
            )
        elif not market_open:
            direction = "closed"
            conviction = 0
        elif in_event_window:
            direction = "standby"
            conviction = min(conviction, STANDBY_CONVICTION_CAP)
        elif tech_cov <= 0 or quality < MIN_QUALITY_FOR_DIRECTION:
            direction = "neutral"
            if abs(composite) >= DIRECTION_THRESHOLD:
                warnings.append(f"データ品質不足(品質{quality:.0%})のため方向判断を見送り")
        elif composite >= DIRECTION_THRESHOLD:
            direction = "long"
        elif composite <= -DIRECTION_THRESHOLD:
            direction = "short"
        else:
            direction = "neutral"

        calibrated = False
        probabilities: tuple[float, float, float] | None = None
        if calibration_provider is not None:
            probabilities = calibration_provider(symbol, spec.label, composite)
            calibrated = probabilities is not None
        if probabilities is None:
            probabilities = _uncalibrated_probabilities(composite)
        p_up, p_down, p_flat = probabilities

        atr_h = atr_for_horizon(view_atrs, spec.hours)
        threshold = flat_threshold(atr_h, spread)

        band: tuple[float, float, float, str] | None = None
        if band_provider is not None:
            band = band_provider(symbol, spec.label, bucket, session)
        if band is None and atr_h is not None:
            band = (
                _DEFAULT_BAND_ATR[0] * atr_h,
                _DEFAULT_BAND_ATR[1] * atr_h,
                _DEFAULT_BAND_ATR[2] * atr_h,
                "atr_default",
            )
        band_p10: float | None
        band_p50: float | None
        band_p90: float | None
        expected_range: float | None
        if band is not None:
            band_p10, band_p50, band_p90, band_source = band
            expected_range = round(band_p90 - band_p10, 6)
        else:
            band_p10 = band_p50 = band_p90 = None
            band_source = "unavailable"
            expected_range = None
            warnings.append(f"⚠️ ATR取得失敗 — {spec.label}の価格帯・横ばい閾値を算出できず")

        forecasts.append(
            HorizonForecast(
                symbol=symbol,
                horizon=spec.label,
                horizon_hours=spec.hours,
                shadow_only=spec.shadow_only,
                direction=direction,
                composite=round(composite, 4),
                conviction=conviction,
                p_up=p_up,
                p_down=p_down,
                p_flat=p_flat,
                calibrated=calibrated,
                close=close,
                atr_h=round(atr_h, 6) if atr_h is not None else None,
                spread=spread,
                flat_threshold=round(threshold, 6),
                band_p10=round(band_p10, 6) if band_p10 is not None else None,
                band_p50=round(band_p50, 6) if band_p50 is not None else None,
                band_p90=round(band_p90, 6) if band_p90 is not None else None,
                band_source=band_source,
                expected_range=expected_range,
                data_quality=quality,
                weights=used_weights,
                features=dict(base_features),
                gates={
                    "freshness_ok": operational_data_ok,
                    "event_window": in_event_window,
                    "market_open": market_open,
                    "calendar_ok": calendar_ok,
                },
                warnings=warnings,
            )
        )
    return forecasts
