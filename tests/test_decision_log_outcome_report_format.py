"""outcome reportのcompact書式テスト(実機で数百MBに達し5分毎に再読込されるため)。"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from unittest import mock

import pytest

from fx_intel import decision_log


def _report() -> dict[str, object]:
    return {
        "schema": 1,
        "pit_contract": "decision-journal-pit-v1",
        "pit_required": True,
        "summary": {"overall": {"evaluated": 2, "expected_r": 0.12}},
        "outcomes": [
            {
                "decision_id": "d1",
                "symbol": "USDJPY",
                "realized_net_r": 0.35,
                "first_touch": "terminal",
                "note": "日本語の理由",
                "failure_reasons": [],
            },
            {
                "decision_id": "d2",
                "symbol": "EURUSD",
                "realized_net_r": None,
                "first_touch": "unavailable",
                "failure_reasons": [{"key": "missing_cost"}],
            },
        ],
    }


def test_report_round_trips_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"
    report = _report()

    decision_log.save_outcome_report(report, path)

    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_report_is_written_without_indentation_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"

    decision_log.save_outcome_report(_report(), path)
    text = path.read_text(encoding="utf-8")

    # A compact separator layout has no newline padding and no ", " gaps.
    assert "\n" not in text
    assert ", " not in text
    assert '": ' not in text


def test_japanese_stays_readable_and_not_escaped(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"

    decision_log.save_outcome_report(_report(), path)
    text = path.read_text(encoding="utf-8")

    assert "日本語の理由" in text
    assert "\\u" not in text


def test_non_finite_values_are_rejected_not_silently_written(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"
    report = {"outcomes": [{"realized_net_r": float("inf")}]}

    decision_log.save_outcome_report(report, path)

    # _json_ready folds non-finite floats to null, keeping the file valid JSON.
    assert json.loads(path.read_text(encoding="utf-8")) == {"outcomes": [{"realized_net_r": None}]}


def test_replace_failure_preserves_previous_complete_report(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"
    previous = {"outcomes": [{"decision_id": "previous"}]}
    path.write_text(json.dumps(previous), encoding="utf-8")

    with mock.patch.object(decision_log.os, "replace", side_effect=OSError("replace failure")):
        with pytest.raises(OSError, match="replace failure"):
            decision_log.save_outcome_report(_report(), path)

    assert json.loads(path.read_text(encoding="utf-8")) == previous
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_concurrent_reader_sees_previous_report_until_atomic_replace(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.json"
    previous = {"outcomes": [{"decision_id": "previous"}]}
    path.write_text(json.dumps(previous), encoding="utf-8")
    temporary_is_partial = threading.Event()
    allow_completion = threading.Event()
    original_dump = decision_log.json.dump
    errors: list[BaseException] = []

    def slow_dump(payload, handle, **kwargs) -> None:
        handle.write("{")
        handle.flush()
        temporary_is_partial.set()
        assert allow_completion.wait(timeout=5)
        handle.seek(0)
        handle.truncate()
        original_dump(payload, handle, **kwargs)

    def writer() -> None:
        try:
            decision_log.save_outcome_report(_report(), path)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    with mock.patch.object(decision_log.json, "dump", side_effect=slow_dump):
        thread = threading.Thread(target=writer)
        thread.start()
        assert temporary_is_partial.wait(timeout=5)
        assert json.loads(path.read_text(encoding="utf-8")) == previous
        allow_completion.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8")) == _report()
