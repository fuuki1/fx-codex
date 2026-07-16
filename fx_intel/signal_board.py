"""5分ごとの単一Discord通知「FXシグナルボード」を生成する。

時間足別判断から通貨ペアごとの代表候補を1件だけ選び、エントリー適性と
データ品質を1通のプレーンテキストへ集約する。Discordの通知を
複数embedへ分散させず、スマートフォンでも現在の判断を一読できる形にする。
このシステムは自動売買を行わず分析→通知に専念するため、発注経路の
死活監視は表示しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, UTC

from .briefing import JST, format_price
from .calendar import symbol_currencies
from .macro import FRED_SERIES, MacroSnapshot
from .sentiment import MarketAnalysis
from .technicals import PairTechnicals
from .timeframe import TimeframePlan

DISCORD_CONTENT_LIMIT = 2000
TIMEFRAME_ORDER = ("15m", "1h", "4h", "1d")


@dataclass(frozen=True)
class DataQuality:
    """各入力層の短い品質表示。値には「正常」または注意理由が入る。"""

    technical: str
    news: str
    calendar: str
    macro: str

    @property
    def has_warning(self) -> bool:
        return any(value != "正常" for value in self.values())

    def values(self) -> tuple[str, str, str, str]:
        return self.technical, self.news, self.calendar, self.macro


@dataclass(frozen=True)
class EntryAssessment:
    setup: str
    suitability: str
    is_candidate: bool
    judgment: str


@dataclass(frozen=True)
class RankedCandidate:
    plan: TimeframePlan
    assessment: EntryAssessment


def assess_data_quality(
    plans_by_symbol: Mapping[str, Sequence[TimeframePlan]],
    *,
    news_warnings: Sequence[str] = (),
    calendar_ok: bool,
    macro_snapshot: MacroSnapshot | None,
    now: datetime | None = None,
) -> DataQuality:
    """ボード用にテクニカル・ニュース・指標・マクロの品質を要約する。"""

    now = now or datetime.now(UTC)
    missing_technical = [
        f"{symbol} {plan.timeframe}"
        for symbol, plans in plans_by_symbol.items()
        for plan in plans
        if plan.close is None
    ]
    technical = (
        "正常" if not missing_technical else f"注意｜{missing_technical[0]}の価格を取得できません"
    )
    news = "正常" if not news_warnings else "注意｜一部ニュースを取得できません"
    calendar = "正常" if calendar_ok else "注意｜経済指標カレンダーを取得できません"
    macro = _macro_quality(macro_snapshot, now)
    return DataQuality(
        technical=technical,
        news=news,
        calendar=calendar,
        macro=macro,
    )


def _macro_quality(snapshot: MacroSnapshot | None, now: datetime) -> str:
    if snapshot is None:
        return "注意｜マクロデータを取得していません"

    # 意思決定への影響が分かりやすいドル指数を最優先し、その後VIX・金利を確認。
    for key in ("usd_index", "vix", "us10y", "us2y"):
        series = snapshot.series.get(key)
        label = next(
            (label for series_key, label in FRED_SERIES.values() if series_key == key), key
        )
        if series is None or not series.points:
            return f"注意｜{label}を取得できません"
        if series.is_stale(now):
            observed = series.last()
            suffix = (
                f"の最終観測 {observed.when:%m/%d}" if observed is not None else "を取得できません"
            )
            return f"注意｜{label}{suffix}"

    stale_cot = [currency for currency, report in snapshot.cot.items() if report.is_stale(now)]
    if stale_cot:
        return f"注意｜COTの最終観測が古い通貨あり（{stale_cot[0]}）"
    if not snapshot.cot:
        return "注意｜COTデータを取得できません"
    return "正常"


def _entry_assessment(plan: TimeframePlan) -> EntryAssessment:
    rsi = plan.rsi
    overextended = (plan.direction == "long" and rsi is not None and rsi >= 70) or (
        plan.direction == "short" and rsi is not None and rsi <= 30
    )
    if overextended and plan.direction == "long":
        if plan.timeframe in ("4h", "1d"):
            return EntryAssessment(
                setup="押し目待ち",
                suitability="低い",
                is_candidate=False,
                judgment=(
                    "方向性は強いですが、現在値からの飛び乗りは避けます。\n"
                    "押し目形成または短期足の再加速を待ちます。"
                ),
            )
        return EntryAssessment(
            setup="短期調整待ち",
            suitability="低い",
            is_candidate=False,
            judgment="方向は買いですが、現在値からは追いかけません。",
        )
    if overextended:
        return EntryAssessment(
            setup="見送り",
            suitability="低い",
            is_candidate=False,
            judgment="安値追いショートは禁止。戻り形成まで待機します。",
        )

    missing_risk_prices = plan.close is None or plan.stop is None or plan.target1 is None
    if missing_risk_prices or plan.conviction < 45:
        direction_ja = "買い" if plan.direction == "long" else "売り"
        return EntryAssessment(
            setup="見送り",
            suitability="低い",
            is_candidate=False,
            judgment=f"{direction_ja}方向ですが、現時点では根拠または価格条件が不足しています。",
        )

    if plan.conviction >= 70 and (plan.adx is None or plan.adx >= 20):
        return EntryAssessment(
            setup="エントリー検討",
            suitability="高い",
            is_candidate=True,
            judgment="方向・勢い・価格条件がそろっています。損切り水準を守って監視します。",
        )

    return EntryAssessment(
        setup="条件待ち",
        suitability="中程度",
        is_candidate=False,
        judgment="方向は出ていますが、短期足の再加速を確認してから判断します。",
    )


def rank_candidates(
    plans_by_symbol: Mapping[str, Sequence[TimeframePlan]],
    *,
    limit: int = 3,
) -> list[RankedCandidate]:
    """方向判断がある足から通貨ペアごとの最強候補を選び、上位順に返す。"""

    representatives: list[TimeframePlan] = []
    tf_rank = {name: index for index, name in enumerate(TIMEFRAME_ORDER)}
    for plans in plans_by_symbol.values():
        directional = [plan for plan in plans if plan.direction in ("long", "short")]
        if not directional:
            continue
        representatives.append(
            max(
                directional,
                key=lambda plan: (
                    plan.conviction,
                    abs(plan.composite),
                    tf_rank.get(plan.timeframe, -1),
                ),
            )
        )
    representatives.sort(key=lambda plan: (plan.conviction, abs(plan.composite)), reverse=True)
    return [
        RankedCandidate(plan=plan, assessment=_entry_assessment(plan))
        for plan in representatives[:limit]
    ]


def _join_timeframes(values: Sequence[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]}と{values[1]}"
    return "、".join(values)


def _candidate_reasons(
    plan: TimeframePlan,
    analysis: MarketAnalysis,
    tech: PairTechnicals | None,
) -> list[str]:
    direction_word = "買い" if plan.direction == "long" else "売り"
    direction_sign = 1 if plan.direction == "long" else -1
    reasons: list[str] = []

    if tech is not None:
        aligned = [
            timeframe
            for timeframe in TIMEFRAME_ORDER
            if (view := tech.views.get(timeframe)) is not None and view.score * direction_sign > 0
        ]
        if len(aligned) >= 2:
            reasons.append(f"{_join_timeframes(aligned)}が{direction_word}方向で一致")

    if plan.adx is not None:
        if plan.adx >= 25:
            trend_word = "上昇" if plan.direction == "long" else "下降"
            reasons.append(f"ADX {plan.adx:.0f}で{trend_word}トレンドあり")
        elif plan.adx < 20:
            reasons.append(f"ADX {plan.adx:.0f}でトレンドの勢いは弱い")

    base, _quote = symbol_currencies(plan.symbol)
    sentiment = analysis.currencies.get(base)
    if sentiment is not None and abs(sentiment.score) >= 0.05:
        reasons.append(f"{base}センチメント {sentiment.score:+.2f}")

    if plan.rsi is not None:
        if plan.rsi >= 70:
            reasons.append(f"RSI {plan.rsi:.0f}で買われすぎ")
        elif plan.rsi <= 30:
            reasons.append(f"RSI {plan.rsi:.0f}で売られすぎ")

    if tech is not None and len(reasons) < 4:
        view = tech.views.get(plan.timeframe)
        if view is not None and view.score * direction_sign > 0:
            reasons.append(f"TradingView総合評価も{direction_word}")

    if not reasons:
        reasons.append(plan.reason or f"複合スコアが{direction_word}方向")
    return reasons[:4]


def _candidate_section(
    rank: int,
    candidate: RankedCandidate,
    analysis: MarketAnalysis,
    tech: PairTechnicals | None,
) -> str:
    plan = candidate.plan
    assessment = candidate.assessment
    icon = "🟢" if plan.direction == "long" else "🔴"
    direction_word = "買い" if plan.direction == "long" else "売り"
    lines = [
        f"{rank}位 {plan.symbol} {plan.timeframe}",
        f"{icon} {direction_word}方向｜{assessment.setup}",
        f"シグナル強度：{plan.conviction}/100",
        f"エントリー適性：{assessment.suitability}",
        "",
        "理由",
    ]
    lines.extend(f"・{reason}" for reason in _candidate_reasons(plan, analysis, tech))
    lines.extend(["", "判断", assessment.judgment])
    if plan.close is not None:
        lines.extend(
            [
                "",
                "参考価格",
                f"現在値：{format_price(plan.symbol, plan.close)}",
                f"無効化水準：{format_price(plan.symbol, plan.stop)}",
                f"目標1：{format_price(plan.symbol, plan.target1)}",
                f"目標2：{format_price(plan.symbol, plan.target2)}",
            ]
        )
    return "\n".join(lines)


def build_signal_board_payload(
    plans_by_symbol: Mapping[str, Sequence[TimeframePlan]],
    analysis: MarketAnalysis,
    tech_map: Mapping[str, PairTechnicals],
    data_quality: DataQuality,
    *,
    now: datetime | None = None,
) -> dict:
    """Discord Webhookへ1回だけ送るシグナルボードpayloadを組み立てる。"""

    now = now or datetime.now(UTC)
    ranked = rank_candidates(plans_by_symbol)
    entry_candidates = [
        f"{item.plan.symbol} {item.plan.timeframe}"
        for item in ranked
        if item.assessment.is_candidate
    ]
    candidate_text = "、".join(entry_candidates) if entry_candidates else "なし"
    quality_icon = "⚠️" if data_quality.has_warning else "✅"

    header = "\n".join(
        [
            f"📊 FXシグナルボード｜{now.astimezone(JST):%m/%d %H:%M} JST",
            "",
            f"{quality_icon} データ品質",
            f"テクニカル：{data_quality.technical}",
            f"ニュース：{data_quality.news}",
            f"経済指標：{data_quality.calendar}",
            f"マクロ：{data_quality.macro}",
            "",
            "━━━━━━━━━━━━━━━━━━",
            "",
            "【現在の判断】",
            "",
            f"新規エントリー候補：{candidate_text}",
        ]
    )
    sections = [
        _candidate_section(index, candidate, analysis, tech_map.get(candidate.plan.symbol))
        for index, candidate in enumerate(ranked, start=1)
    ]
    if not sections:
        sections = ["方向判断が成立した候補はありません。見送りを継続します。"]

    regime = {
        "risk_on": "リスクオン",
        "risk_off": "リスクオフ",
        "neutral": "中立",
    }.get(analysis.regime, analysis.regime_ja)
    footer = "\n".join(
        [
            f"市場環境：{regime}",
            "次回通知：5分後",
            "※シグナル強度は的中確率ではありません。",
        ]
    )
    content = f"{header}\n\n" + "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(sections)
    content += f"\n\n━━━━━━━━━━━━━━━━━━\n\n{footer}"
    if len(content) > DISCORD_CONTENT_LIMIT:
        # 通常は上限内だが、理由文が長い場合もWebhook自体を失敗させない。
        content = content[: DISCORD_CONTENT_LIMIT - 1].rstrip() + "…"
    return {
        "username": "FXシグナルボード",
        "content": content,
        "allowed_mentions": {"parse": []},
    }
