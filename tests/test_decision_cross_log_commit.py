"""Full/compact decision visibility requires one exact cross-log commit."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
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
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    with pytest.raises(
        decision_commit.DecisionCommitError,
        match="compact batch reference",
    ):
        decision_commit.append_commit(
            full_path,
            decision_ids=[decision_id],
            full_batch_sha256=str(full_batch["batch_sha256"]),
            full_batch_line_start_offset=int(full_batch["line_start_offset"]),
            full_batch_line_sha256=str(full_batch["line_sha256"]),
            compact_batch_sha256="0" * 64,
            compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
            compact_batch_byte_length=int(compact_batch["byte_length"]),
            compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )

    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_post_full_fsync_retry_is_idempotent_with_new_timestamp(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-post-fsync"
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

    def finalize(committed_at: datetime) -> dict[str, object]:
        return decision_commit.append_commit(
            full_path,
            decision_ids=[decision_id],
            full_batch_sha256=str(full_batch["batch_sha256"]),
            full_batch_line_start_offset=int(full_batch["line_start_offset"]),
            full_batch_line_sha256=str(full_batch["line_sha256"]),
            compact_batch_sha256=str(compact_batch["batch_sha256"]),
            compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
            compact_batch_byte_length=int(compact_batch["byte_length"]),
            compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
            mode="per_timeframe",
            committed_at=committed_at,
        )

    original_append = decision_commit._append_locked_line  # noqa: SLF001

    def crash_after_full_fsync(handle, path, serialized):
        offset = original_append(handle, path, serialized)
        if path == full_path.resolve():
            raise OSError("simulated crash after final full fsync")
        return offset

    monkeypatch.setattr(
        decision_commit,
        "_append_locked_line",
        crash_after_full_fsync,
    )
    with pytest.raises(OSError, match="after final full fsync"):
        finalize(NOW)
    full_size = full_path.stat().st_size
    compact_size = compact_path.stat().st_size

    monkeypatch.setattr(decision_commit, "_append_locked_line", original_append)
    recovered = finalize(NOW + timedelta(seconds=1))

    assert recovered["committed_at"] == NOW.isoformat()
    assert full_path.stat().st_size == full_size
    assert compact_path.stat().st_size == compact_size
    assert list(decision_commit.load_commits(full_path, verify_compact=False)) == [transaction_id]
    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        decision_id
    ]
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]


def test_compact_replacement_before_finalize_is_rejected(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-compact-replaced"
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
    full_size = full_path.stat().st_size
    replacement = tmp_path / "replacement-compact.jsonl"
    replacement.write_bytes(b"")
    os.replace(replacement, compact_path)

    with pytest.raises(
        decision_commit.DecisionCommitError,
        match="compact batch reference",
    ):
        decision_commit.append_commit(
            full_path,
            decision_ids=[decision_id],
            full_batch_sha256=str(full_batch["batch_sha256"]),
            full_batch_line_start_offset=int(full_batch["line_start_offset"]),
            full_batch_line_sha256=str(full_batch["line_sha256"]),
            compact_batch_sha256=str(compact_batch["batch_sha256"]),
            compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
            compact_batch_byte_length=int(compact_batch["byte_length"]),
            compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )

    assert full_path.stat().st_size == full_size
    assert decision_commit.load_commits(full_path, verify_compact=False) == {}
    assert list(decision_log.read_decision_events(full_path)) == []


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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
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


def test_distinct_valid_receipts_fail_closed_for_both_readers(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-conflicting-receipts"
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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )

    original_receipt = json.loads(compact_path.read_text(encoding="utf-8").splitlines()[-1])
    original_commit = json.loads(full_path.read_text(encoding="utf-8").splitlines()[-1])
    conflicting_commit = {
        **original_commit,
        "committed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    conflicting_commit["commit_record_sha256"] = decision_commit.canonical_sha256(
        {key: value for key, value in conflicting_commit.items() if key != "commit_record_sha256"}
    )
    commit_payload = decision_commit._serialized_json_line(conflicting_commit)  # noqa: SLF001
    conflicting_receipt = {
        **original_receipt,
        "committed_at": conflicting_commit["committed_at"],
        "full_commit_record_sha256": conflicting_commit["commit_record_sha256"],
        "full_commit_line_start_offset": full_path.stat().st_size,
        "full_commit_line_sha256": hashlib.sha256(commit_payload).hexdigest(),
    }
    conflicting_receipt["receipt_record_sha256"] = decision_commit.canonical_sha256(
        {key: value for key, value in conflicting_receipt.items() if key != "receipt_record_sha256"}
    )
    with compact_path.open("ab") as handle:
        handle.write(decision_commit._serialized_json_line(conflicting_receipt))  # noqa: SLF001
    with full_path.open("ab") as handle:
        handle.write(commit_payload)

    assert decision_commit.load_commits(full_path, verify_compact=False) == {}
    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_nan_receipt_is_ignored_without_crashing_compact_reader(tmp_path) -> None:
    full_path = tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision_id = "decision-nan-receipt"
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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )
    malformed = json.loads(compact_path.read_text(encoding="utf-8").splitlines()[-1])
    malformed["committed_at"] = float("nan")
    with compact_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(malformed, allow_nan=True) + "\n")

    assert decision_commit.validated_receipt(malformed) is None
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]


def test_prepared_receipt_without_commit_is_hidden_and_late_retry_fails_closed(
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
            compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
            compact_batch_byte_length=int(compact_batch["byte_length"]),
            compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
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

    with pytest.raises(
        decision_commit.DecisionCommitError,
        match="full batch line reference",
    ):
        commit("decision-late-a", full_a, compact_a)
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [
        "decision-committed-b"
    ]
    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        "decision-committed-b",
    ]
    assert list(decision_commit.load_commits(full_path, verify_compact=False)) == [
        decision_commit.transaction_id_for(["decision-committed-b"])
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
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
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
