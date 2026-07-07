"""Detailed trade notice tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel.briefing import TradePlan
from fx_intel.calendar import EconomicEvent
from fx_intel.discord_delivery import build_discord_text_payloads, split_markdown_for_discord
from fx_intel.market_structure import OhlcBar, build_entry_levels
from fx_intel.notice_renderer import render_notice_markdown
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.trade_notice import build_detailed_notice

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # 21:00 JST


def _view(interval: str, recommendation: str, close: float = 162.296):
    summary = {"RECOMMENDATION": recommendation, "BUY": 12, "SELL": 5, "NEUTRAL": 9}
    indicators = {
        "close": close,
        "RSI": 57.0,
        "ADX": 24.0,
        "ATR": 0.153,
        "SMA20": 162.20,
        "SMA100": 162.40,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": _view("15m", "BUY"),
        "1h": _view("1h", "BUY"),
        "4h": _view("4h", "STRONG_BUY"),
        "1d": _view("1d", "STRONG_BUY"),
    }
    return tech


def _plan() -> TradePlan:
    return TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=52,
        composite=0.52,
        tech_score=0.55,
        news_score=0.10,
        close=162.296,
        atr=0.153,
        stop=161.914,
        target1=162.678,
        target2=163.060,
        data_quality=0.8,
        interval_summary="15m 買い | 1h 買い | 4h 強い買い | 1d 強い買い",
        ma_note="移動平均線MA(20/100): デッドクロス(売りが優勢のサイン)",
    )


def _analysis() -> MarketAnalysis:
    return MarketAnalysis(
        engine="analyst",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", score=0.2, headline_count=3),
            "JPY": CurrencySentiment("JPY", score=-0.1, headline_count=2),
        },
        summary="",
    )


def _events() -> list[EconomicEvent]:
    return [
        EconomicEvent(
            "ISM Services PMI",
            "USD",
            NOW + timedelta(hours=2),  # 23:00 JST
            "high",
        )
    ]


def _ohlc_bars() -> list[OhlcBar]:
    lows = [162.34, 162.31, 162.28, 162.24, 162.20, 162.23, 162.27, 162.30]
    highs = [162.38, 162.39, 162.41, 162.43, 162.40, 162.42, 162.45, 162.37]
    bars: list[OhlcBar] = []
    for index, (low, high) in enumerate(zip(lows, highs, strict=True)):
        close = (low + high) / 2
        bars.append(
            OhlcBar(
                timestamp=NOW - timedelta(minutes=(len(lows) - index) * 15),
                open=close,
                high=high,
                low=low,
                close=close,
            )
        )
    return bars


def test_build_detailed_notice_low_conviction_long_is_conditional() -> None:
    notice = build_detailed_notice(_plan(), _tech(), _analysis(), _events(), now=NOW)

    assert notice.header_label == "小幅ロングバイアス / 条件付き"
    assert notice.stance_label == "見送り寄りの条件付きロング"
    assert notice.valid_until == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    assert notice.no_entry_window is not None
    assert notice.no_entry_window.start == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    assert notice.no_entry_window.end == datetime(2026, 7, 6, 14, 15, tzinfo=UTC)
    assert notice.price_plan.stop_pips == pytest.approx(38.2)
    assert notice.price_plan.rr_t1 == pytest.approx(1.0)
    assert notice.price_plan.rr_t2 == pytest.approx(2.0)
    assert [scenario.title for scenario in notice.entry_scenarios] == [
        "押し目ロング条件",
        "ブレイク維持ロング条件",
    ]


def test_market_structure_entry_levels_override_atr_fallback() -> None:
    levels = build_entry_levels(
        "USDJPY",
        "long",
        _ohlc_bars(),
        current_price=162.296,
        atr=0.153,
        lookback_bars=8,
    )
    assert levels is not None
    assert levels.breakout_level == pytest.approx(162.43)

    notice = build_detailed_notice(
        _plan(), _tech(), _analysis(), _events(), now=NOW, entry_levels=levels
    )
    text = render_notice_markdown(notice)

    assert "162.430を上抜け" in text
    assert "直近OHLC8本から抽出" in text
    assert "ATRベースの暫定ライン" not in text


def test_expectancy_guard_warning_shapes_detailed_notice_execution() -> None:
    guarded_plan = replace(
        _plan(),
        conviction=23,
        warnings=["📉 期待値ガード: 通貨ペア USDJPYの期待Rは-0.20Rで非正"],
    )

    notice = build_detailed_notice(guarded_plan, _tech(), _analysis(), [], now=NOW)

    assert any("期待値ガード" in item for item in notice.caution_factors)
    assert notice.final_actions[0].startswith("期待値ガード")
    assert "見送り優先" in notice.final_evaluation


def test_notice_ohlc_loader_ignores_future_rows(tmp_path) -> None:
    from fx_briefing import load_notice_entry_levels

    csv_path = tmp_path / "USDJPY.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,symbol,open,high,low,close",
                "2026-07-06 10:00:00,USDJPY,162.30,162.38,162.25,162.31",
                "2026-07-06 10:15:00,USDJPY,162.31,162.40,162.20,162.30",
                "2026-07-06 10:30:00,USDJPY,162.30,162.42,162.24,162.32",
                "2026-07-06 10:45:00,USDJPY,162.32,162.45,162.27,162.34",
                "2026-07-06 11:00:00,USDJPY,162.34,162.39,162.29,162.33",
                "2026-07-06 12:00:00,USDJPY,162.33,162.41,162.28,162.35",
                "2026-07-06 15:00:00,USDJPY,162.35,200.00,162.30,199.00",
            ]
        ),
        encoding="utf-8",
    )
    warnings: list[str] = []
    levels = load_notice_entry_levels([csv_path], [_plan()], NOW, 12, warnings)

    assert warnings == []
    assert levels["USDJPY"].recent_high < 200.0


def test_render_notice_contains_required_sections_and_event_times() -> None:
    notice = build_detailed_notice(_plan(), _tech(), _analysis(), _events(), now=NOW)
    text = render_notice_markdown(notice)

    assert "USD/JPY 分析通知" in text
    assert "確信度：52/100" in text
    assert "判定：見送り寄りの条件付きロング" in text
    assert "新規エントリー禁止時間：22:30〜23:15 JST" in text
    assert "この52/100は勝率ではありません" in text
    assert "【エントリー条件】" in text
    assert "押し目ロング条件" in text
    assert "ブレイク維持ロング条件" in text
    assert "【イベント対応シナリオ】" in text
    assert "未確認ニュース" in text


def test_discord_delivery_splits_long_markdown_under_limit() -> None:
    text = "見出し\n\n" + "\n\n".join(f"段落{i} " + ("x" * 300) for i in range(20))
    chunks = split_markdown_for_discord(text, limit=900)
    payloads = build_discord_text_payloads(text, limit=900)

    assert len(chunks) > 1
    assert len(payloads) == len(chunks)
    assert all(len(payload["content"]) <= 2000 for payload in payloads)
    assert payloads[-1]["content"].endswith(f"({len(payloads)}/{len(payloads)})")
