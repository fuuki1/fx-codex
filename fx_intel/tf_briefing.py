"""時間足別判断を、結論先行のFX統合ブリーフィングへ整形する。"""

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
MAX_EMBEDS = 10
MAX_CONTENT = 2000
MAX_FIELD = 1024
MAX_EMBED_CHARS = 5800
# Discord documents 25 fields per embed, but its webhook backend currently
# returns HTTP 500 for a valid multi-embed payload whose aggregate field count
# exceeds 25. Keep a conservative per-message cap; this preserves every embed
# by moving the overflow to the next message.
MAX_FIELDS_PER_MESSAGE = 25

_DIRECTION_COLOR = {
    "long": COLOR_LONG,
    "short": COLOR_SHORT,
    "standby": COLOR_STANDBY,
    "closed": COLOR_CLOSED,
}
TIMEFRAME_LABEL_JA = {"15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足"}


def _clip(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_horizon(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)}分後"
    return f"{int(hours)}時間後" if hours == int(hours) else f"{hours}時間後"


def _tech_label(score: float) -> str:
    if score >= 0.6:
        return "強い買い"
    if score >= 0.15:
        return "買い"
    if score <= -0.6:
        return "強い売り"
    if score <= -0.15:
        return "売り"
    return "中立"


def _tech_side(plans: Sequence[TimeframePlan]) -> str:
    scores = [plan.tf_score for plan in plans if abs(plan.tf_score) >= 0.15]
    average = sum(scores) / len(scores) if scores else 0.0
    if average >= 0.15:
        return "long"
    if average <= -0.15:
        return "short"
    return "neutral"


def _word(side: str) -> str:
    return {"long": "買い", "short": "売り", "neutral": "中立"}.get(side, side)


def _news_side(symbol: str, analysis: MarketAnalysis) -> tuple[str, float]:
    try:
        base, quote = symbol_currencies(symbol)
    except (TypeError, ValueError):
        return "neutral", 0.0
    bias = pair_bias(base, quote, analysis.currencies)
    if bias >= 0.08:
        return "long", bias
    if bias <= -0.08:
        return "short", bias
    return "neutral", bias


def _best_plan(plans: Sequence[TimeframePlan]) -> TimeframePlan | None:
    directional = [plan for plan in plans if plan.direction in ("long", "short")]
    return max(directional, key=lambda plan: plan.conviction) if directional else None


def _is_conflict(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> bool:
    tech = _tech_side(plans)
    news, _ = _news_side(symbol, analysis)
    return tech in ("long", "short") and news in ("long", "short") and tech != news


def _summary_line(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> str:
    side = _tech_side(plans)
    best = _best_plan(plans)
    if side == "neutral":
        return f"⚪ **{symbol}：方向感不足／新規エントリーは見送り**"
    if _is_conflict(symbol, plans, analysis):
        return f"🔴 **{symbol}：{_word(side)}目線／マクロと矛盾するため見送り**"
    if best is None or best.conviction < 50:
        return f"🟡 **{symbol}：{_word(side)}目線維持／ただし今は見送り**"
    if best.data_quality < 0.6:
        return f"🟡 **{symbol}：{_word(side)}目線維持／データ品質を理由に見送り**"
    return f"🟢 **{symbol}：{_word(side)}候補／条件成立後のみ再評価**"


def _action(symbol: str, plans: Sequence[TimeframePlan], analysis: MarketAnalysis) -> str:
    side = _tech_side(plans)
    best = _best_plan(plans)
    if side == "neutral":
        return f"**{symbol}**：方向感が弱いため見送り。"
    if _is_conflict(symbol, plans, analysis):
        return (
            f"**{symbol}**：テクニカルは{_word(side)}ですがニュース方向と不一致。"
            "両者がそろうまで見送ります。"
        )
    if best is None or best.conviction < 50:
        return f"**{symbol}**：{_word(side)}方向を監視しますが、確信度不足で見送り。"
    if best.stop is None:
        return f"**{symbol}**：無効化水準を確認できるまで見送り。"
    return (
        f"**{symbol}**：{_word(side)}方向を監視。"
        f"{format_price(symbol, best.target1)}付近への進展後に再評価し、"
        f"{format_price(symbol, best.stop)}到達で短期仮説を無効とします。"
    )


def _symbol_color(plans: Sequence[TimeframePlan]) -> int:
    best = _best_plan(plans)
    if best is not None:
        return _DIRECTION_COLOR.get(best.direction, COLOR_NEUTRAL)
    return _DIRECTION_COLOR.get(_tech_side(plans), COLOR_NEUTRAL)


def _timeframe_field(plan: TimeframePlan) -> dict:
    label = TIMEFRAME_LABEL_JA.get(plan.timeframe, plan.timeframe)
    horizon = _fmt_horizon(plan.horizon_hours)
    lines = [
        f"{plan.emoji} **{plan.direction_ja}**　確信度 {plan.conviction}（採点: {horizon}）",
        f"テクニカル: {_tech_label(plan.tf_score)}",
    ]
    if plan.direction in ("long", "short") and plan.stop is not None:
        lines.append(
            f"現値 {format_price(plan.symbol, plan.close)} / "
            f"SL {format_price(plan.symbol, plan.stop)} / "
            f"T1 {format_price(plan.symbol, plan.target1)} / "
            f"T2 {format_price(plan.symbol, plan.target2)}"
        )
    metrics = []
    if plan.rsi is not None:
        metrics.append(f"RSI {plan.rsi:.0f}")
    if plan.adx is not None:
        metrics.append(f"ADX {plan.adx:.0f}")
    if plan.atr is not None:
        metrics.append(f"ATR {format_price(plan.symbol, plan.atr)}")
    if metrics:
        lines.append(" / ".join(metrics))
    if plan.reason:
        lines.append(plan.reason)
    warnings = [
        warning
        for warning in plan.warnings
        if any(key in warning for key in ("期待値", "最大化", "TP/SL", "データ"))
    ]
    if warnings:
        lines.append(warnings[0])
    return {
        "name": f"{label}｜主ホライズン{horizon}",
        "value": _clip("\n".join(lines), MAX_FIELD),
        "inline": False,
    }


def _levels(symbol: str, plans: Sequence[TimeframePlan]) -> str:
    plan = _best_plan(plans) or (plans[0] if plans else None)
    if plan is None:
        return "価格データなし"
    lines = [f"現在値：**{format_price(symbol, plan.close)}**"]
    if plan.stop is not None:
        lines.append(f"無効化水準：{format_price(symbol, plan.stop)}")
    if plan.target1 is not None:
        lines.append(f"第1目標：{format_price(symbol, plan.target1)}")
    if plan.target2 is not None:
        lines.append(f"第2目標：{format_price(symbol, plan.target2)}")
    return "\n".join(lines)


def _evidence(
    symbol: str,
    plans: Sequence[TimeframePlan],
    analysis: MarketAnalysis,
    events: Sequence[EconomicEvent],
) -> tuple[str, str, str]:
    side = _tech_side(plans)
    news, bias = _news_side(symbol, analysis)
    strengths: list[str] = []
    blockers: list[str] = []
    if side in ("long", "short") and all(
        plan.tf_score >= 0.15 if side == "long" else plan.tf_score <= -0.15 for plan in plans
    ):
        strengths.append(f"15分足〜日足まで{_word(side)}方向が一致")
    if news == side and side in ("long", "short"):
        strengths.append(f"ニュースフローも{_word(side)}方向（{bias:+.2f}）")
    elif news in ("long", "short") and side in ("long", "short"):
        blockers.append(f"ニュースは{_word(news)}、テクニカルは{_word(side)}で矛盾")
    else:
        blockers.append("ニュース方向が中立でテクニカルを補強していない")
    rsi = [plan.rsi for plan in plans if plan.rsi is not None]
    if rsi and all(30 <= value <= 70 for value in rsi):
        strengths.append("主要時間足のRSIは過熱圏ではない")
    adx = [plan.adx for plan in plans if plan.adx is not None]
    if adx and max(adx) >= 25:
        strengths.append(f"ADX最大{max(adx):.0f}で一定のトレンド強度")
    weak = [
        plan for plan in plans if plan.direction not in ("long", "short") or plan.conviction < 50
    ]
    if weak:
        blockers.append(f"4時間足中{len(weak)}本が見送りまたは確信度50未満")
    warnings = []
    for plan in plans:
        for warning in plan.warnings:
            cleaned = warning.strip().lstrip("⚠️⛔・ ")
            if cleaned and cleaned not in warnings:
                warnings.append(cleaned)
    blockers.extend(warnings[:3])
    try:
        base, quote = symbol_currencies(symbol)
        related = [event for event in events if event.currency in {base, quote}]
    except (TypeError, ValueError):
        related = list(events)
    if related:
        blockers.append("関連する高重要度イベント前後は新規エントリー停止")
    strengths = strengths or ["明確な優位性を確認できる材料は不足"]
    blockers = blockers or ["条件成立前の先回りは避ける"]
    heading = "強気材料" if side == "long" else "弱気材料" if side == "short" else "方向材料"
    return (
        heading,
        "\n".join(f"・{item}" for item in strengths[:4]),
        "\n".join(f"・{item}" for item in blockers[:6]),
    )


def _symbol_embed(
    symbol: str,
    plans: Sequence[TimeframePlan],
    analysis: MarketAnalysis,
    events: Sequence[EconomicEvent],
    aux_reports: Mapping[str, str] | None,
) -> dict:
    heading, strengths, blockers = _evidence(symbol, plans, analysis, events)
    directions = "\n".join(
        f"{TIMEFRAME_LABEL_JA.get(plan.timeframe, plan.timeframe)}：**{_tech_label(plan.tf_score)}**"
        for plan in plans
    )
    fields = [_timeframe_field(plan) for plan in plans]
    fields.extend(
        [
            {"name": "テクニカル方向", "value": directions, "inline": True},
            {"name": "現在値と参考水準", "value": _levels(symbol, plans), "inline": True},
            {"name": heading, "value": _clip(strengths, MAX_FIELD), "inline": False},
            {"name": "見送り材料", "value": _clip(blockers, MAX_FIELD), "inline": False},
        ]
    )
    if aux_reports:
        value = "\n".join(line for line in aux_reports.values() if line)
        if value:
            fields.append(
                {
                    "name": "補助ホライズン（観測用・学習には不使用）",
                    "value": _clip(value, MAX_FIELD),
                    "inline": False,
                }
            )
    fields.append({"name": "📌 判断", "value": _action(symbol, plans, analysis), "inline": False})
    return {
        "title": f"{symbol} — 統合判断",
        "description": _summary_line(symbol, plans, analysis),
        "color": _symbol_color(plans),
        "fields": fields,
    }


def _quality_note(
    plans_by_symbol: Mapping[str, Sequence[TimeframePlan]], warnings: Sequence[str]
) -> str:
    plans = [plan for group in plans_by_symbol.values() for plan in group]
    if not plans:
        return "判断データなし"
    average = sum(plan.data_quality for plan in plans) / len(plans)
    low = sum(plan.data_quality < 0.6 for plan in plans)
    lines = [f"平均データ品質：**{average * 100:.0f}%**", f"品質60%未満：{low}/{len(plans)}件"]
    lines.extend(f"・{warning}" for warning in list(dict.fromkeys(warnings))[:4])
    lines.append("この通知は実売買シグナルではなく、監視・研究用の判断材料です。")
    return _clip("\n".join(lines), MAX_FIELD)


def _embed_chars(embed: Mapping[str, object]) -> int:
    count = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    fields = embed.get("fields")
    if isinstance(fields, Sequence):
        for field in fields:
            if isinstance(field, Mapping):
                count += len(str(field.get("name", "")))
                count += len(str(field.get("value", "")))
    footer = embed.get("footer")
    if isinstance(footer, Mapping):
        count += len(str(footer.get("text", "")))
    author = embed.get("author")
    if isinstance(author, Mapping):
        count += len(str(author.get("name", "")))
    return count


def _embed_field_count(embed: Mapping[str, object]) -> int:
    fields = embed.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        return 0
    return sum(1 for field in fields if isinstance(field, Mapping))


def _field_fragments(field: Mapping[str, object]) -> list[dict[str, object]]:
    """Split an overlong field value without silently discarding its text."""

    name = str(field.get("name", ""))
    if len(name) > 256:
        raise ValueError("Discord embed field name exceeds 256 characters")
    value = str(field.get("value", ""))
    chunks = [value[index : index + MAX_FIELD] for index in range(0, len(value), MAX_FIELD)]
    if not chunks:
        chunks = [""]
    fragments: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        fragment_name = name
        if index:
            fragment_name = _clip(f"{name}（続き {index + 1}）", 256)
        fragments.append(
            {
                "name": fragment_name,
                "value": chunk,
                "inline": bool(field.get("inline", False)),
            }
        )
    return fragments


def _continuation_embed(embed: Mapping[str, object], index: int) -> dict[str, object]:
    continuation: dict[str, object] = {
        "title": _clip(f"{embed.get('title', '')}（続き {index}）", 256),
        "fields": [],
    }
    if "color" in embed:
        continuation["color"] = embed["color"]
    return continuation


def _fragment_embed(embed: Mapping[str, object]) -> list[dict]:
    """Split one embed into valid fragments while retaining all field values."""

    original = deepcopy(dict(embed))
    raw_fields = original.pop("fields", [])
    fields: list[dict[str, object]] = []
    if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, (str, bytes)):
        for field in raw_fields:
            if isinstance(field, Mapping):
                fields.extend(_field_fragments(field))
    original["fields"] = []
    if _embed_chars(original) > MAX_EMBED_CHARS:
        raise ValueError("Discord embed metadata exceeds the safe character budget")

    fragments: list[dict] = [original]
    for field in fields:
        current = fragments[-1]
        projected_chars = _embed_chars(current) + len(str(field["name"])) + len(str(field["value"]))
        if (
            _embed_field_count(current) >= MAX_FIELDS_PER_MESSAGE
            or projected_chars > MAX_EMBED_CHARS
        ):
            current = _continuation_embed(embed, len(fragments) + 1)
            fragments.append(current)
        current_fields = current.get("fields")
        assert isinstance(current_fields, list)
        current_fields.append(field)
    return fragments


def _batch_embeds(embeds: Sequence[Mapping[str, object]]) -> list[list[dict]]:
    """内容を削らず、Discordの1メッセージ上限(embed合計文字数・個数)ごとに分ける。

    各field valueを1024文字ごと、各embedを25 field/文字数上限ごとに分割した上で、
    embedを複数メッセージへ振り分ける。送信内容は切り捨てない。
    """

    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    fields_used = 0
    for embed in embeds:
        for row in _fragment_embed(embed):
            size = _embed_chars(row)
            field_count = _embed_field_count(row)
            over_char_budget = current and used + size > MAX_EMBED_CHARS
            over_count_budget = len(current) >= MAX_EMBEDS
            over_field_budget = current and fields_used + field_count > MAX_FIELDS_PER_MESSAGE
            if over_char_budget or over_count_budget or over_field_budget:
                batches.append(current)
                current = []
                used = 0
                fields_used = 0
            current.append(row)
            used += size
            fields_used += field_count
    if current:
        batches.append(current)
    return batches


def split_timeframe_payloads(payload: Mapping[str, object]) -> list[dict]:
    """1つのブリーフィングpayloadを、Discord上限内の複数payloadへ分割する。

    embedを削らず`_batch_embeds`で分け、各メッセージへ同じusernameを付ける。
    contentは(長さ制限のある)先頭メッセージにだけ載せ、2通目以降には付けない。
    embedが上限内に収まる通常時は1件のリストを返す(既存挙動と同じ送信結果)。
    """

    content = payload.get("content")
    embeds = payload.get("embeds")
    embed_list = list(embeds) if isinstance(embeds, Sequence) else []
    if not embed_list:
        single = {key: value for key, value in payload.items() if key != "embeds"}
        if content is not None:
            single["content"] = content
        return [single]

    batches = _batch_embeds(embed_list)
    payloads: list[dict] = []
    passthrough = {key: value for key, value in payload.items() if key not in {"content", "embeds"}}
    for index, batch in enumerate(batches):
        message: dict[str, object] = {**passthrough, "embeds": batch}
        if index == 0 and content is not None:
            message["content"] = content
        payloads.append(message)
    return payloads


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
    """時間足別判断を、根拠・反証・運用制限付きのDiscord通知にする。"""
    now = now or datetime.now(UTC)
    aux_reports_by_symbol = aux_reports_by_symbol or {}
    engine = {"claude": "Claude分析", "analyst": "自前分析エンジン", "lexicon": "語彙分析"}.get(
        analysis.engine, analysis.engine
    )
    regime = REGIME_HINT_JA.get(analysis.regime, analysis.regime_ja)
    summaries = [
        _summary_line(symbol, plans, analysis) for symbol, plans in plans_by_symbol.items()
    ]
    candidate = any("条件成立後" in line for line in summaries)
    conclusion = (
        "条件付き候補あり。ただし即時エントリーは避ける"
        if candidate
        else "新規エントリーは見送り優先"
    )
    content = (
        f"📊 **FX統合ブリーフィング｜{now.astimezone(JST):%m/%d %H:%M} JST**\n"
        f"対象：{' / '.join(plans_by_symbol)}\n"
        f"結論：**{conclusion}**\n"
        f"地合い：{regime}（{engine}）\n\n" + "\n".join(summaries)
    )
    sentiments = _sentiment_lines(analysis, currencies)
    if sentiments != "データなし":
        sentiments += "\n※ニュースから測った通貨の強さ。+1.0に近いほど買われやすい"
    strength = sorted(
        (analysis.currencies[ccy] for ccy in currencies if ccy in analysis.currencies),
        key=lambda item: item.score,
        reverse=True,
    )
    actions = [_action(symbol, plans, analysis) for symbol, plans in plans_by_symbol.items()]
    actions.append("**本日の基本方針**：期待値の低い取引を避けることを優先します。")
    reliability = learning_note.strip() or "成熟サンプル不足のため確率校正は未完成です。"
    reliability += "\n⚠️ 確信度は実確率ではなく、分析根拠の一致度です。"
    macro_fields = [
        {"name": "🎯 総合判断", "value": _clip("\n".join(summaries), MAX_FIELD), "inline": False},
        {
            "name": "現在の相対的な通貨強弱",
            "value": "**" + " ＞ ".join(item.currency for item in strength) + "**",
            "inline": False,
        },
    ]
    if analysis.summary:
        macro_fields.append({"name": "市況要約", "value": analysis.summary, "inline": False})
    macro_fields.extend(
        [
            {"name": "🌍 マクロ・センチメント", "value": sentiments, "inline": False},
            {
                "name": "🗓️ 今後48時間の重要イベント",
                "value": _clip(_events_lines(list(events_48h)), MAX_FIELD),
                "inline": False,
            },
        ]
    )
    if journal_note:
        macro_fields.append(
            {"name": "判断の検証（自己採点）", "value": journal_note, "inline": False}
        )
    macro_fields.extend(
        [
            {
                "name": "🧠 自動学習の信頼性",
                "value": _clip(reliability, MAX_FIELD),
                "inline": False,
            },
            {
                "name": "⚠️ データ品質・運用制限",
                "value": _quality_note(plans_by_symbol, fetch_warnings),
                "inline": False,
            },
            {
                "name": "✅ 最終アクション",
                "value": _clip("\n\n".join(actions), MAX_FIELD),
                "inline": False,
            },
        ]
    )
    embeds = [
        {
            "title": "マクロ・センチメント概況",
            "description": "結論 → 根拠 → 反証 → 運用制限の順で確認します。",
            "color": COLOR_NEUTRAL,
            "fields": macro_fields,
            "footer": {"text": "fx-codex 統合ブリーフィング | research/shadow only"},
            "timestamp": now.isoformat(),
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
        "content": _clip(content, MAX_CONTENT),
        "embeds": embeds,
    }
