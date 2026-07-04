"""fx_briefing --per-timeframe パスの結合テスト(ネットワーク不要・モック注入)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

import fx_briefing
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
            "15m": _view("15m", "SELL", 156.20, rsi=68, adx=16, atr=0.08),
            "1h": _view("1h", "BUY", 156.25, rsi=55, adx=28, atr=0.15),
            "4h": _view("4h", "STRONG_BUY", 156.30, rsi=60, adx=35, atr=0.30),
            "1d": _view("1d", "BUY", 156.10, rsi=52, adx=25, atr=0.80),
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
    """ジャーナル・学習の書き込み先を一時ディレクトリへ差し替える。"""
    tf_journal = tmp_path / "briefing_tf_journal.jsonl"
    tf_learning = tmp_path / "briefing_tf_learning.json"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", tf_journal),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tf_learning),
    ):
        yield tf_journal, tf_learning


def _run(argv, capsys):
    """全ネットワーク経路をモックして fx_briefing.main を実行、payload を返す。"""
    with (
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=([], [])),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
    ):
        rc = fx_briefing.main(argv)
    return rc


def test_per_timeframe_dry_run_builds_payload(patched_paths, capsys) -> None:
    rc = _run(["--per-timeframe", "--dry-run", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert rc == 0
    out = capsys.readouterr().out
    assert "時間足別" in out
    # embed JSON に4時間足のフィールドが出る
    assert "15分足" in out and "日足" in out
    assert "主ホライズン15分後" in out


def test_per_timeframe_dry_run_does_not_write_journal(patched_paths, capsys) -> None:
    tf_journal, _ = patched_paths
    _run(["--per-timeframe", "--dry-run", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert not tf_journal.exists()  # dry-run は記録しない


def test_per_timeframe_writes_journal_when_not_dry_run(patched_paths, capsys) -> None:
    tf_journal, tf_learning = patched_paths
    with mock.patch.object(fx_briefing, "load_webhook_url", return_value=None):
        # webhook 未設定なので送信段階で rc=1 になるが、ジャーナル追記はその前に済む
        _run(["--per-timeframe", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert tf_journal.exists()
    rows = [json.loads(line) for line in tf_journal.read_text().splitlines() if line.strip()]
    timeframes = {row["timeframe"] for row in rows}
    assert timeframes == {"15m", "1h", "4h", "1d"}
    # 各行に主ホライズンが紐づく
    horizons = {row["timeframe"]: row["horizon_hours"] for row in rows}
    assert horizons == {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def test_per_timeframe_learning_feeds_back(patched_paths, capsys) -> None:
    """既存の時間足別ジャーナルがあれば学習が働き、学習ファイルが書かれる。"""
    tf_journal, tf_learning = patched_paths
    # 事前に 1h の負け履歴を仕込む(全 miss で減衰が発動する量)
    start = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    price = 156.0
    lines = []
    for i in range(20):
        ts = start + timedelta(hours=i)
        lines.append(
            json.dumps(
                {
                    "ts": ts.isoformat(),
                    "symbol": "USDJPY",
                    "timeframe": "1h",
                    "horizon_hours": 1.0,
                    "direction": "long",
                    "conviction": 60,
                    "tech_score": 0.5,
                    "news_score": 0.2,
                    "close": price,
                    "atr": 0.10,
                    "features": {"rsi_1h": 70.0, "adx_1h": 15.0},
                }
            )
        )
        price -= 0.05
    tf_journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with mock.patch.object(fx_briefing, "load_webhook_url", return_value=None):
        _run(["--per-timeframe", "--no-macro", "--symbols", "USDJPY"], capsys)
    # 学習プロファイルが保存される
    assert tf_learning.exists()
    payload = json.loads(tf_learning.read_text(encoding="utf-8"))
    assert "USDJPY|1h" in payload["profiles"]


def test_no_learning_flag_skips_profile(patched_paths, capsys) -> None:
    tf_journal, tf_learning = patched_paths
    with mock.patch.object(fx_briefing, "load_webhook_url", return_value=None):
        _run(
            ["--per-timeframe", "--no-learning", "--no-macro", "--symbols", "USDJPY"],
            capsys,
        )
    assert not tf_learning.exists()  # 学習無効時は保存しない
