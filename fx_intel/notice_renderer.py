"""Render detailed trade notice models to Markdown."""

from __future__ import annotations

from collections.abc import Sequence

from .briefing import format_price
from .trade_notice import DetailedTradeNotice, JST

SECTION = "━━━━━━━━━━━━━━"


def _fmt_dt(moment) -> str:
    return moment.astimezone(JST).strftime("%Y/%m/%d %H:%M JST")


def _fmt_hm(moment) -> str:
    return moment.astimezone(JST).strftime("%H:%M")


def _price(notice: DetailedTradeNotice, value: float | None) -> str:
    return format_price(notice.symbol, value)


def _symbol_label(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "")
    if len(cleaned) == 6:
        return f"{cleaned[:3]}/{cleaned[3:]}"
    return symbol


def _pips(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}pips"


def _rr(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _bullet(lines: Sequence[str]) -> str:
    return "\n".join(f"・{line}" for line in lines) if lines else "・なし"


def _section(title: str, body: str) -> str:
    return f"{SECTION}\n【{title}】\n{SECTION}\n\n{body.strip()}"


def render_notice_markdown(notice: DetailedTradeNotice) -> str:
    """Render a single notice as Japanese Markdown/plain text."""
    event = notice.important_event
    no_entry = notice.no_entry_window
    header_lines = [
        f"🟡 {_symbol_label(notice.symbol)} 分析通知 — {notice.header_label}",
        "",
        f"確信度：{notice.conviction}/100",
        "",
        f"判定：{notice.stance_label}",
        "",
        f"現在値：{_price(notice, notice.current_price)}",
        "",
        f"分析時刻：{_fmt_dt(notice.analyzed_at)}",
        "",
        f"有効期限：{_fmt_dt(notice.valid_until)}まで" if notice.valid_until else "有効期限：—",
    ]
    if event is not None:
        header_lines.extend(
            [
                "",
                f"重要イベント：{event.currency} {event.title}",
                "",
                f"発表時刻：{_fmt_dt(event.when)}",
            ]
        )
    if no_entry is not None:
        header_lines.extend(
            [
                "",
                f"新規エントリー禁止時間：{_fmt_hm(no_entry.start)}〜{_fmt_hm(no_entry.end)} JST",
            ]
        )

    conclusion = "結論：\n\n" + "\n\n".join(notice.conclusion_lines)

    price_plan = notice.price_plan
    summary = _section(
        "総合判定",
        "\n".join(
            [
                f"方向：{notice.header_label}",
                f"確信度：{notice.conviction}/100",
                f"推奨スタンス：{notice.stance_label}",
                "新規成行：非推奨" if notice.direction in ("long", "short") else "新規成行：禁止",
                (
                    "イベント直前：新規エントリー禁止"
                    if no_entry
                    else "イベント直前：該当イベントなし"
                ),
                f"シナリオ無効ライン：{_price(notice, notice.invalidation_line)}",
                f"優先判断：{notice.priority}",
                "",
                f"この{notice.conviction}/100は勝率ではありません。",
                "テクニカル、ニュース、イベントリスク、介入警戒を統合した方向優位性スコアです。",
                "50を少し上回る程度なら、方向感はあってもエントリー品質が低ければ見送りが正解です。",
            ]
        ),
    )

    grounds = _section(
        "根拠サマリー",
        "\n".join(
            [
                "強気材料：",
                _bullet(notice.bullish_factors),
                "",
                "弱気・警戒材料：",
                _bullet(notice.caution_factors),
                "",
                "判断：",
                "方向は出ていますが、飛び乗りではなく確認型エントリーのみ有効です。",
            ]
        ),
    )

    tf_lines = [f"{item.timeframe}：{item.label_ja}" for item in notice.timeframe_assessments] or [
        "テクニカル取得失敗"
    ]
    timeframes = _section(
        "時間足別評価",
        "\n".join(
            [
                *tf_lines,
                "",
                "MTFは方向判断の補助材料です。",
                "短期MAやイベントリスクが残る場合、直近高値追い/安値追いは期待値が落ちます。",
            ]
        ),
    )

    trade_plan = _section(
        "売買プラン",
        "\n".join(
            [
                f"現在値：{_price(notice, price_plan.current)}",
                f"損切り：{_price(notice, price_plan.stop)}",
                f"第1利確：{_price(notice, price_plan.target1)}",
                f"第2利確：{_price(notice, price_plan.target2)}",
                "",
                f"損切り幅：{_pips(price_plan.stop_pips)}",
                f"第1利確まで：{_pips(price_plan.target1_pips)}",
                f"第2利確まで：{_pips(price_plan.target2_pips)}",
                f"R:R：T1 = {_rr(price_plan.rr_t1)} / T2 = {_rr(price_plan.rr_t2)}",
                f"ATR：{_price(notice, price_plan.atr)}",
                (
                    f"SL幅：約{price_plan.stop_atr_multiple:.1f}ATR"
                    if price_plan.stop_atr_multiple is not None
                    else "SL幅：—"
                ),
                "",
                "通常ノイズに対して狭すぎる設定か、イベント前後に耐えられる設定かを必ず分けて判断します。",
            ]
        ),
    )

    scenario_blocks = []
    for index, scenario in enumerate(notice.entry_scenarios, start=1):
        scenario_blocks.append(
            "\n".join(
                [
                    f"{index}. {scenario.title}",
                    scenario.trigger,
                    scenario.confirmation,
                    "",
                    scenario.entry,
                    scenario.stop,
                    scenario.targets,
                    scenario.invalidation,
                ]
            )
        )
    if not scenario_blocks:
        scenario_blocks.append(
            "方向判断またはATR/SLが不足しているため、今回は新規エントリー条件を提示しません。"
        )
    entries = _section(
        "エントリー条件",
        "\n\n".join(
            [
                "以下の条件を満たす場合のみエントリー検討。",
                *scenario_blocks,
                "",
                "禁止事項：",
                _bullet(notice.forbidden_actions),
            ]
        ),
    )

    sizing = notice.position_sizing
    position = _section(
        "ポジションサイズ",
        "\n".join(
            [
                f"1トレードの最大損失は口座資金の{sizing.risk_pct_min:g}〜{sizing.risk_pct_max:g}%以内。",
                "重要イベント前は通常より小さめのロットを推奨。",
                "",
                "ロット計算式：",
                sizing.formula,
                "",
                "例：",
                sizing.example,
                "",
                "T1到達時：半分利確。",
                "残りは建値付近までSLを引き上げ、T2を狙う。",
            ]
        ),
    )

    skip = _section(
        "見送り条件", "以下のどれかに該当すれば見送り。\n\n" + _bullet(notice.skip_conditions)
    )

    event_section = ""
    if notice.event_playbook is not None:
        playbook = notice.event_playbook
        event_section = _section(
            "イベント対応シナリオ",
            "\n".join(
                [
                    "発表前：",
                    playbook.before,
                    "",
                    "発表直後：",
                    playbook.after,
                    "",
                    "結果が強い場合：",
                    playbook.strong,
                    "",
                    "結果が弱い場合：",
                    playbook.weak,
                    "",
                    "結果がまちまちの場合：",
                    playbook.mixed,
                ]
            ),
        )

    fundamentals = _section(
        "ニュース・ファンダメンタル評価",
        "\n\n".join(notice.fundamental_summary),
    )

    final = _section(
        "最終判断",
        "\n".join(
            [
                notice.final_evaluation,
                "",
                "推奨アクション：",
                _bullet(notice.final_actions),
                "",
                "最終評価：",
                notice.final_evaluation,
                "条件未達なら見送りが正解です。",
            ]
        ),
    )

    sections = [
        "\n".join(header_lines),
        conclusion,
        summary,
        grounds,
        timeframes,
        trade_plan,
        entries,
        position,
        skip,
        event_section,
        fundamentals,
        final,
    ]
    return "\n\n".join(section for section in sections if section).strip()


def render_notices_markdown(notices: Sequence[DetailedTradeNotice]) -> str:
    """Render one or more notices into a single report body."""
    return "\n\n\n".join(render_notice_markdown(notice) for notice in notices)
