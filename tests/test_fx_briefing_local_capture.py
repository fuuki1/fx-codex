"""fx_briefing のローカル学習ログ収集モードのテスト。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

import fx_briefing
from fx_intel import trade_outcome as to
from fx_intel.append_only import AppendOnlyWriteError, canonical_row_hash
from fx_intel.calendar import EconomicEvent
from fx_intel.market import is_market_open
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
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    decision_latest_path = tmp_path / "briefing_decisions_latest.json"
    decision_outcomes_path = tmp_path / "briefing_decision_outcomes.json"
    decision_feedback_path = tmp_path / "briefing_decision_feedback.json"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_LEARNING_PATH", learning_path),
        mock.patch.object(fx_briefing, "DEFAULT_PROMOTION_STATE", promotion_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", decision_log_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LATEST_PATH", decision_latest_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_OUTCOMES_PATH", decision_outcomes_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_FEEDBACK_PATH", decision_feedback_path),
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
    assert decision_log_path.exists()
    assert decision_latest_path.exists()
    assert decision_outcomes_path.exists()
    assert decision_feedback_path.exists()
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    decision_rows = [
        json.loads(line) for line in decision_log_path.read_text().splitlines() if line.strip()
    ]
    profile = json.loads(learning_path.read_text(encoding="utf-8"))
    outcome_report = json.loads(decision_outcomes_path.read_text(encoding="utf-8"))
    feedback = json.loads(decision_feedback_path.read_text(encoding="utf-8"))
    assert rows and rows[0]["symbol"] == "USDJPY"
    assert decision_rows and decision_rows[0]["learning_context"]["promotion"]["stages"]
    assert outcome_report["scoring_method"] == "tp_sl_mfe_mae_first_touch"
    assert "cells" in feedback
    assert "evaluated" in profile
    assert "Discord送信なし" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failing_append",
    [
        "fx_intel.journal.append_plans",
        "fx_intel.decision_log.append_decision_events",
    ],
)
def test_fusion_persistence_failure_suppresses_normal_discord(
    failing_append: str,
    capsys,
) -> None:
    event = EconomicEvent(
        "NFP",
        "USD",
        datetime.now(UTC) + timedelta(hours=24),
        "high",
    )
    slot = datetime.now(UTC)
    slot = slot.replace(minute=slot.minute - slot.minute % 5, second=0, microsecond=0)

    with (
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=([event], [])),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
        mock.patch(
            failing_append,
            side_effect=AppendOnlyWriteError("simulated required persistence failure"),
        ),
        mock.patch.object(fx_briefing, "load_webhook_url", return_value="x"),
        mock.patch.object(fx_briefing, "post_to_discord") as posted,
    ):
        rc = fx_briefing.main(
            [
                "--no-macro",
                "--no-ml",
                "--no-learning",
                "--no-trade-expectancy",
                "--no-export-events",
                "--no-event-archive",
                "--run-slot",
                slot.isoformat(),
                "--symbols",
                "USDJPY",
            ]
        )

    assert rc == 1
    posted.assert_not_called()
    assert "通常Discord通知を抑止" in capsys.readouterr().err


@pytest.mark.parametrize("mode_args", [[], ["--per-timeframe"]])
def test_external_notification_rejects_no_journal_for_both_modes(
    mode_args: list[str],
    capsys,
) -> None:
    with pytest.raises(SystemExit) as captured:
        fx_briefing.main(["--no-journal", *mode_args])

    assert captured.value.code == 2
    assert "external notifications require durable decision journals" in capsys.readouterr().err


def test_external_notification_requires_persisted_receipt() -> None:
    with pytest.raises(AppendOnlyWriteError, match="永続化済み判断batch"):
        fx_briefing._bind_notification_receipt_or_fail(
            {"content": "candidate"},
            [],
            require_for_external_send=True,
        )


def test_external_notification_rejects_excessively_old_run_slot(capsys) -> None:
    old_slot = datetime.now(UTC) - timedelta(hours=2)
    old_slot = old_slot.replace(
        minute=old_slot.minute - old_slot.minute % 5,
        second=0,
        microsecond=0,
    )

    with pytest.raises(SystemExit) as captured:
        fx_briefing.main(["--run-slot", old_slot.isoformat()])

    assert captured.value.code == 2
    assert "older than 65 minutes" in capsys.readouterr().err


def _approved_overall_policy_registry(path, candidate_id: str) -> None:
    candidate = to.TradeImprovementCandidate(
        candidate_id,
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
        None, [candidate], now=datetime(2026, 7, 1, tzinfo=UTC)
    )
    registry = to.update_improvement_registry(
        registry, [candidate], now=datetime(2026, 7, 1, 1, tzinfo=UTC)
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


def _losing_policy_journal(path, candidate_id: str) -> None:
    """承認済みTP/SLで採点された負け履歴(自動停止の発火条件)を仕込む。

    3時間おきの下落系列で、各判断は次の価格点で必ずSLに到達する
    (=適用後期待Rが-1.0、サンプル数はグループ最低数を満たす)。
    """
    start = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    lines = []
    for i in range(20):
        close = 100.0 - 1.5 * i
        row = {
            "ts": (start + timedelta(hours=i * 3)).isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "conviction": 20,
            "composite": 0.6,
            "tech_score": 0.6,
            "news_score": 0.1,
            "close": close,
            "atr": 1.0,
            "stop": close - 1.0,
            "target1": close + 0.75,
            "target2": close + 1.5,
            "data_quality": 1.0,
            "target_policy": {
                "candidate_id": candidate_id,
                "scope": "overall",
                "key": "",
                "target1_r": 0.75,
                "target2_r": 1.5,
            },
            "features": {},
            "components": [],
        }
        row["schema_version"] = 2
        row["content_hash"] = canonical_row_hash(row)
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_auto_paused_policy_not_applied_to_same_run_plans(
    tmp_path,
    capsys,
    isolated_fx_briefing_runtime,
) -> None:
    """実行中に自動停止した承認済みTP/SLは、その実行のプランにも適用しない。

    修正前は実行冒頭のレジストリで注入器を作っていたため、悪化を検知して
    auto_paused に落としたポリシーがその回のプランへ最後まで適用されていた。
    """
    if not is_market_open(datetime.now(UTC)):
        pytest.skip("FX市場休場中はSL/TP付きプランが生成されないため検証不能")

    candidate_id = "pol-overall-tp"
    journal_path = tmp_path / "briefing_journal.jsonl"
    learning_path = tmp_path / "briefing_learning.json"
    promotion_path = tmp_path / "promotion_state.json"
    registry_path = tmp_path / "trade_improvement_candidates.json"
    _approved_overall_policy_registry(registry_path, candidate_id)
    _losing_policy_journal(journal_path, candidate_id)

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
                "--no-learning",
                "--no-trade-expectancy-guard",
                "--no-export-events",
                "--no-event-archive",
                "--trade-improvement-registry",
                str(registry_path),
                "--symbols",
                "USDJPY",
            ]
        )

    assert rc == 0
    assert fx_briefing.DEFAULT_DECISION_LOG_PATH == (
        isolated_fx_briefing_runtime / "logs" / "briefing_decisions.jsonl"
    )
    assert fx_briefing.DEFAULT_DECISION_LOG_PATH.exists()
    # 負け履歴の悪化検知で、承認済みポリシーはこの実行内で自動停止される
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["candidates"][candidate_id]["stage"] == "auto_paused"
    # 今回追記されたプラン行(履歴20行の後)には停止済みポリシーを適用しない
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    new_rows = rows[20:]
    assert new_rows, "今回のプランがジャーナルへ追記されていること"
    assert any(row["direction"] in ("long", "short") for row in new_rows)
    for row in new_rows:
        assert row["target_policy"] == {}, (
            "自動停止したTP/SLポリシーが同一実行のプランに適用されている: "
            f"{row['target_policy']}"
        )
