"""テクニカル・ニュース・イベントリスクを融合したトレードプラン生成。

機関投資家デスクのモーニングブリーフィングを模した構成:

- 複合スコア = テクニカル(55%) + ニュースセンチメント(45%)
- 高影響イベントの警戒窓に入っている場合は方向に関係なく「様子見」
- ATRベースのストップ/ターゲットと推奨リスク(research-maxプリセット準拠)

判断の確実性を担保するデータ品質ゲート:

- データ品質 = テクニカル取得カバレッジ50% + 関連ニュース量30% + カレンダー可用20%
- 確信度はデータ品質で減衰し、品質が閾値未満なら方向判断そのものを見送る
- テクニカルとニュースが強く対立する場合は警告して確信度を減衰
- 経済指標カレンダーが取得不能(=イベントリスク未確認)なら確信度に上限
- FX市場の休場中(週末)はスキャナーが金曜クローズの価格を返し続けるため、
  stale価格での判断を防ぐべく方向判断を「休場」に固定する

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, UTC
from collections.abc import Callable, Mapping, Sequence
from math import isfinite

from .calendar import (
    EconomicEvent,
    RiskWindow,
    active_and_next_window,
    symbol_currencies,
)
from .market import is_market_open
from .news import NewsItem
from .sentiment import CurrencySentiment, MarketAnalysis, pair_bias
from .technicals import PairTechnicals

JST = timezone(timedelta(hours=9))

TECH_WEIGHT = 0.55
NEWS_WEIGHT = 0.45
MA_AGREEMENT_BONUS = 0.15
DIRECTION_THRESHOLD = 0.15
STANDBY_CONVICTION_CAP = 25

# データ品質ゲート
QUALITY_TECH_WEIGHT = 0.5
QUALITY_NEWS_WEIGHT = 0.3
QUALITY_CALENDAR_WEIGHT = 0.2
NEWS_FULL_COVERAGE_COUNT = 5  # 関連記事がこの件数あればニュース品質を満点とする
MIN_QUALITY_FOR_DIRECTION = 0.4  # これ未満なら方向判断を出さない
CONFLICT_THRESHOLD = 0.35  # テクニカル/ニュース対立とみなす両者の最小強度
CONFLICT_CONVICTION_FACTOR = 0.75
CALENDAR_UNKNOWN_CONVICTION_CAP = 40  # イベントリスク未確認時の確信度上限

# research_pack/research_max_config.json の risk_per_trade に合わせる
DEFAULT_RISK_PCT = 0.5
DEFAULT_ATR_MULTIPLE = 2.5
DEFAULT_TARGET1_R = 1.0
DEFAULT_TARGET2_R = 2.0
TargetRAdjustment = tuple[float, float, str] | tuple[float, float, str, Mapping[str, object]]
TargetRAdjuster = Callable[[str, str, int], TargetRAdjustment | None]

COLOR_LONG = 0x2ECC71
COLOR_SHORT = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6
COLOR_STANDBY = 0xF39C12

DIRECTION_JA = {
    "long": "ロング(買い)",
    "short": "ショート(売り)",
    "neutral": "見送り(取引しない)",
    "standby": "様子見(重要指標の前後)",
    "closed": "休場(週末クローズ)",
}

ACTION_JA = {
    "long": "ロング(買い)",
    "short": "ショート(売り)",
    "no_trade": "見送り(取引しない)",
}

# 初心者向けに「この判断が何を意味するか」を一文で言い換える
DIRECTION_HINT_JA = {
    "long": "値上がりを見込んで「買い」が優勢という判断です。",
    "short": "値下がりを見込んで「売り」が優勢という判断です。",
    "neutral": "買い・売りどちらの根拠も弱いため、今回は取引しないのが無難です。",
    "standby": "重要な経済指標の発表が近く、価格が急に動きやすいので新規の取引は控えます。",
    "closed": "FX市場は週末のためお休み中です。表示されている価格は金曜の最終値のままです。",
}

REGIME_HINT_JA = {
    "risk_on": "リスクオン(投資家が強気。株や高金利の通貨が買われやすい)",
    "risk_off": "リスクオフ(投資家が慎重。円・ドルなど安全とされる通貨が買われやすい)",
    "neutral": "中立(相場全体の方向感が出にくい)",
}

DIRECTION_EMOJI = {
    "long": "🟢",
    "short": "🔴",
    "neutral": "⚪",
    "standby": "🟠",
    "closed": "💤",
}


@dataclass(frozen=True)
class ScoreComponent:
    """複合スコアに参加する追加委員1人ぶんの意見(committee.pyが組み立てる)。

    tech/newsの2委員はbuild_trade_planの固有引数のまま残し(後方互換と
    学習ループのスキーマ安定のため)、マクロ・MLなど新しい委員はこの形で
    extra_componentsに渡す。weightはtech+news=1.0に対する相対値で、
    合成時に全体で正規化される。
    """

    key: str  # "macro" / "ml" など(ジャーナルの特徴量キーにも使う)
    label_ja: str
    score: float  # -1.0〜+1.0
    weight: float
    detail: str = ""  # 根拠の一言(注意点・内訳表示に使う)


@dataclass
class TradePlan:
    symbol: str
    direction: str  # long / short / neutral / standby
    conviction: int  # 0〜100
    composite: float
    tech_score: float
    news_score: float
    # ``direction`` is the analytical signal.  ``action`` is the final,
    # independently gated decision.  A plan is non-trading unless the shared
    # decision pipeline explicitly promotes it after every veto has passed.
    action: str = "no_trade"  # long / short / no_trade
    horizon_hours: float = 24.0
    close: float | None = None
    atr: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    risk_pct: float = DEFAULT_RISK_PCT
    data_quality: float = 1.0  # 0.0〜1.0。判断の根拠データがどれだけ揃っていたか
    tech_weight: float = TECH_WEIGHT  # 実際に使った複合重み(学習調整で変わりうる)
    news_weight: float = NEWS_WEIGHT
    # 判断時点のチャート状態(RSI/MA乖離/ボラ/時間足一致度/ニュース量など)。
    # ジャーナルに残し、learning.pyが「どんな状態で当たりやすいか」を学習する
    features: dict[str, float] = field(default_factory=dict)
    # 複合スコアの内訳(tech/news+追加委員)。表示とジャーナル記録用
    components: list[dict] = field(default_factory=list)
    # 委員会の見解メモ(shadow検証中の委員の意見も含む。判断根拠の可視化)
    committee_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    headlines: list[NewsItem] = field(default_factory=list)
    interval_summary: str = ""
    ma_note: str = ""
    target_policy: dict[str, object] = field(default_factory=dict)
    # 発注前9段チェックリスト(decision_pipeline.build_checklist の結果)。
    # {"steps": [...], "net_expected_r": ..., "position_units": ...} の辞書。
    # 空なら未算出。表示・ジャーナル記録に使う(判断そのものには影響しない)。
    checklist: dict[str, object] = field(default_factory=dict)

    @property
    def direction_ja(self) -> str:
        return DIRECTION_JA.get(self.direction, self.direction)

    @property
    def emoji(self) -> str:
        return DIRECTION_EMOJI.get(self.direction, "⚪")

    @property
    def action_ja(self) -> str:
        return ACTION_JA.get(self.action, "見送り(取引しない)")

    @property
    def action_emoji(self) -> str:
        return DIRECTION_EMOJI.get(self.action, "⚪")


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _price_digits(symbol: str) -> int:
    return 3 if symbol.upper().endswith("JPY") else 5


def format_price(symbol: str, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.{_price_digits(symbol)}f}"


def _tech_score(tech: PairTechnicals) -> tuple[float, str]:
    alignment = tech.alignment_score()
    ma_side = tech.ma_side()
    if ma_side == "long":
        ma_dir = 1.0
        ma_note = (
            f"移動平均線MA({tech.fast_window}/{tech.slow_window}): "
            "ゴールデンクロス(買いが優勢のサイン)"
        )
    elif ma_side == "short":
        ma_dir = -1.0
        ma_note = (
            f"移動平均線MA({tech.fast_window}/{tech.slow_window}): "
            "デッドクロス(売りが優勢のサイン)"
        )
    else:
        ma_dir, ma_note = 0.0, f"移動平均線MA({tech.fast_window}/{tech.slow_window}): 判定不能"
    return _clip(alignment + MA_AGREEMENT_BONUS * ma_dir), ma_note


def _extract_features(tech: PairTechnicals, news_count: int) -> dict[str, float]:
    """判断時点のチャート状態を学習用の特徴量にする(取得できたものだけ)。

    - rsi_1h / adx_1h: 1時間足のRSIとADX(トレンド強度)
    - ma_gap_atr: MA(fast/slow)の乖離をATR何個分かで正規化した値(符号=向き)
    - atr_pct: ATR(1h)÷終値の百分率。ボラティリティレジームの指標
    - tf_agreement: 時間足レーティングの向きの一致度(0.0〜1.0)
    - rating_4h / rating_1d: 上位足レーティング(-1.0〜+1.0)。
      学習側が「上位足逆行の判断は当たるか」を方向別に採点するのに使う
    - news_count: 関連ニュース件数
    """
    features: dict[str, float] = {"news_count": float(news_count)}
    for interval in ("4h", "1d"):
        higher = tech.usable_view(interval)
        if higher is not None:
            features[f"rating_{interval}"] = higher.score
    view = tech.usable_view("1h")
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


def _interval_summary(tech: PairTechnicals) -> str:
    parts = []
    for interval in ("15m", "1h", "4h", "1d"):
        view = tech.usable_view(interval)
        if view is not None:
            parts.append(f"{interval} {view.recommendation_ja}")
    return " | ".join(parts) if parts else "テクニカル取得失敗"


def _event_warnings(
    windows: Sequence[RiskWindow], now: datetime, lookahead_hours: float = 12.0
) -> tuple[bool, list[str]]:
    """アクティブな警戒窓の有無と警告文を返す。"""
    active, upcoming = active_and_next_window(windows, now)
    warnings: list[str] = []
    if active is not None:
        event = active.event
        warnings.append(
            f"⚠️ イベント警戒中: {event.currency}「{event.title}」"
            f"({event.when.astimezone(JST):%m/%d %H:%M} JST, 影響度{event.impact_ja})"
            f" — {active.end.astimezone(JST):%H:%M} JSTまでは値動きが荒れやすく、新規取引は控える時間帯"
        )
    if upcoming is not None and upcoming.start <= now + timedelta(hours=lookahead_hours):
        event = upcoming.event
        warnings.append(
            f"⏳ 次の重要イベント: {event.currency}「{event.title}」"
            f" {event.when.astimezone(JST):%m/%d %H:%M} JST (影響度{event.impact_ja})"
        )
    return active is not None, warnings


def build_trade_plan(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping[str, CurrencySentiment],
    windows: Sequence[RiskWindow],
    news_items: Sequence[NewsItem],
    now: datetime | None = None,
    horizon_hours: float = 24.0,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    tech_weight: float = TECH_WEIGHT,
    news_weight: float = NEWS_WEIGHT,
    conviction_factor: float = 1.0,
    condition_adjuster: Callable[[Mapping[str, float], str], tuple[float, str]] | None = None,
    expectancy_adjuster: Callable[[str, str, int], tuple[float, str, bool]] | None = None,
    target_r_adjuster: TargetRAdjuster | None = None,
    extra_components: Sequence[ScoreComponent] = (),
    extra_features: Mapping[str, float] | None = None,
    committee_notes: Sequence[str] = (),
) -> TradePlan:
    """1ペア分のトレードプランを組み立てる。

    calendar_ok=False は経済指標カレンダーが取得できず、イベントリスクを
    確認できていない状態。警戒窓判定が機能しないため確信度に上限を掛ける。

    operational_data_ok=False は独立鮮度モニターの欠落・stale・warning・critical
    を表し、方向スコアに関係なく新規リスクを neutral へ落とす。

    now がFX市場の休場中(週末クローズ)の場合、テクニカルの価格は最終取引
    時点のstale値なので、方向判断を出さず direction="closed" に固定する。

    tech_weight/news_weight/conviction_factor は学習プロファイル
    (fx_intel.learning)による自動調整の注入点。conviction_factor<1.0は
    「過去の的中率が低いペアなので確信度を減衰する」の意味で、
    方向判断そのものは変えず確信度だけを下げる。

    condition_adjuster は「現在のチャート状態(特徴量)×判断方向が過去に
    外しやすかった組み合わせか」を判定するフック。(特徴量, "long"/"short")を
    受けて(減衰係数, 理由文)を返し、係数<1.0なら確信度を減衰して理由を
    注意点に載せる。方向が決まった後(long/shortのときだけ)呼ばれ、
    方向判断そのものは変えない。

    expectancy_adjuster は「過去にこの判断と近いセルが実際のTP/SL期待値を
    持っているか」を判定するフック。(symbol, direction, conviction)を
    受けて(減衰係数, 理由文, 見送りに落とすか)を返す。十分なサンプルで
    期待Rが非正のセルだけ direction="neutral" に戻せる。

    extra_components はマクロ・MLなど追加委員の意見(committee.py参照)。
    複合スコアは全委員の重み付き平均になり、重みは全体で正規化するため
    追加委員が無ければ従来のtech/news合成と完全に一致する。
    extra_features は特徴量への追記(委員のスコアをジャーナルに残し、
    shadow段階の委員でも成績を後から採点できるようにする)。
    """
    now = now or datetime.now(UTC)
    if (
        not isinstance(horizon_hours, (int, float))
        or isinstance(horizon_hours, bool)
        or not isfinite(horizon_hours)
        or horizon_hours <= 0
    ):
        raise ValueError("horizon_hours must be a finite positive number")
    base, quote = symbol_currencies(symbol)

    tech_score, ma_note = _tech_score(tech)
    news_score = pair_bias(base, quote, currency_scores)
    weighted_sum = tech_weight * tech_score + news_weight * news_score
    total_weight = tech_weight + news_weight
    for component in extra_components:
        weighted_sum += component.weight * _clip(component.score)
        total_weight += component.weight
    composite = round(weighted_sum / total_weight, 3) if total_weight > 0 else 0.0

    components_record = [
        {
            "key": "tech",
            "label_ja": "テクニカル",
            "score": round(tech_score, 3),
            "weight": round(tech_weight / total_weight, 3),
        },
        {
            "key": "news",
            "label_ja": "ニュース",
            "score": news_score,
            "weight": round(news_weight / total_weight, 3),
        },
    ] + [
        {
            "key": component.key,
            "label_ja": component.label_ja,
            "score": round(_clip(component.score), 3),
            "weight": round(component.weight / total_weight, 3),
            "detail": component.detail,
        }
        for component in extra_components
    ]

    # 両通貨に言及する記事(ペア固有ニュース)を優先し、片方のみは補完扱い
    def _relevance(item: NewsItem) -> int:
        return (base in item.currencies) + (quote in item.currencies)

    relevant_items = [item for item in news_items if _relevance(item) > 0]
    related = sorted(
        relevant_items,
        key=lambda item: (-_relevance(item), -item.published.timestamp()),
    )[:3]

    # 判断時点のチャート状態を記録(learning.pyの状態別学習の入力)。
    # 追加委員のスコア(macro_score等)もここに合流させてジャーナルに残す
    features = _extract_features(tech, len(relevant_items))
    if extra_features:
        for key, value in extra_features.items():
            if isinstance(value, (int, float)):
                features[str(key)] = round(float(value), 4)

    # データ品質: 根拠データの揃い具合。確信度の減衰と方向判断の見送りに使う
    tech_cov = tech.coverage()
    technical_quality_issues = tech.critical_quality_issues()
    news_cov = min(1.0, len(relevant_items) / NEWS_FULL_COVERAGE_COUNT)
    quality = round(
        QUALITY_TECH_WEIGHT * tech_cov
        + QUALITY_NEWS_WEIGHT * news_cov
        + QUALITY_CALENDAR_WEIGHT * (1.0 if calendar_ok else 0.0),
        3,
    )
    conviction = min(100, round(abs(composite) * 100 * quality))

    in_event_window, warnings = _event_warnings(windows, now)

    # 学習調整: 直近の的中率が低いペアは確信度を減衰(方向判断は変えない)
    conviction_factor = max(0.0, min(1.0, conviction_factor))
    if conviction_factor < 1.0:
        conviction = round(conviction * conviction_factor)
        warnings.append(
            f"📉 学習調整: このペアは過去の方向判断の的中率が低めのため"
            f"確信度を×{conviction_factor:.2f}に減衰"
        )

    if tech_cov <= 0:
        warnings.append("⚠️ テクニカル全時間足の取得に失敗 — テクニカル根拠なし")
    elif tech_cov < 1.0:
        missing = ", ".join(tech.missing_intervals())
        warnings.append(f"テクニカル欠損: {missing} 未取得(取得率{tech_cov:.0%})")
    if technical_quality_issues:
        detail = "; ".join(
            f"{interval}={','.join(issues)}"
            for interval, issues in sorted(technical_quality_issues.items())
        )
        warnings.append(f"⛔ テクニカル品質違反: {detail}")
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
        tech_score * news_score < 0 and min(abs(tech_score), abs(news_score)) >= CONFLICT_THRESHOLD
    )
    if conflict:
        warnings.append(
            f"テクニカル({tech_score:+.2f})とニュース({news_score:+.2f})が反対方向を示しており、"
            "方向感が定まらないため確信度を下げて評価"
        )
        conviction = round(conviction * CONFLICT_CONVICTION_FACTOR)

    if not operational_data_ok or technical_quality_issues:
        direction = "neutral"
        conviction = 0
    elif not is_market_open(now):
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

    # 学習調整: いまのチャート状態×方向が過去に外しやすかった状態なら確信度を減衰
    if condition_adjuster is not None and direction in ("long", "short"):
        condition_factor, condition_reason = condition_adjuster(features, direction)
        condition_factor = max(0.0, min(1.0, condition_factor))
        if condition_factor < 1.0 and condition_reason:
            conviction = round(conviction * condition_factor)
            warnings.append(f"📉 学習調整: {condition_reason}")

    # 期待値ガード: 方向的中ではなく、過去のTP/SL込み期待Rで新規判断を制御する
    if expectancy_adjuster is not None and direction in ("long", "short"):
        expectancy_factor, expectancy_reason, expectancy_block = expectancy_adjuster(
            symbol, direction, conviction
        )
        expectancy_factor = max(0.0, min(1.0, expectancy_factor))
        if expectancy_factor < 1.0:
            conviction = round(conviction * expectancy_factor)
        if expectancy_reason:
            warnings.append(f"📉 期待値ガード: {expectancy_reason}")
        if expectancy_block:
            direction = "neutral"
            conviction = 0

    close = tech.close()
    atr = tech.atr()
    stop = target1 = target2 = None
    target_policy: dict[str, object] = {}
    if direction in ("long", "short") and (atr is None or atr <= 0):
        # ATRが無い方向判断は、SL/TPを提示できないだけでなく学習側の
        # 小動き除外・ATR換算期待値も無効になる(ジャーナルに残る欠陥データ)
        warnings.append(
            "⚠️ ATR(1h)取得失敗 — SL/TPを算出できず、学習の小動き判定・期待値計算も無効"
        )
    if close is not None and atr is not None and atr > 0 and direction in ("long", "short"):
        risk_distance = atr * atr_multiple
        sign = 1.0 if direction == "long" else -1.0
        target1_r = DEFAULT_TARGET1_R
        target2_r = DEFAULT_TARGET2_R
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

    return TradePlan(
        symbol=symbol,
        direction=direction,
        conviction=conviction,
        composite=composite,
        tech_score=round(tech_score, 3),
        news_score=news_score,
        horizon_hours=float(horizon_hours),
        close=close,
        atr=atr,
        stop=stop,
        target1=target1,
        target2=target2,
        risk_pct=risk_pct,
        data_quality=quality,
        tech_weight=tech_weight,
        news_weight=news_weight,
        features=features,
        components=components_record,
        committee_notes=list(committee_notes),
        warnings=warnings,
        headlines=related,
        interval_summary=_interval_summary(tech),
        ma_note=ma_note,
        target_policy=target_policy,
    )


def _sentiment_lines(analysis: MarketAnalysis, currencies: Sequence[str]) -> str:
    lines = []
    for ccy in currencies:
        sentiment = analysis.currencies.get(ccy)
        if sentiment is None:
            continue
        bar = f"{sentiment.score:+.2f}"
        line = f"**{ccy}** {bar} {sentiment.label_ja}"
        extras = []
        if sentiment.headline_count:
            extras.append(f"記事{sentiment.headline_count}件")
        if sentiment.confidence is not None:
            extras.append(f"確信{sentiment.confidence:.0%}")
        if extras:
            line += f" ({', '.join(extras)})"
        if sentiment.comment:
            line += f" — {sentiment.comment}"
        elif sentiment.themes:
            line += f" — {'、'.join(sentiment.themes)}"
        lines.append(line)
    return "\n".join(lines) if lines else "データなし"


def _events_lines(events: Sequence[EconomicEvent], limit: int = 8) -> str:
    if not events:
        return "48時間以内に重要イベントなし"
    lines = []
    for event in events[:limit]:
        line = (
            f"{event.when.astimezone(JST):%m/%d %H:%M} JST "
            f"[{event.currency}/{event.impact_ja}] {event.title}"
        )
        extras = []
        if event.forecast:
            extras.append(f"予想 {event.forecast}")
        if event.previous:
            extras.append(f"前回 {event.previous}")
        if extras:
            line += f" ({' / '.join(extras)})"
        lines.append(line)
    if len(events) > limit:
        lines.append(f"…ほか{len(events) - limit}件")
    lines.append("※発表の前後は価格が急に動きやすいので新規の取引は控えるのが安全")
    return "\n".join(lines)


_CHECK_EMOJI = {"ok": "✅", "warn": "⚠️", "block": "⛔", "skip": "➖"}


def _checklist_field(plan: TradePlan) -> dict | None:
    """発注前9段チェックリストをDiscordの1フィールドに整形。

    plan.checklist が空(未算出)なら None を返し、フィールドを足さない。
    各ステップは「絵文字 番号. 項目 — 理由」の1行。純期待R/ポジションサイズは
    末尾にまとめて添える。
    """
    checklist = plan.checklist or {}
    raw_steps = checklist.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str | bytes):
        return None
    steps = [step for step in raw_steps if isinstance(step, Mapping)]
    if not steps:
        return None
    lines = []
    for step in steps:
        emoji = _CHECK_EMOJI.get(str(step.get("status")), "•")
        head = f"{emoji} {step.get('order')}. {step.get('label_ja')}"
        note = step.get("note")
        lines.append(f"{head} — {note}" if note else head)
    footer_bits = []
    net_r = checklist.get("net_expected_r")
    if isinstance(net_r, (int, float)):
        footer_bits.append(f"執行コスト控除後の純期待 **{net_r:+.2f}R**")
    units = checklist.get("position_units")
    if isinstance(units, (int, float)):
        footer_bits.append(f"想定サイズ {units:,.0f}通貨単位")
    if footer_bits:
        lines.append("— " + " / ".join(footer_bits))
    return {
        "name": "🧭 発注前チェックリスト(9段)",
        "value": "\n".join(lines)[:1024],
        "inline": False,
    }


def _plan_embed(plan: TradePlan, fast: int, slow: int) -> dict:
    color = {
        "long": COLOR_LONG,
        "short": COLOR_SHORT,
        "standby": COLOR_STANDBY,
    }.get(plan.action, COLOR_NEUTRAL)

    hint = DIRECTION_HINT_JA.get(plan.direction, "")
    if plan.components:
        parts = " + ".join(
            f"{c['label_ja']} {c['score']:+.2f}({c['weight']:.0%})" for c in plan.components
        )
    else:
        parts = (
            f"テクニカル {plan.tech_score:+.2f}({plan.tech_weight:.0%})"
            f" + ニュース {plan.news_score:+.2f}({plan.news_weight:.0%})"
        )
    breakdown = (
        f"根拠の内訳: {parts}"
        f" = 複合 **{plan.composite:+.2f}**"
        f" | データ品質 {plan.data_quality:.0%}"
    )
    fields = [
        {
            "name": "最終判断",
            "value": (
                f"{plan.action_emoji} **{plan.action_ja}**\n"
                f"分析方向: {plan.emoji} {plan.direction_ja} — "
                f"シグナル強度 {plan.conviction}/100\n"
                f"{hint}\n"
                f"{breakdown}\n{plan.ma_note}"
            ),
            "inline": False,
        },
        {
            "name": "時間足ごとの見立て",
            "value": (
                f"{plan.interval_summary}\n"
                "※15m=15分足 / 1h=1時間足 / 4h=4時間足 / 1d=日足。"
                "短いほど直近の動きを反映"
            ),
            "inline": False,
        },
    ]
    if plan.action in ("long", "short") and plan.stop is not None:
        fields.append(
            {
                "name": "売買プラン(価格の目安)",
                "value": (
                    f"いまの価格: {format_price(plan.symbol, plan.close)}\n"
                    f"損切り(SL): {format_price(plan.symbol, plan.stop)}"
                    " ← 予想と逆に動いたらここで撤退\n"
                    f"利確の目標: 第1目標(T1) {format_price(plan.symbol, plan.target1)}"
                    f" / 第2目標(T2) {format_price(plan.symbol, plan.target2)}\n"
                    f"1回の取引で許容する損失: 資金の{plan.risk_pct:.2g}%まで\n"
                    f"※直近の平均的な値動き幅 ATR(1h) {format_price(plan.symbol, plan.atr)}"
                    " をもとに算出"
                ),
                "inline": False,
            }
        )
    if plan.committee_notes:
        fields.append(
            {
                "name": "🧩 委員会の見解(役割別AI)",
                "value": "\n".join(plan.committee_notes)[:1024],
                "inline": False,
            }
        )
    checklist_field = _checklist_field(plan)
    if checklist_field is not None:
        fields.append(checklist_field)
    if plan.warnings:
        fields.append({"name": "⚠️ 注意点", "value": "\n".join(plan.warnings), "inline": False})
    if plan.headlines:
        fields.append(
            {
                "name": "関連ニュース",
                "value": "\n".join(
                    f"・[{item.source}] {item.title[:90]}" for item in plan.headlines
                ),
                "inline": False,
            }
        )
    return {
        "title": f"{plan.symbol} — 最終判断 {plan.action_ja}",
        "color": color,
        "fields": fields,
    }


def build_discord_payload(
    plans: Sequence[TradePlan],
    analysis: MarketAnalysis,
    events_48h: Sequence[EconomicEvent],
    currencies: Sequence[str],
    fast_window: int,
    slow_window: int,
    fetch_warnings: Sequence[str] = (),
    journal_note: str = "",
    learning_note: str = "",
    promotion_note: str = "",
    now: datetime | None = None,
) -> dict:
    """Discord Webhook用のペイロードを組み立てる。"""
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()

    headline_parts = [
        f"{plan.action_emoji} {plan.symbol} {plan.action_ja}"
        f"(分析:{plan.direction_ja} {plan.conviction})"
        for plan in plans
    ]
    engine_ja = {
        "claude": "Claude分析",
        "analyst": "自前分析エンジン",
        "lexicon": "語彙分析",
    }.get(analysis.engine, analysis.engine)
    regime_ja = REGIME_HINT_JA.get(analysis.regime, analysis.regime_ja)
    content = (
        f"📊 **FXデスクブリーフィング** {now.astimezone(JST):%m/%d %H:%M} JST ({engine_ja})\n"
        f"相場のムード: {regime_ja}\n" + " / ".join(headline_parts)
    )

    sentiment_value = _sentiment_lines(analysis, currencies)
    if sentiment_value != "データなし":
        sentiment_value += "\n※ニュースから測った通貨の強さ。+1.0に近いほど買われやすい"
    macro_fields = [
        {
            "name": "通貨センチメント",
            "value": sentiment_value,
            "inline": False,
        },
        {
            "name": "今後48時間の重要イベント",
            "value": _events_lines(list(events_48h)),
            "inline": False,
        },
    ]
    if analysis.summary:
        macro_fields.insert(0, {"name": "市況要約", "value": analysis.summary, "inline": False})
    if journal_note:
        macro_fields.append(
            {"name": "判断の検証(自己採点)", "value": journal_note, "inline": False}
        )
    if learning_note:
        macro_fields.append(
            {
                "name": "🧠 学習メモ(過去の判断からの自動調整)",
                "value": learning_note[:1024],  # Discordのフィールド上限
                "inline": False,
            }
        )
    if promotion_note:
        macro_fields.append(
            {
                "name": "🎖️ 委員の運用段階(shadow固定)",
                "value": promotion_note[:1024],
                "inline": False,
            }
        )
    if fetch_warnings:
        macro_fields.append(
            {
                "name": "データ取得の注意",
                "value": "\n".join(f"・{w}" for w in list(fetch_warnings)[:6]),
                "inline": False,
            }
        )
    macro_fields.append(
        {
            "name": "📘 用語ミニ解説(FX初心者向け)",
            "value": (
                "・**ロング/ショート**: 値上がりを狙う「買い」/値下がりを狙う「売り」\n"
                "・**確信度**: 分析根拠のそろい具合(0〜100)。低いときは見送りが無難\n"
                "・**損切り(SL)**: 予想が外れたとき、損失を小さく確定して撤退する価格\n"
                "・**利確(T1/T2)**: 利益を確定する目安の価格。T2の方が遠い目標\n"
                "・**ATR**: 直近の平均的な値動きの幅。損切り・利確までの距離の物差し"
            ),
            "inline": False,
        }
    )

    embeds = [
        {
            "title": "マクロ・センチメント概況",
            "color": COLOR_NEUTRAL,
            "fields": macro_fields,
            "footer": {"text": f"fx-codex fx_briefing | MA({fast_window}/{slow_window}) | OANDA"},
            "timestamp": now_iso,
        }
    ]
    embeds.extend(_plan_embed(plan, fast_window, slow_window) for plan in plans)
    return {
        "username": "fx-codex デスクブリーフィング",
        "content": content,
        "embeds": embeds[:10],  # Discordの上限
    }
