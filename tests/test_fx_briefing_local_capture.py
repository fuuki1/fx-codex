"""fx_briefing のローカル学習ログ収集モードのテスト。"""

from __future__ import annotations

import json
from unittest import mock

import fx_briefing
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view


def _view(interval: str, rec: str, close: float, atr: float = 0.15):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 2, "NEUTRAL": 5}
    indicators = {
        "close": close,
        "RSI": 55.0,
        "ADX": 25.0,
        "ATR": atr,
        "SMA20": close * 1.001,
        "SMA100": close,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _tech_for(symbols, **_kwargs):
    result = {}
    for symbol in symbols:
        tech = PairTechnicals(symbol=symbol)
        tech.views = {
            "15m": _view("15m", "BUY", 156.2, atr=0.08),
            "1h": _view("1h", "BUY", 156.25, atr=0.15),
            "4h": _view("4h", "BUY", 156.3, atr=0.30),
            "1d": _view("1d", "BUY", 156.1, atr=0.80),
        }
        result[symbol] = tech
    return result, []


def _analysis():
    return MarketAnalysis(
        engine="lexicon",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", 0.4, 1),
            "JPY": CurrencySentiment("JPY", -0.2, 1),
        },
        summary="",
    )


def test_no_discord_writes_fusion_journal_and_learning(tmp_path, capsys) -> None:
    journal_path = tmp_path / "briefing_journal.jsonl"
    learning_path = tmp_path / "briefing_learning.json"
    promotion_path = tmp_path / "promotion_state.json"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_LEARNING_PATH", learning_path),
        mock.patch.object(fx_briefing, "DEFAULT_PROMOTION_STATE", promotion_path),
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=([], [])),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
        mock.patch.object(fx_briefing, "load_webhook_url", side_effect=AssertionError),
        mock.patch.object(fx_briefing, "post_to_discord", side_effect=AssertionError),
    ):
        rc = fx_briefing.main(
            [
                "--no-discord",
                "--no-macro",
                "--no-ml",
                "--no-trade-expectancy",
                "--no-export-events",
                "--no-event-archive",
                "--symbols",
                "USDJPY",
            ]
        )

    assert rc == 0
    assert journal_path.exists()
    assert learning_path.exists()
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    profile = json.loads(learning_path.read_text(encoding="utf-8"))
    assert rows and rows[0]["symbol"] == "USDJPY"
    assert "evaluated" in profile
    assert "Discord送信なし" in capsys.readouterr().out
