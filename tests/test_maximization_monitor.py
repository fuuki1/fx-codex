"""最大化モニター運用ランナーのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from fx_intel import price_history as ph
from fx_intel.append_only import canonical_row_hash

_MONITOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "maximization_monitor.py"
NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("maximization_monitor", _MONITOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision(ts: datetime) -> dict:
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "long",
        "conviction": 70,
        "close": 100.0,
        "atr": 1.0,
        "stop": 99.0,
        "target1": 101.0,
        "target2": 102.0,
        "data_quality": 1.0,
    }


def _price(ts: datetime, close: float) -> dict:
    return ph.snapshot_entries(
        {"USDJPY": {"1h": {"close": close}}},
        now=ts,
        run_id=f"test-{ts.isoformat()}",
        writer_id="test-writer",
    )[0]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    prepared: list[dict] = []
    for row in rows:
        candidate = dict(row)
        if "content_hash" not in candidate:
            candidate["schema_version"] = 2
            candidate["content_hash"] = canonical_row_hash(candidate)
        prepared.append(candidate)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in prepared) + "\n",
        encoding="utf-8",
    )


def _losing_rows(count: int = 30) -> tuple[list[dict], list[dict]]:
    decisions: list[dict] = []
    prices: list[dict] = []
    for index in range(count):
        ts = NOW + timedelta(hours=index * 3)
        decisions.append(_decision(ts))
        prices.extend(
            [
                _price(ts + timedelta(minutes=20), 99.0),
                _price(ts + timedelta(minutes=40), 99.0),
                _price(ts + timedelta(hours=1), 99.0),
            ]
        )
    return decisions, prices


def test_maximization_monitor_writes_profile_monitor_and_candidates(monitor, tmp_path) -> None:
    journal_path = tmp_path / "briefing_tf_journal.jsonl"
    prices_path = tmp_path / "briefing_tf_prices.jsonl"
    profile_path = tmp_path / "briefing_maximization.json"
    monitor_path = tmp_path / "maximization_monitor.json"
    decisions, prices = _losing_rows()
    _write_jsonl(journal_path, decisions)
    _write_jsonl(prices_path, prices)

    result = monitor.run_maximization_monitor(
        journal_path=journal_path,
        prices_path=prices_path,
        profile_json_path=profile_path,
        monitor_json_path=monitor_path,
        now=NOW + timedelta(days=5),
    )

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    payload = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert result["exit_code"] == 1
    assert payload["status"] == "fail"
    assert payload["summary"]["action_counts"]["avoid"] == 1
    assert payload["improvement_candidates"][0]["action_type"] == "max_expectancy_avoid"
    assert profile["cells"]["USDJPY|1h|long"]["action"] == "avoid"


def test_maximization_monitor_cli_quiet_returns_exit_code(monitor, tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    prices_path = tmp_path / "prices.jsonl"
    decisions, prices = _losing_rows()
    _write_jsonl(journal_path, decisions)
    _write_jsonl(prices_path, prices)

    exit_code = monitor.main(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(prices_path),
            "--profile-json",
            str(tmp_path / "profile.json"),
            "--monitor-json",
            str(tmp_path / "monitor.json"),
            "--quiet",
        ]
    )

    assert exit_code == 1
