"""Build structured, long-form trade notices from existing trade plans.

This module intentionally does not fetch data and does not decide direction.
It turns a verified ``briefing.TradePlan`` plus already-fetched technical and
calendar context into a deterministic report model that can be rendered,
tested, journaled, and sent to Discord in chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, UTC
from collections.abc import Mapping, Sequence

from .briefing import TradePlan, format_price
from .calendar import EconomicEvent, symbol_currencies
from .market_structure import EntryLevels
from .notice_policy import NoticePolicy
from .sentiment import MarketAnalysis
from .technicals import PairTechnicals

JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class EventNotice:
    title: str
    currency: str
    when: datetime
    impact: str
    forecast: str = ""
    previous: str = ""


@dataclass(frozen=True)
class NoEntryWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PricePlan:
    current: float | None
    stop: float | None
    target1: float | None
    target2: float | None
    atr: float | None
    stop_pips: float | None
    target1_pips: float | None
    target2_pips: float | None
    rr_t1: float | None
    rr_t2: float | None
    stop_atr_multiple: float | None


@dataclass(frozen=True)
class TimeframeAssessment:
    timeframe: str
    label_ja: str
    score: float


@dataclass(frozen=True)
class EntryScenario:
    title: str
    trigger: str
    confirmation: str
    entry: str
    stop: str
    targets: str
    invalidation: str


@dataclass(frozen=True)
class PositionSizingGuide:
    risk_pct_min: float
    risk_pct_max: float
    stop_pips: float | None
    formula: str
    example: str


@dataclass(frozen=True)
class EventPlaybook:
    before: str
    after: str
    strong: str
    weak: str
    mixed: str


@dataclass(frozen=True)
class DetailedTradeNotice:
    symbol: str
    header_label: str
    stance_label: str
    conviction: int
    analyzed_at: datetime
    valid_until: datetime | None
    important_event: EventNotice | None
    no_entry_window: NoEntryWindow | None
    direction: str
    current_price: float | None
    invalidation_line: float | None
    priority: str
    conclusion_lines: list[str]
    bullish_factors: list[str]
    caution_factors: list[str]
    timeframe_assessments: list[TimeframeAssessment]
    price_plan: PricePlan
    entry_scenarios: list[EntryScenario]
    forbidden_actions: list[str]
    position_sizing: PositionSizingGuide
    skip_conditions: list[str]
    event_playbook: EventPlaybook | None
    fundamental_summary: list[str]
    final_actions: list[str]
    final_evaluation: str
    warnings: list[str] = field(default_factory=list)


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


def _pips(symbol: str, distance: float | None) -> float | None:
    if distance is None:
        return None
    return round(abs(distance) / _pip_size(symbol), 1)


def _fmt_time(moment: datetime) -> str:
    return moment.astimezone(JST).strftime("%Y/%m/%d %H:%M JST")


def _fmt_hm(moment: datetime) -> str:
    return moment.astimezone(JST).strftime("%H:%M")


def _direction_word(direction: str) -> str:
    return {
        "long": "ロング",
        "short": "ショート",
        "neutral": "見送り",
        "standby": "様子見",
        "closed": "休場",
    }.get(direction, direction)


def _display_symbol(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "")
    if len(cleaned) == 6:
        return f"{cleaned[:3]}/{cleaned[3:]}"
    return symbol


def _header_label(plan: TradePlan, policy: NoticePolicy) -> str:
    word = _direction_word(plan.direction)
    if plan.direction in ("neutral", "standby", "closed"):
        return word
    if plan.conviction <= policy.low_conviction_max:
        return f"小幅{word}バイアス / 条件付き"
    if plan.conviction >= policy.strong_conviction_min:
        return f"{word}優勢 / 確認型"
    return f"{word}バイアス / 条件付き"


def _stance_label(plan: TradePlan, policy: NoticePolicy) -> str:
    word = _direction_word(plan.direction)
    if plan.direction == "neutral":
        return "見送り"
    if plan.direction == "standby":
        return "イベント前後の様子見"
    if plan.direction == "closed":
        return "市場再開待ち"
    if plan.conviction <= policy.low_conviction_max:
        return f"見送り寄りの条件付き{word}"
    return f"条件達成時のみ{word}検討"


def _related_event(
    symbol: str,
    events: Sequence[EconomicEvent],
    now: datetime,
) -> EconomicEvent | None:
    base, quote = symbol_currencies(symbol)
    currencies = {base, quote}
    candidates = [
        event
        for event in events
        if event.currency in currencies and event.when >= now and event.impact_rank >= 3
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda event: event.when)


def _event_notice(event: EconomicEvent | None) -> EventNotice | None:
    if event is None:
        return None
    return EventNotice(
        title=event.title,
        currency=event.currency,
        when=event.when,
        impact=event.impact,
        forecast=event.forecast,
        previous=event.previous,
    )


def _notice_no_entry_window(
    event: EconomicEvent | None, policy: NoticePolicy
) -> NoEntryWindow | None:
    if event is None:
        return None
    return NoEntryWindow(
        start=event.when - timedelta(minutes=policy.no_entry_minutes_before_event),
        end=event.when + timedelta(minutes=policy.no_entry_minutes_after_event),
    )


def _valid_until(now: datetime, no_entry: NoEntryWindow | None, policy: NoticePolicy) -> datetime:
    default_until = now + timedelta(hours=policy.default_valid_hours)
    if no_entry is not None and no_entry.start > now:
        return min(default_until, no_entry.start)
    return default_until


def _price_plan(plan: TradePlan) -> PricePlan:
    stop_distance = None
    target1_distance = None
    target2_distance = None
    if plan.close is not None and plan.stop is not None:
        stop_distance = abs(plan.close - plan.stop)
    if plan.close is not None and plan.target1 is not None:
        target1_distance = abs(plan.target1 - plan.close)
    if plan.close is not None and plan.target2 is not None:
        target2_distance = abs(plan.target2 - plan.close)

    stop_pips = _pips(plan.symbol, stop_distance)
    target1_pips = _pips(plan.symbol, target1_distance)
    target2_pips = _pips(plan.symbol, target2_distance)
    rr_t1 = (
        round(target1_distance / stop_distance, 2)
        if stop_distance and target1_distance is not None
        else None
    )
    rr_t2 = (
        round(target2_distance / stop_distance, 2)
        if stop_distance and target2_distance is not None
        else None
    )
    stop_atr_multiple = (
        round(stop_distance / plan.atr, 2) if stop_distance and plan.atr and plan.atr > 0 else None
    )
    return PricePlan(
        current=plan.close,
        stop=plan.stop,
        target1=plan.target1,
        target2=plan.target2,
        atr=plan.atr,
        stop_pips=stop_pips,
        target1_pips=target1_pips,
        target2_pips=target2_pips,
        rr_t1=rr_t1,
        rr_t2=rr_t2,
        stop_atr_multiple=stop_atr_multiple,
    )


def _timeframe_assessments(tech: PairTechnicals) -> list[TimeframeAssessment]:
    output: list[TimeframeAssessment] = []
    for timeframe in ("15m", "1h", "4h", "1d"):
        view = tech.views.get(timeframe)
        if view is not None:
            output.append(TimeframeAssessment(timeframe, view.recommendation_ja, view.score))
    return output


def _level(symbol: str, value: float | None) -> str:
    return format_price(symbol, value)


def _entry_scenarios(
    plan: TradePlan, policy: NoticePolicy, entry_levels: EntryLevels | None = None
) -> list[EntryScenario]:
    if plan.direction not in ("long", "short") or plan.close is None or plan.atr is None:
        return []
    if plan.stop is None:
        return []

    sign = 1.0 if plan.direction == "long" else -1.0
    if entry_levels is not None and entry_levels.direction == plan.direction:
        pullback_low = entry_levels.pullback_low
        pullback_high = entry_levels.pullback_high
        reclaim = entry_levels.reclaim_level
        breakout = entry_levels.breakout_level
        source_note = f"直近OHLC{entry_levels.bars_used}本から抽出。"
    else:
        pullback_outer = plan.close - sign * plan.atr * policy.pullback_atr_fraction
        pullback_low = min(pullback_outer, plan.close)
        pullback_high = max(pullback_outer, plan.close)
        reclaim = plan.close + sign * plan.atr * policy.reclaim_atr_fraction
        breakout = plan.close + sign * plan.atr * policy.breakout_atr_fraction
        source_note = "ATRベースの暫定ライン。"
    word = _direction_word(plan.direction)
    pullback_title = "押し目ロング条件" if plan.direction == "long" else "戻り売りショート条件"
    breakout_title = (
        "ブレイク維持ロング条件" if plan.direction == "long" else "ブレイク維持ショート条件"
    )
    hold_phrase = "下げ止まり" if plan.direction == "long" else "上げ止まり"
    reclaim_phrase = "再上抜け" if plan.direction == "long" else "再下抜け"
    breakout_phrase = "上抜け" if plan.direction == "long" else "下抜け"
    reject_phrase = "すぐ押し戻される" if plan.direction == "long" else "すぐ買い戻される"

    pullback_zone = f"{_level(plan.symbol, pullback_low)}〜{_level(plan.symbol, pullback_high)}"
    targets = f"T1：{_level(plan.symbol, plan.target1)} / T2：{_level(plan.symbol, plan.target2)}"
    return [
        EntryScenario(
            title=pullback_title,
            trigger=f"{pullback_zone}付近で{hold_phrase}を確認。その後、{_level(plan.symbol, reclaim)}を{reclaim_phrase}。{source_note}",
            confirmation=(
                "15分足で下ヒゲ、陽線反転、または戻り高値更新が確認できること。"
                if plan.direction == "long"
                else "15分足で上ヒゲ、陰線反転、または戻り安値更新が確認できること。"
            ),
            entry=f"想定エントリー：{_level(plan.symbol, reclaim)}前後",
            stop=f"SL：{_level(plan.symbol, plan.stop)}",
            targets=targets,
            invalidation=f"{_level(plan.symbol, plan.stop)}割れで{word}シナリオ無効",
        ),
        EntryScenario(
            title=breakout_title,
            trigger=f"{_level(plan.symbol, breakout)}を{breakout_phrase}。その後、その水準付近を維持。",
            confirmation=(
                f"{_level(plan.symbol, breakout)}付近で{reject_phrase}場合は見送り。"
                "15分足確定、または押し戻し後の再上昇を確認。"
                if plan.direction == "long"
                else f"{_level(plan.symbol, breakout)}付近で{reject_phrase}場合は見送り。"
                "15分足確定、または買い戻し後の再下落を確認。"
            ),
            entry=f"想定エントリー：{_level(plan.symbol, breakout)}付近",
            stop=f"SL：{_level(plan.symbol, plan.stop)}",
            targets=targets,
            invalidation=f"{_level(plan.symbol, plan.stop)}到達で{word}シナリオ無効",
        ),
    ]


def _forbidden_actions(plan: TradePlan, no_entry: NoEntryWindow | None) -> list[str]:
    word = _direction_word(plan.direction)
    if plan.direction not in ("long", "short"):
        return ["方向判断が出ていない状態での新規エントリー禁止"]
    actions = [
        f"現在値からの感情的な成行{word}禁止",
        "ブレイク直後の飛び乗り禁止",
        "スプレッド拡大時のエントリー禁止",
    ]
    if no_entry is not None:
        actions.insert(2, f"{_fmt_hm(no_entry.start)} JST以降の新規エントリー禁止")
    return actions


def _position_sizing(
    plan: TradePlan, policy: NoticePolicy, price_plan: PricePlan
) -> PositionSizingGuide:
    if price_plan.stop_pips is None or price_plan.stop_pips <= 0:
        formula = "許容損失額 ÷ SL幅(pips) = 許容ロット"
        example = "SL幅が未確定のため、ロットは算出しない。"
    else:
        max_loss = round(policy.example_account_jpy * policy.risk_pct_max / 100)
        yen_per_pip = max_loss / price_plan.stop_pips
        formula = f"許容損失額 ÷ {price_plan.stop_pips:.1f}pips = 許容ロット"
        example = (
            f"{policy.example_account_jpy:,}円口座で損失上限{policy.risk_pct_max:g}%なら、"
            f"最大損失額は{max_loss:,}円。1pipsあたり約{yen_per_pip:.0f}円まで。"
        )
    return PositionSizingGuide(
        risk_pct_min=policy.risk_pct_min,
        risk_pct_max=policy.risk_pct_max,
        stop_pips=price_plan.stop_pips,
        formula=formula,
        example=example,
    )


def _bullish_factors(plan: TradePlan, tech: PairTechnicals) -> list[str]:
    assessments = _timeframe_assessments(tech)
    positives = [item for item in assessments if item.score > 0]
    factors: list[str] = []
    if positives:
        factors.append(
            "、".join(item.timeframe for item in positives) + "足が買い方向"
            if plan.direction == "long"
            else "複数時間足で下方向の圧力がある"
        )
    higher = [item for item in assessments if item.timeframe in ("4h", "1d") and item.score > 0]
    if plan.direction == "long" and higher:
        factors.append("4時間足と日足は上方向の地合いが強い")
    if plan.direction == "long" and plan.symbol.upper().endswith("JPY"):
        factors.append("米日金利差を背景に、USD/JPYの上方向圧力は残っている")
    elif plan.direction == "short":
        factors.append("短期の戻りが弱ければ、戻り売りが入りやすい")
    if plan.direction == "long" and plan.close is not None:
        factors.append(
            f"{format_price(plan.symbol, plan.close)}付近で下げが限定的なら、押し目買いが入りやすい"
        )
    return factors or ["方向優位性は限定的で、条件確認が必要"]


def _expectancy_guard_warnings(plan: TradePlan) -> list[str]:
    return [warning for warning in plan.warnings if "期待値ガード" in warning]


def _caution_factors(
    plan: TradePlan,
    tech: PairTechnicals,
    event: EventNotice | None,
    policy: NoticePolicy,
) -> list[str]:
    factors: list[str] = []
    ma_side = tech.ma_side()
    if plan.direction == "long" and ma_side == "short":
        factors.append(f"MA{tech.fast_window}/{tech.slow_window}はデッドクロス")
    elif plan.direction == "short" and ma_side == "long":
        factors.append(f"MA{tech.fast_window}/{tech.slow_window}はゴールデンクロス")
    if plan.conviction <= policy.low_conviction_max:
        factors.append("確信度はほぼ中立に近く、強いシグナルではない")
    if (
        plan.symbol.upper() == "USDJPY"
        and plan.close is not None
        and plan.close >= policy.jpy_intervention_warning_level
    ):
        factors.append("160円台は介入警戒が強い水準")
    if event is not None:
        factors.append(f"{event.title}前で、発表前後に急変動しやすい")
        factors.append("イベント前はスプレッド拡大、初動のダマシ、逆指値滑りのリスクがある")
    factors.extend(
        warning for warning in plan.warnings if "学習調整" in warning or "期待値ガード" in warning
    )
    return factors or ["短期の形が崩れた場合は見送り"]


def _skip_conditions(
    plan: TradePlan, no_entry: NoEntryWindow | None, event: EventNotice | None
) -> list[str]:
    conditions: list[str] = []
    if plan.stop is not None:
        conditions.append(
            f"{format_price(plan.symbol, plan.stop)}を下抜ける"
            if plan.direction == "long"
            else f"{format_price(plan.symbol, plan.stop)}を上抜ける"
        )
    if plan.close is not None and plan.atr is not None and plan.direction == "long":
        conditions.append(
            f"{format_price(plan.symbol, plan.close - plan.atr * 0.65)}を維持できない"
        )
        conditions.append("ブレイク後にすぐ押し戻される")
    elif plan.close is not None and plan.atr is not None and plan.direction == "short":
        conditions.append(f"{format_price(plan.symbol, plan.close + plan.atr * 0.65)}を突破される")
        conditions.append("下抜け後にすぐ買い戻される")
    if no_entry is not None:
        conditions.append(f"{_fmt_hm(no_entry.start)} JST以降に新規エントリーする必要がある")
    if plan.symbol.upper().startswith("USD"):
        conditions.append(
            "米金利が急低下する" if plan.direction == "long" else "米金利が急上昇する"
        )
        conditions.append("DXYが急落する" if plan.direction == "long" else "DXYが急騰する")
    if "JPY" in plan.symbol.upper():
        conditions.append(
            "円買いニュースが出る" if plan.direction == "long" else "円売りニュースが出る"
        )
        conditions.append("日本当局の介入警戒報道が強まる")
    if event is not None:
        conditions.append("発表前後でスプレッドが通常より明確に広がる")
    conditions.extend(["1分足で急騰急落が連発している", "エントリー根拠が「上がりそう」だけになる"])
    return conditions


def _event_playbook(
    event: EventNotice | None, no_entry: NoEntryWindow | None
) -> EventPlaybook | None:
    if event is None or no_entry is None:
        return None
    is_ism = "ism" in event.title.lower() or "pmi" in event.title.lower()
    strong = (
        "総合PMI、Prices、Employmentが強ければ、米金利上昇を通じてUSD/JPY上昇要因。"
        if is_ism and event.currency == "USD"
        else "結果が予想を明確に上回れば、対象通貨の買い材料。"
    )
    weak = (
        "総合PMI、Employmentが弱ければドル売り・米金利低下になりやすい。"
        if is_ism and event.currency == "USD"
        else "結果が予想を明確に下回れば、対象通貨の売り材料。"
    )
    return EventPlaybook(
        before=(
            f"{_fmt_hm(no_entry.start)}〜{_fmt_hm(event.when)}は新規エントリー禁止。"
            "既存ポジションがある場合、T1未達なら縮小または撤退を優先。"
        ),
        after=(
            f"{_fmt_hm(event.when)}〜{_fmt_hm(no_entry.end)}は新規エントリー禁止。"
            "初動はダマシが出やすいため、15分足確定とスプレッド正常化を待つ。"
        ),
        strong=strong,
        weak=weak,
        mixed=(
            "総合・Prices・Employmentなどの内訳がまちまちの場合は方向が荒れやすい。"
            "無理に入らず、15分足2本分の方向確認を優先。"
        ),
    )


def _fundamental_summary(plan: TradePlan, analysis: MarketAnalysis) -> list[str]:
    lines = [
        f"現在のニュース/ファンダメンタル評価エンジン：{analysis.engine}",
    ]
    if plan.direction == "long" and plan.symbol.upper() == "USDJPY":
        lines.append("米日金利差を背景にドル円の上方向圧力は残っています。")
        lines.append("一方で、円安水準では日本当局の介入警戒が上値を抑えるリスクがあります。")
    elif plan.direction == "short":
        lines.append(
            "短期的には下方向を見ますが、上位足やイベントで反転しやすい局面では確認を優先します。"
        )
    else:
        lines.append("方向優位性が不足しているため、無理に売買判断へ変換しません。")
    lines.append("未確認ニュースや出典確認が取れていない見通し変更は主材料にしません。")
    return lines


def _final_actions(
    plan: TradePlan, scenarios: Sequence[EntryScenario], no_entry: NoEntryWindow | None
) -> list[str]:
    if plan.direction not in ("long", "short"):
        return ["条件未達なら見送り", "イベント後は再判定"]
    actions = [scenario.trigger for scenario in scenarios]
    if _expectancy_guard_warnings(plan):
        actions.insert(
            0, "期待値ガード: 過去の期待値が改善する条件確認まで新規エントリーは見送り優先"
        )
    if plan.stop is not None:
        actions.append(f"{format_price(plan.symbol, plan.stop)}到達でシナリオ無効")
    if no_entry is not None:
        actions.append(f"{_fmt_hm(no_entry.start)} JST以降は新規エントリー禁止")
    actions.append("T1到達後は半分利確し、残りは建値付近までSL引き上げ")
    return actions


def _final_evaluation(plan: TradePlan, policy: NoticePolicy) -> str:
    if _expectancy_guard_warnings(plan):
        return "期待値ガードが出ているため、方向目線は参考に留め、今回は見送り優先です。"
    if plan.direction == "long":
        if plan.conviction <= policy.low_conviction_max:
            return "買い目線は維持。ただし、今すぐ買う局面ではありません。"
        return "買い目線は有効。ただし、条件確認後に限定します。"
    if plan.direction == "short":
        if plan.conviction <= policy.low_conviction_max:
            return "売り目線は維持。ただし、今すぐ売る局面ではありません。"
        return "売り目線は有効。ただし、条件確認後に限定します。"
    return "方向感が不足しているため、今回は見送りが優先です。"


def _conclusion_lines(
    plan: TradePlan, no_entry: NoEntryWindow | None, policy: NoticePolicy
) -> list[str]:
    word = _direction_word(plan.direction)
    if plan.direction not in ("long", "short"):
        return ["今回の最適行動は見送りです。", "条件が整うまで新規エントリーは行いません。"]
    lines = [
        f"{_display_symbol(plan.symbol)}は{word}方向にやや優位性があります。",
    ]
    if plan.conviction <= policy.low_conviction_max:
        lines.append(
            f"ただし、確信度{plan.conviction}/100は「強い{word}」ではなく、ほぼ中立に近い小幅バイアスです。"
        )
        lines.append(f"したがって、現在値からの成行{word}は非推奨です。")
    lines.append(
        "今回の最適行動は、条件確認後だけエントリー、またはブレイク維持確認後のエントリーです。"
    )
    if no_entry is not None:
        lines.append("重要イベント前に無理に入る局面ではありません。")
    return lines


def build_detailed_notice(
    plan: TradePlan,
    tech: PairTechnicals,
    analysis: MarketAnalysis,
    events: Sequence[EconomicEvent],
    now: datetime | None = None,
    policy: NoticePolicy | None = None,
    entry_levels: EntryLevels | None = None,
) -> DetailedTradeNotice:
    """Build one detailed notice from an existing plan and context."""
    policy = policy or NoticePolicy()
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    event = _related_event(plan.symbol, events, now)
    no_entry = _notice_no_entry_window(event, policy)
    event_notice = _event_notice(event)
    valid_until = _valid_until(now, no_entry, policy)
    price = _price_plan(plan)
    scenarios = _entry_scenarios(plan, policy, entry_levels)
    return DetailedTradeNotice(
        symbol=plan.symbol,
        header_label=_header_label(plan, policy),
        stance_label=_stance_label(plan, policy),
        conviction=plan.conviction,
        analyzed_at=now,
        valid_until=valid_until,
        important_event=event_notice,
        no_entry_window=no_entry,
        direction=plan.direction,
        current_price=plan.close,
        invalidation_line=plan.stop,
        priority=(
            "利益機会より、重要イベント前の事故回避を優先" if event_notice else "条件達成時のみ実行"
        ),
        conclusion_lines=_conclusion_lines(plan, no_entry, policy),
        bullish_factors=_bullish_factors(plan, tech),
        caution_factors=_caution_factors(plan, tech, event_notice, policy),
        timeframe_assessments=_timeframe_assessments(tech),
        price_plan=price,
        entry_scenarios=scenarios,
        forbidden_actions=_forbidden_actions(plan, no_entry),
        position_sizing=_position_sizing(plan, policy, price),
        skip_conditions=_skip_conditions(plan, no_entry, event_notice),
        event_playbook=_event_playbook(event_notice, no_entry),
        fundamental_summary=_fundamental_summary(plan, analysis),
        final_actions=_final_actions(plan, scenarios, no_entry),
        final_evaluation=_final_evaluation(plan, policy),
        warnings=list(plan.warnings),
    )


def build_detailed_notices(
    plans: Sequence[TradePlan],
    technicals: Mapping[str, PairTechnicals],
    analysis: MarketAnalysis,
    events: Sequence[EconomicEvent],
    now: datetime | None = None,
    policy: NoticePolicy | None = None,
    entry_levels_by_symbol: Mapping[str, EntryLevels] | None = None,
) -> list[DetailedTradeNotice]:
    """Build detailed notices for all available plans."""
    entry_levels_by_symbol = entry_levels_by_symbol or {}
    return [
        build_detailed_notice(
            plan,
            technicals[plan.symbol],
            analysis,
            events,
            now=now,
            policy=policy,
            entry_levels=entry_levels_by_symbol.get(plan.symbol),
        )
        for plan in plans
        if plan.symbol in technicals
    ]
