"""複数AI委員会 — 役割の異なる分析エンジンの意見を統合してトレードプランを作る。

機関投資家デスクの投資委員会を模した役割分担:

- テクニカルアナリスト: TradingViewマルチタイムフレーム+MAクロス(既存)
- ニュースアナリスト:   自前分析エンジン/Claude APIのセンチメント(既存)
- マクロアナリスト:     COTポジショニングとリスクレジーム(macro.py)
- MLアナリスト:         GBDT確率モデルのロング/ショート優位差(ml.py)
- リスクオフィサー:     briefing.build_trade_plan内の決定論ゲート
                        (休場・イベント警戒窓・データ品質・確信度上限)。
                        委員の総意に対して常に拒否権を持つ

新任委員(マクロ・ML)は promotion.py の昇格ゲートに従う:

- shadow: 意見は計算・記録・表示されるが、複合スコアには参加しない。
          ジャーナルの特徴量(macro_score / ml_edge)として蓄積され、
          後から成績を採点できる
- paper:  複合スコアに参加する(Discord助言に影響する)
- live:   実売買への接続を許可する段階(明示承認でのみ到達)

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。データ取得は呼び出し側(fx_briefing.py)が行い、
取得済みのスナップショット・学習済みモデルを注入する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Callable, Mapping, Sequence

from .briefing import (
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_RISK_PCT,
    NEWS_WEIGHT,
    TECH_WEIGHT,
    ScoreComponent,
    TradePlan,
    build_trade_plan,
    _data_quality,
    _extract_features,
)
from .calendar import RiskWindow, symbol_currencies
from .macro import MacroSnapshot, macro_pair_view
from .ml import MLArtifact
from .news import NewsItem
from .sentiment import CurrencySentiment
from .technicals import PairTechnicals

# 追加委員の生重み(tech+news=1.0に対する相対値。合成時に全体正規化)
MACRO_WEIGHT = 0.15
ML_WEIGHT = 0.20

# MLの優位差(p_long − p_short)がこの値未満なら「意見なし」扱い
ML_MIN_EDGE = 0.05

STAGE_ACTIVE = ("paper", "live")  # 複合スコアに参加できる段階
STAGE_LABEL_JA = {"shadow": "shadow検証中", "paper": "paper参加中", "live": "live"}


@dataclass(frozen=True)
class Opinion:
    """委員1人の意見。activeがFalse(shadow等)は合成に参加しない。"""

    role: str  # "macro" / "ml"
    label_ja: str
    score: float  # -1.0〜+1.0
    weight: float  # 合成時の生重み(activeな場合のみ使用)
    stage: str  # shadow / paper / live
    active: bool
    rationale_ja: list[str] = field(default_factory=list)

    def note_ja(self) -> str:
        direction = "買い" if self.score > 0 else ("売り" if self.score < 0 else "中立")
        stage_ja = STAGE_LABEL_JA.get(self.stage, self.stage)
        head = f"{self.label_ja}: {direction} {self.score:+.2f} [{stage_ja}]"
        if not self.active:
            head += "(複合スコアには不参加)"
        if self.rationale_ja:
            head += " — " + " / ".join(self.rationale_ja)
        return head


def macro_opinion(
    symbol: str, snapshot: MacroSnapshot | None, stage: str = "shadow"
) -> Opinion | None:
    """マクロアナリストの意見(COT+レジーム)。データが無ければNone。"""
    if snapshot is None:
        return None
    base, quote = symbol_currencies(symbol)
    score, confidence, notes = macro_pair_view(base, quote, snapshot)
    if confidence <= 0:
        return None
    return Opinion(
        role="macro",
        label_ja="マクロ委員(COT・レジーム)",
        score=score,
        # データが揃っていない(confidence<1)ぶん発言力を下げる
        weight=round(MACRO_WEIGHT * confidence, 4),
        stage=stage,
        active=stage in STAGE_ACTIVE,
        rationale_ja=notes,
    )


def ml_opinion(
    artifact: MLArtifact | None,
    tech_score: float,
    news_score: float,
    chart_features: Mapping[str, float],
    data_quality: float | None = None,
    stage: str = "shadow",
) -> Opinion | None:
    """MLアナリストの意見。P(hit|long)とP(hit|short)の差を方向スコアにする。

    モデルが無い・スキルゲート不合格(usable=False)・優位差が閾値未満なら
    意見を出さない(「わからない」を沈黙で表明する)。
    """
    if artifact is None:
        return None
    edge = artifact.direction_edge(tech_score, news_score, chart_features, data_quality)
    if edge is None:
        return None
    p_long, p_short = edge
    score = round(p_long - p_short, 3)
    if abs(score) < ML_MIN_EDGE:
        return None
    rationale_ja = [
        f"的中確率 ロング{p_long:.0%} vs ショート{p_short:.0%}",
    ]
    if artifact.val_brier is not None and artifact.baseline_brier is not None:
        rationale_ja.append(
            f"検証Brier {artifact.val_brier:.3f}(基準率 {artifact.baseline_brier:.3f})"
        )
    return Opinion(
        role="ml",
        label_ja="ML委員(GBDT確率モデル)",
        score=score,
        weight=ML_WEIGHT,
        stage=stage,
        active=stage in STAGE_ACTIVE,
        rationale_ja=rationale_ja,
    )


def deliberate(
    symbol: str,
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
    expectancy_adjuster: Callable[[str, str], tuple[float, str]] | None = None,
    macro_snapshot: MacroSnapshot | None = None,
    ml_artifact: MLArtifact | None = None,
    stages: Mapping[str, str] | None = None,
) -> TradePlan:
    """1ペアぶんの委員会審議。build_trade_planの上位互換ラッパー。

    - 追加委員(マクロ・ML)の意見を集め、昇格段階に応じて合成に参加させる
    - shadow委員の意見もextra_featuresとしてジャーナルに記録し、
      promotion.pyが後から成績を採点できるようにする
    - 最終的なゲート適用(休場・イベント窓・品質・学習調整)は
      build_trade_plan(リスクオフィサー)がすべて握る
    """
    stages = stages or {}
    base, quote = symbol_currencies(symbol)

    # tech/newsスコアはbuild_trade_planが再計算するが、ML委員の入力にも
    # 必要なのでここでも同じ計算を行う(_tech_score/pair_biasは決定論的)
    from .briefing import _tech_score
    from .sentiment import pair_bias

    tech_score, _ = _tech_score(tech)
    news_score = pair_bias(base, quote, currency_scores)

    def _relevance(item: NewsItem) -> int:
        return (base in item.currencies) + (quote in item.currencies)

    relevant_count = sum(1 for item in news_items if _relevance(item) > 0)
    chart_features = _extract_features(tech, relevant_count)
    # build_trade_plan と同一の式でデータ品質を先に確定させ、ML委員へ渡す。
    # ここで None を渡すと学習時(実値)と推論時(中央値補完)で特徴量がずれる
    data_quality = _data_quality(tech.coverage(), relevant_count, calendar_ok)

    opinions: list[Opinion] = []
    macro = macro_opinion(symbol, macro_snapshot, stage=stages.get("macro", "shadow"))
    if macro is not None:
        opinions.append(macro)
    ml = ml_opinion(
        ml_artifact,
        tech_score,
        news_score,
        chart_features,
        data_quality=data_quality,  # 学習時(build_dataset)と同じ実値を渡す
        stage=stages.get("ml", "shadow"),
    )
    if ml is not None:
        opinions.append(ml)

    extra_components = [
        ScoreComponent(
            key=opinion.role,
            label_ja={"macro": "マクロ", "ml": "ML"}.get(opinion.role, opinion.role),
            score=opinion.score,
            weight=opinion.weight,
            detail=" / ".join(note for note in opinion.rationale_ja if note),
        )
        for opinion in opinions
        if opinion.active
    ]
    # shadow委員も含め、全委員のスコアを特徴量としてジャーナルに残す
    extra_features: dict[str, float] = {}
    for opinion in opinions:
        key = {"macro": "macro_score", "ml": "ml_edge"}.get(opinion.role, opinion.role)
        extra_features[key] = opinion.score

    committee_notes = [opinion.note_ja() for opinion in opinions]

    return build_trade_plan(
        symbol,
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
        condition_adjuster=condition_adjuster,
        expectancy_adjuster=expectancy_adjuster,
        extra_components=extra_components,
        extra_features=extra_features,
        committee_notes=committee_notes,
    )
