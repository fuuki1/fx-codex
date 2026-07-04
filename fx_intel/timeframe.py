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
- 学習プロファイル(conviction_factor / condition_adjuster)の注入点も同じ

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
from .calendar import RiskWindow, symbol_currencies
from .market import is_market_open
from .news import NewsItem
from .sentiment import CurrencySentiment, pair_bias
from .technicals import PairTechnicals

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
    close: float | None = None
    atr: float | None = None
    rsi: float | None = None
    adx: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
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
    # 補助ホライズン(観測専用)。{ホライズン時間: ラベル} の順序付き情報
    auxiliary_horizons: tuple[float, ...] = ()

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
    tech_weight: float = TECH_WEIGHT,
    news_weight: float = NEWS_WEIGHT,
    conviction_factor: float = 1.0,
    condition_adjuster: Callable[[Mapping[str, float], str], tuple[float, str]] | None = None,
) -> TimeframePlan:
    """1ペア・1時間足ぶんの独立した方向判断を組み立てる。

    ゲート(データ品質・イベント警戒窓・週末クローズ・テク/ニュース対立)と
    学習調整(conviction_factor / condition_adjuster)は briefing.build_trade_plan と
    同一の設計。違いは「複合スコアのテクニカル側がその時間足単体のスコアである」
    ことと、判断に主ホライズンが紐づくこと。
    """
    now = now or datetime.now(UTC)
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

    conflict = (
        tf_score * news_score < 0 and min(abs(tf_score), abs(news_score)) >= CONFLICT_THRESHOLD
    )
    if conflict:
        warnings.append(
            f"テクニカル({tf_score:+.2f})とニュース({news_score:+.2f})が反対方向を示しており、"
            "方向感が定まらないため確信度を下げて評価"
        )
        conviction = round(conviction * CONFLICT_CONVICTION_FACTOR)

    if not is_market_open(now):
        direction = "closed"
        conviction = 0
        warnings.append(
            "💤 FX市場休場中(週末クローズ) — 表示価格は最終取引時点のもの。"
            "方向判断は市場再開後に実施"
        )
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

    if condition_adjuster is not None and direction in ("long", "short"):
        condition_factor, condition_reason = condition_adjuster(features, direction)
        condition_factor = max(0.0, min(1.0, condition_factor))
        if condition_factor < 1.0 and condition_reason:
            conviction = round(conviction * condition_factor)
            warnings.append(f"📉 学習調整: {condition_reason}")

    view = tech.views.get(timeframe)
    close = view.close if view else None
    atr = view.atr if view else None
    rsi = view.rsi if view else None
    adx = view.adx if view else None

    stop = target1 = target2 = None
    if direction in ("long", "short") and (atr is None or atr <= 0):
        warnings.append(
            f"⚠️ ATR({timeframe})取得失敗 — SL/TPを算出できず、"
            "学習の小動き判定・期待値計算も無効"
        )
    if close is not None and atr is not None and atr > 0 and direction in ("long", "short"):
        risk_distance = atr * atr_multiple
        sign = 1.0 if direction == "long" else -1.0
        stop = close - sign * risk_distance
        target1 = close + sign * risk_distance
        target2 = close + sign * risk_distance * 2

    return TimeframePlan(
        symbol=symbol,
        timeframe=timeframe,
        horizon_hours=horizon_hours,
        direction=direction,
        conviction=conviction,
        tf_score=round(tf_score, 3),
        news_score=news_score,
        composite=composite,
        close=close,
        atr=atr,
        rsi=rsi,
        adx=adx,
        stop=stop,
        target1=target1,
        target2=target2,
        risk_pct=risk_pct,
        data_quality=quality,
        tech_weight=tech_weight,
        news_weight=news_weight,
        features=features,
        components=components,
        reason=tf_reason,
        warnings=warnings,
        auxiliary_horizons=AUXILIARY_HORIZON_HOURS.get(timeframe, ()),
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
    profile_lookup: Callable[[str, str], tuple[float, float, float, Callable | None]] | None = None,
) -> list[TimeframePlan]:
    """1ペアについて、各時間足の独立した判断をまとめて作る。

    profile_lookup は (symbol, timeframe) → (tech_weight, news_weight,
    conviction_factor, condition_adjuster) を返すフック。learning.py の
    時間足別プロファイルをここから注入する。None なら全時間足で既定値を使う
    (=学習前・後方互換の挙動)。
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
                tech_weight=tech_weight,
                news_weight=news_weight,
                conviction_factor=conviction_factor,
                condition_adjuster=adjuster,
            )
        )
    return plans
