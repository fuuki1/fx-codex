"""Fail-closed tests for prospective continuous-operation evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.data_platform_scorecard import Evidence, ScorecardInputError, compute_scorecard


def _write_report(
    directory: Path,
    report_date: str,
    *,
    qualifying: bool = True,
    file_name: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "report_date": report_date,
        "prospective_window_ok": True,
        "qualifying_day": qualifying,
        "raw_hash_verified": True,
        "replay_ok": True,
        "critical_incidents": 0,
        "primary_up": True,
        "secondary_up": True,
    }
    name = file_name or f"daily_report_{report_date}.json"
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _empty_bundle(path: Path) -> Path:
    path.mkdir()
    return path


def test_only_explicit_qualifying_days_receive_operation_points(tmp_path: Path) -> None:
    bundle = _empty_bundle(tmp_path / "bundle")
    operations = tmp_path / "operations"
    for day in range(1, 6):
        _write_report(operations, f"2026-08-{day:02d}")
    for day in range(6, 31):
        _write_report(operations, f"2026-08-{day:02d}", qualifying=False)

    result = compute_scorecard(Evidence.load(bundle, operations))

    operation = next(
        award for award in result["awards"] if award["section"] == "continuous_operation"
    )
    assert operation["points"] == pytest.approx(20 * 5 / 30)
    assert result["hard_cap"] <= 85


def test_false_underlying_field_cannot_be_overridden_by_qualifying_flag(tmp_path: Path) -> None:
    bundle = _empty_bundle(tmp_path / "bundle")
    operations = tmp_path / "operations"
    _write_report(operations, "2026-08-01")
    path = operations / "daily_report_2026-08-01.json"
    payload = json.loads(path.read_text())
    payload["raw_hash_verified"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = compute_scorecard(Evidence.load(bundle, operations))

    operation = next(
        award for award in result["awards"] if award["section"] == "continuous_operation"
    )
    assert operation["points"] == 0


def test_filename_must_match_report_date(tmp_path: Path) -> None:
    bundle = _empty_bundle(tmp_path / "bundle")
    operations = tmp_path / "operations"
    _write_report(
        operations,
        "2026-08-01",
        file_name="daily_report_2026-08-02.json",
    )

    with pytest.raises(ScorecardInputError, match="filename/date mismatch"):
        Evidence.load(bundle, operations)


def test_report_date_must_be_iso_date(tmp_path: Path) -> None:
    bundle = _empty_bundle(tmp_path / "bundle")
    operations = tmp_path / "operations"
    _write_report(operations, "not-a-date", file_name="daily_report_not-a-date.json")

    with pytest.raises(ScorecardInputError, match="invalid report_date"):
        Evidence.load(bundle, operations)


def test_copied_report_with_altered_filename_is_rejected(tmp_path: Path) -> None:
    bundle = _empty_bundle(tmp_path / "bundle")
    operations = tmp_path / "operations"
    _write_report(operations, "2026-08-01")
    original = operations / "daily_report_2026-08-01.json"
    (operations / "daily_report_2026-08-02.json").write_text(
        original.read_text(),
        encoding="utf-8",
    )

    with pytest.raises(ScorecardInputError, match="filename/date mismatch"):
        Evidence.load(bundle, operations)
