"""時間足別FX統合ブリーフィングのDiscordペイロード生成。

15m / 1h / 4h / 1d の独立判断を、単なる一覧ではなく、
「総合結論 → マクロ → 通貨ペア別根拠 → 学習信頼性 → データ品質 → 最終行動」
の順で読めるデスク向け通知へ整形する。

判断ロジック自体は timeframe.py が担い、このモジュールは表示だけを担当する。
Discord の content / embed / field 上限を超えないよう、ペア数に応じて自動圧縮する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
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
from .calendar import EconomicEvent, symbol_currencies
from .sentiment import MarketAnalysis, pair_bias
from .timeframe import TimeframePlan

COLOR_CLOSED = 0x607D8B
DISCORD_CONTENT_LIMIT = 2000
DISCORD_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_TOTAL_SOFT_LIMIT = 5800
MAX_EMBEDS = 10

_DIRECTION_COLOR = {
    "long": COLOR_LONG,
    "short": COLOR_SHORT,
    "standby": COLOR_STANDBY,
    "closed": COLOR_CLOSED,
}

TIMEFRAME_LABEL_JA = {"15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足"}


def _clip_text(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _fmt_horizon(hours: float) -> str:
    if hours < 1.0:
        return f"{round(hours * 60)}分後"
    if hours == int(hours):
        return f"{int(hours)}時間後"
    return f"{hours}時間後"


def _technical_label(score: float) -> str:
    if score >= 0.6:
        return "強い買い"
    if score >= 0.15:
        return "買い"
    if score <= -0.6:
        return "強い売り"
    if score <= -0.15:
        return "売り"
    return "中立"


def _technical_side(plans: Sequence[TimeframePlan]) -> str:
    directional = [plan.tf_score for plan in plans if abs(plan.tf_score) >= 0.15]
    if not directional:
        return "neutral"
    average = sum(directional) / len(directional)
    if average >= 0.15:
        return "long"
    if average <= -0.15:
        return "short"
    return "neutral"


def _symbol_color(plans: Sequence[TimeframePlan]) -> int:
    directional = [p for p in plans if p.direction in ("long", "short")]
    if directional:
        best = max(directional, key=lambda p: p.conviction)
        return _DIRECTION_COLOR.get(best.direction, COLOR_NEUTRAL)
    technical_side = _technical_side(plans)
    if technical_side in _DIRECTION_COLOR:
        return _DIRECTION_COLOR[technical_side]
    for plan in plans:
        if plan.direction in _DIRECTION_COLOR:
            return _DIRECTION_COLOR[plan.direction]
    return COLOR_NEUTRAL


def _pair_news_bias(symbol: str, analysis: MarketAnalysis) -> float:
    try:
        base, quote = symbol_currencies(symbol)
    except (TypeError, ValueError):
        return 0.0
    return pair_bias(base, quote, analysis.currencies)


def _direction_word(direction: str) -> str:
    return {"long": "買い", "short": "売り", "neutral": "中立"}.get(direction, direction)


def _bias_side(value: float) -> str:
    if value >= 0.08:
        return "long"
    if value <= -0.08:
        return "short"
    return "neutral"


def _has_conflict(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> bool:
    technical_side = _technical_side(plans)
    news_side = _bias_side(_pair_news_bias(symbol, analysis))
    return technical_side in ("long", "short") and news_side in ("long", "short") and technical_side != news_side


def _best_plan(plans: Sequence[TimeframePlan]) -> TimeframePlan | None:
    directional = [p for p in plans if p.direction in ("long", "short")]
    if directional:
        return max(directional, key=lambda p: (p.conviction, -p.horizon_hours))
    return plans[0] if plans else None


def _pair_summary_line(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> str:
    side = _technical_side(plans)
    conflict = _has_conflict(symbol, plans, analysis)
    best = _best_plan(plans)
    label = _direction_word(side)
    if side == "neutral":
        return f"⚪ **{symbol}：方向感不足／新規エントリーは見送り**"
    if conflict:
        return f"🔴 **{symbol}：{label}目線／ただしマクロと矛盾するため見送り**"
    if best is None or best.direction not in ("long", "short") or best.conviction < 50:
        return f"🟡 **{symbol}：{label}目線維持／ただし今は見送り**"
    if best.data_quality < 0.6 or any("ブロック" in warning or "停止" in warning for warning in best.warnings):
        return f"🟡 **{symbol}：{label}目線維持／データ品質を理由に見送り**"
    return f"🟢 **{symbol}：{label}候補／条件成立後のみ再評価**"


def _pair_action(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> str:
    side = _technical_side(plans)
    conflict = _has_conflict(symbol, plans, analysis)
    best = _best_plan(plans)
    if side == "neutral":
        return f"**{symbol}**：方向感が弱いため見送り。"
    label = _direction_word(side)
    if conflict:
        return f"**{symbol}**：テクニカルは{label}ですがニュース方向と不一致。両者がそろうまで見送ります。"
    if best is None or best.conviction < 50:
        return f"**{symbol}**：{label}方向を監視しますが、確信度が不足しているため見送ります。"
    if best.stop is None:
        return f"**{symbol}**：{label}候補ですが、無効化水準を確認できるまで見送ります。"
    breakout = best.target1 if side == "long" else best.target1
    return (
        f"**{symbol}**：{label}方向を監視。"
        f"{format_price(symbol, breakout)}付近への進展と判定維持を確認後に再評価し、"
        f"{format_price(symbol, best.stop)}到達で短期仮説を無効とします。"
    )


def _timeframe_field(plan: TimeframePlan) -> dict:
    label = TIMEFRAME_LABEL_JA.get(plan.timeframe, plan.timeframe)
    horizon = _fmt_horizon(plan.horizon_hours)
    lines = [f"{plan.emoji} **{plan.direction_ja}**　確信度 {plan.conviction}（採点: {horizon}）"]
    lines.append(f"テクニカル: {_technical_label(plan.tf_score)}")
    if plan.direction in ("long", "short") and plan.stop is not None:
        lines.append(
            f"現値 {format_price(plan.symbol, plan.close)} / SL {format_price(plan.symbol, plan.stop)}"
            f" / T1 {format_price(plan.symbol, plan.target1)} / T2 {format_price(plan.symbol, plan.target2)}"
        )
    metric_parts = []
    if plan.rsi is not None:
        metric_parts.append(f"RSI {plan.rsi:.0f}")
    if plan.adx is not None:
        metric_parts.append(f"ADX {plan.adx:.0f}")
    if plan.atr is not None:
        metric_parts.append(f"ATR {format_price(plan.symbol, plan.atr)}")
    if metric_parts:
        lines.append(" / ".join(metric_parts))
    if plan.reason:
        lines.append(plan.reason)
    learn_warnings = [
        warning
        for warning in plan.warnings
        if any(key in warning for key in ("学習調整", "期待値", "最大化", "TP/SL", "データ"))
    ]
    if learn_warnings:
        lines.append(learn_warnings[0])
    return {
        "name": f"{label}｜主ホライズン {horizon}",
        "value": _clip_text("\n".join(lines), DISCORD_FIELD_VALUE_LIMIT),
        "inline": False,
    }


def _technical_direction_field(plans: Sequence[TimeframePlan]) -> dict:
    lines = [
        f"{TIMEFRAME_LABEL_JA.get(plan.timeframe, plan.timeframe)}：**{_technical_label(plan.tf_score)}**"
        for plan in plans
    ]
    return {"name": "テクニカル方向", "value": "\n".join(lines), "inline": True}


def _reference_levels_field(symbol: str, plans: Sequence[TimeframePlan]) -> dict:
    plan = _best_plan(plans)
    if plan is None:
        value = "価格データなし"
    else:
        lines = [f"現在値：**{format_price(symbol, plan.close)}**"]
        if plan.stop is not None:
            lines.append(f"無効化水準：{format_price(symbol, plan.stop)}")
        if plan.target1 is not None:
            lines.append(f"第1目標：{format_price(symbol, plan.target1)}")
        if plan.target2 is not None:
            lines.append(f"第2目標：{format_price(symbol, plan.target2)}")
        value = "\n".join(lines)
    return {"name": "現在値と参考水準", "value": value, "inline": True}


def _evidence_fields(
    symbol: str,
    plans: Sequence[TimeframePlan],
    analysis: MarketAnalysis,
    events_48h: Sequence[EconomicEvent],
) -> list[dict]:
    side = _technical_side(plans)
    news_bias = _pair_news_bias(symbol, analysis)
    news_side = _bias_side(news_bias)
    technical_labels = [_technical_label(plan.tf_score) for plan in plans]
    positives: list[str] = []
    blockers: list[str] = []

    if side in ("long", "short") and all(
        (plan.tf_score >= 0.15 if side == "long" else plan.tf_score <= -0.15) for plan in plans
    ):
        positives.append(f"15分足〜日足まで{_direction_word(side)}方向が一致")
    elif side in ("long", "short"):
        positives.append(f"時間足平均では{_direction_word(side)}優勢（{' / '.join(technical_labels)}）")

    if news_side == side and side in ("long", "short"):
        positives.append(f"ニュースフローも{_direction_word(side)}方向（ペアバイアス {news_bias:+.2f}）")
    elif news_side in ("long", "short") and side in ("long", "short"):
        blockers.append(
            f"ニュースは{_direction_word(news_side)}、テクニカルは{_direction_word(side)}で方向が矛盾"
        )
    else:
        blockers.append("ニュース方向が中立で、テクニカルを補強していない")

    rsi_values = [plan.rsi for plan in plans if plan.rsi is not None]
    if rsi_values and all(30.0 <= value <= 70.0 for value in rsi_values):
        positives.append("主要時間足のRSIは過熱圏ではない")
    adx_values = [plan.adx for plan in plans if plan.adx is not None]
    if adx_values and max(adx_values) >= 25:
        positives.append(f"ADX最大{max(adx_values):.0f}で一定のトレンド強度")

    average_quality = sum(plan.data_quality for plan in plans) / max(1, len(plans))
    if average_quality < 0.6:
        blockers.append(f"平均データ品質が{average_quality * 100:.0f}%で基準未達")
    low_conviction = [plan for plan in plans if plan.direction not in ("long", "short") or plan.conviction < 50]
    if low_conviction:
        blockers.append(f"4時間足中{len(low_conviction)}本が見送りまたは確信度50未満")

    warnings: list[str] = []
    for plan in plans:
        for warning in plan.warnings:
            clean = warning.strip().lstrip("⚠️⛔・ ")
            if clean and clean not in warnings:
                warnings.append(clean)
    blockers.extend(warnings[:3])
    try:
        base, quote = symbol_currencies(symbol)
        relevant_events = [event for event in events_48h if event.currency in {base, quote}]
    except (TypeError, ValueError):
        relevant_events = list(events_48h)
    if relevant_events:
        blockers.append("今後48時間に関連する高重要度イベントがあり、発表前後は新規エントリー停止")

    if not positives:
        positives.append("明確な優位性を確認できる材料は不足")
    if not blockers:
        blockers.append("重大な矛盾はないが、条件成立前の先回りは避ける")

    directional_name = "強気材料" if side == "long" else "弱気材料" if side == "short" else "方向材料"
    return [
        {
            "name": directional_name,
            "value": _clip_text("\n".join(f"・{line}" for line in positives[:4]), DISCORD_FIELD_VALUE_LIMIT),
            "inline": False,
        },
        {
            "name": "見送り材料",
            "value": _clip_text("\n".join(f"・{line}" for line in blockers[:6]), DISCORD_FIELD_VALUE_LIMIT),
            "inline": False,
        },
    ]


def _symbol_embed(
    symbol: str,
    plans: Sequence[TimeframePlan],
    analysis: MarketAnalysis,
    events_48h: Sequence[EconomicEvent],
    aux_reports: Mapping[str, str] | None = None,
) -> dict:
    fields = [_timeframe_field(plan) for plan in plans]
    fields.extend((_technical_direction_field(plans), _reference_levels_field(symbol, plans)))
    fields.extend(_evidence_fields(symbol, plans, analysis, events_48h))
    if aux_reports:
        aux_lines = [line for line in aux_reports.values() if line]
        if aux_lines:
            fields.append(
                {
                    "name": "補助ホライズン（観測用・学習には不使用）",
                    "value": _clip_text("\n".join(aux_lines), DISCORD_FIELD_VALUE_LIMIT),
                    "inline": False,
                }
            )
    fields.append(
        {
            "name": "📌 判断",
            "value": _clip_text(_pair_action(symbol, plans, analysis), DISCORD_FIELD_VALUE_LIMIT),
            "inline": False,
        }
    )
    return {
        "title": f"{symbol} — 統合判断",
        "description": _pair_summary_line(symbol, plans, analysis),
        "color": _symbol_color(plans),
        "fields": fields[:25],
    }


def _relative_strength(analysis: MarketAnalysis, currencies: Sequence[str]) -> str:
    rows = [analysis.currencies[ccy] for ccy in currencies if ccy in analysis.currencies]
    if not rows:
        return "判定不能"
    rows.sort(key=lambda item: item.score, reverse=True)
    return " ＞ ".join(item.currency for item in rows)


def _data_quality_note(plans_by_symbol: Mapping[str, Sequence[TimeframePlan]], fetch_warnings: Sequence[str]) -> str:
    plans = [plan for symbol_plans in plans_by_symbol.values() for plan in symbol_plans]
    if not plans:
        return "判断データなし"
    average = sum(plan.data_quality for plan in plans) / len(plans)
    directional = [plan for plan in plans if plan.direction in ("long", "short")]
    low_quality = sum(plan.data_quality < 0.6 for plan in plans)
    lines = [
        f"平均データ品質：**{average * 100:.0f}%**",
        f"方向判断：{len(directional)}/{len(plans)}件、品質60%未満：{low_quality}件",
    ]
    unique_warnings: list[str] = []
    for warning in fetch_warnings:
        clean = warning.strip().lstrip("⚠️⛔・ ")
        if clean and clean not in unique_warnings:
            unique_warnings.append(clean)
    lines.extend(f"・{warning}" for warning in unique_warnings[:4])
    lines.append("この通知は実売買シグナルではなく、監視・研究用の判断材料です。")
    return _clip_text("\n".join(lines), DISCORD_FIELD_VALUE_LIMIT)


def _final_actions(plans_by_symbol: Mapping[str, Sequence[TimeframePlan]], analysis: MarketAnalysis) -> str:
    lines = [_pair_action(symbol, plans, analysis) for symbol, plans in plans_by_symbol.items()]
    lines.append("**本日の基本方針**：ポジションを取ることより、期待値の低い取引を避けることを優先します。")
    return _clip_text("\n\n".join(lines), DISCORD_FIELD_VALUE_LIMIT)


def _embed_char_count(embed: Mapping[str, object]) -> int:
    count = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    footer = embed.get("footer")
    if isinstance(footer, Mapping):
        count += len(str(footer.get("text", "")))
    fields = embed.get("fields")
    if isinstance(fields, Sequence):
        for field in fields:
            if isinstance(field, Mapping):
                count += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    return count


def _shrink_embed(embed: Mapping[str, object], budget: int) -> dict:
    clone = deepcopy(dict(embed))
    clone["title"] = _clip_text(clone.get("title", ""), 256)
    clone["description"] = _clip_text(clone.get("description", ""), min(4096, max(80, budget // 3)))
    fields = clone.get("fields")
    if not isinstance(fields, list):
        return clone
    kept: list[dict] = []
    used = len(clone["title"]) + len(clone["description"])
    remaining_fields = len(fields)
    for field in fields:
        remaining_fields -= 1
        name = _clip_text(field.get("name", ""), 256)
        reserve = max(0, remaining_fields * 24)
        available = max(24, min(DISCORD_FIELD_VALUE_LIMIT, budget - used - len(name) - reserve))
        if budget - used - len(name) <= 8:
            break
        value = _clip_text(field.get("value", ""), available)
        kept.append({"name": name, "value": value, "inline": bool(field.get("inline", False))})
        used += len(name) + len(value)
    clone["fields"] = kept[:25]
    return clone


def _fit_embed_budget(embeds: Sequence[Mapping[str, object]]) -> list[dict]:
    visible = list(embeds[:MAX_EMBEDS])
    if not visible:
        return []
    macro = _shrink_embed(visible[0], 2200)
    result = [macro]
    remaining = max(900, DISCORD_EMBED_TOTAL_SOFT_LIMIT - _embed_char_count(macro))
    symbol_count = max(1, len(visible) - 1)
    per_symbol = max(350, remaining // symbol_count)
    result.extend(_shrink_embed(embed, per_symbol) for embed in visible[1:])
    return result[:MAX_EMBEDS]


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
    """時間足別判断を、結論先行の統合ブリーフィングへ整形する。"""
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    aux_reports_by_symbol = aux_reports_by_symbol or {}

    engine_ja = {
        "claude": "Claude分析",
        "analyst": "自前分析エンジン",
        "lexicon": "語彙分析",
    }.get(analysis.engine, analysis.engine)
    regime_ja = REGIME_HINT_JA.get(analysis.regime, analysis.regime_ja)

    pair_lines = [
        _pair_summary_line(symbol, plans, analysis) for symbol, plans in plans_by_symbol.items()
    ]
    has_entry_candidate = any("条件成立後" in line for line in pair_lines)
    conclusion = "条件付き候補あり。ただし即時エントリーは避ける" if has_entry_candidate else "新規エントリーは見送り優先"
    content = (
        f"📊 **FX統合ブリーフィング｜{now.astimezone(JST):%m/%d %H:%M} JST**\n"
        f"対象：{' / '.join(plans_by_symbol)}\n"
        f"結論：**{conclusion}**\n"
        f"地合い：{regime_ja}（{engine_ja}）\n\n"
        + "\n".join(pair_lines)
    )

    sentiment_value = _sentiment_lines(analysis, currencies)
    if sentiment_value != "データなし":
        sentiment_value += "\n※ニュースから測った通貨の強さ。+1.0に近いほど買われやすい"

    macro_fields: list[dict] = [
        {
            "name": "🎯 総合判断",
            "value": _clip_text("\n".join(pair_lines), DISCORD_FIELD_VALUE_LIMIT),
            "inline": False,
        },
        {
            "name": "現在の相対的な通貨強弱",
            "value": f"**{_relative_strength(analysis, currencies)}**",
            "inline": False,
        },
    ]
    if analysis.summary:
        macro_fields.append(
            {"name": "市況要約", "value": _clip_text(analysis.summary, DISCORD_FIELD_VALUE_LIMIT), "inline": False}
        )
    macro_fields.extend(
        [
            {"name": "🌍 マクロ・センチメント", "value": sentiment_value, "inline": False},
            {
                "name": "🗓️ 今後48時間の重要イベント",
                "value": _clip_text(_events_lines(list(events_48h)), DISCORD_FIELD_VALUE_LIMIT),
                "inline": False,
            },
        ]
    )
    if journal_note:
        macro_fields.append(
            {"name": "判断の検証（自己採点）", "value": _clip_text(journal_note, DISCORD_FIELD_VALUE_LIMIT), "inline": False}
        )
    reliability = learning_note.strip() or "成熟した採点サンプルが不足しており、確率校正は未完成です。"
    reliability = (
        reliability
        + "\n⚠️ 確信度は上昇・下落の実確率ではなく、分析根拠の一致度として扱います。"
    )
    macro_fields.append(
        {
            "name": "🧠 自動学習の信頼性",
            "value": _clip_text(reliability, DISCORD_FIELD_VALUE_LIMIT),
            "inline": False,
        }
    )
    macro_fields.append(
        {
            "name": "⚠️ データ品質・運用制限",
            "value": _data_quality_note(plans_by_symbol, fetch_warnings),
            "inline": False,
        }
    )
    macro_fields.append(
        {"name": "✅ 最終アクション", "value": _final_actions(plans_by_symbol, analysis), "inline": False}
    )

    embeds: list[dict] = [
        {
            "title": "マクロ・センチメント概況",
            "description": "総合結論を先に示し、その後に根拠・反証・運用制限を確認します。",
            "color": COLOR_NEUTRAL,
            "fields": macro_fields,
            "footer": {"text": "fx-codex 統合ブリーフィング | research/shadow only"},
            "timestamp": now_iso,
        }
    ]
    for symbol, plans in plans_by_symbol.items():
        embeds.append(
            _symbol_embed(
                symbol,
                plans,
                analysis,
                events_48h,
                aux_reports_by_symbol.get(symbol),
            )
        )

    return {
        "username": "fx-codex 統合デスク",
        "content": _clip_text(content, DISCORD_CONTENT_LIMIT),
        "embeds": _fit_embed_budget(embeds),
    }
