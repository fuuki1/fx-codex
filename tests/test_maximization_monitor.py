"""最大化モニター運用ランナーのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

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
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "neutral",
        "conviction": 0,
        "close": close,
        "atr": 1.0,
        "data_quality": 1.0,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
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
    assert payload["summary"]["action_counts"]["collect_samples"] == 1
    assert payload["summary"]["evaluation_unavailable_cell_count"] == 1
    assert payload["improvement_candidates"] == []
    assert profile["cells"]["USDJPY|1h|long"]["action"] == "collect_samples"


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


def test_maximization_monitor_default_is_not_canonical_bundle_member(monitor) -> None:
    args = monitor.build_parser().parse_args([])

    assert args.profile_json == monitor.DEFAULT_PROFILE_PATH
    assert args.profile_json != monitor.CANONICAL_PROFILE_PATH


def test_maximization_monitor_rejects_canonical_bundle_member(monitor, tmp_path) -> None:
    with pytest.raises(ValueError, match="owned by timeframe_learning_monitor"):
        monitor.run_maximization_monitor(
            journal_path=tmp_path / "unused-journal.jsonl",
            prices_path=tmp_path / "unused-prices.jsonl",
            profile_json_path=monitor.CANONICAL_PROFILE_PATH,
            monitor_json_path=tmp_path / "unused-monitor.json",
            now=NOW,
        )


def test_maximization_monitor_reserves_every_canonical_bundle_path(monitor, tmp_path) -> None:
    for index, reserved in enumerate(monitor._canonical_bundle_paths()):
        with pytest.raises(ValueError, match="owned by timeframe_learning_monitor"):
            monitor.run_maximization_monitor(
                journal_path=tmp_path / "unused-journal.jsonl",
                prices_path=tmp_path / "unused-prices.jsonl",
                profile_json_path=tmp_path / f"diagnostic-{index}.json",
                monitor_json_path=reserved,
                now=NOW,
            )


def test_maximization_monitor_rejects_symlink_and_case_aliases(monitor, tmp_path) -> None:
    alias = tmp_path / "manifest-alias.json"
    alias.symlink_to(monitor.CANONICAL_MANIFEST_PATH)
    with pytest.raises(ValueError, match="owned by timeframe_learning_monitor"):
        monitor.run_maximization_monitor(
            journal_path=tmp_path / "unused-journal.jsonl",
            prices_path=tmp_path / "unused-prices.jsonl",
            profile_json_path=tmp_path / "diagnostic-profile.json",
            monitor_json_path=alias,
            now=NOW,
        )

    case_alias = monitor.CANONICAL_MANIFEST_PATH.with_name(
        monitor.CANONICAL_MANIFEST_PATH.name.upper()
    )
    with pytest.raises(ValueError, match="owned by timeframe_learning_monitor"):
        monitor.run_maximization_monitor(
            journal_path=tmp_path / "unused-journal.jsonl",
            prices_path=tmp_path / "unused-prices.jsonl",
            profile_json_path=case_alias,
            monitor_json_path=tmp_path / "diagnostic-monitor.json",
            now=NOW,
        )
