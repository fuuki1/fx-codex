"""The incremental commit-scan cache must be indistinguishable from a full scan.

``load_commits`` re-read a 2.1 GB log on every five-minute briefing because the
commits live inside that same log. The cache skips the bytes already scanned, so
every test here asks the same question: does the cached answer still equal the
answer a full scan would have produced?

The dangerous cases are structural, not incidental: a batch and the commit that
publishes it can straddle the resume point, and a later duplicate must still be
able to demote a commit accepted before the resume point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

from fx_intel import decision_commit, decision_log, journal

NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


def _pit_envelope(decision_id: str, ts: datetime) -> dict[str, object]:
    return {
        "ts": ts.isoformat(),
        "prediction_time": ts.isoformat(),
        "source_cutoff": ts.isoformat(),
        "max_feature_available_time": ts.isoformat(),
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "per_timeframe",
        "producer": journal.TIMEFRAME_PRODUCER,
        "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
    }


def _full_event(decision_id: str, ts: datetime) -> dict[str, object]:
    return {
        "schema": 1,
        "event_type": decision_log.EVENT_TYPE,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "source": "fx_briefing",
        "decision": {"direction": "long", "close": 150.0},
        **_pit_envelope(decision_id, ts),
    }


def _compact_row(decision_id: str, ts: datetime) -> dict[str, object]:
    return {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "direction": "long",
        "close": 150.0,
        **_pit_envelope(decision_id, ts),
    }


def _write_committed(full_path, compact_path, decision_id: str, ts: datetime) -> None:
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path, [_full_event(decision_id, ts)], transaction_id=transaction_id
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path, [_compact_row(decision_id, ts)], decision_transaction_id=transaction_id
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
        committed_at=ts,
    )


def _paths(tmp_path):
    return (
        tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME,
        tmp_path / "briefing_tf_journal.jsonl",
    )


def test_cached_scan_equals_full_scan_after_appends(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    _write_committed(full_path, compact_path, "first", NOW)

    # Warm the cache, then append more so the next call resumes mid-file.
    decision_commit.load_commits(full_path)
    _write_committed(full_path, compact_path, "second", NOW + timedelta(minutes=5))

    assert decision_commit.load_commits(full_path) == decision_commit.load_commits(
        full_path, use_cache=False
    )


def test_cache_survives_a_batch_commit_pair_split_by_the_resume_point(tmp_path) -> None:
    """The resume point lands between a batch wrapper and its commit line."""

    full_path, compact_path = _paths(tmp_path)
    decision_id = "straddler"
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path, [_full_event(decision_id, NOW)], transaction_id=transaction_id
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path, [_compact_row(decision_id, NOW)], decision_transaction_id=transaction_id
    )

    # Cache now ends exactly after the batch wrapper, before its commit exists.
    decision_commit.load_commits(full_path)

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

    cached = decision_commit.load_commits(full_path)
    assert cached == decision_commit.load_commits(full_path, use_cache=False)
    assert decision_id in {
        decision_id
        for record in cached.values()
        for decision_id in list(record.get("decision_ids") or [])
    }


def test_truncated_log_discards_the_cache(tmp_path) -> None:
    """A shrunk file means the cached offset may point at different bytes."""

    full_path, compact_path = _paths(tmp_path)
    _write_committed(full_path, compact_path, "first", NOW)
    _write_committed(full_path, compact_path, "second", NOW + timedelta(minutes=5))
    decision_commit.load_commits(full_path)

    kept = full_path.read_text(encoding="utf-8").splitlines()[:2]
    full_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert decision_commit.load_commits(full_path) == decision_commit.load_commits(
        full_path, use_cache=False
    )


def test_corrupt_cache_falls_back_to_a_full_scan(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    _write_committed(full_path, compact_path, "first", NOW)
    decision_commit.load_commits(full_path)

    decision_commit.commit_scan_cache_path_for(full_path).write_text(
        "not json at all", encoding="utf-8"
    )

    assert decision_commit.load_commits(full_path) == decision_commit.load_commits(
        full_path, use_cache=False
    )


def test_cache_from_a_foreign_contract_is_ignored(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    _write_committed(full_path, compact_path, "first", NOW)
    expected = decision_commit.load_commits(full_path, use_cache=False)

    decision_commit.commit_scan_cache_path_for(full_path).write_text(
        json.dumps(
            {
                "contract": "some-other-cache-v9",
                "source_mtime_ns": 0,
                "scanned_bytes": 0,
                "pending": {},
                "commits": {},
                "conflicts": [],
            }
        ),
        encoding="utf-8",
    )

    assert decision_commit.load_commits(full_path) == expected


def test_chunked_scan_equals_single_pass_scan(tmp_path) -> None:
    """Folding values in chunks must equal folding them in one pass."""

    full_path, compact_path = _paths(tmp_path)
    for index in range(6):
        _write_committed(
            full_path, compact_path, f"decision-{index}", NOW + timedelta(minutes=index)
        )

    values = [
        json.loads(line) for line in full_path.read_text(encoding="utf-8").splitlines() if line
    ]
    single = decision_commit.commits_from_values(values)

    state = decision_commit.CommitScanState()
    for start in range(0, len(values), 3):
        decision_commit.advance_commit_scan(values[start : start + 3], state)

    assert state.commits == single
