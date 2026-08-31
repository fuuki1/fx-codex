"""Crash-safe compact journal batch visibility."""

from __future__ import annotations

import json
import hashlib

import pytest

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


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b'{"ok": true}\n{broken}\n', "malformed JSONL row"),
        (b'{"ok": true}\n[]\n', "non-object JSONL row"),
        (b'{"ok": true}', "incomplete JSONL row"),
        (b'{"ok": true}\n\xff\n', "malformed JSONL row"),
    ),
)
def test_strict_read_entries_rejects_incomplete_or_malformed_source(
    tmp_path, payload, message
) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(payload)

    with pytest.raises(journal.JournalIntegrityError, match=message):
        list(journal.read_entries(path, strict=True))


def test_read_entries_default_remains_tolerant_and_strict_audits_bytes(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"id": 1}\n{broken}\n')

    assert list(journal.read_entries(path)) == [{"id": 1}]
    with pytest.raises(journal.JournalIntegrityError):
        list(journal.read_entries(path, strict=True, audit={}))

    valid = tmp_path / "valid.jsonl"
    valid.write_bytes(b'{"id": 1}\n\n{"id": 2}\n')
    audit: dict[str, object] = {}
    assert list(journal.read_entries(valid, strict=True, audit=audit)) == [
        {"id": 1},
        {"id": 2},
    ]
    assert audit["physical_rows"] == 2
    assert audit["parsed_object_rows"] == 2
    assert audit["blank_rows"] == 1
    assert len(str(audit["source_sha256"])) == 64


def test_strict_read_entries_pins_complete_prefix_while_source_grows(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    first = b'{"id": 1}\n'
    path.write_bytes(first)
    boundary = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b'{"id": 2}\n')

    audit: dict[str, object] = {}
    assert list(journal.read_entries(path, strict=True, audit=audit, end_offset=boundary)) == [
        {"id": 1}
    ]
    assert audit["physical_rows"] == 1
    assert audit["source_sha256"] == hashlib.sha256(first).hexdigest()


def test_strict_read_entries_rejects_boundary_inside_row(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"id": 1}\n')

    with pytest.raises(journal.JournalIntegrityError, match="snapshot ends inside JSONL row"):
        list(journal.read_entries(path, strict=True, end_offset=5))
