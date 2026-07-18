"""fx_briefing --per-timeframe パスの結合テスト(ネットワーク不要・モック注入)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

import fx_briefing
from fx_intel.calendar import EconomicEvent
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel import trade_outcome as to


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


@pytest.fixture(autouse=True)
def _market_always_open():
    """市場を常にオープン扱いに固定する(テストの実行曜日依存を排除)。

    fx_briefing.main は datetime.now の実時刻で is_market_open を判定するため、
    週末に実行すると全時間足が「休場」standbyへ倒れ、方向判断に依存する検証
    (承認済みTP/SL・期待値ガード等)が実行日によって失敗する。ネットワークを
    全モックした決定論テストなので、市場状態も固定する。
    """
    with (
        mock.patch("fx_intel.timeframe.is_market_open", return_value=True),
        mock.patch("fx_intel.briefing.is_market_open", return_value=True),
    ):
        yield


def _approved_tp_registry(path) -> None:
    candidate = to.TradeImprovementCandidate(
        "approved-overall-tp",
        "TP/SL候補",
        "overall",
        "high",
        "tp_sl_variant_paper_test",
        "TP1=0.75R / TP2=1.5Rをpaper検証",
        "期待R改善",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "overall",
            "key": "",
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.75,
            "delta_expectancy_r": 1.75,
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(
        None,
        [candidate],
        now=datetime(2026, 7, 1, tzinfo=UTC),
        data_contract=fx_briefing.journal.FUSION_PIT_DATA_CONTRACT,
    )
    registry = to.update_improvement_registry(
        registry,
        [candidate],
        now=datetime(2026, 7, 1, 1, tzinfo=UTC),
        data_contract=fx_briefing.journal.FUSION_PIT_DATA_CONTRACT,
    )
    registry, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        now=datetime(2026, 7, 1, 2, tzinfo=UTC),
    )
    assert result["status"] == "approved"
    to.save_improvement_registry(registry, path)


@pytest.fixture
def patched_paths(tmp_path):
    """ジャーナル・学習の書き込み先を一時ディレクトリへ差し替える。"""
    tf_journal = tmp_path / "briefing_tf_journal.jsonl"
    tf_prices = tmp_path / "briefing_tf_prices.jsonl"
    tf_learning = tmp_path / "briefing_tf_learning.json"
    tp_sl_learning = tmp_path / "briefing_tp_sl_learning.json"
    maximization = tmp_path / "briefing_maximization.json"
    decision_log = tmp_path / "briefing_decisions.jsonl"
    decision_latest = tmp_path / "briefing_decisions_latest.json"
    decision_outcomes = tmp_path / "briefing_decision_outcomes.json"
    decision_feedback = tmp_path / "briefing_decision_feedback.json"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", tf_journal),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", tf_prices),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tf_learning),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", tp_sl_learning),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", maximization),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", decision_log),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LATEST_PATH", decision_latest),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_OUTCOMES_PATH", decision_outcomes),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_FEEDBACK_PATH", decision_feedback),
    ):
        yield tf_journal, tf_learning


def _run(argv, capsys, calendar_result=None):
    """全ネットワーク経路をモックして fx_briefing.main を実行、payload を返す。

    ``fx_briefing.main`` は ``datetime.now`` から実時刻を取るため、テスト実行日が
    週末だと ``is_market_open`` が False になり全時間足が「休場」へ倒れて方向判断
    (と期待値ガード等の方向依存出力)が出なくなる。ネットワークを全モックした
    決定論テストが実行曜日で挙動を変えないよう、市場は常にオープン扱いにする。
    """
    calendar_result = calendar_result if calendar_result is not None else ([], [])
    with (
        mock.patch("fx_intel.timeframe.is_market_open", return_value=True),
        mock.patch("fx_intel.briefing.is_market_open", return_value=True),
        mock.patch("fx_intel.decision_pipeline.is_market_open", return_value=True),
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=calendar_result),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
    ):
        rc = fx_briefing.main(argv)
    return rc


def test_promote_live_flag_is_rejected_before_any_network_call(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        fx_briefing.main(["--promote-live", "ml", "--dry-run"])

    assert error.value.code == 2
    error = capsys.readouterr().err
    assert "--promote-live is disabled" in error
    assert "research/shadow only" in error


def test_per_timeframe_dry_run_builds_payload(patched_paths, capsys) -> None:
    rc = _run(["--per-timeframe", "--dry-run", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert rc == 0
    out = capsys.readouterr().out
    assert "時間足別" in out
    # embed JSON に4時間足のフィールドが出る
    assert "15分足" in out and "日足" in out
    assert "主ホライズン15分後" in out


def test_signal_board_flag_enables_single_board_payload(patched_paths, capsys) -> None:
    rc = _run(
        ["--signal-board", "--dry-run", "--no-macro", "--symbols", "USDJPY"],
        capsys,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "FXシグナルボード" in out
    # 自動売買を行わないため発注経路の死活監視は表示しない
    assert "システム状態" not in out
    assert "データ品質" in out
    assert "マクロ・センチメント概況" not in out


def test_signal_board_records_its_own_five_minute_price_series(patched_paths, capsys) -> None:
    rc = _run(
        ["--signal-board", "--no-discord", "--no-macro", "--symbols", "USDJPY"],
        capsys,
    )

    assert rc == 0
    rows = [
        json.loads(line)
        for line in fx_briefing.DEFAULT_TF_PRICES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["timeframe"] for row in rows} == {"15m", "1h", "4h", "1d"}
    assert all("direction" not in row for row in rows)


def test_per_timeframe_dry_run_does_not_write_journal(patched_paths, capsys) -> None:
    tf_journal, _ = patched_paths
    _run(["--per-timeframe", "--dry-run", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert not tf_journal.exists()  # dry-run は記録しない


def test_per_timeframe_writes_journal_when_not_dry_run(patched_paths, capsys) -> None:
    tf_journal, tf_learning = patched_paths
    with mock.patch.object(fx_briefing, "load_webhook_url", return_value=None):
        # webhook 未設定は通知専用exit codeだが、ジャーナル追記はその前に済む
        rc = _run(["--per-timeframe", "--no-macro", "--symbols", "USDJPY"], capsys)
    assert rc == fx_briefing.NOTIFICATION_FAILURE_EXIT_CODE
    assert tf_journal.exists()
    rows = [json.loads(line) for line in tf_journal.read_text().splitlines() if line.strip()]
    timeframes = {row["timeframe"] for row in rows}
    assert timeframes == {"15m", "1h", "4h", "1d"}
    # 各行に主ホライズンが紐づく
    horizons = {row["timeframe"]: row["horizon_hours"] for row in rows}
    assert horizons == {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def test_per_timeframe_journal_failure_stops_later_writers(patched_paths, capsys) -> None:
    with (
        mock.patch.object(
            fx_briefing.journal,
            "append_timeframe_plans",
            side_effect=OSError("read-only"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "append_decision_events",
            side_effect=AssertionError("must not run"),
        ),
    ):
        rc = _run(
            ["--per-timeframe", "--no-discord", "--no-macro", "--symbols", "USDJPY"],
            capsys,
        )

    assert rc == fx_briefing.JOURNAL_WRITE_FAILURE_EXIT_CODE
    assert "ジャーナル書き込み失敗" in capsys.readouterr().err


def test_per_timeframe_no_discord_writes_journal_without_posting(patched_paths, capsys) -> None:
    tf_journal, tf_learning = patched_paths
    with (
        mock.patch.object(fx_briefing, "load_webhook_url", side_effect=AssertionError),
        mock.patch.object(fx_briefing, "post_to_discord", side_effect=AssertionError),
    ):
        rc = _run(
            ["--per-timeframe", "--no-discord", "--no-macro", "--symbols", "USDJPY"],
            capsys,
        )

    assert rc == 0
    assert tf_journal.exists()
    assert tf_learning.exists()
    assert fx_briefing.DEFAULT_DECISION_LOG_PATH.exists()
    assert fx_briefing.DEFAULT_DECISION_LATEST_PATH.exists()
    assert fx_briefing.DEFAULT_DECISION_OUTCOMES_PATH.exists()
    assert fx_briefing.DEFAULT_DECISION_FEEDBACK_PATH.exists()
    decision_rows = [
        json.loads(line)
        for line in fx_briefing.DEFAULT_DECISION_LOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["timeframe"] for row in decision_rows} == {"15m", "1h", "4h", "1d"}
    latest = json.loads(fx_briefing.DEFAULT_DECISION_LATEST_PATH.read_text(encoding="utf-8"))
    assert latest["event_count"] == 4
    outcome_report = json.loads(
        fx_briefing.DEFAULT_DECISION_OUTCOMES_PATH.read_text(encoding="utf-8")
    )
    assert outcome_report["scoring_method"] == "tp_sl_mfe_mae_first_touch"
    assert set(outcome_report["metrics"]) >= {"first_touch", "mfe_r", "mae_r"}
    feedback = json.loads(fx_briefing.DEFAULT_DECISION_FEEDBACK_PATH.read_text(encoding="utf-8"))
    assert "cells" in feedback
    out = capsys.readouterr().out
    assert "Discord送信なし" in out


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


def test_per_timeframe_expectancy_guard_uses_timeframe_cell(patched_paths, capsys) -> None:
    tf_journal, _ = patched_paths
    tf_prices = fx_briefing.DEFAULT_TF_PRICES_PATH
    start = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    journal_lines = []
    price_lines = []
    for i in range(20):
        ts = start + timedelta(hours=i * 3)
        journal_lines.append(
            json.dumps(
                {
                    "ts": ts.isoformat(),
                    "symbol": "USDJPY",
                    "timeframe": "1h",
                    "horizon_hours": 1.0,
                    "direction": "long",
                    "conviction": 70,
                    "composite": 0.7,
                    "tech_score": 0.7,
                    "news_score": 0.1,
                    "close": 100.0,
                    "atr": 1.0,
                    "stop": 99.0,
                    "target1": 101.0,
                    "target2": 102.0,
                    "data_quality": 1.0,
                    "features": {},
                    "components": [],
                }
            )
        )
        price_lines.append(
            json.dumps(
                {
                    "ts": (ts + timedelta(hours=1)).isoformat(),
                    "symbol": "USDJPY",
                    "timeframe": "1h",
                    "close": 99.0,
                }
            )
        )
    tf_journal.write_text("\n".join(journal_lines) + "\n", encoding="utf-8")
    tf_prices.write_text("\n".join(price_lines) + "\n", encoding="utf-8")

    calendar_event = EconomicEvent(
        "calendar ok marker", "EUR", datetime(2026, 7, 8, tzinfo=UTC), "low"
    )
    rc = _run(
        [
            "--per-timeframe",
            "--dry-run",
            "--no-learning",
            "--no-macro",
            "--no-export-events",
            "--no-event-archive",
            "--symbols",
            "USDJPY",
        ],
        capsys,
        calendar_result=([calendar_event], []),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "時間足別期待値監視" in out
    assert "・1h: 期待R -1.00R" in out
    assert "期待値ガード" in out
    assert "1時間足" in out


def test_per_timeframe_applies_approved_tp_sl_registry(patched_paths, tmp_path, capsys) -> None:
    registry_path = tmp_path / "trade_registry.json"
    _approved_tp_registry(registry_path)
    calendar_event = EconomicEvent(
        "calendar ok marker", "EUR", datetime(2026, 7, 8, tzinfo=UTC), "low"
    )

    rc = _run(
        [
            "--per-timeframe",
            "--dry-run",
            "--no-learning",
            "--no-macro",
            "--no-export-events",
            "--no-event-archive",
            "--trade-improvement-registry",
            str(registry_path),
            "--symbols",
            "USDJPY",
        ],
        capsys,
        calendar_result=([calendar_event], []),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "承認済みTP/SL" in out
    assert "T1 156.531" in out
    assert "T2 156.812" in out


def test_no_learning_flag_skips_profile(patched_paths, capsys) -> None:
    tf_journal, tf_learning = patched_paths
    with mock.patch.object(fx_briefing, "load_webhook_url", return_value=None):
        _run(
            ["--per-timeframe", "--no-learning", "--no-macro", "--symbols", "USDJPY"],
            capsys,
        )
    assert not tf_learning.exists()  # 学習無効時は保存しない
