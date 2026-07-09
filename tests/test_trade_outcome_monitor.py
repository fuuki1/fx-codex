"""trade_outcome_monitor の運用ランナーテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from fx_intel import trade_outcome as to

_MONITOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "trade_outcome_monitor.py"
NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("trade_outcome_monitor", _MONITOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(ts: datetime, symbol: str, close: float, **overrides: object) -> dict:
    row = {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "direction": "neutral",
        "conviction": 0,
        "composite": 0.0,
        "tech_score": 0.0,
        "news_score": 0.0,
        "close": close,
        "atr": 1.0,
        "data_quality": 0.9,
    }
    row.update(overrides)
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _variant_rows(count: int = 20) -> list[dict]:
    rows: list[dict] = []
    for index in range(count):
        ts = NOW + timedelta(minutes=index)
        symbol = f"TST{index:02d}"
        rows.extend(
            [
                _entry(
                    ts,
                    symbol,
                    100.0,
                    direction="long",
                    conviction=65,
                    stop=99.0,
                    target1=101.0,
                    target2=102.0,
                ),
                _entry(ts + timedelta(hours=8), symbol, 100.8),
                _entry(ts + timedelta(hours=16), symbol, 99.0),
                _entry(ts + DAY, symbol, 99.0),
            ]
        )
    return rows


def test_monitor_runner_updates_registry_reports_and_ready_stage(monitor, tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    registry_path = tmp_path / "registry.json"
    monitor_path = tmp_path / "monitor.json"
    outcome_path = tmp_path / "outcomes.json"
    variant_path = tmp_path / "variants.json"
    _write_jsonl(journal_path, _variant_rows())

    first = monitor.run_trade_outcome_monitor(
        journal_path=journal_path,
        registry_path=registry_path,
        monitor_json_path=monitor_path,
        outcome_json_path=outcome_path,
        variant_json_path=variant_path,
        target1_r_candidates=[0.75, 1.0],
        target2_r_candidates=[1.5, 2.0],
        now=NOW + timedelta(days=2),
    )
    monitor.run_trade_outcome_monitor(
        journal_path=journal_path,
        registry_path=registry_path,
        monitor_json_path=monitor_path,
        outcome_json_path=outcome_path,
        variant_json_path=variant_path,
        target1_r_candidates=[0.75, 1.0],
        target2_r_candidates=[1.5, 2.0],
        now=NOW + timedelta(days=2, hours=1),
    )
    third = monitor.run_trade_outcome_monitor(
        journal_path=journal_path,
        registry_path=registry_path,
        monitor_json_path=monitor_path,
        outcome_json_path=outcome_path,
        variant_json_path=variant_path,
        target1_r_candidates=[0.75, 1.0],
        target2_r_candidates=[1.5, 2.0],
        now=NOW + timedelta(days=2, hours=2),
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    monitor_payload = json.loads(monitor_path.read_text(encoding="utf-8"))
    outcome_report = json.loads(outcome_path.read_text(encoding="utf-8"))
    variant_report = json.loads(variant_path.read_text(encoding="utf-8"))

    assert first["exit_code"] == 1
    assert third["exit_code"] == 1
    assert registry["paper_ready_count"] >= 1
    assert any(
        record["stage"] == "paper_ready" and record["action_type"] == "tp_sl_variant_paper_test"
        for record in registry["candidates"].values()
    )
    assert monitor_payload["runner"]["registry_updated"] is True
    assert monitor_payload["variant_retest"]["candidate_count"] >= 1
    assert outcome_report["summary"]["overall"]["expectancy_r"] == -1.0
    assert variant_report["best"]["target1_r"] == 0.75


def test_monitor_runner_auto_pauses_underperforming_approved_policy(monitor, tmp_path) -> None:
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
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    registry, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        now=NOW + timedelta(hours=2),
    )
    assert result["status"] == "approved"

    rows: list[dict] = []
    for index in range(to.MIN_GROUP_EXPECTANCY_SAMPLES):
        ts = NOW + timedelta(minutes=index)
        rows.extend(
            [
                _entry(
                    ts,
                    "USDJPY",
                    100.0,
                    direction="long",
                    conviction=65,
                    stop=99.0,
                    target1=100.75,
                    target2=101.5,
                    target_policy={
                        "candidate_id": candidate.candidate_id,
                        "scope": "overall",
                        "key": "",
                        "target1_r": 0.75,
                        "target2_r": 1.5,
                    },
                ),
                _entry(ts + DAY, "USDJPY", 98.8),
            ]
        )
    journal_path = tmp_path / "journal.jsonl"
    registry_path = tmp_path / "registry.json"
    _write_jsonl(journal_path, rows)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")

    run = monitor.run_trade_outcome_monitor(
        journal_path=journal_path,
        registry_path=registry_path,
        monitor_json_path=tmp_path / "monitor.json",
        outcome_json_path=None,
        variant_json_path=None,
        retest_variants=False,
        now=NOW + timedelta(hours=3),
    )
    saved = json.loads(registry_path.read_text(encoding="utf-8"))

    assert saved["candidates"][candidate.candidate_id]["stage"] == "auto_paused"
    assert run["monitor"]["runner"]["auto_paused_policy_count"] == 1
    assert any(alert["type"] == "auto_paused" for alert in run["monitor"]["alerts"])
