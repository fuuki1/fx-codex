"""Crash-safe compact journal batch visibility."""

from __future__ import annotations

import json

from fx_intel import journal


def _write(path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_read_entries_hides_uncommitted_batch_and_keeps_next_complete_batch(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    _write(
        path,
        [
            {"ts": "legacy", "direction": "neutral"},
            {
                "journal_batch_id": "crashed",
                "journal_batch_index": 0,
                "journal_batch_size": 2,
                "ts": "hidden",
            },
            {
                "journal_batch_id": "complete",
                "journal_batch_index": 0,
                "journal_batch_size": 1,
                "ts": "visible",
            },
            {
                "event_type": journal.JOURNAL_BATCH_COMMIT,
                "journal_batch_id": "complete",
                "journal_batch_size": 1,
            },
        ],
    )

    assert [row["ts"] for row in journal.read_entries(path)] == ["legacy", "visible"]


def test_read_entries_rejects_commit_with_missing_or_reordered_rows(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    _write(
        path,
        [
            {
                "journal_batch_id": "bad",
                "journal_batch_index": 1,
                "journal_batch_size": 2,
                "ts": "hidden",
            },
            {
                "event_type": journal.JOURNAL_BATCH_COMMIT,
                "journal_batch_id": "bad",
                "journal_batch_size": 1,
            },
        ],
    )

    assert list(journal.read_entries(path)) == []
