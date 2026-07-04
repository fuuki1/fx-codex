"""fx_intel パッケージのテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from typing import cast

import pytest
import requests

from fx_intel import briefing
from fx_intel.calendar import (
    EconomicEvent,
    active_and_next_window,
    append_events_archive,
    export_events_csv,
    fetch_calendar,
    parse_calendar_json,
    risk_windows,
    symbol_currencies,
    upcoming_events,
)
from fx_intel.market import is_market_open, open_hours_between
from fx_intel.news import NewsItem, dedupe_and_sort, parse_rss, tag_currencies
from fx_intel.journal import (
    DirectionalStats,
    append_plans,
    evaluate_directional_accuracy,
    format_stats_ja,
)
from fx_intel.sentiment import (
    CurrencySentiment,
    _extract_json_block,
    analyze_market,
    build_analysis_from_claude_json,
    pair_bias,
    score_headlines_lexicon,
)
from fx_intel.technicals import PairTechnicals, build_interval_view

UTC = UTC
NOW = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)


def make_news(title: str, hours_ago: float = 1.0, summary: str = "") -> NewsItem:
    published = NOW - timedelta(hours=hours_ago)
    return NewsItem(
        title=title,
        source="Test",
        link="https://example.com",
        published=published,
        summary=summary,
        currencies=tag_currencies(f"{title} {summary}"),
    )


# ---------------------------------------------------------------- calendar


def test_parse_calendar_json_converts_to_utc() -> None:
    raw = [
        {
            "title": "Non-Farm Employment Change",
            "country": "USD",
            "date": "2026-07-03T08:30:00-04:00",
            "impact": "High",
            "forecast": "110K",
            "previous": "139K",
        },
        {"title": "壊れた行", "country": "JPY", "date": "invalid", "impact": "Low"},
    ]
    events = parse_calendar_json(raw)
    assert len(events) == 1
    event = events[0]
    assert event.when == datetime(2026, 7, 3, 12, 30, tzinfo=UTC)
    assert event.impact == "high"
    assert event.currency == "USD"
    assert event.forecast == "110K"


def test_upcoming_events_filters_currency_horizon_and_impact() -> None:
    events = [
        EconomicEvent("NFP", "USD", NOW + timedelta(hours=6), "high"),
        EconomicEvent("過去", "USD", NOW - timedelta(hours=1), "high"),
        EconomicEvent("遠い", "USD", NOW + timedelta(hours=100), "high"),
        EconomicEvent("対象外通貨", "AUD", NOW + timedelta(hours=6), "high"),
        EconomicEvent("低影響", "USD", NOW + timedelta(hours=6), "low"),
    ]
    selected = upcoming_events(events, {"USD", "JPY"}, NOW, hours_ahead=48, min_impact="high")
    assert [e.title for e in selected] == ["NFP"]


def test_risk_windows_and_active_next() -> None:
    events = [
        EconomicEvent("CPI", "USD", NOW + timedelta(minutes=60), "high"),
        EconomicEvent("後のイベント", "JPY", NOW + timedelta(hours=10), "high"),
        EconomicEvent("低影響は除外", "USD", NOW + timedelta(minutes=30), "low"),
    ]
    windows = risk_windows(events, {"USD", "JPY"}, minutes_before=120, minutes_after=180)
    assert len(windows) == 2

    active, upcoming = active_and_next_window(windows, NOW)
    assert active is not None and active.event.title == "CPI"  # 60分前 < 120分前窓
    assert upcoming is not None and upcoming.event.title == "後のイベント"

    quiet_time = NOW - timedelta(hours=5)
    active, upcoming = active_and_next_window(windows, quiet_time)
    assert active is None
    assert upcoming is not None and upcoming.event.title == "CPI"


def test_export_events_csv_is_loadable_by_backtester(tmp_path) -> None:
    events = [EconomicEvent("NFP", "USD", NOW, "high", forecast="110K", previous="139K")]
    path = export_events_csv(events, tmp_path / "upcoming.csv")

    from fx_backtester.data import load_economic_events_csv

    frame = load_economic_events_csv(path)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["currency"] == "USD"
    assert row["impact"] == "high"
    assert row["name"] == "NFP"


def test_append_events_archive_accumulates_without_duplicates(tmp_path) -> None:
    archive = tmp_path / "event_history.csv"
    nfp = EconomicEvent("NFP", "USD", NOW, "high", forecast="110K", previous="139K")
    cpi = EconomicEvent("CPI y/y", "JPY", NOW + timedelta(hours=6), "medium")

    _, appended = append_events_archive([nfp], archive, now=NOW)
    assert appended == 1
    # 同一内容の再実行では増えない(毎時実行しても膨張しない)
    _, appended = append_events_archive([nfp], archive, now=NOW + timedelta(hours=1))
    assert appended == 0
    # 新イベントは追記され、既存行は残る
    _, appended = append_events_archive([nfp, cpi], archive, now=NOW + timedelta(hours=2))
    assert appended == 1

    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # header + 2行


def test_append_events_archive_keeps_revisions_as_new_rows(tmp_path) -> None:
    archive = tmp_path / "event_history.csv"
    draft = EconomicEvent("NFP", "USD", NOW, "high", forecast="", previous="139K")
    revised = EconomicEvent("NFP", "USD", NOW, "high", forecast="110K", previous="139K")

    append_events_archive([draft], archive, now=NOW)
    _, appended = append_events_archive([revised], archive, now=NOW + timedelta(days=1))
    assert appended == 1  # forecast確定は別内容として履歴に残る(point-in-time記録)

    import csv as csvlib

    with archive.open(encoding="utf-8", newline="") as handle:
        rows = list(csvlib.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["recorded_at"] != rows[1]["recorded_at"]


def test_append_events_archive_is_loadable_by_backtester(tmp_path) -> None:
    archive = tmp_path / "event_history.csv"
    append_events_archive(
        [EconomicEvent("NFP", "USD", NOW, "high", forecast="110K", previous="139K")],
        archive,
        now=NOW,
    )

    from fx_backtester.data import load_economic_events_csv

    frame = load_economic_events_csv(archive)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["currency"] == "USD"
    assert row["impact"] == "high"
    assert row["name"] == "NFP"


def test_symbol_currencies() -> None:
    assert symbol_currencies("usd/jpy") == ("USD", "JPY")
    with pytest.raises(ValueError):
        symbol_currencies("XAU")


class _FailingSession:
    """常に失敗するHTTPセッション(呼ばれた回数を記録)。"""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, timeout: float = 0):
        self.calls += 1
        raise ConnectionError("simulated outage")


def test_fetch_calendar_uses_fresh_cache_without_network(tmp_path) -> None:
    import json as jsonlib

    cache_path = tmp_path / "cache.json"
    raw = [
        {
            "title": "NFP",
            "country": "USD",
            "date": "2026-07-03T08:30:00-04:00",
            "impact": "High",
        }
    ]
    cache_path.write_text(
        jsonlib.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "weeks": {"thisweek": raw},
            }
        )
    )
    session = _FailingSession()
    events, warnings = fetch_calendar(
        session=cast(requests.Session, session), cache_path=cache_path, cache_max_age_minutes=45
    )
    assert session.calls == 0  # 新鮮なキャッシュがあればネットワークを叩かない
    assert len(events) == 1 and events[0].title == "NFP"
    assert warnings == []


def test_fetch_calendar_falls_back_to_stale_cache(tmp_path) -> None:
    import json as jsonlib

    cache_path = tmp_path / "cache.json"
    stale_time = datetime.now(UTC) - timedelta(hours=5)
    cache_path.write_text(
        jsonlib.dumps(
            {
                "fetched_at": stale_time.isoformat(),
                "weeks": {
                    "thisweek": [
                        {
                            "title": "CPI",
                            "country": "USD",
                            "date": "2026-07-03T08:30:00-04:00",
                            "impact": "High",
                        }
                    ]
                },
            }
        )
    )
    session = _FailingSession()
    events, warnings = fetch_calendar(
        session=cast(requests.Session, session), cache_path=cache_path
    )
    assert session.calls == 2  # thisweek/nextweek両方失敗
    assert len(events) == 1 and events[0].title == "CPI"
    assert any("キャッシュを使用" in w for w in warnings)


def test_fetch_calendar_no_cache_no_network_returns_empty(tmp_path) -> None:
    events, warnings = fetch_calendar(
        session=cast(requests.Session, _FailingSession()), cache_path=tmp_path / "none.json"
    )
    assert events == []
    assert len(warnings) == 2


# -------------------------------------------------------------------- news


SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item>
  <title>Japanese Yen: Higher Japanese rates and intervention risks</title>
  <description>&lt;p&gt;MUFG argues Japanese rates clearly have to head higher.&lt;/p&gt;</description>
  <link>https://example.com/jpy</link>
  <pubDate>Thu, 02 Jul 2026 06:50:43 Z</pubDate>
</item>
<item>
  <title>USD/CAD remains in tight range near 1.4200</title>
  <description>Eyes on US NFP</description>
  <link>https://example.com/usdcad</link>
  <pubDate>Thu, 02 Jul 2026 06:49:00 GMT</pubDate>
</item>
<item>
  <title>日付なしはスキップ</title>
  <description>no pubDate</description>
</item>
</channel></rss>
"""


def test_parse_rss_extracts_items_and_tags() -> None:
    items = parse_rss(SAMPLE_RSS, "FXStreet")
    assert len(items) == 2
    first = items[0]
    assert first.title.startswith("Japanese Yen")
    assert first.published == datetime(2026, 7, 2, 6, 50, 43, tzinfo=UTC)
    assert "JPY" in first.currencies
    assert "<p>" not in first.summary
    second = items[1]
    assert set(second.currencies) >= {"USD", "CAD"}


def test_tag_currencies_pair_and_keywords() -> None:
    assert set(tag_currencies("EURUSD hits new high as ECB stays put")) >= {
        "EUR",
        "USD",
    }
    assert tag_currencies("Bank of Japan hints at hike") == ("JPY",)
    assert tag_currencies("stocks rally on tech earnings") == ()


def test_dedupe_and_sort_removes_duplicates() -> None:
    items = [
        make_news("USD/JPY rises to fresh highs", hours_ago=2),
        make_news("USD/JPY Rises to Fresh Highs!", hours_ago=1),
        make_news("別のニュース ECB", hours_ago=3),
    ]
    unique = dedupe_and_sort(items)
    assert len(unique) == 2
    assert unique[0].published > unique[1].published


# --------------------------------------------------------------- sentiment


def test_lexicon_hawkish_positive_for_tagged_currency() -> None:
    items = [
        make_news("BOJ signals rate hike as inflation stays hot"),
        make_news("Bank of Japan hawkish tilt continues"),
    ]
    scores = score_headlines_lexicon(items, ["USD", "JPY"])
    assert scores["JPY"].score > 0
    assert scores["JPY"].headline_count == 2
    assert scores["USD"].headline_count == 0


def test_lexicon_pair_move_direction() -> None:
    items = [make_news("USD/JPY rises above 160 on dollar strength")]
    scores = score_headlines_lexicon(items, ["USD", "JPY"])
    assert scores["USD"].score > 0
    assert scores["JPY"].score < 0


def test_pair_bias_difference() -> None:
    currencies = {
        "USD": CurrencySentiment("USD", score=0.6),
        "JPY": CurrencySentiment("JPY", score=-0.4),
    }
    assert pair_bias("USD", "JPY", currencies) == 0.5
    assert pair_bias("JPY", "USD", currencies) == -0.5


def test_analyze_market_falls_back_to_local_analyst() -> None:
    """APIキー無しの既定経路は自前分析エンジン(analyst)になる。"""
    items = [make_news("Fed dovish shift: rate cut expected")]
    analysis = analyze_market(items, ["USD", "JPY"], use_llm=False, now=NOW)
    assert analysis.engine == "analyst"
    assert analysis.currencies["USD"].score < 0


def test_extract_json_block() -> None:
    text = '前置き {"a": 1, "b": {"c": 2}} 後書き'
    assert _extract_json_block(text) == {"a": 1, "b": {"c": 2}}
    assert _extract_json_block("JSONなし") is None


def test_lexicon_single_headline_is_shrunk() -> None:
    """記事1件では±1.0に振り切らない(過大評価の防止)。"""
    items = [make_news("USD/JPY rises above 160 on dollar strength")]
    scores = score_headlines_lexicon(items, ["USD", "JPY"])
    assert 0 < scores["USD"].score <= 0.34  # shrink = 1/(1+2)
    assert -0.34 <= scores["JPY"].score < 0


def test_lexicon_more_headlines_increase_magnitude() -> None:
    one = score_headlines_lexicon([make_news("BOJ hawkish rate hike")], ["JPY"])
    many = score_headlines_lexicon(
        [make_news(f"BOJ hawkish rate hike {i}", hours_ago=i) for i in range(4)],
        ["JPY"],
    )
    assert many["JPY"].score > one["JPY"].score > 0


def test_claude_json_bias_scaled_by_confidence() -> None:
    parsed = {
        "currencies": {
            "USD": {"bias": 0.8, "confidence": 0.5, "themes": ["雇用"], "comment": "強い"},
            "JPY": {"bias": -1.0},  # confidence欠落 → 0.5扱い
        },
        "market_regime": "risk_on",
        "summary": "要約",
    }
    analysis = build_analysis_from_claude_json(parsed, ["JPY", "USD"])
    assert analysis is not None and analysis.engine == "claude"
    assert analysis.currencies["USD"].score == pytest.approx(0.4)  # 0.8 × 0.5
    assert analysis.currencies["USD"].confidence == 0.5
    assert analysis.currencies["JPY"].score == pytest.approx(-0.5)
    assert analysis.regime == "risk_on"


def test_claude_json_invalid_payloads_rejected() -> None:
    assert build_analysis_from_claude_json(None, ["USD"]) is None
    assert build_analysis_from_claude_json({"summary": "通貨なし"}, ["USD"]) is None
    # 壊れた値は0扱い・範囲外はクリップ
    parsed = {"currencies": {"USD": {"bias": "abc", "confidence": 5.0}}}
    analysis = build_analysis_from_claude_json(parsed, ["USD"])
    assert analysis is not None
    assert analysis.currencies["USD"].score == 0.0


# -------------------------------------------------------------- technicals


def make_view(interval: str, rec: str, **kwargs):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 5, "NEUTRAL": 11}
    indicators = {
        "close": kwargs.get("close", 150.0),
        "RSI": 55.0,
        "ATR": kwargs.get("atr", 0.5),
        "SMA20": kwargs.get("sma_fast", 150.5),
        "SMA100": kwargs.get("sma_slow", 149.0),
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def test_alignment_score_and_ma_side() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": make_view("15m", "BUY"),
        "1h": make_view("1h", "STRONG_BUY"),
        "4h": make_view("4h", "BUY"),
        "1d": make_view("1d", "NEUTRAL"),
    }
    # 0.5*0.15 + 1.0*0.30 + 0.5*0.30 + 0.0*0.25 = 0.525
    assert tech.alignment_score() == 0.525
    assert tech.ma_side() == "long"
    assert tech.close() == 150.0
    assert tech.atr() == 0.5


def test_ma_side_short_and_missing() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {"1h": make_view("1h", "SELL", sma_fast=148.0, sma_slow=149.0)}
    assert tech.ma_side() == "short"
    assert PairTechnicals(symbol="EURUSD").ma_side() is None


def test_coverage_and_missing_intervals() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    assert tech.coverage() == 0.0
    tech.views = {"1h": make_view("1h", "BUY")}
    assert tech.coverage() == pytest.approx(0.30)
    assert tech.missing_intervals() == ["15m", "4h", "1d"]
    tech.views = {
        "15m": make_view("15m", "BUY"),
        "1h": make_view("1h", "BUY"),
        "4h": make_view("4h", "BUY"),
        "1d": make_view("1d", "BUY"),
    }
    assert tech.coverage() == 1.0
    assert tech.missing_intervals() == []


# ---------------------------------------------------------------- briefing


def bullish_tech(symbol: str = "USDJPY") -> PairTechnicals:
    tech = PairTechnicals(symbol=symbol)
    tech.views = {
        "15m": make_view("15m", "BUY"),
        "1h": make_view("1h", "STRONG_BUY"),
        "4h": make_view("4h", "STRONG_BUY"),
        "1d": make_view("1d", "BUY"),
    }
    return tech


def test_build_trade_plan_long_with_levels() -> None:
    currencies = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    plan = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), currencies, [], [], now=NOW, atr_multiple=2.5
    )
    assert plan.direction == "long"
    assert plan.conviction > 40
    assert plan.stop == pytest.approx(150.0 - 0.5 * 2.5)
    assert plan.target1 == pytest.approx(150.0 + 0.5 * 2.5)
    assert plan.target2 == pytest.approx(150.0 + 0.5 * 5.0)


def test_build_trade_plan_closed_on_weekend() -> None:
    """市場休場中はどれだけシグナルが強くても方向判断を出さない(stale価格ガード)。"""
    currencies = {
        "USD": CurrencySentiment("USD", score=0.8),
        "JPY": CurrencySentiment("JPY", score=-0.8),
    }
    saturday = datetime(2026, 7, 4, 8, 0, tzinfo=UTC)
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=saturday)
    assert plan.direction == "closed"
    assert plan.conviction == 0
    assert plan.stop is None
    assert any("休場" in w for w in plan.warnings)


def test_build_trade_plan_standby_in_event_window() -> None:
    events = [EconomicEvent("FOMC", "USD", NOW + timedelta(minutes=30), "high")]
    windows = risk_windows(events, {"USD", "JPY"})
    currencies = {
        "USD": CurrencySentiment("USD", score=0.8),
        "JPY": CurrencySentiment("JPY", score=-0.8),
    }
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, windows, [], now=NOW)
    assert plan.direction == "standby"
    assert plan.conviction <= briefing.STANDBY_CONVICTION_CAP
    assert plan.stop is None
    assert any("イベント警戒中" in w for w in plan.warnings)


def test_build_trade_plan_neutral_when_signals_conflict() -> None:
    currencies = {
        "USD": CurrencySentiment("USD", score=-0.6),  # ニュースはドル売り
        "JPY": CurrencySentiment("JPY", score=0.6),
    }
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {"1h": make_view("1h", "BUY")}  # テクニカルは弱い買い
    plan = briefing.build_trade_plan("USDJPY", tech, currencies, [], [], now=NOW)
    assert plan.direction == "neutral"
    assert plan.stop is None


def test_trade_plan_quality_penalizes_missing_news() -> None:
    """関連ニュースが無い場合は品質70%扱いになり確信度が減衰する。"""
    currencies = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=NOW)
    assert plan.data_quality == pytest.approx(0.7)
    assert plan.conviction == round(abs(plan.composite) * 100 * 0.7)
    assert any("関連ニュース0件" in w for w in plan.warnings)


def test_trade_plan_no_technicals_forces_neutral() -> None:
    """テクニカルが全滅したらニュースが強くても方向判断を出さない。"""
    currencies = {
        "USD": CurrencySentiment("USD", score=0.9),
        "JPY": CurrencySentiment("JPY", score=-0.9),
    }
    tech = PairTechnicals(symbol="USDJPY")  # views空 = 全時間足取得失敗
    news = [make_news(f"USD/JPY rises session {i}", hours_ago=i + 1) for i in range(5)]
    plan = briefing.build_trade_plan("USDJPY", tech, currencies, [], news, now=NOW)
    assert plan.direction == "neutral"
    assert any("テクニカル全時間足" in w for w in plan.warnings)
    assert any("方向判断を見送り" in w for w in plan.warnings)


def test_trade_plan_partial_technicals_warns() -> None:
    currencies = {"USD": CurrencySentiment("USD"), "JPY": CurrencySentiment("JPY")}
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {"1h": make_view("1h", "BUY")}
    plan = briefing.build_trade_plan("USDJPY", tech, currencies, [], [], now=NOW)
    assert any("テクニカル欠損" in w and "15m" in w for w in plan.warnings)


def test_trade_plan_calendar_unavailable_caps_conviction() -> None:
    """カレンダー取得不能=イベントリスク未確認なら確信度に上限。"""
    currencies = {
        "USD": CurrencySentiment("USD", score=0.8),
        "JPY": CurrencySentiment("JPY", score=-0.8),
    }
    news = [make_news(f"USD/JPY rallies session {i}", hours_ago=i + 1) for i in range(5)]
    plan = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), currencies, [], news, now=NOW, calendar_ok=False
    )
    assert plan.direction == "long"
    assert plan.conviction <= briefing.CALENDAR_UNKNOWN_CONVICTION_CAP
    assert any("イベントリスク未確認" in w for w in plan.warnings)


def test_trade_plan_conflict_between_tech_and_news_warns() -> None:
    """テクニカルとニュースが強く対立したら警告して確信度を減衰。"""
    currencies = {
        "USD": CurrencySentiment("USD", score=-0.5),  # ニュースはドル売り
        "JPY": CurrencySentiment("JPY", score=0.5),
    }
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=NOW)
    assert any("反対方向" in w for w in plan.warnings)
    undamped = round(round(abs(plan.composite) * 100 * plan.data_quality))
    assert plan.conviction == round(undamped * briefing.CONFLICT_CONVICTION_FACTOR)


# ------------------------------------------------------------------ market


def test_is_market_open_weekend_closure() -> None:
    assert is_market_open(datetime(2026, 7, 2, 8, 0, tzinfo=UTC))  # 木曜
    assert is_market_open(datetime(2026, 7, 3, 20, 59, tzinfo=UTC))  # 金曜クローズ直前
    assert not is_market_open(datetime(2026, 7, 3, 21, 0, tzinfo=UTC))  # 金曜21:00 UTC
    assert not is_market_open(datetime(2026, 7, 4, 12, 0, tzinfo=UTC))  # 土曜
    assert not is_market_open(datetime(2026, 7, 5, 21, 59, tzinfo=UTC))  # 日曜再開直前
    assert is_market_open(datetime(2026, 7, 5, 22, 0, tzinfo=UTC))  # 日曜22:00 UTC
    assert is_market_open(datetime(2026, 7, 6, 8, 0, tzinfo=UTC))  # 月曜


def test_open_hours_between() -> None:
    wed = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    thu = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)
    assert open_hours_between(wed, thu) == pytest.approx(24.0)  # 平日は壁時計と同じ
    assert open_hours_between(thu, wed) == 0.0  # 逆転は0

    friday = datetime(2026, 7, 3, 18, 0, tzinfo=UTC)
    saturday = friday + timedelta(hours=24)
    monday = datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    assert open_hours_between(friday, saturday) == pytest.approx(3.0)  # 金18時→21時のみ
    assert open_hours_between(friday, monday) == pytest.approx(24.0)  # 週末49hを除外

    # 2週末を跨ぐ場合は49h×2を除外
    next_monday = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    assert open_hours_between(friday, next_monday) == pytest.approx(240.0 - 98.0)


# ----------------------------------------------------------------- journal


def test_journal_append_and_evaluate(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    currencies = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    recorded_at = NOW - timedelta(hours=24)  # 固定ホライズン(24±2h)に収まる経過時間
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=recorded_at)
    assert plan.direction == "long" and plan.close == 150.0
    append_plans(path, [plan], now=recorded_at)
    path.open("a", encoding="utf-8").write("壊れた行 not json\n")  # 耐性確認

    # 記録時150.0 → 現値151.0: ロング的中(ATR0.5×10%=0.05を超える動き)
    stats = evaluate_directional_accuracy(path, {"USDJPY": 151.0}, now=NOW)
    assert stats.evaluated == 1 and stats.hits == 1
    # 現値149.0: 不的中
    stats = evaluate_directional_accuracy(path, {"USDJPY": 149.0}, now=NOW)
    assert stats.evaluated == 1 and stats.hits == 0
    # ATR閾値未満の小動きは的中/不的中のどちらにも数えない
    stats = evaluate_directional_accuracy(path, {"USDJPY": 150.02}, now=NOW)
    assert stats.evaluated == 0 and stats.flat == 1
    # ホライズン前(1h)は評価対象外
    stats = evaluate_directional_accuracy(
        path, {"USDJPY": 151.0}, now=recorded_at + timedelta(hours=1)
    )
    assert stats.evaluated == 0
    # ホライズン超過(40h)も評価対象外(広い窓での多重評価を防ぐ)
    stats = evaluate_directional_accuracy(
        path, {"USDJPY": 151.0}, now=recorded_at + timedelta(hours=40)
    )
    assert stats.evaluated == 0
    # 存在しないファイルは空集計
    stats = evaluate_directional_accuracy(tmp_path / "none.jsonl", {}, now=NOW)
    assert stats.evaluated == 0


def test_journal_weekend_gap_uses_market_open_hours(tmp_path) -> None:
    """週末を跨いだ判断は「市場オープン時間」が24hに達してから評価する。"""
    path = tmp_path / "journal.jsonl"
    currencies = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    friday = datetime(2026, 7, 3, 18, 0, tzinfo=UTC)  # 金曜クローズ3時間前
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=friday)
    assert plan.direction == "long"
    append_plans(path, [plan], now=friday)

    # 土曜18時: 壁時計では24hだが市場は3hしか開いていない → 評価しない
    stats = evaluate_directional_accuracy(path, {"USDJPY": 151.0}, now=friday + timedelta(hours=24))
    assert stats.evaluated == 0 and stats.flat == 0
    # 月曜19時: オープン時間換算で24h → 評価する
    monday = datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    stats = evaluate_directional_accuracy(path, {"USDJPY": 151.0}, now=monday)
    assert stats.evaluated == 1 and stats.hits == 1


def test_journal_records_score_breakdown_and_levels(tmp_path) -> None:
    """スコア内訳とSL/TPが記録され、後からキャリブレーション分析に使える。"""
    import json as jsonlib

    path = tmp_path / "journal.jsonl"
    currencies = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), currencies, [], [], now=NOW)
    append_plans(path, [plan], now=NOW)

    entry = jsonlib.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["tech_score"] == plan.tech_score
    assert entry["news_score"] == plan.news_score
    assert entry["atr"] == 0.5
    assert entry["stop"] == plan.stop
    assert entry["target1"] == plan.target1
    assert entry["target2"] == plan.target2


def test_journal_skips_non_directional_entries(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    neutral = briefing.TradePlan(
        symbol="EURUSD",
        direction="neutral",
        conviction=0,
        composite=0.0,
        tech_score=0.0,
        news_score=0.0,
        close=1.1,
    )
    append_plans(path, [neutral], now=NOW - timedelta(hours=24))
    stats = evaluate_directional_accuracy(path, {"EURUSD": 1.2}, now=NOW)
    assert stats.evaluated == 0


def test_format_stats_ja() -> None:
    line = format_stats_ja(DirectionalStats(evaluated=4, hits=3))
    assert "4件中 3件的中" in line
    assert "的中率 75%" in line
    assert "約24時間前" in line
    assert format_stats_ja(DirectionalStats()) == ""
    assert "小動き" in format_stats_ja(DirectionalStats(flat=2))
    assert "ほか1件は小動き" in format_stats_ja(DirectionalStats(evaluated=4, hits=3, flat=1))


def test_build_discord_payload_structure() -> None:
    from fx_intel.sentiment import MarketAnalysis

    currencies = {
        "USD": CurrencySentiment("USD", score=0.4, headline_count=5),
        "JPY": CurrencySentiment("JPY", score=-0.2, headline_count=3),
    }
    analysis = MarketAnalysis(currencies=currencies, regime="risk_on", engine="lexicon")
    plan = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), currencies, [], [make_news("USD/JPY rises")], now=NOW
    )
    events = [EconomicEvent("NFP", "USD", NOW + timedelta(hours=30), "high", "110K")]
    payload = briefing.build_discord_payload(
        [plan],
        analysis,
        events,
        ["JPY", "USD"],
        20,
        100,
        fetch_warnings=["テスト警告"],
        journal_note="直近48h内の方向判断 4件中 3件的中 — 的中率 75%",
        now=NOW,
    )
    assert "FXデスクブリーフィング" in payload["content"]
    assert "リスクオン" in payload["content"]
    assert len(payload["embeds"]) == 2
    macro = payload["embeds"][0]
    field_names = [f["name"] for f in macro["fields"]]
    assert "通貨センチメント" in field_names
    assert "今後48時間の重要イベント" in field_names
    assert "データ取得の注意" in field_names
    assert "判断の検証(自己採点)" in field_names
    pair_embed = payload["embeds"][1]
    assert pair_embed["title"].startswith("USDJPY")
    assert any("売買プラン" in f["name"] for f in pair_embed["fields"])
    plan_field = next(f for f in pair_embed["fields"] if f["name"] == "判断")
    assert "買い" in plan_field["value"]  # 初心者向けの言い換えが入る
    assert any("用語ミニ解説" in f["name"] for f in macro["fields"])


def test_price_formatting_by_symbol() -> None:
    assert briefing.format_price("USDJPY", 150.123456) == "150.123"
    assert briefing.format_price("EURUSD", 1.123456) == "1.12346"
    assert briefing.format_price("EURUSD", None) == "—"
