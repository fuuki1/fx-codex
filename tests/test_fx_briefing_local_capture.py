"""fx_briefing のローカル学習ログ収集モードのテスト。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

import fx_briefing
from fx_intel import trade_outcome as to
from fx_intel.evaluation_labels import (
    DEFAULT_COST_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from tests.support.prospective_registry import mark_candidate_ready_for_review


def _view(interval: str, rec: str, close: float, atr: float = 0.15):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 2, "NEUTRAL": 5}
    indicators = {
        "close": close,
        "bid": close - 0.005,
        "ask": close + 0.005,
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


def test_load_pit_decision_outcome_report_rejects_legacy_or_unrequired(tmp_path) -> None:
    path = tmp_path / "outcomes.json"
    path.write_text(json.dumps({"outcomes": [{"realized_net_r": 1.0}]}), encoding="utf-8")
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}

    path.write_text(
        json.dumps(
            {
                "pit_contract": fx_briefing.journal.DECISION_JOURNAL_PIT_CONTRACT,
                "pit_required": False,
                "outcomes": [{"realized_net_r": 1.0}],
            }
        ),
        encoding="utf-8",
    )
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}


def test_no_discord_writes_fusion_journal_without_canonical_outcomes(tmp_path, capsys) -> None:
    journal_path = tmp_path / "briefing_journal.jsonl"
    learning_path = tmp_path / "briefing_learning.json"
    promotion_path = tmp_path / "promotion_state.json"
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    decision_latest_path = tmp_path / "briefing_decisions_latest.json"
    decision_outcomes_path = tmp_path / "briefing_decision_outcomes.json"
    decision_feedback_path = tmp_path / "briefing_decision_feedback.json"
    stage_timing_path = tmp_path / "stage_timings.jsonl"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_LEARNING_PATH", learning_path),
        mock.patch.object(fx_briefing, "DEFAULT_PROMOTION_STATE", promotion_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", decision_log_path),
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LATEST_PATH", decision_latest_path),
        mock.patch.object(
            fx_briefing,
            "DEFAULT_FUSION_DECISION_LATEST_PATH",
            decision_latest_path,
        ),
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
                "--stage-timing-log",
                str(stage_timing_path),
                "--symbols",
                "USDJPY",
            ]
        )

    assert rc == 0
    assert journal_path.exists()
    assert learning_path.exists()
    assert decision_log_path.exists()
    assert decision_latest_path.exists()
    assert not decision_outcomes_path.exists()
    assert not decision_feedback_path.exists()
    rows = list(fx_briefing.journal.read_entries(journal_path))
    decision_rows = list(fx_briefing.decision_log.read_decision_events(decision_log_path))
    profile = json.loads(learning_path.read_text(encoding="utf-8"))
    assert rows and rows[0]["symbol"] == "USDJPY"
    assert rows[0]["pit_eligible"] is True
    assert (
        datetime.fromisoformat(rows[0]["source_cutoff"])
        <= datetime.fromisoformat(rows[0]["max_feature_available_time"])
        <= datetime.fromisoformat(rows[0]["prediction_time"])
    )
    assert rows[0]["ts"] == rows[0]["prediction_time"]
    assert rows[0]["decision_id"] == decision_rows[0]["decision_id"]
    assert rows[0]["source_record_ids"]
    assert rows[0]["pit_contract"] == fx_briefing.journal.DECISION_JOURNAL_PIT_CONTRACT
    assert decision_rows and decision_rows[0]["learning_context"]["promotion"]["stages"]
    assert "evaluated" in profile
    timing_rows = [
        json.loads(line)
        for line in stage_timing_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    timing_stages = {str(row["stage"]) for row in timing_rows}
    assert {
        "calendar_fetch",
        "technical_load",
        "decision_event_build",
        "decision_transaction_append",
        "latest_snapshot_save",
        "historical_scoring_deferred",
    } <= timing_stages
    ordered_stages = [str(row["stage"]) for row in timing_rows]
    assert ordered_stages.index("decision_transaction_append") < ordered_stages.index(
        "latest_snapshot_save"
    )
    assert ordered_stages.index("latest_snapshot_save") < ordered_stages.index(
        "historical_scoring_deferred"
    )
    assert all(row["status"] == "ok" for row in timing_rows)
    assert "Discord送信なし" in capsys.readouterr().out


def test_fusion_cannot_overwrite_canonical_timeframe_latest(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        fx_briefing.main(
            [
                "--decision-latest-output",
                str(fx_briefing.DEFAULT_DECISION_LATEST_PATH),
                "--dry-run",
            ]
        )

    assert error.value.code == 2
    assert "fusion cannot overwrite" in capsys.readouterr().err


def test_decision_batch_is_published_before_historical_scoring(tmp_path) -> None:
    calls: list[str] = []
    recorder = fx_briefing.stage_timing.StageTimingRecorder(
        None,
        pipeline="test",
    )
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    latest_path = tmp_path / "briefing_decisions_latest.json"

    def fail_read(_path):
        calls.append("read")
        raise OSError("historical log unavailable")

    with (
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", decision_log_path),
        mock.patch.object(
            fx_briefing.decision_log,
            "append_decision_transaction",
            side_effect=lambda *_args, **_kwargs: calls.append("append"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "save_latest_snapshot",
            side_effect=lambda *_args, **_kwargs: calls.append("latest"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "read_decision_events",
            side_effect=fail_read,
        ),
        pytest.raises(OSError, match="historical log unavailable"),
    ):
        fx_briefing._persist_decision_batch_with_timing(
            [{"decision_id": "decision-1"}],
            compact_rows=[{"decision_id": "decision-1"}],
            compact_path=tmp_path / "briefing_journal.jsonl",
            mode="fusion",
            prediction_time=datetime.now(UTC),
            timing=recorder,
            latest_snapshot_path=latest_path,
        )

    assert calls == ["append", "latest", "read"]
    assert [str(row["stage"]) for row in recorder.records] == [
        "decision_transaction_append",
        "latest_snapshot_save",
        "decision_log_read",
    ]
    assert recorder.records[-1]["status"] == "error"


def test_decision_batch_can_defer_historical_scoring_after_publication(tmp_path) -> None:
    calls: list[str] = []
    recorder = fx_briefing.stage_timing.StageTimingRecorder(None, pipeline="test")

    with (
        mock.patch.object(
            fx_briefing.decision_log,
            "append_decision_transaction",
            side_effect=lambda *_args, **_kwargs: calls.append("append"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "save_latest_snapshot",
            side_effect=lambda *_args, **_kwargs: calls.append("latest"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "read_decision_events",
            side_effect=AssertionError("deferred scorer must own the historical read"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "score_decision_events",
            side_effect=AssertionError("deferred scorer must own scoring"),
        ),
    ):
        result = fx_briefing._persist_decision_batch_with_timing(
            [{"decision_id": "decision-1"}],
            compact_rows=[{"decision_id": "decision-1"}],
            compact_path=tmp_path / "briefing_journal.jsonl",
            mode="per_timeframe",
            prediction_time=datetime.now(UTC),
            timing=recorder,
            latest_snapshot_path=tmp_path / "briefing_decisions_latest.json",
            score_history=False,
        )

    assert result is None
    assert calls == ["append", "latest"]
    assert [str(row["stage"]) for row in recorder.records] == [
        "decision_transaction_append",
        "latest_snapshot_save",
        "historical_scoring_deferred",
    ]


def _pit_transaction_row(
    decision_id: str,
    prediction_time: datetime,
) -> dict[str, object]:
    return {
        "ts": prediction_time.isoformat(),
        "prediction_time": prediction_time.isoformat(),
        "source_cutoff": prediction_time.isoformat(),
        "max_feature_available_time": prediction_time.isoformat(),
        "pit_eligible": True,
        "pit_contract": fx_briefing.journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "per_timeframe",
        "producer": fx_briefing.journal.TIMEFRAME_PRODUCER,
        "producer_version": fx_briefing.journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
    }


def test_next_run_scores_prior_committed_batch_once(tmp_path) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    latest_path = tmp_path / "briefing_decisions_latest.json"
    first_time = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    score_calls: list[list[str]] = []

    def event(decision_id: str, prediction_time: datetime) -> dict[str, object]:
        return {
            "schema": 1,
            "event_type": fx_briefing.decision_log.EVENT_TYPE,
            "source": "fx_briefing",
            "decision": {"direction": "standby", "close": 150.0},
            **_pit_transaction_row(decision_id, prediction_time),
        }

    def score(events, **_kwargs):
        score_calls.append([str(row["decision_id"]) for row in events])
        return {
            "pit_contract": fx_briefing.journal.DECISION_JOURNAL_PIT_CONTRACT,
            "pit_required": True,
            "outcomes": [],
        }

    with (
        mock.patch.object(fx_briefing, "DEFAULT_DECISION_LOG_PATH", decision_log_path),
        mock.patch.object(fx_briefing.decision_log, "score_decision_events", side_effect=score),
        mock.patch.object(fx_briefing.decision_log, "save_outcome_report"),
        mock.patch.object(
            fx_briefing.decision_feedback, "derive_decision_feedback", return_value={}
        ),
        mock.patch.object(fx_briefing.decision_feedback, "save_decision_feedback"),
    ):
        for offset, decision_id in enumerate(("run-1", "run-2")):
            prediction_time = first_time + timedelta(minutes=offset)
            fx_briefing._persist_decision_batch_with_timing(
                [event(decision_id, prediction_time)],
                compact_rows=[_pit_transaction_row(decision_id, prediction_time)],
                compact_path=compact_path,
                mode="per_timeframe",
                prediction_time=prediction_time,
                timing=fx_briefing.stage_timing.StageTimingRecorder(None, pipeline="test"),
                latest_snapshot_path=latest_path,
            )

    assert score_calls == [["run-1"], ["run-1", "run-2"]]
    assert [
        str(row["decision_id"])
        for row in fx_briefing.decision_log.read_decision_events(decision_log_path)
    ] == ["run-1", "run-2"]
    assert [str(row["decision_id"]) for row in fx_briefing.journal.read_entries(compact_path)] == [
        "run-1",
        "run-2",
    ]


def test_fusion_journal_failure_stops_full_decision_writer(tmp_path, capsys) -> None:
    journal_path = tmp_path / "briefing_journal.jsonl"
    with (
        mock.patch.object(fx_briefing, "DEFAULT_JOURNAL_PATH", journal_path),
        mock.patch("fx_intel.technicals.fetch_pair_technicals", side_effect=_tech_for),
        mock.patch("fx_intel.calendar.fetch_calendar", return_value=([], [])),
        mock.patch("fx_intel.news.fetch_news_for_symbols", return_value=([], [])),
        mock.patch("fx_intel.sentiment.analyze_market", return_value=_analysis()),
        mock.patch.object(
            fx_briefing.journal,
            "build_plan_rows",
            side_effect=OSError("read-only"),
        ),
        mock.patch.object(
            fx_briefing.decision_log,
            "append_decision_transaction",
            side_effect=AssertionError("must not run"),
        ),
    ):
        rc = fx_briefing.main(
            [
                "--no-discord",
                "--no-macro",
                "--no-ml",
                "--no-learning",
                "--no-trade-expectancy",
                "--no-export-events",
                "--no-event-archive",
                "--symbols",
                "USDJPY",
            ]
        )

    assert rc == fx_briefing.JOURNAL_WRITE_FAILURE_EXIT_CODE
    assert "ジャーナル書き込み失敗" in capsys.readouterr().err


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
            "label_version": NET_LABEL_VERSION,
            "label_provenance": NET_LABEL_PROVENANCE,
            "cost_model_id": DEFAULT_COST_MODEL_ID,
            "net_label_samples": to.MIN_EXPECTANCY_SAMPLES,
            "net_label_coverage": 1.0,
            "baseline_net_expectancy_r": -1.0,
            "candidate_net_expectancy_r": 0.75,
            "delta_net_expectancy_r": 1.75,
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
            "trial_count": 1,
            "trial_sharpes": [0.0],
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
    registry = mark_candidate_ready_for_review(
        registry,
        candidate.candidate_id,
        evaluated_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
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
        prediction_time = start + timedelta(hours=i * 3)
        lines.append(
            json.dumps(
                {
                    "ts": prediction_time.isoformat(),
                    "prediction_time": prediction_time.isoformat(),
                    "source_cutoff": (prediction_time - timedelta(minutes=2)).isoformat(),
                    "max_feature_available_time": (
                        prediction_time - timedelta(seconds=1)
                    ).isoformat(),
                    "pit_eligible": True,
                    "pit_contract": fx_briefing.journal.DECISION_JOURNAL_PIT_CONTRACT,
                    "decision_id": f"decision:{prediction_time.isoformat()}",
                    "mode": "fusion",
                    "producer": fx_briefing.journal.FUSION_PRODUCER,
                    "producer_version": fx_briefing.journal.FUSION_PRODUCER_VERSION,
                    "input_context_id": f"context:{prediction_time.isoformat()}",
                    "source_record_ids": [f"source:{prediction_time.isoformat()}"],
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
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_manual_fusion_does_not_mutate_policy_registry_or_outcomes(
    tmp_path,
    capsys,
    isolated_fx_briefing_runtime,
) -> None:
    """Manual fusion is observation-only and cannot write canonical policy/feedback."""

    candidate_id = "pol-overall-tp"
    journal_path = tmp_path / "briefing_journal.jsonl"
    learning_path = tmp_path / "briefing_learning.json"
    promotion_path = tmp_path / "promotion_state.json"
    registry_path = tmp_path / "trade_improvement_candidates.json"
    _approved_overall_policy_registry(registry_path, candidate_id)
    _losing_policy_journal(journal_path, candidate_id)
    registry_before = registry_path.read_bytes()

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
                "--no-journal",
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
    assert registry_path.read_bytes() == registry_before
    assert not fx_briefing.DEFAULT_DECISION_OUTCOMES_PATH.exists()
    assert not fx_briefing.DEFAULT_DECISION_FEEDBACK_PATH.exists()
