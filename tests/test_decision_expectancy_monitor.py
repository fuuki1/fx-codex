"""完全判断ログの期待R監視ランナーのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from fx_intel import decision_log, journal, price_history, price_path_adapter
from fx_intel.briefing import TradePlan
from fx_intel.timeframe import TimeframePlan

_MONITOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "decision_expectancy_monitor.py"
NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("decision_expectancy_monitor", _MONITOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision_event(ts: datetime, index: int) -> dict:
    return {
        "schema": 2,
        "event_type": "chart_decision",
        "decision_id": f"decision-{index}",
        "run_id": "test-run",
        "ts": ts.isoformat(),
        "mode": "per_timeframe",
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "prediction_time": ts.isoformat(),
        "source_cutoff": ts.isoformat(),
        "max_feature_available_time": ts.isoformat(),
        "pit_eligible": True,
        "pit_contract": "decision-journal-pit-v2",
        "producer": "timeframe_raw",
        "producer_version": "timeframe-journal-v2",
        "input_context_id": f"context:sha256:{index:064x}",
        "source_record_ids": [f"source-{index}"],
        "decision": {
            "symbol": "USDJPY",
            "timeframe": "1h",
            "horizon_hours": 1.0,
            "direction": "long",
            "conviction": 76,
            "tf_score": 0.62,
            "news_score": 0.15,
            "composite": 0.48,
            "close": 100.0,
            "atr": 1.0,
            "stop": 99.0,
            "target1": 101.0,
            "target2": 102.0,
            "data_quality": 1.0,
            "features": {"adx_1h": 15.0, "rating_4h": -0.4},
            "components": [{"key": "tech", "score": 0.62, "weight": 0.7}],
        },
    }


def _price(ts: datetime, close: float) -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "close": close,
        "high": close + 0.05,
        "low": close - 0.05,
    }


def _pit_price(
    ts: datetime,
    close: float,
    *,
    symbol: str = "USDJPY",
    timeframe: str = "1h",
) -> dict:
    return price_history.snapshot_entries(
        {
            symbol: {
                timeframe: {
                    "close": close,
                    "high": close + 0.05,
                    "low": close - 0.05,
                }
            }
        },
        now=ts,
        run_id=f"test-price-{symbol}-{timeframe}-{ts.timestamp()}",
        writer_id="test-suite",
    )[0]


def _shared_path_price(
    ts: datetime,
    close: float,
    *,
    symbol: str = "USDJPY",
) -> dict:
    bar = price_path_adapter.ClosedBar(
        symbol=symbol,
        bar_start=ts - timedelta(minutes=5),
        bar_end=ts,
        open=close,
        high=close + 0.05,
        low=close - 0.05,
        close=close,
        bid=close - 0.01,
        ask=close + 0.01,
        tick_count=5,
        available_time=ts,
    )
    return price_path_adapter.build_rows([bar], run_id=f"test-shared-{symbol}-{ts}")[0]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("mode", ["fusion", "per_timeframe"])
def test_production_compact_builder_preserves_actual_scoring_projection(mode) -> None:
    common = {
        "symbol": "USDJPY",
        "direction": "long",
        "conviction": 76,
        "news_score": 0.15,
        "composite": 0.48,
        "close": 100.0,
        "atr": 1.0,
        "stop": 99.0,
        "target1": 101.0,
        "target2": 102.0,
        "input_context_id": "context:production-builder",
        "quote_source_record_id": "quote:production-builder",
        "price_frame": {"basis": "mid", "captured_at": NOW.isoformat()},
    }
    if mode == "fusion":
        plan = TradePlan(tech_score=0.62, **common)
        event = decision_log.build_fusion_decision_events(
            [plan], now=NOW, analysis=SimpleNamespace(currencies={}), tech_map={}
        )[0]
        compact = journal.build_plan_rows(
            [plan],
            NOW,
            source_cutoff=NOW - timedelta(minutes=2),
            max_feature_available_time=NOW - timedelta(minutes=1),
            decision_ids=[str(event["decision_id"])],
        )[0]
    else:
        plan = TimeframePlan(timeframe="1h", horizon_hours=1.0, tf_score=0.62, **common)
        event = decision_log.build_timeframe_decision_events(
            {"USDJPY": [plan]},
            now=NOW,
            analysis=SimpleNamespace(currencies={}),
            tech_map={},
        )[0]
        compact = journal.build_timeframe_plan_rows(
            [plan],
            NOW,
            source_cutoff=NOW - timedelta(minutes=2),
            max_feature_available_time=NOW - timedelta(minutes=1),
            decision_ids=[str(event["decision_id"])],
        )[0]
    event.update(
        journal.pit_metadata_for_plan(
            plan,
            prediction_time=NOW,
            source_cutoff=NOW - timedelta(minutes=2),
            max_feature_available_time=NOW - timedelta(minutes=1),
            decision_id=str(event["decision_id"]),
            mode=mode,
        )
    )

    full_projection = decision_log.decision_event_to_scoring_entry(event)
    compact_projection = decision_log.decision_event_to_scoring_entry(compact)

    assert full_projection is not None and compact_projection is not None
    assert compact["pit_eligible"] is True
    assert compact_projection["price_frame"] == common["price_frame"]
    excluded_complete_audit_only = {"run_id", "learning_context"}
    assert {
        key: value
        for key, value in full_projection.items()
        if key not in excluded_complete_audit_only
    } == {
        key: value
        for key, value in compact_projection.items()
        if key not in excluded_complete_audit_only
    }


def _write_decisions(path: Path, rows: list[dict]) -> None:
    decision_log.append_decision_transaction(
        rows,
        rows,
        mode="per_timeframe",
        full_path=path,
        compact_path=path.parent / "briefing_tf_journal.jsonl",
        committed_at=NOW,
    )


def _write_decisions_with_flat_compact(path: Path, rows: list[dict]) -> Path:
    compact_path = path.parent / "briefing_tf_journal.jsonl"
    compact_rows = []
    for row in rows:
        compact = dict(row["decision"])
        compact.update(
            {
                key: row.get(key)
                for key in (
                    "ts",
                    "prediction_time",
                    "source_cutoff",
                    "max_feature_available_time",
                    "pit_eligible",
                    "pit_contract",
                    "decision_id",
                    "mode",
                    "producer",
                    "producer_version",
                    "input_context_id",
                    "source_record_ids",
                    "symbol",
                    "timeframe",
                    "horizon_hours",
                )
            }
        )
        compact_rows.append(compact)
    decision_log.append_decision_transaction(
        rows,
        compact_rows,
        mode="per_timeframe",
        full_path=path,
        compact_path=compact_path,
        committed_at=NOW,
    )
    return compact_path


def _losing_rows(count: int = 25) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    prices: list[dict] = []
    for index in range(count):
        ts = NOW + timedelta(hours=index * 3)
        events.append(_decision_event(ts, index))
        prices.extend(
            [
                _price(ts + timedelta(minutes=20), 99.0),
                _price(ts + timedelta(minutes=40), 99.0),
                _price(ts + timedelta(hours=1), 99.0),
            ]
        )
    return events, prices


def test_decision_expectancy_monitor_writes_report_feedback_and_warns_on_missing_net_labels(
    monitor,
    tmp_path,
) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    outcome_path = tmp_path / "briefing_decision_outcomes.json"
    feedback_path = tmp_path / "briefing_decision_feedback.json"
    monitor_path = tmp_path / "decision_expectancy_monitor.json"
    timing_path = tmp_path / "decision_pipeline_stage_timings.jsonl"
    events, prices = _losing_rows()
    _write_decisions(decision_log_path, events)
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=outcome_path,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        stage_timing_log=timing_path,
        price_stale_minutes=None,
        now=NOW + timedelta(days=5),
    )

    outcome_report = json.loads(outcome_path.read_text(encoding="utf-8"))
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    payload = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert result["exit_code"] == 0
    assert payload["status"] == "warn"
    assert payload["summary"]["overall"]["gross_expectancy_r"] == -1.0
    assert payload["summary"]["overall"]["net_expectancy_r"] is None
    assert payload["summary"]["action_counts"]["quality_guard"] == 1
    assert payload["runner"]["decision_event_count"] == 25
    audit_snapshots = payload["runner"]["decision_source_snapshots"]
    assert {Path(row["source_path"]).name for row in audit_snapshots} == {
        "briefing_decisions.jsonl",
        "briefing_tf_journal.jsonl",
    }
    assert all(len(row["source_sha256"]) == 64 for row in audit_snapshots)
    assert outcome_report["scored_outcomes"] == 25
    assert outcome_report["pit_required"] is True
    assert outcome_report["pit_contract"] == "decision-journal-pit-v2"
    assert outcome_report["failure_reason_summary"]
    assert feedback["cells"]["USDJPY|1h|long"]["action"] == "quality_guard"
    assert feedback["cells"]["USDJPY|1h|long"]["block"] is False
    timing_rows = [json.loads(line) for line in timing_path.read_text().splitlines()]
    assert [row["stage"] for row in timing_rows] == [
        "decision_log_reader_open",
        "price_journal_read",
        "price_health_build",
        "decision_log_read_and_outcome_score",
        "decision_feedback_derive",
        "monitor_snapshot_build",
        "outcome_report_save",
        "decision_feedback_save",
        "monitor_save",
    ]
    assert all(row["status"] == "ok" for row in timing_rows)


def test_decision_expectancy_monitor_marks_immature_horizon_pending(
    monitor,
    tmp_path,
) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    monitor_path = tmp_path / "decision_expectancy_monitor.json"
    _write_decisions(decision_log_path, [_decision_event(NOW, 1)])
    _write_jsonl(
        prices_path,
        [_pit_price(NOW, 100.0)],
    )

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=tmp_path / "outcomes.json",
        feedback_json_path=tmp_path / "feedback.json",
        monitor_json_path=monitor_path,
        price_stale_minutes=60.0,
        now=NOW + timedelta(minutes=30),
    )

    payload = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert result["exit_code"] == 0
    assert payload["status"] == "pending"
    reasons = payload["summary"]["tradable_zero_reasons"]
    assert reasons["pending_count"] == 1
    assert reasons["blocking_count"] == 0
    assert reasons["reasons"][0]["key"] == "pending_horizon_not_mature"


def test_decision_expectancy_monitor_fails_stale_price_series(monitor, tmp_path) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    monitor_path = tmp_path / "decision_expectancy_monitor.json"
    _write_decisions(decision_log_path, [_decision_event(NOW, 1)])
    _write_jsonl(prices_path, [_pit_price(NOW, 100.0)])

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=tmp_path / "outcomes.json",
        feedback_json_path=tmp_path / "feedback.json",
        monitor_json_path=monitor_path,
        now=NOW + timedelta(hours=2),
    )

    payload = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert result["exit_code"] == 1
    assert payload["status"] == "fail"
    assert payload["summary"]["price_health"]["status"] == "fail"
    assert payload["summary"]["price_health"]["age_minutes"] == 120.0


def test_decision_expectancy_monitor_cli_quiet_returns_exit_code(monitor, tmp_path) -> None:
    decision_log_path = tmp_path / "decisions.jsonl"
    prices_path = tmp_path / "prices.jsonl"
    events, prices = _losing_rows()
    _write_decisions(decision_log_path, events)
    _write_jsonl(prices_path, prices)

    exit_code = monitor.main(
        [
            "--decision-log",
            str(decision_log_path),
            "--prices",
            str(prices_path),
            "--outcome-json",
            str(tmp_path / "outcomes.json"),
            "--feedback-json",
            str(tmp_path / "feedback.json"),
            "--monitor-json",
            str(tmp_path / "monitor.json"),
            "--quiet",
        ]
    )

    assert exit_code == 1


def test_compact_feedback_profile_matches_complete_actual_decisions(monitor, tmp_path) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(decision_log_path, events)
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )

    full = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=None,
        monitor_json_path=tmp_path / "full-monitor.json",
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5),
    )
    compact = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=None,
        monitor_json_path=tmp_path / "compact-monitor.json",
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5),
    )

    assert compact["profile"].to_dict() == full["profile"].to_dict()
    assert compact["outcome_report"]["shadow_scoring"] == "not_requested"
    assert compact["monitor"]["scope"] == "actual_decisions_only"
    snapshot_paths = {
        Path(row["source_path"]).name
        for row in compact["monitor"]["runner"]["decision_source_snapshots"]
    }
    assert snapshot_paths == {"briefing_tf_journal.jsonl", "briefing_decisions.jsonl"}


def test_feedback_refresh_preserves_prior_profile_when_price_is_stale(monitor, tmp_path) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    feedback_path = tmp_path / "feedback.json"
    compact_path = _write_decisions_with_flat_compact(decision_log_path, [_decision_event(NOW, 1)])
    _write_jsonl(prices_path, [_pit_price(NOW, 100.0)])
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(hours=2),
    )

    assert result["exit_code"] == 1
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_complete_audit_rejects_malformed_full_tail_and_preserves_report(
    monitor,
    tmp_path,
) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    _write_decisions(full_path, [_decision_event(NOW - timedelta(hours=2), 1)])
    with full_path.open("ab") as handle:
        handle.write(b"{malformed-tail\n")
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(prices_path, [_pit_price(NOW - timedelta(hours=1), 99.0)])
    outcome_path = tmp_path / "outcomes.json"
    outcome_path.write_text('{"preserved":true}\n', encoding="utf-8")

    with pytest.raises(monitor.journal.JournalIntegrityError, match="malformed JSON row"):
        monitor.run_decision_expectancy_monitor(
            decision_log_path=full_path,
            prices_path=prices_path,
            outcome_json_path=outcome_path,
            feedback_json_path=None,
            monitor_json_path=tmp_path / "audit-monitor.json",
            now=NOW,
        )

    assert outcome_path.read_text(encoding="utf-8") == '{"preserved":true}\n'
    assert not (tmp_path / "audit-monitor.json").exists()


def test_required_price_contexts_matches_published_per_timeframe_consumer_scope(
    monitor,
) -> None:
    report = {
        "outcomes": [
            {
                "ts": (NOW - timedelta(minutes=5)).isoformat(),
                "symbol": "USDJPY",
                "mode": "fusion",
                "timeframe": "fusion",
            },
            {
                "ts": (NOW - timedelta(days=17)).isoformat(),
                "symbol": "USDJPY",
                "mode": "per_timeframe",
                "timeframe": "1h",
            },
        ]
    }

    contexts = monitor._required_price_contexts(report)

    assert contexts == (("USDJPY", "per_timeframe", "1h"),)


def test_feedback_refresh_rejects_future_price_and_preserves_profile(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(
        full_path, [_decision_event(NOW - timedelta(hours=2), 1)]
    )
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [
            _pit_price(NOW - timedelta(hours=1), 99.0),
            _pit_price(NOW + timedelta(hours=1), 99.0),
        ],
    )
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 1
    assert health["quality"]["future_timestamp_rows"] == 1
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_complete_audit_excludes_future_price_and_preserves_feedback(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    _write_decisions(full_path, [_decision_event(NOW - timedelta(hours=2), 1)])
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(prices_path, [_pit_price(NOW + timedelta(hours=1), 99.0)])
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=full_path,
        prices_path=prices_path,
        outcome_json_path=tmp_path / "outcomes.json",
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "audit-monitor.json",
        now=NOW,
    )

    outcomes = result["outcome_report"]["outcomes"]
    health = result["monitor"]["summary"]["price_health"]
    assert health["quality"]["future_timestamp_rows"] == 1
    assert all(row["realized_r"] is None for row in outcomes)
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_feedback_refresh_rejects_malformed_price_tail(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(full_path, [_decision_event(NOW, 1)])
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    valid = json.dumps(_pit_price(NOW, 100.0), ensure_ascii=False)
    prices_path.write_text(f"{valid}\n{{bad json}}\n", encoding="utf-8")
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    with pytest.raises(monitor.journal.JournalIntegrityError, match="malformed JSONL"):
        monitor.run_decision_expectancy_monitor(
            decision_log_path=None,
            decision_journal_paths=[compact_path],
            prices_path=prices_path,
            outcome_json_path=None,
            feedback_json_path=feedback_path,
            monitor_json_path=tmp_path / "refresh-monitor.json",
            feedback_refresh_only=True,
            allow_continuity_bootstrap=True,
            now=NOW,
        )

    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_feedback_refresh_requires_fresh_price_for_scored_context(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(
        full_path, [_decision_event(NOW - timedelta(hours=2), 1)]
    )
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [
            _pit_price(NOW - timedelta(hours=1), 99.0),
            _pit_price(NOW, 1.2, symbol="EURUSD"),
        ],
    )
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 1
    assert health["failed_contexts"] == ["USDJPY|per_timeframe|1h"]
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_feedback_refresh_accepts_stale_fusion_with_fresh_active_and_shared_paths(
    monitor,
    tmp_path,
) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(
        full_path, [_decision_event(NOW - timedelta(hours=2), 1)]
    )
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [
            _pit_price(NOW, 99.0),
            _shared_path_price(NOW, 99.0),
            _pit_price(NOW - timedelta(hours=4), 99.0, timeframe="fusion"),
        ],
    )

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=tmp_path / "feedback.json",
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        require_shared_price_path=True,
        active_symbols=("USDJPY",),
        active_timeframes=("1h",),
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 0
    assert health["status"] == "pass"
    assert health["failed_shared_path_symbols"] == []
    assert health["shared_path_health"][0]["latest_ts"] == NOW.isoformat()


def test_feedback_refresh_rejects_stale_shared_path_and_preserves_profile(
    monitor,
    tmp_path,
) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(
        full_path, [_decision_event(NOW - timedelta(hours=2), 1)]
    )
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [
            _pit_price(NOW, 99.0),
            _shared_path_price(NOW - timedelta(hours=4), 99.0),
            # A scanner snapshot may use the reserved timeframe name, but its
            # forming OHLC is not trusted shared-path evidence.
            _pit_price(NOW, 99.0, timeframe="5m_datafeed"),
        ],
    )
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        require_shared_price_path=True,
        active_symbols=("USDJPY",),
        active_timeframes=("1h",),
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 1
    assert health["failed_shared_path_symbols"] == ["USDJPY"]
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_shared_path_freshness_uses_market_open_age_over_weekend(monitor, tmp_path) -> None:
    friday = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    sunday = datetime(2026, 7, 12, 23, 0, tzinfo=UTC)
    prices_path = tmp_path / "prices.jsonl"
    rows = [
        _pit_price(sunday, 100.0),
        _shared_path_price(friday, 100.0),
    ]
    _write_jsonl(prices_path, rows)

    health = monitor._price_file_health(
        prices_path,
        rows,
        now=sunday,
        stale_minutes=15.0,
        quality={
            "strict_pit": True,
            "invalid_timestamp_rows": 0,
            "future_timestamp_rows": 0,
            "invalid_canonical_rows": 0,
        },
        required_contexts=(("USDJPY", "per_timeframe", "1h"),),
        required_shared_path_symbols=("USDJPY",),
        shared_path_stale_minutes=180.0,
    )

    shared = health["shared_path_health"][0]
    assert health["status"] == "pass"
    assert shared["age_basis"] == "fx_market_open_time"
    assert shared["age_minutes"] == 120.0
    assert shared["wall_age_minutes"] == 3060.0


def test_default_shared_path_freshness_covers_delayed_hourly_adapter(
    monitor,
    tmp_path,
) -> None:
    prices_path = tmp_path / "prices.jsonl"
    rows = [
        _pit_price(NOW, 100.0),
        _shared_path_price(NOW - timedelta(minutes=200), 100.0),
    ]
    _write_jsonl(prices_path, rows)

    health = monitor._price_file_health(
        prices_path,
        rows,
        now=NOW,
        stale_minutes=15.0,
        quality={
            "strict_pit": True,
            "invalid_timestamp_rows": 0,
            "future_timestamp_rows": 0,
            "invalid_canonical_rows": 0,
        },
        required_contexts=(("USDJPY", "per_timeframe", "1h"),),
        required_shared_path_symbols=("USDJPY",),
    )

    assert monitor.DEFAULT_SHARED_PRICE_PATH_STALE_MINUTES == 210.0
    assert health["status"] == "pass"
    assert health["shared_path_health"][0]["age_minutes"] == 200.0


def test_retired_per_timeframe_symbol_is_scored_without_freshness_veto(
    monitor,
    tmp_path,
) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    usd = _decision_event(NOW - timedelta(hours=2), 1)
    retired = _decision_event(NOW - timedelta(hours=2), 2)
    retired["symbol"] = "GBPUSD"
    retired["decision"]["symbol"] = "GBPUSD"
    compact_path = _write_decisions_with_flat_compact(full_path, [usd, retired])
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [
            _pit_price(NOW, 99.0),
            _shared_path_price(NOW, 99.0),
            _pit_price(NOW - timedelta(hours=1), 99.0, symbol="GBPUSD"),
        ],
    )

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=tmp_path / "feedback.json",
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        require_shared_price_path=True,
        active_symbols=("USDJPY",),
        active_timeframes=("1h",),
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 0
    assert result["outcome_report"]["scored_outcomes"] == 2
    assert health["required_context_count"] == 1
    assert health["failed_contexts"] == []


def test_feedback_refresh_rejects_naive_price_timestamp(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    compact_path = _write_decisions_with_flat_compact(full_path, [_decision_event(NOW, 1)])
    row = _pit_price(NOW, 100.0)
    row["ts"] = NOW.replace(tzinfo=None).isoformat()
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(prices_path, [row])
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text('{"preserved":true}\n', encoding="utf-8")

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=tmp_path / "refresh-monitor.json",
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW,
    )

    health = result["monitor"]["summary"]["price_health"]
    assert result["exit_code"] == 1
    assert health["quality"]["invalid_timestamp_rows"] == 1
    assert feedback_path.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_feedback_refresh_rejects_same_inode_prefix_rewrite(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(full_path, events)
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )
    feedback_path = tmp_path / "feedback.json"
    monitor_path = tmp_path / "refresh-monitor.json"
    first = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5),
    )
    published = feedback_path.read_bytes()
    original_inode = compact_path.stat().st_ino

    with compact_path.open("r+b") as handle:
        payload = handle.read()
        changed = payload.replace(b'"close": 100.0', b'"close": 101.0', 1)
        assert changed != payload
        handle.seek(0)
        handle.write(changed)
        handle.seek(0, os.SEEK_END)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())

    second = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5, minutes=1),
    )

    failures = second["monitor"]["summary"]["source_continuity"]["failures"]
    assert first["exit_code"] == 0
    assert compact_path.stat().st_ino == original_inode
    assert any(item.startswith("source_prefix_changed:") for item in failures)
    assert second["exit_code"] == 1
    assert feedback_path.read_bytes() == published


def _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, minutes):
    return monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5, minutes=minutes),
    )


def test_feedback_refresh_accepts_identical_inode_replacement_as_relocation(
    monitor, tmp_path
) -> None:
    # 2026-09 production: a byte-identical copy gets a new inode (and, after a
    # block clone, a new st_dev). The previously scored prefix still matches,
    # so this is a relocation, not a rollback; refusing it deadlocked refresh
    # forever because the anchor only advances on success.
    full_path = tmp_path / "briefing_decisions.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(full_path, events)
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )
    feedback_path = tmp_path / "feedback.json"
    monitor_path = tmp_path / "refresh-monitor.json"
    first = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 0)
    old_inode = prices_path.stat().st_ino
    replacement = tmp_path / "replacement-prices.jsonl"
    replacement.write_bytes(prices_path.read_bytes())
    replacement.replace(prices_path)

    second = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 1)

    continuity = second["monitor"]["summary"]["source_continuity"]
    assert first["exit_code"] == 0
    assert prices_path.stat().st_ino != old_inode
    assert continuity["status"] == "pass", continuity["failures"]
    assert [row["previous_inode"] for row in continuity["relocated_sources"]] == [old_inode]
    assert second["exit_code"] == 0


def test_feedback_refresh_rejects_replacement_that_rewrites_prior_prefix(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(full_path, events)
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )
    feedback_path = tmp_path / "feedback.json"
    monitor_path = tmp_path / "refresh-monitor.json"
    first = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 0)
    published = feedback_path.read_bytes()
    payload = prices_path.read_bytes()
    changed = payload.replace(b'"close": 99.0', b'"close": 98.0', 1)
    assert changed != payload
    replacement = tmp_path / "replacement-prices.jsonl"
    replacement.write_bytes(changed)
    replacement.replace(prices_path)

    second = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 1)

    failures = second["monitor"]["summary"]["source_continuity"]["failures"]
    assert first["exit_code"] == 0
    assert any(item.startswith("source_prefix_changed:") for item in failures)
    assert second["exit_code"] == 1
    assert feedback_path.read_bytes() == published


def test_feedback_refresh_accepts_logs_relocated_behind_symlinks(monitor, tmp_path) -> None:
    logs = tmp_path / "logs"
    hot = tmp_path / "hot"
    logs.mkdir()
    hot.mkdir()
    full_path = logs / "briefing_decisions.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(full_path, events)
    prices_path = logs / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )
    feedback_path = tmp_path / "feedback.json"
    monitor_path = tmp_path / "refresh-monitor.json"
    first = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 0)
    for source in (full_path, compact_path):
        moved = hot / source.name
        moved.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(moved)

    second = _refresh(monitor, compact_path, prices_path, feedback_path, monitor_path, 1)

    continuity = second["monitor"]["summary"]["source_continuity"]
    assert first["exit_code"] == 0
    assert continuity["status"] == "pass", continuity["failures"]
    moved_paths = sorted(Path(row["source_path"]).name for row in continuity["relocated_sources"])
    assert moved_paths == ["briefing_decisions.jsonl", "briefing_tf_journal.jsonl"]
    assert second["exit_code"] == 0


def _continuity_snapshot(path: Path) -> dict:
    stat = path.stat()
    return {
        "source_path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "read_offset_bytes": stat.st_size,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("prior_offset", ["full", "empty"])
def test_feedback_continuity_device_change_needs_nonempty_prefix_proof(
    monitor, tmp_path, prior_offset
) -> None:
    source = tmp_path / "briefing_tf_prices.jsonl"
    source.write_bytes(b'{"id": 1}\n')
    present = _continuity_snapshot(source)
    prior = {**present, "device": present["device"] + 1}
    if prior_offset == "empty":
        prior.update(read_offset_bytes=0, source_sha256=hashlib.sha256(b"").hexdigest())
    previous = tmp_path / "refresh-monitor.json"
    previous.write_text(
        json.dumps(
            {
                "monitor_contract": "fx-codex-decision-feedback-refresh-v1",
                "status": "ok",
                "continuity_anchor_snapshots": [prior],
            }
        ),
        encoding="utf-8",
    )

    continuity, _anchor = monitor._source_continuity(
        [present], previous_monitor_path=previous, allow_bootstrap=False
    )

    if prior_offset == "full":
        assert continuity["status"] == "pass"
        assert continuity["relocated_sources"][0]["previous_device"] == prior["device"]
    else:
        assert continuity["failures"] == [f"source_identity_changed:{source}"]


def test_feedback_refresh_cannot_bootstrap_after_monitor_deletion(monitor, tmp_path) -> None:
    full_path = tmp_path / "briefing_decisions.jsonl"
    events, prices = _losing_rows()
    compact_path = _write_decisions_with_flat_compact(full_path, events)
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    _write_jsonl(
        prices_path,
        [_pit_price(datetime.fromisoformat(row["ts"]), float(row["close"])) for row in prices],
    )
    feedback_path = tmp_path / "feedback.json"
    monitor_path = tmp_path / "refresh-monitor.json"
    first = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        price_stale_minutes=None,
        feedback_refresh_only=True,
        allow_continuity_bootstrap=True,
        now=NOW + timedelta(days=5),
    )
    published = feedback_path.read_bytes()
    replacement = tmp_path / "replacement-prices.jsonl"
    replacement.write_bytes(prices_path.read_bytes())
    replacement.replace(prices_path)
    monitor_path.unlink()

    second = monitor.run_decision_expectancy_monitor(
        decision_log_path=None,
        decision_journal_paths=[compact_path],
        prices_path=prices_path,
        outcome_json_path=None,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        price_stale_minutes=None,
        feedback_refresh_only=True,
        now=NOW + timedelta(days=5, minutes=1),
    )

    continuity = second["monitor"]["summary"]["source_continuity"]
    assert first["exit_code"] == 0
    assert continuity["bootstrap"] is False
    assert continuity["failures"] == ["previous_monitor_missing"]
    assert second["exit_code"] == 1
    assert feedback_path.read_bytes() == published


def _hold_exclusive(path: Path, release: threading.Event, locked: threading.Event) -> None:
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked.set()
        assert release.wait(timeout=10)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_source_snapshot_times_out_only_after_the_configured_bound(
    monitor, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "briefing_tf_prices.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    release, locked = threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_exclusive, args=(source, release, locked))
    writer.start()
    assert locked.wait(timeout=5)
    monkeypatch.setattr(monitor, "SOURCE_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError, match="decision source lock timeout"):
            monitor._snapshot_paths([source])
    finally:
        release.set()
        writer.join(timeout=5)


def test_source_snapshot_waits_for_a_slow_writer_within_the_bound(
    monitor, tmp_path, monkeypatch
) -> None:
    # A writer re-reading the whole prices file under LOCK_EX can take longer
    # than the old fixed 5s under I/O contention; the reader must wait it out.
    source = tmp_path / "briefing_tf_prices.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    release, locked = threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_exclusive, args=(source, release, locked))
    writer.start()
    assert locked.wait(timeout=5)
    monkeypatch.setattr(monitor, "SOURCE_LOCK_TIMEOUT_SECONDS", 5.0)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        snapshots = monitor._snapshot_paths([source])
    finally:
        release.set()
        timer.cancel()
        writer.join(timeout=5)

    assert snapshots[source.resolve()]["read_offset_bytes"] == source.stat().st_size


def test_source_lock_bound_exceeds_observed_writer_hold_time(monitor) -> None:
    assert monitor.SOURCE_LOCK_TIMEOUT_SECONDS >= 120.0
