"""テクニカル・ニュース・イベントリスクを融合したトレードプラン生成。

機関投資家デスクのモーニングブリーフィングを模した構成:

- 複合スコア = テクニカル(55%) + ニュースセンチメント(45%)
- 高影響イベントの警戒窓に入っている場合は方向に関係なく「様子見」
- ATRベースのストップ/ターゲットと推奨リスク(research-maxプリセット準拠)

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .calendar import (
    EconomicEvent,
    RiskWindow,
    active_and_next_window,
    symbol_currencies,
)
from .news import NewsItem
from .sentiment import CurrencySentiment, MarketAnalysis, pair_bias
from .technicals import PairTechnicals

JST = timezone(timedelta(hours=9))

TECH_WEIGHT = 0.55
NEWS_WEIGHT = 0.45
MA_AGREEMENT_BONUS = 0.15
DIRECTION_THRESHOLD = 0.15
STANDBY_CONVICTION_CAP = 25

# research_pack/research_max_config.json の risk_per_trade に合わせる
DEFAULT_RISK_PCT = 0.5
DEFAULT_ATR_MULTIPLE = 2.5

COLOR_LONG = 0x2ECC71
COLOR_SHORT = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6
COLOR_STANDBY = 0xF39C12

DIRECTION_JA = {
    "long": "ロング",
    "short": "ショート",
    "neutral": "中立(見送り)",
    "standby": "様子見(イベント警戒)",
}

DIRECTION_EMOJI = {
    "long": "🟢",
    "short": "🔴",
    "neutral": "⚪",
    "standby": "🟠",
}


@dataclass
class TradePlan:
    symbol: str
    direction: str  # long / short / neutral / standby
    conviction: int  # 0〜100
    composite: float
    tech_score: float
    news_score: float
    close: float | None = None
    atr: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    risk_pct: float = DEFAULT_RISK_PCT
    warnings: list[str] = field(default_factory=list)
    headlines: list[NewsItem] = field(default_factory=list)
    interval_summary: str = ""
    ma_note: str = ""

    @property
    def direction_ja(self) -> str:
        return DIRECTION_JA.get(self.direction, self.direction)

    @property
    def emoji(self) -> str:
        return DIRECTION_EMOJI.get(self.direction, "⚪")


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
        ma_dir, ma_note = 1.0, f"MA({tech.fast_window}/{tech.slow_window}): ゴールデン(ロング目線)"
    elif ma_side == "short":
        ma_dir, ma_note = -1.0, f"MA({tech.fast_window}/{tech.slow_window}): デッド(ショート目線)"
    else:
        ma_dir, ma_note = 0.0, f"MA({tech.fast_window}/{tech.slow_window}): 判定不能"
    return _clip(alignment + MA_AGREEMENT_BONUS * ma_dir), ma_note


def _interval_summary(tech: PairTechnicals) -> str:
    parts = []
    for interval in ("15m", "1h", "4h", "1d"):
        view = tech.views.get(interval)
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
            f" — 窓終了 {active.end.astimezone(JST):%H:%M} JST"
        )
    if upcoming is not None and upcoming.start <= now + timedelta(hours=lookahead_hours):
        event = upcoming.event
        warnings.append(
            f"⏳ 次のイベント: {event.currency}「{event.title}」"
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
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> TradePlan:
    """1ペア分のトレードプランを組み立てる。"""
    now = now or datetime.now(timezone.utc)
    base, quote = symbol_currencies(symbol)

    tech_score, ma_note = _tech_score(tech)
    news_score = pair_bias(base, quote, currency_scores)
    composite = round(TECH_WEIGHT * tech_score + NEWS_WEIGHT * news_score, 3)
    conviction = min(100, round(abs(composite) * 100))

    in_event_window, warnings = _event_warnings(windows, now)
    if in_event_window:
        direction = "standby"
        conviction = min(conviction, STANDBY_CONVICTION_CAP)
    elif composite >= DIRECTION_THRESHOLD:
        direction = "long"
    elif composite <= -DIRECTION_THRESHOLD:
        direction = "short"
    else:
        direction = "neutral"

    close = tech.close()
    atr = tech.atr()
    stop = target1 = target2 = None
    if close is not None and atr is not None and atr > 0 and direction in ("long", "short"):
        risk_distance = atr * atr_multiple
        sign = 1.0 if direction == "long" else -1.0
        stop = close - sign * risk_distance
        target1 = close + sign * risk_distance
        target2 = close + sign * risk_distance * 2

    # 両通貨に言及する記事(ペア固有ニュース)を優先し、片方のみは補完扱い
    def _relevance(item: NewsItem) -> int:
        return (base in item.currencies) + (quote in item.currencies)

    related = sorted(
        (item for item in news_items if _relevance(item) > 0),
        key=lambda item: (-_relevance(item), -item.published.timestamp()),
    )[:3]

    return TradePlan(
        symbol=symbol,
        direction=direction,
        conviction=conviction,
        composite=composite,
        tech_score=round(tech_score, 3),
        news_score=news_score,
        close=close,
        atr=atr,
        stop=stop,
        target1=target1,
        target2=target2,
        risk_pct=risk_pct,
        warnings=warnings,
        headlines=related,
        interval_summary=_interval_summary(tech),
        ma_note=ma_note,
    )


def _sentiment_lines(analysis: MarketAnalysis, currencies: Sequence[str]) -> str:
    lines = []
    for ccy in currencies:
        sentiment = analysis.currencies.get(ccy)
        if sentiment is None:
            continue
        bar = f"{sentiment.score:+.2f}"
        line = f"**{ccy}** {bar} {sentiment.label_ja}"
        if sentiment.headline_count:
            line += f" (記事{sentiment.headline_count}件)"
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
            extras.append(f"予:{event.forecast}")
        if event.previous:
            extras.append(f"前:{event.previous}")
        if extras:
            line += f" ({' '.join(extras)})"
        lines.append(line)
    if len(events) > limit:
        lines.append(f"…ほか{len(events) - limit}件")
    return "\n".join(lines)


def _plan_embed(plan: TradePlan, fast: int, slow: int) -> dict:
    color = {
        "long": COLOR_LONG,
        "short": COLOR_SHORT,
        "standby": COLOR_STANDBY,
    }.get(plan.direction, COLOR_NEUTRAL)

    breakdown = (
        f"複合 **{plan.composite:+.2f}**"
        f" = テクニカル {plan.tech_score:+.2f}×{TECH_WEIGHT:.0%}"
        f" + ニュース {plan.news_score:+.2f}×{NEWS_WEIGHT:.0%}"
    )
    fields = [
        {
            "name": "判断",
            "value": (
                f"{plan.emoji} **{plan.direction_ja}** 確信度 {plan.conviction}/100\n"
                f"{breakdown}\n{plan.ma_note}"
            ),
            "inline": False,
        },
        {
            "name": "時間足レーティング",
            "value": plan.interval_summary,
            "inline": False,
        },
    ]
    if plan.direction in ("long", "short") and plan.stop is not None:
        fields.append(
            {
                "name": "プライスプラン (ATRベース)",
                "value": (
                    f"現値 {format_price(plan.symbol, plan.close)}"
                    f" / SL {format_price(plan.symbol, plan.stop)}\n"
                    f"T1 {format_price(plan.symbol, plan.target1)} (1R)"
                    f" / T2 {format_price(plan.symbol, plan.target2)} (2R)\n"
                    f"ATR(1h) {format_price(plan.symbol, plan.atr)}"
                    f" / 推奨リスク {plan.risk_pct:.2g}%"
                ),
                "inline": False,
            }
        )
    if plan.warnings:
        fields.append(
            {"name": "イベントリスク", "value": "\n".join(plan.warnings), "inline": False}
        )
    if plan.headlines:
        fields.append(
            {
                "name": "関連ヘッドライン",
                "value": "\n".join(
                    f"・[{item.source}] {item.title[:90]}" for item in plan.headlines
                ),
                "inline": False,
            }
        )
    return {
        "title": f"{plan.symbol} — {plan.direction_ja}",
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
    now: datetime | None = None,
) -> dict:
    """Discord Webhook用のペイロードを組み立てる。"""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    headline_parts = [
        f"{plan.emoji} {plan.symbol} {plan.direction_ja}({plan.conviction})"
        for plan in plans
    ]
    engine_ja = "Claude分析" if analysis.engine == "claude" else "語彙分析"
    content = (
        f"📊 **FXデスクブリーフィング** {now.astimezone(JST):%m/%d %H:%M} JST"
        f" | 地合い: {analysis.regime_ja} ({engine_ja})\n"
        + " / ".join(headline_parts)
    )

    macro_fields = [
        {
            "name": "通貨センチメント",
            "value": _sentiment_lines(analysis, currencies),
            "inline": False,
        },
        {
            "name": "今後48時間の重要イベント",
            "value": _events_lines(list(events_48h)),
            "inline": False,
        },
    ]
    if analysis.summary:
        macro_fields.insert(
            0, {"name": "市況要約", "value": analysis.summary, "inline": False}
        )
    if fetch_warnings:
        macro_fields.append(
            {
                "name": "データ取得の注意",
                "value": "\n".join(f"・{w}" for w in list(fetch_warnings)[:6]),
                "inline": False,
            }
        )

    embeds = [
        {
            "title": "マクロ・センチメント概況",
            "color": COLOR_NEUTRAL,
            "fields": macro_fields,
            "footer": {
                "text": f"fx-codex fx_briefing | MA({fast_window}/{slow_window}) | OANDA"
            },
            "timestamp": now_iso,
        }
    ]
    embeds.extend(_plan_embed(plan, fast_window, slow_window) for plan in plans)
    return {
        "username": "fx-codex デスクブリーフィング",
        "content": content,
        "embeds": embeds[:10],  # Discordの上限
    }
