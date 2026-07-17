"""fx_briefing --per-timeframe へのマルチホライズンshadow配線の統合テスト。"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import fx_briefing
from fx_intel import horizon_journal
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view


def _view(interval, rec, close, rsi=55.0, adx=25.0, atr=0.15):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 3, "NEUTRAL": 5}
    indicators = {
        "close": close,
        "RSI": rsi,
        "ADX": adx,
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
            "15m": _view("15m", "SELL", 156.20, atr=0.08),
            "1h": _view("1h", "BUY", 156.25, atr=0.15),
            "4h": _view("4h", "STRONG_BUY", 156.30, atr=0.30),
            "1d": _view("1d", "BUY", 156.10, atr=0.80),
        }
        result[symbol] = tech
    return result, []


def _analysis():
    return MarketAnalysis(
        engine="lexicon",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", 0.1, 1),
            "JPY": CurrencySentiment("JPY", -0.1, 1),
        },
        summary="",
    )


@pytest.fixture
def patched_paths(tmp_path):
    horizon = tmp_path / "briefing_horizon_forecasts.jsonl"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", tmp_path / "tf.jsonl"),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", tmp_path / "prices.jsonl"),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tmp_path / "tf_learning.json"),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", tmp_path / "tp_sl.json"),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", tmp_path / "max.json"),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", tmp_path / "dec.jsonl"),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LATEST_PATH", tmp_path / "latest.json"),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_OUTCOMES_PATH", tmp_path / "out.json"),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_FEEDBACK_PATH", tmp_path / "fb.json"),
        mock.patch.object(fx_briefing, "DEFAULT_HORIZON_JOURNAL_PATH", horizon),
    ):
        yield horizon


def _run(argv, extra_patches=()):
    with (
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=([], [])),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
    ):
        return fx_briefing.main(argv)


def test_per_timeframe_writes_nine_horizon_rows_per_symbol(patched_paths) -> None:
    horizon_path = patched_paths
    rc = _run(["--per-timeframe", "--no-discord", "--no-macro", "--symbols", "USDJPY"])
    assert rc == 0
    rows = [
        json.loads(line)
        for line in horizon_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 9  # 本番8 + 5m shadow
    assert {row["horizon"] for row in rows} == {
        "5m",
        "15m",
        "30m",
        "1h",
        "3h",
        "6h",
        "12h",
        "24h",
        "3d",
    }
    for row in rows:
        assert row["symbol"] == "USDJPY"
        assert row["contract"] == "horizon-pit-v1"
        assert horizon_journal.is_pit_eligible_horizon_entry(row)
        assert row["shadow_only"] == (row["horizon"] == "5m")
        assert row["p_up"] + row["p_down"] + row["p_flat"] == pytest.approx(1.0, abs=1e-4)


def test_dry_run_does_not_write_horizon_journal(patched_paths) -> None:
    horizon_path = patched_paths
    rc = _run(["--per-timeframe", "--dry-run", "--no-macro", "--symbols", "USDJPY"])
    assert rc == 0
    assert not horizon_path.exists()


def test_flag_disables_horizon_journal(patched_paths) -> None:
    horizon_path = patched_paths
    rc = _run(
        [
            "--per-timeframe",
            "--no-discord",
            "--no-macro",
            "--no-horizon-forecasts",
            "--symbols",
            "USDJPY",
        ]
    )
    assert rc == 0
    assert not horizon_path.exists()


def test_horizon_write_failure_is_warn_only(patched_paths, capsys) -> None:
    """shadowジャーナルの失敗は既存経路(時間足別判断)を止めない。"""
    with mock.patch.object(
        fx_briefing.horizon_journal,
        "append_horizon_forecasts",
        side_effect=OSError("disk full"),
    ):
        rc = _run(["--per-timeframe", "--no-discord", "--no-macro", "--symbols", "USDJPY"])
    assert rc == 0  # 主経路は成功のまま
    assert fx_briefing.DEFAULT_TF_JOURNAL_PATH.exists()  # 時間足別ジャーナルは書けている
