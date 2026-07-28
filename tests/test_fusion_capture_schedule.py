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


def _commit_receipt(timestamp: datetime) -> str:
    """decision_commit がバッチ確定時に追記する receipt 行(tsを持たない)。"""

    return json.dumps(
        {
            "event_type": "decision_batch_commit",
            "committed_at": timestamp.isoformat(),
            "decision_transaction_id": "t" * 20,
            "compact_batch_line_start_offset": 0,
        }
    )


def test_trailing_commit_receipt_does_not_break_schedule(tmp_path: Path) -> None:
    """末尾が commit receipt でも、直前の判断行の ts で判定する。

    2026-07-28 の実機で、判断行の後ろに receipt が追記される構成になった結果
    `最終行のtsが空または文字列ではありません` で exit 2 が継続した回帰。
    """

    journal = tmp_path / "fusion.jsonl"
    decision_ts = NOW - timedelta(minutes=10)
    journal.write_text(
        json.dumps({"ts": decision_ts.isoformat(), "symbol": "USDJPY"})
        + "\n"
        + _commit_receipt(NOW)
        + "\n",
        encoding="utf-8",
    )

    assert schedule.latest_journal_timestamp(journal) == decision_ts
    # 10分しか経っていないので取得は不要
    assert not schedule.capture_is_due(journal, now=NOW)
    # 55分以上経てば取得対象になる
    assert schedule.capture_is_due(journal, now=decision_ts + timedelta(minutes=56))


def test_journal_with_only_receipts_is_due(tmp_path: Path) -> None:
    """判断行が無く receipt しか無い場合は「未取得」として due にする。"""

    journal = tmp_path / "fusion.jsonl"
    journal.write_text(_commit_receipt(NOW) + "\n", encoding="utf-8")

    assert schedule.latest_journal_timestamp(journal) is None
    assert schedule.capture_is_due(journal, now=NOW)


def test_receipt_between_decisions_uses_latest_decision(tmp_path: Path) -> None:
    """receipt を挟んでも最新の判断行が選ばれる。"""

    journal = tmp_path / "fusion.jsonl"
    older = NOW - timedelta(hours=3)
    newer = NOW - timedelta(minutes=5)
    journal.write_text(
        "\n".join(
            [
                json.dumps({"ts": older.isoformat(), "symbol": "USDJPY"}),
                _commit_receipt(older),
                json.dumps({"ts": newer.isoformat(), "symbol": "EURUSD"}),
                _commit_receipt(newer),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert schedule.latest_journal_timestamp(journal) == newer
