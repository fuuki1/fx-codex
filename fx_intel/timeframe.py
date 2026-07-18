"""時間足別の独立した方向判断を生成する。

briefing.build_trade_plan が「全時間足を融合した1ペア1判断」を出すのに対し、
このモジュールは 15m / 1h / 4h / 1d の各時間足を**独立した1人のアナリスト**として
扱い、それぞれに long / short / neutral(見送り) / standby(様子見) / closed(休場) の
判断を出す。プロトレーダーが複数時間足を別々に読み、各時間軸で別の結論を持つのと同じ。

各時間足には「主ホライズン」がある: その時間足での判断が的中したかを
どれだけ先の値動きで採点するか。

    15m → 15分後    1h → 1時間後    4h → 4時間後    1d → 24時間後

補助ホライズン(15m:30分/1h、1h:4h/8h 等)は Discord 表示・分析確認用の
「観測」であって学習には使わない(learning.py は主ホライズンのみで学習する)。
主ホライズンだけを学習に使うのは、同じ判断を複数の未来時間で採点すると
多重検定で偶然のパターンを拾ってしまうため。

判断ロジックは briefing の複合スコア設計を1時間足に絞って再利用する:

- その時間足自身のレーティング(score)を中核に、上位足レーティングを
  順張り/逆張りボーナスとして加味した「時間足スコア」を作る
- ニュースセンチメント(ペア単位・全時間足共通)を briefing と同じ重みで合成
- データ品質・イベント警戒窓・週末クローズ・テク/ニュース対立の各ゲートは
  briefing と同一(判断の確実性を担保するため)
- 学習プロファイル(conviction_factor / condition_adjuster)と期待値ガードの
  注入点も同じ

ネットワークアクセスを持たない純粋ロジックで、テストから直接検証できる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from collections.abc import Callable, Mapping, Sequence

from .briefing import (
    CALENDAR_UNKNOWN_CONVICTION_CAP,
    CONFLICT_CONVICTION_FACTOR,
    CONFLICT_THRESHOLD,
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_RISK_PCT,
    DIRECTION_EMOJI,
    DIRECTION_JA,
    DIRECTION_THRESHOLD,
    MIN_QUALITY_FOR_DIRECTION,
    NEWS_WEIGHT,
    QUALITY_CALENDAR_WEIGHT,
    QUALITY_NEWS_WEIGHT,
    QUALITY_TECH_WEIGHT,
    STANDBY_CONVICTION_CAP,
    TECH_WEIGHT,
    _clip,
    _event_warnings,
    NEWS_FULL_COVERAGE_COUNT,
)
from .calendar import RiskWindow, active_and_next_window, symbol_currencies
from .market import is_market_open
from .news import NewsItem
from .sentiment import CurrencySentiment, pair_bias
from .technicals import PairTechnicals
from .evaluation_labels import (
    DEFAULT_COMMISSION_R,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_SLIPPAGE_R,
)
from .shadow_learning import build_shadow_predictions, prediction_draft
from . import input_context as decision_inputs

ExpectancyAdjuster = Callable[[str, str, int], tuple[float, str, bool]]
ExpectancyLookup = Callable[[str, str], ExpectancyAdjuster | None]
TargetRAdjustment = tuple[float, float, str] | tuple[float, float, str, Mapping[str, object]]
TargetRAdjuster = Callable[[str, str, int], TargetRAdjustment | None]

# 時間足ごとの主ホライズン(学習に使う。単位=時間、市場オープン時間換算)と
# 補助ホライズン(表示・観測専用)。ユーザー仕様に対応:
#   15m→15分後(補助 30分/1h) / 1h→1h(補助 4h/8h) /
#   4h→4h(補助 12h/24h)     / 1d→24h(補助 48h/72h)
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

PRIMARY_HORIZON_HOURS: dict[str, float] = {
    "15m": 0.25,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
}

AUXILIARY_HORIZON_HOURS: dict[str, tuple[float, ...]] = {
    "15m": (0.5, 1.0),
    "1h": (4.0, 8.0),
    "4h": (12.0, 24.0),
    "1d": (48.0, 72.0),
}

# 採点のホライズンごとに市場オープン時間換算の許容誤差(±)を決める。
# 短い足ほど狭く、長い足ほど広く取る(TradingView足の刻みに合わせる)。
HORIZON_TOLERANCE_HOURS: dict[float, float] = {
    0.25: 0.1,
    0.5: 0.15,
    1.0: 0.25,
    4.0: 1.0,
    8.0: 1.5,
    12.0: 2.0,
    24.0: 2.0,
    48.0: 4.0,
    72.0: 6.0,
}
DEFAULT_TOLERANCE_HOURS = 2.0

# 上位足レーティングを順張り/逆張りとして時間足スコアに加味する重み。
# その足自身のレーティングを主、上位足を従にする(合計で -1.0〜+1.0 にクランプ)。
HIGHER_TF_BONUS = 0.2
# 15分足の観測分析だけに使う低い閾値。本番売買のDIRECTION_THRESHOLDは変更しない。
FIFTEEN_MINUTE_ANALYSIS_THRESHOLD = 0.05

# どの時間足がどの上位足を「上位」とみなすか(順張り判定に使う)。
HIGHER_TIMEFRAMES: dict[str, tuple[str, ...]] = {
    "15m": ("1h", "4h"),
    "1h": ("4h", "1d"),
    "4h": ("1d",),
    "1d": (),
}


def tolerance_for(horizon_hours: float) -> float:
    """ホライズン(時間)に対応する採点許容誤差(±時間)。"""
    return HORIZON_TOLERANCE_HOURS.get(horizon_hours, DEFAULT_TOLERANCE_HOURS)


@dataclass
class TimeframePlan:
    """1時間足ぶんの独立した方向判断。

    briefing.TradePlan と同じ情報を持ちつつ、timeframe(どの足の判断か)と
    horizon_hours(その判断を何時間後の値動きで採点するか=主ホライズン)を
    加える。ジャーナル・学習はこの2つでセルを分ける。
    """

    symbol: str
    timeframe: str
    horizon_hours: float
    direction: str  # long / short / neutral / standby / closed
    conviction: int  # 0〜100
    tf_score: float  # その時間足の方向スコア(-1.0〜+1.0)
    news_score: float
    composite: float
    # 売買ゲート適用前の観測用分析。standby中も別系列で満期採点する。
    analysis_direction: str = "neutral"
    analysis_conviction: int = 0
    close: float | None = None
    atr: float | None = None
    rsi: float | None = None
    adx: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    entry_bid: float | None = None
    entry_ask: float | None = None
    quote_observed_at: str | None = None
    cost_model_id: str = DEFAULT_COST_MODEL_ID
    slippage_r: float = DEFAULT_SLIPPAGE_R
    commission_r: float = DEFAULT_COMMISSION_R
    direction_threshold: float = DIRECTION_THRESHOLD
    risk_pct: float = DEFAULT_RISK_PCT
    data_quality: float = 1.0
    tech_weight: float = TECH_WEIGHT
    news_weight: float = NEWS_WEIGHT
    # 判断時点のチャート状態(learning.py の状態別学習の入力)。
    # briefing と同じ特徴量スキーマ + timeframe 固有の rsi/adx を含める
    features: dict[str, float] = field(default_factory=dict)
    components: list[dict] = field(default_factory=list)
    reason: str = ""  # 判断根拠の一文(Discord表示・監査用)
    warnings: list[str] = field(default_factory=list)
    target_policy: dict[str, object] = field(default_factory=dict)
    # 補助ホライズン(観測専用)。{ホライズン時間: ラベル} の順序付き情報
    auxiliary_horizons: tuple[float, ...] = ()
    learning_dimensions: dict[str, object] = field(default_factory=dict)
    gate_trace: list[dict[str, object]] = field(default_factory=list)
    shadow_predictions: list[dict[str, object]] = field(default_factory=list)
    input_context_id: str = ""
    input_features: dict[str, float | None] = field(default_factory=dict)
    input_feature_masks: dict[str, int] = field(default_factory=dict)
    input_context: dict[str, object] = field(default_factory=dict)

    @property
    def direction_ja(self) -> str:
        return DIRECTION_JA.get(self.direction, self.direction)

    @property
    def emoji(self) -> str:
        return DIRECTION_EMOJI.get(self.direction, "⚪")


def _tf_direction_score(tech: PairTechnicals, timeframe: str) -> tuple[float, str] | None:
    """時間足自身のレーティング + 上位足順張りボーナスで方向スコアを作る。

    その足の IntervalView が無ければ None(判断不能)。戻り値は
    (スコア -1.0〜+1.0, 根拠の一文)。上位足が同じ向きなら加点、逆なら減点する。
    """
    view = tech.views.get(timeframe)
    if view is None:
        return None
    base = view.score
    higher_notes: list[str] = []
    bonus = 0.0
    for higher in HIGHER_TIMEFRAMES.get(timeframe, ()):  # 上位足の順張り/逆行
        higher_view = tech.views.get(higher)
        if higher_view is None:
            continue
        bonus += HIGHER_TF_BONUS * higher_view.score
        if higher_view.score != 0:
            direction_word = "順行" if (higher_view.score > 0) == (base >= 0) else "逆行"
            higher_notes.append(f"{higher}は{higher_view.recommendation_ja}({direction_word})")
    score = _clip(base + bonus)
    reason = f"{timeframe}レーティング {view.recommendation_ja}"
    if higher_notes:
        reason += " / 上位足: " + "、".join(higher_notes)
    return score, reason


def _extract_tf_features(tech: PairTechnicals, timeframe: str, news_count: int) -> dict[str, float]:
    """時間足別の学習特徴量。

    briefing._extract_features は 1h 固定でチャート状態を採るが、こちらは
    判断対象の時間足自身の RSI/ADX/MA乖離/ボラを採り、上位足レーティングを
    add する。learning.py の FEATURE_SPECS(rsi_1h/adx_1h/ma_gap_atr/atr_pct/
    rating_4h/rating_1d/tf_agreement/news_count)と同じキー名で記録するため、
    その足の値を rsi_1h 等の共通キーに入れる(セルは timeframe で別集計される)。
    """
    features: dict[str, float] = {"news_count": float(news_count)}
    for interval in ("4h", "1d"):
        higher = tech.views.get(interval)
        if higher is not None:
            features[f"rating_{interval}"] = higher.score
    view = tech.views.get(timeframe)
    if view is not None:
        if view.rsi is not None:
            features["rsi_1h"] = round(view.rsi, 2)
        if view.adx is not None:
            features["adx_1h"] = round(view.adx, 2)
        if (
            view.sma_fast is not None
            and view.sma_slow is not None
            and view.atr is not None
            and view.atr > 0
        ):
            features["ma_gap_atr"] = round((view.sma_fast - view.sma_slow) / view.atr, 3)
        if view.atr is not None and view.close:
            features["atr_pct"] = round(view.atr / view.close * 100, 4)
    agreement = tech.agreement_ratio()
    if agreement is not None:
        features["tf_agreement"] = agreement
    return features


def build_timeframe_plan(
    symbol: str,
    timeframe: str,
    tech: PairTechnicals,
    currency_scores: Mapping[str, CurrencySentiment],
    windows: Sequence[RiskWindow],
    news_items: Sequence[NewsItem],
    now: datetime | None = None,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    tech_weight: float = TECH_WEIGHT,
    news_weight: float = NEWS_WEIGHT,
    conviction_factor: float = 1.0,
    condition_adjuster: Callable[[Mapping[str, float], str], tuple[float, str]] | None = None,
    expectancy_adjuster: ExpectancyAdjuster | None = None,
    target_r_adjuster: TargetRAdjuster | None = None,
    direction_threshold: float = DIRECTION_THRESHOLD,
    learning_dimensions: Mapping[str, object] | None = None,
    input_context: Mapping[str, object] | None = None,
) -> TimeframePlan:
    """1ペア・1時間足ぶんの独立した方向判断を組み立てる。

    ゲート(データ品質・イベント警戒窓・週末クローズ・テク/ニュース対立)と
    学習調整(conviction_factor / condition_adjuster)と期待値ガードは
    briefing.build_trade_plan と同一の設計。違いは「複合スコアのテクニカル側が
    その時間足単体のスコアである」ことと、判断に主ホライズンが紐づくこと。
    """
    now = now or datetime.now(UTC)
    direction_threshold = max(DIRECTION_THRESHOLD, min(1.0, float(direction_threshold)))
    base, quote = symbol_currencies(symbol)
    horizon_hours = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)

    tf_result = _tf_direction_score(tech, timeframe)
    tf_available = tf_result is not None
    tf_score, tf_reason = (
        tf_result if tf_result is not None else (0.0, f"{timeframe}レーティング取得失敗")
    )

    news_score = pair_bias(base, quote, currency_scores)
    total_weight = tech_weight + news_weight
    composite = (
        round((tech_weight * tf_score + news_weight * news_score) / total_weight, 3)
        if total_weight > 0
        else 0.0
    )

    components = [
        {
            "key": "tech",
            "label_ja": f"テクニカル({timeframe})",
            "score": round(tf_score, 3),
            "weight": round(tech_weight / total_weight, 3) if total_weight else 0.0,
        },
        {
            "key": "news",
            "label_ja": "ニュース",
            "score": news_score,
            "weight": round(news_weight / total_weight, 3) if total_weight else 0.0,
        },
    ]

    def _relevance(item: NewsItem) -> int:
        return (base in item.currencies) + (quote in item.currencies)

    relevant_items = [item for item in news_items if _relevance(item) > 0]
    features = _extract_tf_features(tech, timeframe, len(relevant_items))
    serialized_context = dict(input_context or {})
    view_for_context = tech.views.get(timeframe)
    input_features, input_masks = decision_inputs.flat_features_from_mapping(
        serialized_context,
        atr=view_for_context.atr if view_for_context is not None else None,
    )
    features.update({key: value for key, value in input_features.items() if value is not None})
    features.update({f"{key}__available": float(mask) for key, mask in input_masks.items()})

    # データ品質: その時間足が取れたか(0/1) を tech カバレッジとして扱う。
    # 融合版は全足の重み和だが、時間足別ではその足の有無が本質なので簡潔にする。
    tech_cov = 1.0 if tf_available else 0.0
    news_cov = min(1.0, len(relevant_items) / NEWS_FULL_COVERAGE_COUNT)
    quality = round(
        QUALITY_TECH_WEIGHT * tech_cov
        + QUALITY_NEWS_WEIGHT * news_cov
        + QUALITY_CALENDAR_WEIGHT * (1.0 if calendar_ok else 0.0),
        3,
    )
    conviction = min(100, round(abs(composite) * 100 * quality))

    in_event_window, warnings = _event_warnings(windows, now)

    conviction_factor = max(0.0, min(1.0, conviction_factor))
    if conviction_factor < 1.0:
        conviction = round(conviction * conviction_factor)
        warnings.append(
            f"📉 学習調整: この{timeframe}判断は過去の的中率が低めのため"
            f"確信度を×{conviction_factor:.2f}に減衰"
        )

    if not tf_available:
        warnings.append(f"⚠️ {timeframe}足の取得に失敗 — テクニカル根拠なし")
    if not relevant_items:
        warnings.append("関連ニュース0件 — ニュース根拠なし")
    if not calendar_ok:
        warnings.append(
            "⚠️ 経済指標カレンダー取得不能 — イベントリスク未確認のため"
            f"確信度を{CALENDAR_UNKNOWN_CONVICTION_CAP}以下に制限"
        )
        conviction = min(conviction, CALENDAR_UNKNOWN_CONVICTION_CAP)
    if not operational_data_ok:
        warnings.append(
            "⛔ 運用データ鮮度ゲート: "
            + (operational_data_reason or "正常性を証明できないため新規判断を停止")
        )

    conflict = (
        tf_score * news_score < 0 and min(abs(tf_score), abs(news_score)) >= CONFLICT_THRESHOLD
    )
    if conflict:
        warnings.append(
            f"テクニカル({tf_score:+.2f})とニュース({news_score:+.2f})が反対方向を示しており、"
            "方向感が定まらないため確信度を下げて評価"
        )
        conviction = round(conviction * CONFLICT_CONVICTION_FACTOR)

    market_open = is_market_open(now)
    analysis_threshold = (
        min(direction_threshold, FIFTEEN_MINUTE_ANALYSIS_THRESHOLD)
        if timeframe == "15m"
        else direction_threshold
    )
    if tech_cov <= 0 or quality < MIN_QUALITY_FOR_DIRECTION:
        analysis_direction = "neutral"
    elif composite >= analysis_threshold:
        analysis_direction = "long"
    elif composite <= -analysis_threshold:
        analysis_direction = "short"
    else:
        analysis_direction = "neutral"
    analysis_conviction = conviction if analysis_direction in ("long", "short") else 0
    # 運用データ鮮度が証明できない時は分析ビューも空にする(fail-closed)
    if not operational_data_ok or not market_open:
        analysis_direction = ""
        analysis_conviction = 0
    active_window, _next_window = active_and_next_window(windows, now)
    gate_reasons: list[str] = []
    if not operational_data_ok:
        direction = "neutral"
        conviction = 0
        gate_reasons.append("operational_data_stale")
    elif not market_open:
        direction = "closed"
        conviction = 0
        gate_reasons.append("market_closed")
        warnings.append(
            "💤 FX市場休場中(週末クローズ) — 表示価格は最終取引時点のもの。"
            "方向判断は市場再開後に実施"
        )
    elif in_event_window:
        direction = "standby"
        conviction = min(conviction, STANDBY_CONVICTION_CAP)
        gate_reasons.append("event_window")
    elif tech_cov <= 0 or quality < MIN_QUALITY_FOR_DIRECTION:
        direction = "neutral"
        gate_reasons.append("missing_technical" if tech_cov <= 0 else "low_data_quality")
        if abs(composite) >= direction_threshold:
            warnings.append(f"データ品質不足(品質{quality:.0%})のため方向判断を見送り")
    elif composite >= direction_threshold:
        direction = "long"
    elif composite <= -direction_threshold:
        direction = "short"
    else:
        direction = "neutral"
        gate_reasons.append("below_production_threshold")

    if condition_adjuster is not None and direction in ("long", "short"):
        condition_factor, condition_reason = condition_adjuster(features, direction)
        condition_factor = max(0.0, min(1.0, condition_factor))
        if condition_factor < 1.0 and condition_reason:
            conviction = round(conviction * condition_factor)
            warnings.append(f"📉 学習調整: {condition_reason}")

    if expectancy_adjuster is not None and direction in ("long", "short"):
        expectancy_factor, expectancy_reason, expectancy_block = expectancy_adjuster(
            symbol, direction, conviction
        )
        expectancy_factor = max(0.0, min(1.10, expectancy_factor))
        if expectancy_factor != 1.0:
            conviction = min(100, round(conviction * expectancy_factor))
        if expectancy_reason:
            marker = "📈" if expectancy_factor > 1.0 and not expectancy_block else "📉"
            warnings.append(f"{marker} 期待値ガード: {expectancy_reason}")
        if expectancy_block:
            direction = "neutral"
            conviction = 0
            gate_reasons.append("expectancy_guard")

    view = tech.views.get(timeframe)
    close = view.close if view else None
    atr = view.atr if view else None
    rsi = view.rsi if view else None
    adx = view.adx if view else None
    entry_bid = view.bid if view else None
    entry_ask = view.ask if view else None
    quote_observed_at = now.isoformat() if entry_bid is not None and entry_ask is not None else None
    context_bid, context_ask, context_quote_at = decision_inputs.decision_quote_from_mapping(
        serialized_context
    )
    if context_bid is not None and context_ask is not None:
        entry_bid, entry_ask, quote_observed_at = context_bid, context_ask, context_quote_at

    stop = target1 = target2 = None
    target_policy: dict[str, object] = {}
    if direction in ("long", "short") and (atr is None or atr <= 0):
        gate_reasons.append("missing_atr")
        warnings.append(
            f"⚠️ ATR({timeframe})取得失敗 — SL/TPを算出できず、"
            "学習の小動き判定・期待値計算も無効"
        )
    if close is not None and atr is not None and atr > 0 and direction in ("long", "short"):
        risk_distance = atr * atr_multiple
        sign = 1.0 if direction == "long" else -1.0
        target1_r = 1.0
        target2_r = 2.0
        if target_r_adjuster is not None:
            adjusted_targets = target_r_adjuster(symbol, direction, conviction)
            if adjusted_targets is not None:
                candidate_target1_r, candidate_target2_r, reason = adjusted_targets[:3]
                if candidate_target1_r > 0 and candidate_target2_r > candidate_target1_r:
                    target1_r = candidate_target1_r
                    target2_r = candidate_target2_r
                    if len(adjusted_targets) >= 4 and isinstance(adjusted_targets[3], Mapping):
                        target_policy = dict(adjusted_targets[3])
                    else:
                        target_policy = {
                            "target1_r": target1_r,
                            "target2_r": target2_r,
                            "reason_ja": reason,
                        }
                    if reason:
                        warnings.append(f"✅ 承認済みTP/SL: {reason}")
        stop = close - sign * risk_distance
        target1 = close + sign * risk_distance * target1_r
        target2 = close + sign * risk_distance * target2_r

    dimensions = dict(learning_dimensions or {})
    shadow_drafts = [prediction_draft("timeframe_raw", composite)]
    macro_score = decision_inputs.macro_score_from_mapping(serialized_context)
    if macro_score is not None:
        shadow_drafts.append(
            prediction_draft(
                "macro",
                macro_score,
                stage="shadow",
                producer_version="macro-features-v1",
            )
        )
    shadow_predictions = build_shadow_predictions(
        shadow_drafts,
        close=close,
        atr=atr,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        quote_observed_at=quote_observed_at,
        cost_model_id=DEFAULT_COST_MODEL_ID,
        slippage_r=DEFAULT_SLIPPAGE_R,
        commission_r=DEFAULT_COMMISSION_R,
        atr_multiple=atr_multiple,
        production_threshold=direction_threshold,
        horizon_hours=horizon_hours,
        blocked_by=gate_reasons,
        market_open=market_open,
        learning_dimensions=dimensions,
    )
    gate_trace: list[dict[str, object]] = []
    for blocked_gate in gate_reasons:
        trace: dict[str, object] = {"gate": blocked_gate, "status": "blocked"}
        if blocked_gate == "event_window" and active_window is not None:
            trace.update(
                {
                    "event_currency": active_window.event.currency,
                    "event_title": active_window.event.title,
                    "event_impact": active_window.event.impact,
                    "event_time": active_window.event.when.isoformat(),
                    "blocked_until": active_window.end.isoformat(),
                }
            )
        gate_trace.append(trace)
    liquidity_trace = decision_inputs.liquidity_gate_trace_from_mapping(serialized_context)
    if liquidity_trace is not None:
        gate_trace.append(liquidity_trace)
    return TimeframePlan(
        symbol=symbol,
        timeframe=timeframe,
        horizon_hours=horizon_hours,
        direction=direction,
        conviction=conviction,
        tf_score=round(tf_score, 3),
        news_score=news_score,
        composite=composite,
        analysis_direction=analysis_direction,
        analysis_conviction=analysis_conviction,
        close=close,
        atr=atr,
        rsi=rsi,
        adx=adx,
        stop=stop,
        target1=target1,
        target2=target2,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        quote_observed_at=quote_observed_at,
        direction_threshold=direction_threshold,
        risk_pct=risk_pct,
        data_quality=quality,
        tech_weight=tech_weight,
        news_weight=news_weight,
        features=features,
        components=components,
        reason=tf_reason,
        warnings=warnings,
        target_policy=target_policy,
        auxiliary_horizons=AUXILIARY_HORIZON_HOURS.get(timeframe, ()),
        learning_dimensions=dimensions,
        gate_trace=gate_trace,
        shadow_predictions=shadow_predictions,
        input_context_id=str(serialized_context.get("context_id") or ""),
        input_features=input_features,
        input_feature_masks=input_masks,
        input_context=serialized_context,
    )


def build_timeframe_plans(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping[str, CurrencySentiment],
    windows: Sequence[RiskWindow],
    news_items: Sequence[NewsItem],
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    now: datetime | None = None,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    profile_lookup: Callable[[str, str], tuple[float, float, float, Callable | None]] | None = None,
    expectancy_lookup: ExpectancyLookup | None = None,
    target_r_adjuster: TargetRAdjuster | None = None,
    direction_threshold: float = DIRECTION_THRESHOLD,
    learning_dimensions: Mapping[str, object] | None = None,
    input_context: Mapping[str, object] | None = None,
) -> list[TimeframePlan]:
    """1ペアについて、各時間足の独立した判断をまとめて作る。

    profile_lookup は (symbol, timeframe) → (tech_weight, news_weight,
    conviction_factor, condition_adjuster) を返すフック。learning.py の
    時間足別プロファイルをここから注入する。None なら全時間足で既定値を使う
    (=学習前・後方互換の挙動)。

    expectancy_lookup は (symbol, timeframe) → 期待値ガードを返すフック。
    時間足ごとに主ホライズンが違うため、期待値サマリも時間足単位で分離して渡す。
    """
    now = now or datetime.now(UTC)
    plans: list[TimeframePlan] = []
    for timeframe in timeframes:
        tech_weight, news_weight, conviction_factor, adjuster = (
            TECH_WEIGHT,
            NEWS_WEIGHT,
            1.0,
            None,
        )
        if profile_lookup is not None:
            tech_weight, news_weight, conviction_factor, adjuster = profile_lookup(
                symbol, timeframe
            )
        expectancy_adjuster = (
            expectancy_lookup(symbol, timeframe) if expectancy_lookup is not None else None
        )
        plans.append(
            build_timeframe_plan(
                symbol,
                timeframe,
                tech,
                currency_scores,
                windows,
                news_items,
                now=now,
                atr_multiple=atr_multiple,
                risk_pct=risk_pct,
                calendar_ok=calendar_ok,
                operational_data_ok=operational_data_ok,
                operational_data_reason=operational_data_reason,
                tech_weight=tech_weight,
                news_weight=news_weight,
                conviction_factor=conviction_factor,
                condition_adjuster=adjuster,
                expectancy_adjuster=expectancy_adjuster,
                target_r_adjuster=target_r_adjuster,
                direction_threshold=direction_threshold,
                learning_dimensions=learning_dimensions,
                input_context=input_context,
            )
        )
    return plans
