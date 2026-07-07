"""Detailed notice journal tests."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from fx_intel import notice_journal
from fx_intel.briefing import TradePlan
from fx_intel.calendar import EconomicEvent
from fx_intel.market_structure import EntryLevels
from fx_intel.notice_renderer import render_notice_markdown
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.trade_notice import build_detailed_notice

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _view(interval: str, recommendation: str):
    summary = {"RECOMMENDATION": recommendation, "BUY": 12, "SELL": 5, "NEUTRAL": 9}
    indicators = {
        "close": 162.296,
        "RSI": 57.0,
        "ADX": 24.0,
        "ATR": 0.153,
        "SMA20": 162.20,
        "SMA100": 162.40,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _notice():
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": _view("15m", "BUY"),
        "1h": _view("1h", "BUY"),
        "4h": _view("4h", "STRONG_BUY"),
        "1d": _view("1d", "STRONG_BUY"),
    }
    plan = TradePlan(
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
    )
    analysis = MarketAnalysis(
        engine="analyst",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", score=0.2),
            "JPY": CurrencySentiment("JPY", score=-0.1),
        },
    )
    events = [EconomicEvent("ISM Services PMI", "USD", NOW + timedelta(hours=2), "high")]
    levels = EntryLevels(
        "USDJPY",
        "long",
        162.20,
        162.30,
        162.35,
        162.45,
        162.20,
        162.45,
        162.10,
        162.50,
        "recent_ohlc",
        48,
    )
    return build_detailed_notice(plan, tech, analysis, events, now=NOW, entry_levels=levels), levels


def test_append_detailed_notices_records_hash_and_conditions(tmp_path) -> None:
    notice, levels = _notice()
    text = render_notice_markdown(notice)
    path = tmp_path / "trade_notice_journal.jsonl"

    notice_journal.append_detailed_notices(
        path,
        [notice],
        report_text=text,
        entry_levels_by_symbol={"USDJPY": levels},
        chunk_count=3,
        delivery="discord",
        now=NOW,
    )

    rows = list(notice_journal.read_notice_entries(path))
    assert len(rows) == 1
    row = rows[0]
    assert row["schema"] == notice_journal.SCHEMA_VERSION
    assert row["symbol"] == "USDJPY"
    assert row["direction"] == "long"
    assert row["conviction"] == 52
    assert row["report_sha256"] == notice_journal.report_hash(text)
    assert row["report_chars"] == len(text)
    assert row["chunk_count"] == 3
    assert row["important_event"]["title"] == "ISM Services PMI"
    assert row["no_entry_window"]["start"].endswith("13:30:00+00:00")
    assert row["entry_level_source"]["source"] == "recent_ohlc"
    assert row["entry_level_source"]["breakout_level"] == 162.45
    assert row["entry_scenarios"][0]["title"] == "押し目ロング条件"
    assert "USD/JPY 分析通知" not in str(row)  # full body is not stored


def test_append_detailed_notices_marks_atr_fallback_when_no_levels(tmp_path) -> None:
    notice, _levels = _notice()
    path = tmp_path / "journal.jsonl"

    notice_journal.append_detailed_notices(path, [notice], report_text="body", now=NOW)

    row = list(notice_journal.read_notice_entries(path))[0]
    assert row["entry_level_source"] == {"source": "atr_fallback"}


def test_read_notice_entries_skips_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"ok": true}\nnot-json\n[]\n', encoding="utf-8")

    rows = list(notice_journal.read_notice_entries(path))

    assert rows == [{"ok": True}]


def test_load_notice_quality_bars_uses_backtester_csv_schema(tmp_path) -> None:
    from fx_briefing import load_notice_quality_bars

    csv_path = tmp_path / "USDJPY.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,symbol,open,high,low,close",
                "2026-07-06 12:15:00,USDJPY,162.30,162.70,162.20,162.60",
            ]
        ),
        encoding="utf-8",
    )

    bars = load_notice_quality_bars([csv_path])

    assert list(bars) == ["USDJPY"]
    assert bars["USDJPY"][0].high == 162.70
