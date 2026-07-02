"""fx_intel パッケージのテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import briefing
from fx_intel.calendar import (
    EconomicEvent,
    active_and_next_window,
    export_events_csv,
    fetch_calendar,
    parse_calendar_json,
    risk_windows,
    symbol_currencies,
    upcoming_events,
)
from fx_intel.news import NewsItem, dedupe_and_sort, parse_rss, tag_currencies
from fx_intel.sentiment import (
    CurrencySentiment,
    _extract_json_block,
    analyze_market,
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
        session=session, cache_path=cache_path, cache_max_age_minutes=45
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
    events, warnings = fetch_calendar(session=session, cache_path=cache_path)
    assert session.calls == 2  # thisweek/nextweek両方失敗
    assert len(events) == 1 and events[0].title == "CPI"
    assert any("キャッシュを使用" in w for w in warnings)


def test_fetch_calendar_no_cache_no_network_returns_empty(tmp_path) -> None:
    events, warnings = fetch_calendar(session=_FailingSession(), cache_path=tmp_path / "none.json")
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


def test_analyze_market_lexicon_fallback() -> None:
    items = [make_news("Fed dovish shift: rate cut expected")]
    analysis = analyze_market(items, ["USD", "JPY"], use_llm=False)
    assert analysis.engine == "lexicon"
    assert analysis.currencies["USD"].score < 0


def test_extract_json_block() -> None:
    text = '前置き {"a": 1, "b": {"c": 2}} 後書き'
    assert _extract_json_block(text) == {"a": 1, "b": {"c": 2}}
    assert _extract_json_block("JSONなし") is None


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
    pair_embed = payload["embeds"][1]
    assert pair_embed["title"].startswith("USDJPY")
    assert any("プライスプラン" in f["name"] for f in pair_embed["fields"])


def test_price_formatting_by_symbol() -> None:
    assert briefing.format_price("USDJPY", 150.123456) == "150.123"
    assert briefing.format_price("EURUSD", 1.123456) == "1.12346"
    assert briefing.format_price("EURUSD", None) == "—"
