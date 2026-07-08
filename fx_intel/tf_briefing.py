"""時間足別ブリーフィングのDiscordペイロード生成。

briefing.build_discord_payload が「1ペア1判断」の embed を並べるのに対し、
こちらは1ペアにつき4時間足(15m/1h/4h/1d)の判断を1つの embed に
フィールドとしてまとめる。Discord の embed 上限(10)に収めつつ、各時間足の
方向・確信度・主ホライズン・根拠・SL/TP を一覧できるようにする。

マクロ・センチメント・イベント・学習メモの概況 embed は briefing の
ヘルパをそのまま再利用する(表示の一貫性のため)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, UTC

from .briefing import (
    COLOR_LONG,
    COLOR_NEUTRAL,
    COLOR_SHORT,
    COLOR_STANDBY,
    JST,
    REGIME_HINT_JA,
    _events_lines,
    _sentiment_lines,
    format_price,
)
from .calendar import EconomicEvent
from .sentiment import MarketAnalysis
from .timeframe import TimeframePlan

COLOR_CLOSED = 0x607D8B

_DIRECTION_COLOR = {
    "long": COLOR_LONG,
    "short": COLOR_SHORT,
    "standby": COLOR_STANDBY,
    "closed": COLOR_CLOSED,
}

TIMEFRAME_LABEL_JA = {"15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足"}


def _fmt_horizon(hours: float) -> str:
    if hours < 1.0:
        return f"{round(hours * 60)}分後"
    if hours == int(hours):
        return f"{int(hours)}時間後"
    return f"{hours}時間後"


def _symbol_color(plans: Sequence[TimeframePlan]) -> int:
    """ペアの代表色: 主ホライズンが最短(=最も速報性が高い)の足の方向で決める。

    実際には一覧なので、方向がついた足のうち確信度が最も高いものの色にする。
    """
    directional = [p for p in plans if p.direction in ("long", "short")]
    if directional:
        best = max(directional, key=lambda p: p.conviction)
        return _DIRECTION_COLOR.get(best.direction, COLOR_NEUTRAL)
    for plan in plans:
        if plan.direction in _DIRECTION_COLOR:
            return _DIRECTION_COLOR[plan.direction]
    return COLOR_NEUTRAL


def _timeframe_field(plan: TimeframePlan) -> dict:
    """1時間足ぶんのフィールド(名前=時間足、値=判断の要約)。"""
    label = TIMEFRAME_LABEL_JA.get(plan.timeframe, plan.timeframe)
    horizon = _fmt_horizon(plan.horizon_hours)
    lines = [
        f"{plan.emoji} **{plan.direction_ja}** 確信度 {plan.conviction}/100" f"(採点: {horizon})"
    ]
    if plan.reason:
        lines.append(plan.reason)
    if plan.direction in ("long", "short") and plan.stop is not None:
        lines.append(
            f"現値 {format_price(plan.symbol, plan.close)}"
            f" / SL {format_price(plan.symbol, plan.stop)}"
            f" / T1 {format_price(plan.symbol, plan.target1)}"
            f" / T2 {format_price(plan.symbol, plan.target2)}"
        )
    # 主要指標(採点対象の足自身の値)
    metric_parts = []
    if plan.rsi is not None:
        metric_parts.append(f"RSI {plan.rsi:.0f}")
    if plan.adx is not None:
        metric_parts.append(f"ADX {plan.adx:.0f}")
    if plan.atr is not None:
        metric_parts.append(f"ATR {format_price(plan.symbol, plan.atr)}")
    if metric_parts:
        lines.append(" / ".join(metric_parts))
    # 学習調整・期待値ガード・承認済みTP/SLは1件だけ簡潔に載せる(embed肥大を防ぐ)
    learn_warnings = [
        w for w in plan.warnings if "学習調整" in w or "期待値ガード" in w or "承認済みTP/SL" in w
    ]
    if learn_warnings:
        lines.append(learn_warnings[0])
    return {
        "name": f"{label}(主ホライズン{horizon})",
        "value": "\n".join(lines)[:1024],
        "inline": False,
    }


def _symbol_embed(
    symbol: str,
    plans: Sequence[TimeframePlan],
    aux_reports: Mapping[str, str] | None = None,
) -> dict:
    """1ペアぶんの embed(4時間足をフィールドで並べる)。"""
    fields = [_timeframe_field(plan) for plan in plans]
    if aux_reports:
        aux_lines = [line for line in aux_reports.values() if line]
        if aux_lines:
            fields.append(
                {
                    "name": "補助ホライズン(観測用・学習には不使用)",
                    "value": "\n".join(aux_lines)[:1024],
                    "inline": False,
                }
            )
    headline = " / ".join(f"{p.timeframe} {p.emoji}{p.direction_ja}({p.conviction})" for p in plans)
    return {
        "title": f"{symbol} — 時間足別の判断",
        "description": headline,
        "color": _symbol_color(plans),
        "fields": fields[:25],  # Discordのフィールド上限
    }


def build_timeframe_discord_payload(
    plans_by_symbol: Mapping[str, Sequence[TimeframePlan]],
    analysis: MarketAnalysis,
    events_48h: Sequence[EconomicEvent],
    currencies: Sequence[str],
    fetch_warnings: Sequence[str] = (),
    learning_note: str = "",
    journal_note: str = "",
    aux_reports_by_symbol: Mapping[str, Mapping[str, str]] | None = None,
    now: datetime | None = None,
) -> dict:
    """時間足別ブリーフィングのDiscord Webデータを組み立てる。

    plans_by_symbol は {symbol: [TimeframePlan(15m), ...(1h/4h/1d)]}。
    先頭にマクロ・センチメント・学習の概況 embed、続いてペアごとに
    4時間足を1 embed にまとめて並べる。
    """
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    aux_reports_by_symbol = aux_reports_by_symbol or {}

    engine_ja = {
        "claude": "Claude分析",
        "analyst": "自前分析エンジン",
        "lexicon": "語彙分析",
    }.get(analysis.engine, analysis.engine)
    regime_ja = REGIME_HINT_JA.get(analysis.regime, analysis.regime_ja)

    # ヘッドライン: ペアごとに、確信度が最も高い足の判断を代表として1行
    headline_parts = []
    for symbol, plans in plans_by_symbol.items():
        directional = [p for p in plans if p.direction in ("long", "short")]
        lead = (
            max(directional, key=lambda p: p.conviction)
            if directional
            else (plans[0] if plans else None)
        )
        if lead is not None:
            headline_parts.append(
                f"{lead.emoji} {symbol} {lead.timeframe}{lead.direction_ja}({lead.conviction})"
            )
    content = (
        f"📊 **FX時間足別ブリーフィング** {now.astimezone(JST):%m/%d %H:%M} JST ({engine_ja})\n"
        f"相場のムード: {regime_ja}\n"
        "各時間足を独立して分析し、時間足ごとの未来(15m→15分後…1d→24時間後)で自己採点します\n"
        + " / ".join(headline_parts)
    )

    sentiment_value = _sentiment_lines(analysis, currencies)
    if sentiment_value != "データなし":
        sentiment_value += "\n※ニュースから測った通貨の強さ。+1.0に近いほど買われやすい"
    macro_fields = [
        {"name": "通貨センチメント", "value": sentiment_value, "inline": False},
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
            {"name": "判断の検証(自己採点)", "value": journal_note[:1024], "inline": False}
        )
    if learning_note:
        macro_fields.append(
            {
                "name": "🧠 時間足別の学習メモ(過去の判断からの自動調整)",
                "value": learning_note[:1024],
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
                "・**時間足別**: 15分足〜日足を別々のアナリストとして分析し、"
                "それぞれの時間軸の未来で当たり外れを採点します\n"
                "・**主ホライズン**: その時間足の判断を何時間後の値動きで採点するか\n"
                "・**ロング/ショート**: 値上がりを狙う「買い」/値下がりを狙う「売り」\n"
                "・**確信度**: 分析根拠のそろい具合(0〜100)。低いときは見送りが無難"
            ),
            "inline": False,
        }
    )

    embeds = [
        {
            "title": "マクロ・センチメント概況",
            "color": COLOR_NEUTRAL,
            "fields": macro_fields,
            "footer": {"text": "fx-codex fx_briefing 時間足別 | OANDA"},
            "timestamp": now_iso,
        }
    ]
    for symbol, plans in plans_by_symbol.items():
        embeds.append(_symbol_embed(symbol, plans, aux_reports_by_symbol.get(symbol)))
    return {
        "username": "fx-codex 時間足別デスク",
        "content": content[:2000],  # Discord content 上限
        "embeds": embeds[:10],
    }
