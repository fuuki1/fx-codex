"""融合判断の定期取得スケジュール判定を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import fusion_capture_schedule as schedule

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "fusion_capture_schedule.py"
NOW = datetime(2026, 7, 16, 2, 30, tzinfo=UTC)


def _write_row(path: Path, timestamp: datetime | str) -> None:
    value = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    path.write_text(json.dumps({"ts": value, "symbol": "USDJPY"}) + "\n", encoding="utf-8")


def test_missing_or_empty_journal_is_due(tmp_path: Path) -> None:
    journal = tmp_path / "fusion.jsonl"
    assert schedule.capture_is_due(journal, now=NOW)
    journal.write_text("\n", encoding="utf-8")
    assert schedule.capture_is_due(journal, now=NOW)


def test_capture_is_due_at_55_minutes_but_not_before(tmp_path: Path) -> None:
    journal = tmp_path / "fusion.jsonl"
    _write_row(journal, NOW - timedelta(minutes=54, seconds=59))
    assert not schedule.capture_is_due(journal, now=NOW)
    _write_row(journal, NOW - timedelta(minutes=55))
    assert schedule.capture_is_due(journal, now=NOW)


@pytest.mark.parametrize(
    "contents, reason",
    [
        ("{broken\n", "不正なJSON"),
        (json.dumps({"ts": "2026-07-16T02:00:00"}) + "\n", "timezone"),
        (json.dumps(["not", "an", "object"]) + "\n", "object"),
    ],
)
def test_invalid_last_row_fails_closed(tmp_path: Path, contents: str, reason: str) -> None:
    journal = tmp_path / "fusion.jsonl"
    journal.write_text(contents, encoding="utf-8")
    with pytest.raises(schedule.FusionCaptureScheduleError, match=reason):
        schedule.capture_is_due(journal, now=NOW)


def test_future_timestamp_fails_closed(tmp_path: Path) -> None:
    journal = tmp_path / "fusion.jsonl"
    _write_row(journal, NOW + timedelta(seconds=1))
    with pytest.raises(schedule.FusionCaptureScheduleError, match="未来"):
        schedule.capture_is_due(journal, now=NOW)


def test_cli_returns_not_due_and_invalid_codes(tmp_path: Path) -> None:
    journal = tmp_path / "fusion.jsonl"
    _write_row(journal, datetime.now(UTC))
    not_due = subprocess.run(
        [sys.executable, str(SCRIPT), "--journal", str(journal)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert not_due.returncode == schedule.NOT_DUE_EXIT_CODE
    journal.write_text("{broken\n", encoding="utf-8")
    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), "--journal", str(journal)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert invalid.returncode == schedule.INVALID_EXIT_CODE
    assert "schedule invalid" in invalid.stderr
