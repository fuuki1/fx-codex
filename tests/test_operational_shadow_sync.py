from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

import pytest

from fx_intel import decision_commit, decision_log, journal
from fx_intel import operational_contracts as contracts
from fx_intel import operational_full_replay as full_replay
from fx_intel import operational_migration as migration
from fx_intel import operational_read_model as read_model
from fx_intel import operational_scheduler as scheduler
from fx_intel import operational_shadow_sync as shadow
from fx_intel import operational_store as store
from fx_intel import state_io

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.3")


def _decision(identifier: str, *, minutes: int = 0, pit: bool = True) -> dict[str, object]:
    stamp = NOW + timedelta(minutes=minutes)
    row: dict[str, object] = {
        "schema": 3,
        "event_type": "chart_decision",
        "decision_id": identifier,
        "ts": stamp.isoformat(),
        "source": "fx_briefing",
        "mode": "per_timeframe",
        "symbol": "USDJPY",
        "timeframe": "15m",
        "horizon_hours": 0.25,
        "decision": {"direction": "neutral"},
    }
    if pit:
        row.update(
            {
                "prediction_time": stamp.isoformat(),
                "source_cutoff": (stamp - timedelta(seconds=2)).isoformat(),
                "max_feature_available_time": (stamp - timedelta(seconds=1)).isoformat(),
                "pit_eligible": True,
                "pit_contract": contracts.DECISION_JOURNAL_PIT_CONTRACT,
                "producer": contracts.TIMEFRAME_PRODUCER,
                "producer_version": contracts.TIMEFRAME_PRODUCER_VERSION,
                "input_context_id": f"context-{identifier}",
                "source_record_ids": [f"source-{identifier}"],
            }
        )
    return row


def _price(identifier: str, *, minutes: int = 0) -> dict[str, object]:
    stamp = NOW + timedelta(minutes=minutes)
    row: dict[str, object] = {
        "schema_version": 3,
        "event_time": stamp.isoformat(),
        "available_time": stamp.isoformat(),
        "ingested_time": (stamp + timedelta(seconds=1)).isoformat(),
        "source": "oanda",
        "source_record_id": identifier,
        "symbol": "USDJPY",
        "timeframe": "15m",
        "ohlc_scope": "completed_bar",
        "close": 150.0,
    }
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    row["content_hash"] = hashlib.sha256(encoded).hexdigest()
    return row


def _line(row: object) -> bytes:
    return f"{json.dumps(row, ensure_ascii=False)}\n".encode()


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_bytes(b"".join(_line(row) for row in rows))


def _append_decision_batch(path: Path, rows: list[dict[str, object]]) -> None:
    legacy = [row for row in rows if row.get("pit_eligible") is not True]
    if legacy:
        with path.open("ab") as handle:
            handle.write(b"".join(_line(row) for row in legacy))
    current = [row for row in rows if row.get("pit_eligible") is True]
    if not current:
        return
    decision_ids = [str(row["decision_id"]) for row in current]
    transaction_id = decision_commit.transaction_id_for(decision_ids)
    full_batch = decision_log.append_decision_events(
        path,
        current,
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        path.parent / "briefing_tf_journal.jsonl",
        current,
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        path,
        decision_ids=decision_ids,
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


def _write_decision_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_bytes(b"")
    (path.parent / "briefing_tf_journal.jsonl").write_bytes(b"")
    _append_decision_batch(path, rows)


def _create_bootstrapped_candidate(
    tmp_path: Path,
    *,
    decision_rows: list[object] | None = None,
    price_rows: list[object] | None = None,
) -> tuple[Path, Path, Path, Path]:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    database = tmp_path / "candidate.sqlite3"
    parity_path = tmp_path / "parity.json"
    _write_decision_jsonl(
        decisions,
        [dict(row) for row in (decision_rows or [_decision("decision-1")])],
    )
    _write_jsonl(prices, price_rows or [_price("price-1")])
    with store.open_operational_writer(database, writer_id="migration") as opened:
        imported = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=store.file_sha256(decisions),
            price_path=prices,
            price_sha256=store.file_sha256(prices),
            run_id="test-migration",
            now=NOW,
        )
        opened.checkpoint("TRUNCATE")
        audit = opened.audit()
    parity = {
        "schema_version": 1,
        "parity_verdict": "parity_verified",
        "database_sha256": store.file_sha256(database),
        "database": {
            "path": str(database.resolve()),
            "rows": audit.to_dict()["rows"],
        },
        "sources": {
            "decisions": {
                "path": str(decisions.resolve()),
                "bytes": decisions.stat().st_size,
                "sha256": store.file_sha256(decisions),
            },
            "decision_support": [source.to_dict() for source in imported.decision_support_sources],
            "prices": {
                "path": str(prices.resolve()),
                "bytes": prices.stat().st_size,
                "sha256": store.file_sha256(prices),
            },
        },
        "migration_verdict": imported.verdict,
    }
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    shadow.bootstrap_candidate(
        database_path=database,
        parity_report_path=parity_path,
        decision_path=decisions,
        price_path=prices,
        writer_id="bootstrap",
    )
    return database, decisions, prices, parity_path


def test_bootstrap_then_append_sync_and_retry_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with store.open_operational_reader(database) as opened:
        compact_cursor_before = opened.source_cursor(
            f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}briefing_tf_journal.jsonl"
        )
        assert compact_cursor_before is not None
    _append_decision_batch(decisions, [_decision("decision-2", minutes=15)])
    with prices.open("ab") as handle:
        handle.write(_line(_price("price-2", minutes=15)))

    def forbid_full_history_scan(*args, **kwargs):
        raise AssertionError("incremental sync must not call load_commits")

    monkeypatch.setattr(decision_commit, "load_commits", forbid_full_history_scan)
    result = shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="shadow-sync",
    )
    retried = shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="shadow-sync-retry",
    )

    assert result["quality_failure"] is False
    assert result["sources"]["decisions"]["predictions_inserted"] == 1
    assert result["sources"]["prices"]["inserted"] == 1
    assert retried["sources"]["decisions"]["status"] == "no_change"
    assert retried["sources"]["prices"]["status"] == "no_change"
    with store.open_operational_reader(database) as opened:
        audit = opened.audit()
        assert audit.audit_events == 2
        assert audit.predictions == 2
        assert audit.price_points == 2
        assert audit.source_cursors == 3
        assert audit.source_chunks == 6
        assert audit.ingest_rejections == 0
        compact_cursor_after = opened.source_cursor(
            f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}briefing_tf_journal.jsonl"
        )
        assert compact_cursor_after is not None
        assert compact_cursor_after.byte_offset > compact_cursor_before.byte_offset
        assert (
            compact_cursor_after.byte_offset
            == (tmp_path / "briefing_tf_journal.jsonl").stat().st_size
        )


def test_duplicate_full_wrapper_bootstrap_and_replay_use_only_receipt_offset(
    tmp_path: Path,
) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    database = tmp_path / "candidate.sqlite3"
    parity_path = tmp_path / "parity.json"
    decision = _decision("same-wrapper-retry")
    transaction_id = decision_commit.transaction_id_for([str(decision["decision_id"])])
    decision_log.append_decision_events(
        decisions,
        [decision],
        transaction_id=transaction_id,
    )
    retried_full = decision_log.append_decision_events(
        decisions,
        [decision],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [decision],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        decisions,
        decision_ids=[str(decision["decision_id"])],
        full_batch_sha256=str(retried_full["batch_sha256"]),
        full_batch_line_start_offset=int(retried_full["line_start_offset"]),
        full_batch_line_sha256=str(retried_full["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )
    _write_jsonl(prices, [_price("price-1")])

    with store.open_operational_writer(database, writer_id="migration") as opened:
        imported = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=store.file_sha256(decisions),
            price_path=prices,
            price_sha256=store.file_sha256(prices),
            run_id="duplicate-wrapper-migration",
            now=NOW,
        )
        opened.checkpoint("TRUNCATE")
        audit = opened.audit()
    parity = {
        "schema_version": 1,
        "parity_verdict": "parity_verified",
        "database_sha256": store.file_sha256(database),
        "database": {
            "path": str(database.resolve()),
            "rows": audit.to_dict()["rows"],
        },
        "sources": {
            "decisions": {
                "path": str(decisions.resolve()),
                "bytes": decisions.stat().st_size,
                "sha256": store.file_sha256(decisions),
            },
            "decision_support": [source.to_dict() for source in imported.decision_support_sources],
            "prices": {
                "path": str(prices.resolve()),
                "bytes": prices.stat().st_size,
                "sha256": store.file_sha256(prices),
            },
        },
        "migration_verdict": imported.verdict,
    }
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    shadow.bootstrap_candidate(
        database_path=database,
        parity_report_path=parity_path,
        decision_path=decisions,
        price_path=prices,
        writer_id="duplicate-wrapper-bootstrap",
    )
    replay = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="duplicate-wrapper-replay",
    )

    assert imported.decisions.inserted == 1
    assert imported.decisions.excluded == 1
    assert imported.verdict == "parity_with_exclusions"
    assert audit.audit_events == 1
    assert audit.predictions == 1
    assert replay["verdict"] == "full_replay_verified"
    assert replay["sources"]["decisions"]["bootstrap_exclusions"] == 1


def test_duplicate_full_wrapper_incremental_sync_records_superseded_rejection(
    tmp_path: Path,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    decision = _decision("same-wrapper-incremental-retry", minutes=15)
    transaction_id = decision_commit.transaction_id_for([str(decision["decision_id"])])
    decision_log.append_decision_events(
        decisions,
        [decision],
        transaction_id=transaction_id,
    )
    retried_full = decision_log.append_decision_events(
        decisions,
        [decision],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [decision],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        decisions,
        decision_ids=[str(decision["decision_id"])],
        full_batch_sha256=str(retried_full["batch_sha256"]),
        full_batch_line_start_offset=int(retried_full["line_start_offset"]),
        full_batch_line_sha256=str(retried_full["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )

    synced = shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="duplicate-wrapper-incremental-sync",
    )
    replay = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="duplicate-wrapper-incremental-replay",
    )

    assert synced["quality_failure"] is True
    assert synced["sources"]["decisions"]["rejections"] == 1
    assert synced["sources"]["decisions"]["predictions_inserted"] == 1
    assert replay["verdict"] == "full_replay_verified"
    assert replay["sources"]["decisions"]["incremental_rejections"] == 1


def test_bootstrap_verified_snapshot_prefix_onto_live_append_only_sources(
    tmp_path: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    live = tmp_path / "live"
    snapshots.mkdir()
    live.mkdir()
    snapshot_decisions = snapshots / "decisions.jsonl"
    snapshot_prices = snapshots / "prices.jsonl"
    live_decisions = live / "decisions.jsonl"
    live_prices = live / "prices.jsonl"
    database = tmp_path / "candidate.sqlite3"
    parity_path = tmp_path / "parity.json"
    _write_decision_jsonl(snapshot_decisions, [_decision("decision-1")])
    _write_jsonl(snapshot_prices, [_price("price-1")])
    live_decisions.write_bytes(snapshot_decisions.read_bytes())
    live_prices.write_bytes(snapshot_prices.read_bytes())
    (live / "briefing_tf_journal.jsonl").write_bytes(
        (snapshots / "briefing_tf_journal.jsonl").read_bytes()
    )

    with store.open_operational_writer(database, writer_id="migration") as opened:
        migration.migrate_jsonl_candidate(
            opened,
            decision_path=snapshot_decisions,
            decision_sha256=store.file_sha256(snapshot_decisions),
            price_path=snapshot_prices,
            price_sha256=store.file_sha256(snapshot_prices),
            run_id="prefix-migration",
            now=NOW,
        )
        opened.checkpoint("TRUNCATE")
    with store.open_operational_reader(database) as opened:
        parity = migration.verify_candidate_parity(
            opened,
            decision_path=snapshot_decisions,
            decision_sha256=store.file_sha256(snapshot_decisions),
            price_path=snapshot_prices,
            price_sha256=store.file_sha256(snapshot_prices),
        ).to_dict()
    parity["database_sha256"] = store.file_sha256(database)
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    decision_prefix_bytes = live_decisions.stat().st_size
    price_prefix_bytes = live_prices.stat().st_size
    _append_decision_batch(live_decisions, [_decision("decision-2", minutes=15)])
    with live_prices.open("ab") as handle:
        handle.write(_line(_price("price-2", minutes=15)))

    bootstrap = shadow.bootstrap_candidate(
        database_path=database,
        parity_report_path=parity_path,
        decision_path=live_decisions,
        price_path=live_prices,
        writer_id="prefix-bootstrap",
        allow_source_prefix=True,
    )
    synced = shadow.sync_sources(
        database_path=database,
        decision_path=live_decisions,
        price_path=live_prices,
        writer_id="prefix-sync",
    )

    assert "bootstrapped_from_verified_live_prefix" in bootstrap["limitations"]
    with store.open_operational_reader(database) as opened:
        decision_cursor = opened.source_cursor("decisions")
        price_cursor = opened.source_cursor("prices")
        assert decision_cursor is not None
        assert price_cursor is not None
        assert decision_cursor.source_path == str(live_decisions.resolve())
        assert price_cursor.source_path == str(live_prices.resolve())
        assert decision_cursor.byte_offset > decision_prefix_bytes
        assert price_cursor.byte_offset > price_prefix_bytes
    assert synced["sources"]["decisions"]["audit_events_inserted"] == 1
    assert synced["sources"]["prices"]["inserted"] == 1


def test_bootstrap_rejects_compact_source_changed_after_parity(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    database = tmp_path / "candidate.sqlite3"
    parity_path = tmp_path / "parity.json"
    _write_decision_jsonl(decisions, [_decision("decision-1")])
    _write_jsonl(prices, [_price("price-1")])
    with store.open_operational_writer(database, writer_id="migration") as opened:
        migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=store.file_sha256(decisions),
            price_path=prices,
            price_sha256=store.file_sha256(prices),
            run_id="compact-drift-migration",
            now=NOW,
        )
        opened.checkpoint("TRUNCATE")
    with store.open_operational_reader(database) as opened:
        parity = migration.verify_candidate_parity(
            opened,
            decision_path=decisions,
            decision_sha256=store.file_sha256(decisions),
            price_path=prices,
            price_sha256=store.file_sha256(prices),
        ).to_dict()
    parity["database_sha256"] = store.file_sha256(database)
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    compact = tmp_path / "briefing_tf_journal.jsonl"
    compact.write_bytes(compact.read_bytes().replace(b"neutral", b"longxxx"))

    with pytest.raises(shadow.BootstrapEvidenceMismatch, match="SHA-256"):
        shadow.bootstrap_candidate(
            database_path=database,
            parity_report_path=parity_path,
            decision_path=decisions,
            price_path=prices,
            writer_id="bootstrap",
        )


def test_incremental_sync_rejects_compact_hash_drift(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    _append_decision_batch(
        decisions,
        [_decision("decision-after-bootstrap", minutes=15)],
    )
    compact = tmp_path / "briefing_tf_journal.jsonl"
    compact.write_bytes(compact.read_bytes().replace(b"neutral", b"longxxx"))

    with store.open_operational_writer(database, writer_id="compact-drift") as opened:
        before = opened.source_cursor("decisions")
        assert before is not None
        with pytest.raises(
            shadow.ShadowSyncError,
            match="compact batch verification",
        ):
            shadow.sync_one_source(
                opened,
                source_name="decisions",
                source_kind="decisions",
                source_path=decisions,
            )
        after = opened.source_cursor("decisions")
        assert after is not None
        assert after.byte_offset == before.byte_offset
        assert opened.audit().predictions == 1


def test_two_source_sync_rolls_back_decision_advance_when_price_fails(
    tmp_path: Path,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with store.open_operational_reader(database) as opened:
        initial_cursor = opened.source_cursor("decisions")
        initial_support_cursor = opened.source_cursor(
            f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}briefing_tf_journal.jsonl"
        )
        assert initial_cursor is not None
        assert initial_support_cursor is not None
        initial_offset = initial_cursor.byte_offset
        initial_support_offset = initial_support_cursor.byte_offset
    _append_decision_batch(decisions, [_decision("decision-2", minutes=15)])
    prices.write_bytes(b"")

    with pytest.raises(shadow.SourceRotationDetected):
        shadow.sync_sources(
            database_path=database,
            decision_path=decisions,
            price_path=prices,
            writer_id="atomic-rollback",
        )

    with store.open_operational_reader(database) as opened:
        cursor = opened.source_cursor("decisions")
        support_cursor = opened.source_cursor(
            f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}briefing_tf_journal.jsonl"
        )
        assert cursor is not None
        assert support_cursor is not None
        assert cursor.byte_offset == initial_offset
        assert support_cursor.byte_offset == initial_support_offset
        assert opened.audit().audit_events == 1


def test_scheduled_full_replay_catches_up_and_verifies_one_locked_snapshot(
    tmp_path: Path,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    _append_decision_batch(decisions, [_decision("decision-2", minutes=15)])
    with prices.open("ab") as handle:
        handle.write(_line(_price("price-2", minutes=15)))

    result = scheduler.run_scheduled(
        action="full-replay",
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        evidence_dir=tmp_path / "scheduled-evidence",
        run_id="locked-replay",
    )

    assert result.exit_code == 0
    assert result.payload["verdict"] == "full_replay_verified"
    sync_report = result.payload["pre_replay_sync"]
    assert sync_report["sources"]["decisions"]["audit_events_inserted"] == 1
    assert sync_report["sources"]["prices"]["inserted"] == 1
    assert result.intent_path.is_file()
    assert result.report_path.is_file()


def test_scheduled_writer_collision_records_failure_instead_of_skipping(
    tmp_path: Path,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    evidence = tmp_path / "collision-evidence"

    with store.open_operational_writer(database, writer_id="held-writer"):
        with pytest.raises(scheduler.ScheduledRunFailure) as failed:
            scheduler.run_scheduled(
                action="sync",
                database_path=database,
                decision_path=decisions,
                price_path=prices,
                evidence_dir=evidence,
                run_id="writer-busy",
            )

    assert failed.value.intent_path.is_file()
    failure = json.loads(failed.value.failure_path.read_text(encoding="utf-8"))
    assert failure["error_type"] == "WriterBusy"


def test_partial_final_line_is_not_consumed_until_newline_arrives(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    encoded = _line(_decision("decision-2", minutes=15, pit=False))
    initial_offset = decisions.stat().st_size
    with decisions.open("ab") as handle:
        handle.write(encoded[:-1])

    with store.open_operational_writer(database, writer_id="partial") as opened:
        waiting = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
        )
        assert opened.source_cursor("decisions").byte_offset == initial_offset
    with decisions.open("ab") as handle:
        handle.write(b"\n")
    with store.open_operational_writer(database, writer_id="complete") as opened:
        advanced = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
        )

    assert waiting.status == "waiting_for_complete_line"
    assert waiting.partial_tail_bytes == len(encoded) - 1
    assert advanced.status == "quality_failure"
    assert advanced.audit_events_inserted == 1
    assert advanced.predictions_inserted == 0


def test_orphaned_pending_decision_does_not_block_later_commit(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    pending = _decision("decision-pending", minutes=15)
    pending_transaction = decision_commit.transaction_id_for([str(pending["decision_id"])])
    decision_log.append_decision_events(
        decisions,
        [pending],
        transaction_id=pending_transaction,
    )
    with store.open_operational_writer(database, writer_id="pending") as opened:
        before = opened.source_cursor("decisions")
        assert before is not None
        waiting = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
        )
        assert opened.source_cursor("decisions").byte_offset == before.byte_offset

    _append_decision_batch(
        decisions,
        [_decision("decision-after-pending", minutes=30)],
    )
    with store.open_operational_writer(database, writer_id="recover") as opened:
        recovered = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
        )
        audit = opened.audit()

    assert waiting.status == "waiting_for_decision_commit"
    assert recovered.status == "quality_failure"
    assert recovered.rejections == 1
    assert recovered.audit_events_inserted == 1
    assert recovered.predictions_inserted == 1
    assert audit.audit_events == 2
    assert audit.predictions == 2


def test_crashed_receipt_cannot_be_resurrected_after_later_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    pending = _decision("decision-late-a", minutes=15)
    pending_id = str(pending["decision_id"])
    pending_transaction = decision_commit.transaction_id_for([pending_id])
    pending_full = decision_log.append_decision_events(
        decisions,
        [pending],
        transaction_id=pending_transaction,
    )
    pending_compact = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [pending],
        decision_transaction_id=pending_transaction,
    )
    original_append = decision_commit._append_locked_line  # noqa: SLF001

    def fail_final_commit(handle, path, serialized):
        if path == decisions.resolve():
            raise OSError("simulated crash before final full commit")
        return original_append(handle, path, serialized)

    monkeypatch.setattr(decision_commit, "_append_locked_line", fail_final_commit)
    with pytest.raises(OSError, match="simulated crash"):
        decision_commit.append_commit(
            decisions,
            decision_ids=[pending_id],
            full_batch_sha256=str(pending_full["batch_sha256"]),
            full_batch_line_start_offset=int(pending_full["line_start_offset"]),
            full_batch_line_sha256=str(pending_full["line_sha256"]),
            compact_batch_sha256=str(pending_compact["batch_sha256"]),
            compact_batch_line_start_offset=int(pending_compact["line_start_offset"]),
            compact_batch_byte_length=int(pending_compact["byte_length"]),
            compact_batch_payload_sha256=str(pending_compact["payload_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )

    monkeypatch.setattr(decision_commit, "_append_locked_line", original_append)
    _append_decision_batch(
        decisions,
        [_decision("decision-committed-b", minutes=30)],
    )
    synced = shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="sync-after-crash",
    )
    full_size_after_sync = decisions.stat().st_size
    compact_size_after_sync = compact_path.stat().st_size

    with pytest.raises(
        decision_commit.DecisionCommitError,
        match="full batch line reference",
    ):
        decision_commit.append_commit(
            decisions,
            decision_ids=[pending_id],
            full_batch_sha256=str(pending_full["batch_sha256"]),
            full_batch_line_start_offset=int(pending_full["line_start_offset"]),
            full_batch_line_sha256=str(pending_full["line_sha256"]),
            compact_batch_sha256=str(pending_compact["batch_sha256"]),
            compact_batch_line_start_offset=int(pending_compact["line_start_offset"]),
            compact_batch_byte_length=int(pending_compact["byte_length"]),
            compact_batch_payload_sha256=str(pending_compact["payload_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )
    retried = shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="sync-after-rejected-retry",
    )
    replay = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay-after-rejected-retry",
    )

    assert synced["sources"]["decisions"]["status"] == "quality_failure"
    assert synced["sources"]["decisions"]["rejections"] == 1
    assert synced["sources"]["decisions"]["predictions_inserted"] == 1
    assert retried["sources"]["decisions"]["status"] == "no_change"
    assert decisions.stat().st_size == full_size_after_sync
    assert compact_path.stat().st_size == compact_size_after_sync
    assert replay["verdict"] == "full_replay_verified"
    assert replay["sources"]["decisions"]["incremental_rejections"] == 1
    with store.open_operational_reader(database) as opened:
        audit = opened.audit()
    assert audit.audit_events == 2
    assert audit.predictions == 2
    assert audit.ingest_rejections == 1


def test_post_cursor_conflicting_commit_fails_sync_and_read_api_snapshot(
    tmp_path: Path,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    compact_path = tmp_path / "briefing_tf_journal.jsonl"
    original_receipt = json.loads(compact_path.read_text(encoding="utf-8").splitlines()[-1])
    original_commit = json.loads(decisions.read_text(encoding="utf-8").splitlines()[-1])
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
        "full_commit_line_start_offset": decisions.stat().st_size,
        "full_commit_line_sha256": hashlib.sha256(commit_payload).hexdigest(),
    }
    conflicting_receipt["receipt_record_sha256"] = decision_commit.canonical_sha256(
        {key: value for key, value in conflicting_receipt.items() if key != "receipt_record_sha256"}
    )
    with compact_path.open("ab") as handle:
        handle.write(decision_commit._serialized_json_line(conflicting_receipt))  # noqa: SLF001
    with decisions.open("ab") as handle:
        handle.write(commit_payload)

    with store.open_operational_reader(database) as opened:
        decision_before = opened.source_cursor("decisions")
        compact_before = opened.source_cursor(
            f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}{compact_path.name}"
        )
        audit_before = opened.audit()
        with pytest.raises(read_model.ReadModelStaleError, match="advanced"):
            read_model.require_current_source_snapshot(opened)

    with pytest.raises(
        shadow.ShadowSyncError,
        match="unpaired or conflicting full commit",
    ):
        shadow.sync_sources(
            database_path=database,
            decision_path=decisions,
            price_path=prices,
            writer_id="reject-post-cursor-conflict",
        )

    with store.open_operational_reader(database) as opened:
        assert opened.source_cursor("decisions") == decision_before
        assert (
            opened.source_cursor(f"{shadow.DECISION_SUPPORT_CURSOR_PREFIX}{compact_path.name}")
            == compact_before
        )
        assert opened.audit() == audit_before
        with pytest.raises(read_model.ReadModelStaleError, match="advanced"):
            read_model.require_current_source_snapshot(opened)


def test_decision_sync_reads_commit_just_beyond_max_bytes(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    initial_size = decisions.stat().st_size
    _append_decision_batch(
        decisions,
        [_decision("decision-boundary", minutes=15)],
    )
    suffix = decisions.read_bytes()[initial_size:]
    full_batch_line_size = len(suffix.splitlines(keepends=True)[0])

    with store.open_operational_writer(database, writer_id="boundary") as opened:
        result = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
            max_bytes=full_batch_line_size,
        )

    assert result.status == "advanced"
    assert result.predictions_inserted == 1
    assert result.end_offset == decisions.stat().st_size


def test_previous_line_tamper_fails_closed(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    original = decisions.read_bytes()
    assert b"committed_at" in original
    decisions.write_bytes(original.replace(b"committed_at", b"committed_ax"))

    with store.open_operational_writer(database, writer_id="tamper") as opened:
        with pytest.raises(shadow.SourceBoundaryMismatch):
            shadow.sync_one_source(
                opened,
                source_name="decisions",
                source_kind="decisions",
                source_path=decisions,
            )


def test_source_rotation_fails_closed(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    rotated = tmp_path / "decisions.rotated.jsonl"
    decisions.rename(rotated)
    decisions.write_bytes(rotated.read_bytes())

    with store.open_operational_writer(database, writer_id="rotation") as opened:
        with pytest.raises(shadow.SourceRotationDetected):
            shadow.sync_one_source(
                opened,
                source_name="decisions",
                source_kind="decisions",
                source_path=decisions,
            )


def test_malformed_complete_line_is_ledgered_and_cursor_advances(tmp_path: Path) -> None:
    database, decisions, _, _ = _create_bootstrapped_candidate(tmp_path)
    prior_offset = decisions.stat().st_size
    bad_line = b"{bad json}\n"
    with decisions.open("ab") as handle:
        handle.write(bad_line)

    with store.open_operational_writer(database, writer_id="reject") as opened:
        result = shadow.sync_one_source(
            opened,
            source_name="decisions",
            source_kind="decisions",
            source_path=decisions,
        )
        cursor = opened.source_cursor("decisions")
        rejection = opened.connection.execute("""
            SELECT line_start_offset, line_end_offset, raw_line_sha256, reason
            FROM ingest_rejections
            """).fetchone()

    assert result.status == "quality_failure"
    assert result.rejections == 1
    assert cursor is not None
    assert cursor.byte_offset == prior_offset + len(bad_line)
    assert tuple(rejection) == (
        prior_offset,
        prior_offset + len(bad_line),
        hashlib.sha256(bad_line).hexdigest(),
        "malformed_json",
    )


def test_bootstrap_rejects_database_hash_mismatch(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    database = tmp_path / "candidate.sqlite3"
    parity_path = tmp_path / "parity.json"
    _write_jsonl(decisions, [_decision("decision-1")])
    _write_jsonl(prices, [_price("price-1")])
    with store.open_operational_writer(database, writer_id="candidate"):
        pass
    parity_path.write_text(
        json.dumps(
            {
                "parity_verdict": "parity_verified",
                "database_sha256": "0" * 64,
                "database": {"path": str(database.resolve()), "rows": {}},
                "sources": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(shadow.BootstrapEvidenceMismatch, match="database SHA-256"):
        shadow.bootstrap_candidate(
            database_path=database,
            parity_report_path=parity_path,
            decision_path=decisions,
            price_path=prices,
            writer_id="bootstrap",
        )


def test_fingerprint_rejects_incomplete_bootstrap_line(tmp_path: Path) -> None:
    source = tmp_path / "incomplete.jsonl"
    source.write_bytes(b'{"value": 1}')

    with pytest.raises(shadow.BootstrapEvidenceMismatch, match="incomplete"):
        shadow.fingerprint_complete_source(
            source,
            expected_sha256=store.file_sha256(source),
            expected_bytes=source.stat().st_size,
        )


def test_create_only_report_never_replaces_existing_evidence(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    state_io.atomic_write_json_create_only(report, {"run": 1})

    with pytest.raises(FileExistsError):
        state_io.atomic_write_json_create_only(report, {"run": 2})

    assert json.loads(report.read_text(encoding="utf-8")) == {"run": 1}


def test_full_replay_verifies_chunks_payloads_and_bootstrap_exclusions(
    tmp_path: Path,
) -> None:
    invalid_price = _price("legacy-invalid")
    invalid_price.pop("source_record_id")
    database, decisions, prices, _ = _create_bootstrapped_candidate(
        tmp_path,
        decision_rows=[
            _decision("decision-1"),
            _decision("legacy-decision", minutes=1, pit=False),
        ],
        price_rows=[_price("price-1"), invalid_price],
    )

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_verified"
    assert report["violation_count"] == 0
    assert report["data_quality_status"] == "usable_with_exclusions"
    assert report["data_quality_exclusions"] == 2
    assert report["sources"]["decisions"]["pit_ineligible"] == 1
    assert report["sources"]["prices"]["bootstrap_exclusions"] == 1


def test_manual_full_replay_locks_both_sources_for_the_entire_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    original_lock = full_replay.lock_replay_sources
    original_replay_source = full_replay.replay_source
    lock_active = False
    replayed: list[str] = []

    @contextmanager
    def observed_lock(*paths: str | Path):
        nonlocal lock_active
        assert {Path(path).resolve() for path in paths} == {
            decisions.resolve(),
            prices.resolve(),
            (tmp_path / "briefing_tf_journal.jsonl").resolve(),
        }
        with original_lock(*paths):
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

    def observed_replay_source(*args: object, **kwargs: object):
        assert lock_active
        replayed.append(str(kwargs["source_name"]))
        return original_replay_source(*args, **kwargs)

    monkeypatch.setattr(full_replay, "lock_replay_sources", observed_lock)
    monkeypatch.setattr(full_replay, "replay_source", observed_replay_source)

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="manual-full-replay",
    )

    assert report["verdict"] == "full_replay_verified"
    assert replayed == ["decisions", "prices"]
    assert lock_active is False


def test_full_replay_detects_changed_raw_payload(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    original = decisions.read_bytes()
    decisions.write_bytes(original.replace(b"neutral", b"longxxx"))

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    violations = report["sources"]["decisions"]["violations"]
    assert violations["chunk_sha256_mismatch"] == 1
    assert violations["primary_population_mismatch"] == 1
    assert violations["prediction_population_mismatch"] == 1


def test_full_replay_detects_changed_compact_decision_batch(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    compact = tmp_path / "briefing_tf_journal.jsonl"
    original = compact.read_bytes()
    compact.write_bytes(original.replace(b"neutral", b"longxxx"))

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    violations = report["sources"]["decisions"]["violations"]
    assert violations["primary_population_mismatch"] == 1
    assert violations["prediction_population_mismatch"] == 1


def test_full_replay_detects_changed_database_payload(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with store.open_operational_writer(database, writer_id="corruption-fixture") as opened:
        opened.connection.execute("DROP TRIGGER price_points_no_update")
        opened.connection.execute(
            "UPDATE price_points SET payload_json = '{}' WHERE source_record_id = 'price-1'"
        )
        opened.connection.execute("""
            CREATE TRIGGER price_points_no_update
            BEFORE UPDATE ON price_points
            BEGIN
                SELECT RAISE(ABORT, 'price points are append-only');
            END
            """)

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    assert report["sources"]["prices"]["violations"]["price_payload_or_projection_mismatch"] == 1


def test_full_replay_detects_changed_record_sha256(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with store.open_operational_writer(database, writer_id="hash-corruption-fixture") as opened:
        opened.connection.execute("DROP TRIGGER price_points_no_update")
        opened.connection.execute(
            "UPDATE price_points SET record_sha256 = ? WHERE source_record_id = 'price-1'",
            ("0" * 64,),
        )

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    violations = report["sources"]["prices"]["violations"]
    assert violations["price_record_sha256_mismatch"] == 1


def test_full_replay_detects_missing_database_row(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with store.open_operational_writer(database, writer_id="corruption-fixture") as opened:
        opened.connection.execute("DROP TRIGGER price_points_no_delete")
        opened.connection.execute("DELETE FROM price_points WHERE source_record_id = 'price-1'")
        opened.connection.execute("""
            CREATE TRIGGER price_points_no_delete
            BEFORE DELETE ON price_points
            BEGIN
                SELECT RAISE(ABORT, 'price points are append-only');
            END
            """)

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    violations = report["sources"]["prices"]["violations"]
    assert violations["missing_price_point"] == 1
    assert violations["primary_population_mismatch"] == 1


def test_full_replay_detects_complete_unsynced_suffix(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with decisions.open("ab") as handle:
        handle.write(_line(_decision("decision-2", minutes=15, pit=False)))

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    decision_report = report["sources"]["decisions"]
    assert decision_report["unconsumed_complete_lines"] == 1
    assert decision_report["violations"]["unconsumed_complete_lines"] == 1


def test_full_replay_detects_incomplete_source_tail(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with decisions.open("ab") as handle:
        handle.write(b'{"partial":')

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_failed"
    decision_report = report["sources"]["decisions"]
    assert decision_report["incomplete_tail_bytes"] == len(b'{"partial":')
    assert decision_report["violations"]["incomplete_source_tail"] == 1


def test_full_replay_verifies_incremental_rejection_ledger(tmp_path: Path) -> None:
    database, decisions, prices, _ = _create_bootstrapped_candidate(tmp_path)
    with decisions.open("ab") as handle:
        handle.write(b"{bad json}\n")
    shadow.sync_sources(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="shadow-sync",
    )

    report = full_replay.run_full_replay(
        database_path=database,
        decision_path=decisions,
        price_path=prices,
        writer_id="full-replay",
    )

    assert report["verdict"] == "full_replay_verified"
    assert report["data_quality_status"] == "usable_with_exclusions"
    assert report["sources"]["decisions"]["incremental_rejections"] == 1
