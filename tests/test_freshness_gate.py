from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from fx_intel.freshness import evaluate_freshness_report

NOW = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)


def _report(tmp_path, *, overall: str = "ok", monitored_at: datetime = NOW):
    path = tmp_path / "freshness.json"
    path.write_text(
        json.dumps({"overall": overall, "monitor_timestamp": monitored_at.isoformat()}),
        encoding="utf-8",
    )
    return path


def test_recent_explicit_ok_allows_new_risk(tmp_path) -> None:
    gate = evaluate_freshness_report(_report(tmp_path), now=NOW)

    assert gate.allow_new_risk
    assert gate.status == "ok"


@pytest.mark.parametrize("overall", ["warning", "critical", "unknown", ""])
def test_any_non_ok_monitor_state_is_a_hard_veto(tmp_path, overall) -> None:
    gate = evaluate_freshness_report(_report(tmp_path, overall=overall), now=NOW)

    assert not gate.allow_new_risk


def test_missing_malformed_stale_and_future_reports_fail_closed(tmp_path) -> None:
    missing = evaluate_freshness_report(tmp_path / "missing.json", now=NOW)
    assert not missing.allow_new_risk and missing.status == "missing"

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{broken", encoding="utf-8")
    malformed = evaluate_freshness_report(malformed_path, now=NOW)
    assert not malformed.allow_new_risk and malformed.status == "invalid"

    stale = evaluate_freshness_report(
        _report(tmp_path, monitored_at=NOW - timedelta(minutes=11)), now=NOW
    )
    assert not stale.allow_new_risk and stale.status == "stale"

    future = evaluate_freshness_report(
        _report(tmp_path, monitored_at=NOW + timedelta(minutes=2)), now=NOW
    )
    assert not future.allow_new_risk and future.status == "future"


def test_naive_now_and_invalid_thresholds_are_rejected(tmp_path) -> None:
    path = _report(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_freshness_report(path, now=datetime(2026, 7, 10, 7, 0))
    with pytest.raises(ValueError, match="thresholds"):
        evaluate_freshness_report(path, now=NOW, max_report_age_seconds=0)
