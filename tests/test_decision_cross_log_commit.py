"""Full/compact decision visibility requires one exact cross-log commit."""

from __future__ import annotations

from datetime import datetime, UTC
import json
import os
import tracemalloc

import pytest

from fx_intel import decision_commit, decision_log, journal

NOW = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)


def _pit_envelope(decision_id: str) -> dict[str, object]:
    return {
        "ts": NOW.isoformat(),
        "prediction_time": NOW.isoformat(),
        "source_cutoff": NOW.isoformat(),
        "max_feature_available_time": NOW.isoformat(),
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "per_timeframe",
        "producer": journal.TIMEFRAME_PRODUCER,
        "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
    }


def _full_event(decision_id: str) -> dict[str, object]:
    return {
        "schema": 1,
        "event_type": decision_log.EVENT_TYPE,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "source": "fx_briefing",
        "decision": {"direction": "long", "close": 150.0},
        **_pit_envelope(decision_id),
    }


def _compact_row(decision_id: str) -> dict[str, object]:
    return {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "direction": "long",
        "close": 150.0,
        **_pit_envelope(decision_id),
    }


def test_full_and_compact_are_hidden_until_exact_commit(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-1"
    transaction_id = decision_commit.transaction_id_for([decision_id])

    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    assert list(decision_log.read_decision_events(full_path)) == []

    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    assert list(journal.read_entries(compact_path)) == []

    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )

    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        decision_id
    ]
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]
    raw = [json.loads(line) for line in full_path.read_text(encoding="utf-8").splitlines()]
    assert [row["event_type"] for row in raw] == [
        decision_log.DECISION_BATCH_EVENT_TYPE,
        decision_commit.COMMIT_EVENT_TYPE,
    ]


def test_wrong_compact_hash_does_not_publish_either_log(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-2"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256="0" * 64,
        mode="per_timeframe",
        committed_at=NOW,
    )

    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_commit_mode_must_match_full_events_and_compact_filename(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-mode"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )
    lines = [json.loads(line) for line in full_path.read_text(encoding="utf-8").splitlines()]
    lines[-1]["mode"] = "fusion"
    unsigned = {key: value for key, value in lines[-1].items() if key != "commit_record_sha256"}
    lines[-1]["commit_record_sha256"] = decision_commit.canonical_sha256(unsigned)
    full_path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )

    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_commit_requires_canonical_full_wrapper_schema(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-wrapper-schema"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )
    original_stat = full_path.stat()
    lines = [json.loads(line) for line in full_path.read_text(encoding="utf-8").splitlines()]
    lines[0]["schema_version"] = 999
    full_path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    os.utime(
        full_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert decision_commit.load_commits(full_path, verify_compact=True) == {}
    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_prepared_receipt_without_full_commit_is_hidden_and_late_retry_recovers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"

    def append_pending(decision_id: str):
        transaction_id = decision_commit.transaction_id_for([decision_id])
        full_batch = decision_log.append_decision_events(
            full_path,
            [_full_event(decision_id)],
            transaction_id=transaction_id,
        )
        compact_batch = journal._append_journal_batch(  # noqa: SLF001
            compact_path,
            [_compact_row(decision_id)],
            decision_transaction_id=transaction_id,
        )
        return full_batch, compact_batch

    def commit(decision_id: str, full_batch: dict, compact_batch: dict) -> None:
        decision_commit.append_commit(
            full_path,
            decision_ids=[decision_id],
            full_batch_sha256=str(full_batch["batch_sha256"]),
            full_batch_line_start_offset=int(full_batch["line_start_offset"]),
            full_batch_line_sha256=str(full_batch["line_sha256"]),
            compact_batch_sha256=str(compact_batch["batch_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )

    full_a, compact_a = append_pending("decision-late-a")
    original_append = decision_commit._append_locked_line  # noqa: SLF001

    def fail_final_commit(handle, path, serialized):
        if path == full_path.resolve():
            raise OSError("simulated crash before final full commit")
        return original_append(handle, path, serialized)

    monkeypatch.setattr(decision_commit, "_append_locked_line", fail_final_commit)
    with pytest.raises(OSError, match="simulated crash"):
        commit("decision-late-a", full_a, compact_a)

    receipt_rows = [
        json.loads(line) for line in compact_path.read_text(encoding="utf-8").splitlines()
    ]
    assert receipt_rows[-1]["event_type"] == decision_commit.RECEIPT_EVENT_TYPE
    assert list(journal.read_entries(compact_path)) == []

    monkeypatch.setattr(decision_commit, "_append_locked_line", original_append)
    full_b, compact_b = append_pending("decision-committed-b")
    commit("decision-committed-b", full_b, compact_b)
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [
        "decision-committed-b"
    ]

    commit("decision-late-a", full_a, compact_a)
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [
        "decision-late-a",
        "decision-committed-b",
    ]
    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        "decision-late-a",
        "decision-committed-b",
    ]


def test_compact_reader_never_calls_full_history_commit_scan(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-bounded-seeks"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )

    def forbid_full_scan(*args, **kwargs):
        raise AssertionError("compact reader must use receipt offsets, not a full scan")

    monkeypatch.setattr(decision_commit, "load_commits", forbid_full_scan)
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]


def test_load_commits_streams_large_legacy_log_with_bounded_memory(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    line = json.dumps({"event_type": "legacy", "payload": "x" * 8192}) + "\n"
    with full_path.open("w", encoding="utf-8") as handle:
        for _ in range(1024):
            handle.write(line)

    tracemalloc.start()
    try:
        assert decision_commit.load_commits(full_path, verify_compact=False) == {}
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 4 * 1024 * 1024


def test_standalone_pit_event_is_never_visible(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    full_path.write_text(
        json.dumps(_full_event("uncommitted"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert list(decision_log.read_decision_events(full_path)) == []
    source_lines = list(decision_log.iter_decision_source(full_path))
    assert source_lines[0].error == "uncommitted_pit_event"
