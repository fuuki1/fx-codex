"""融合判断の定期取得スケジュール判定を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from fx_intel import decision_commit, decision_log, journal as decision_journal
from tools import fusion_capture_schedule as schedule

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "fusion_capture_schedule.py"
NOW = datetime(2026, 7, 16, 2, 30, tzinfo=UTC)


def _write_row(path: Path, timestamp: datetime | str) -> None:
    value = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    path.write_text(json.dumps({"ts": value, "symbol": "USDJPY"}) + "\n", encoding="utf-8")


def _pit_envelope(decision_id: str, timestamp: datetime) -> dict[str, object]:
    value = timestamp.isoformat()
    return {
        "ts": value,
        "prediction_time": value,
        "source_cutoff": value,
        "max_feature_available_time": value,
        "pit_eligible": True,
        "pit_contract": decision_journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "fusion",
        "producer": decision_journal.FUSION_PRODUCER,
        "producer_version": decision_journal.FUSION_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
    }


def _full_event(decision_id: str, timestamp: datetime) -> dict[str, object]:
    return {
        "schema": 1,
        "event_type": decision_log.EVENT_TYPE,
        "symbol": "USDJPY",
        "timeframe": "fusion",
        "source": "fx_briefing",
        "decision": {"direction": "neutral", "close": 150.0},
        **_pit_envelope(decision_id, timestamp),
    }


def _compact_row(decision_id: str, timestamp: datetime) -> dict[str, object]:
    return {
        "symbol": "USDJPY",
        "direction": "neutral",
        "close": 150.0,
        **_pit_envelope(decision_id, timestamp),
    }


def _append_transaction(path: Path, decision_id: str, timestamp: datetime) -> None:
    decision_log.append_decision_transaction(
        [_full_event(decision_id, timestamp)],
        [_compact_row(decision_id, timestamp)],
        mode="fusion",
        full_path=path.parent / decision_commit.DEFAULT_COMMIT_FILENAME,
        compact_path=path,
    )


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


def test_committed_transaction_receipt_is_not_mistaken_for_missing_ts(tmp_path: Path) -> None:
    journal = tmp_path / "briefing_journal.jsonl"
    _append_transaction(journal, "fusion-committed", NOW - timedelta(minutes=54))

    last_row = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert last_row["event_type"] == decision_commit.RECEIPT_EVENT_TYPE
    assert "ts" not in last_row
    assert not schedule.capture_is_due(journal, now=NOW)


def test_uncommitted_transaction_does_not_suppress_due_capture(tmp_path: Path) -> None:
    journal = tmp_path / "briefing_journal.jsonl"
    _append_transaction(journal, "fusion-old", NOW - timedelta(minutes=60))

    decision_id = "fusion-uncommitted"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    decision_journal._append_journal_batch(  # noqa: SLF001 - crash-state fixture
        journal,
        [_compact_row(decision_id, NOW - timedelta(minutes=1))],
        decision_transaction_id=transaction_id,
    )

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
