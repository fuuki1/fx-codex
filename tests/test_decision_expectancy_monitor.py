"""完全判断ログの期待R監視ランナーのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from fx_intel.append_only import canonical_row_hash
from fx_intel import decision_log as dl
from fx_intel import price_history as ph

_MONITOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "decision_expectancy_monitor.py"
NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("decision_expectancy_monitor", _MONITOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision_event(ts: datetime, _index: int) -> dict:
    mode = "per_timeframe"
    run_slot = ts.replace(
        minute=ts.minute - ts.minute % dl.RUN_CADENCE_MINUTES,
        second=0,
        microsecond=0,
    )
    run_id = dl._run_id(run_slot, mode)
    event = {
        "schema": 3,
        "event_type": "chart_decision",
        "decision_id": dl._decision_id(run_id, mode, "USDJPY", "1h"),
        "run_id": run_id,
        "run_slot": run_slot.isoformat(),
        "ts": ts.isoformat(),
        "mode": mode,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "decision": {
            "symbol": "USDJPY",
            "timeframe": "1h",
            "horizon_hours": 1.0,
            "direction": "long",
            "action": "long",
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
    return dl._bind_notification_batch([event])[0]


def _price(ts: datetime, close: float, *, symbol: str = "USDJPY") -> dict:
    return ph.snapshot_entries(
        {
            symbol: {
                "1h": {
                    "close": close,
                    "high": close + 0.05,
                    "low": close - 0.05,
                }
            }
        },
        now=ts,
        run_id=f"test-{ts.isoformat()}",
        writer_id="test-writer",
    )[0]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    prepared: list[dict] = []
    for row in rows:
        candidate = dict(row)
        schema = candidate.get("schema_version", candidate.get("schema"))
        if isinstance(schema, int) and schema >= 2 and "content_hash" not in candidate:
            candidate["content_hash"] = canonical_row_hash(candidate)
        prepared.append(candidate)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in prepared) + "\n",
        encoding="utf-8",
    )


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


def test_decision_expectancy_monitor_writes_report_feedback_and_fail_status(
    monitor,
    tmp_path,
) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    outcome_path = tmp_path / "briefing_decision_outcomes.json"
    feedback_path = tmp_path / "briefing_decision_feedback.json"
    monitor_path = tmp_path / "decision_expectancy_monitor.json"
    events, prices = _losing_rows()
    _write_jsonl(decision_log_path, events)
    _write_jsonl(prices_path, prices)

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=outcome_path,
        feedback_json_path=feedback_path,
        monitor_json_path=monitor_path,
        now=NOW + timedelta(days=5),
    )

    outcome_report = json.loads(outcome_path.read_text(encoding="utf-8"))
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    payload = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert result["exit_code"] == 1
    assert payload["status"] == "fail"
    assert payload["summary"]["overall"]["expectancy_r"] == -1.0
    assert payload["summary"]["action_counts"]["avoid"] == 1
    assert payload["runner"]["decision_event_count"] == 25
    assert outcome_report["scored_outcomes"] == 25
    assert outcome_report["failure_reason_summary"]
    assert feedback["cells"]["USDJPY|1h|long"]["action"] == "avoid"
    assert feedback["cells"]["USDJPY|1h|long"]["block"] is True


def test_decision_expectancy_monitor_marks_immature_horizon_pending(
    monitor,
    tmp_path,
) -> None:
    decision_log_path = tmp_path / "briefing_decisions.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    monitor_path = tmp_path / "decision_expectancy_monitor.json"
    _write_jsonl(decision_log_path, [_decision_event(NOW, 1)])
    _write_jsonl(
        prices_path,
        [_price(NOW + timedelta(minutes=30), 1.2, symbol="EURUSD")],
    )

    result = monitor.run_decision_expectancy_monitor(
        decision_log_path=decision_log_path,
        prices_path=prices_path,
        outcome_json_path=tmp_path / "outcomes.json",
        feedback_json_path=tmp_path / "feedback.json",
        monitor_json_path=monitor_path,
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
    _write_jsonl(decision_log_path, [_decision_event(NOW, 1)])
    _write_jsonl(prices_path, [_price(NOW, 100.0)])

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
    _write_jsonl(decision_log_path, events)
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
