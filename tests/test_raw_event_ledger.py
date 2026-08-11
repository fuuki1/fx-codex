from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
from threading import Event
import time

import pytest

from data_platform.contracts.raw_event import RawEvent, RawEventContractError
from data_platform.raw.event_ledger import RawEventLedger, RawEventLedgerError
from data_platform.raw.immutable_store import ImmutableRawStore
from data_platform.raw.shadow import RawLedgerShadowWriter, raw_ledger_enabled

RECEIVED = datetime(2026, 8, 1, 0, 0, 1, tzinfo=UTC)
PAYLOAD = b'{"bid":"150.001","ask":"150.004"}'


def event(**overrides: object) -> RawEvent:
    values: dict[str, object] = {
        "event_id": "delivery-1",
        "source": "fixture",
        "source_event_id": "source-1",
        "event_type": "quote",
        "symbol": "USDJPY",
        "event_time": RECEIVED - timedelta(seconds=1),
        "publish_time": RECEIVED - timedelta(milliseconds=500),
        "receive_time": RECEIVED,
        "ingest_time": RECEIVED + timedelta(milliseconds=10),
        "payload_bytes": PAYLOAD,
        "payload_hash": hashlib.sha256(PAYLOAD).hexdigest(),
        "provenance": {
            "endpoint_class": "fixture",
            "writer_id": "test",
            "source_event_id_scope": "fixture:quote:instrument",
        },
    }
    values.update(overrides)
    return RawEvent(**values)  # type: ignore[arg-type]


def test_contract_requires_matching_raw_bytes_and_aware_ordered_times() -> None:
    with pytest.raises(RawEventContractError, match="does not match"):
        event(payload_hash="f" * 64)
    with pytest.raises(RawEventContractError, match="timezone-aware"):
        event(receive_time=datetime(2026, 8, 1))
    skewed = event(publish_time=RECEIVED + timedelta(seconds=1))
    assert skewed.publish_time and skewed.publish_time > skewed.receive_time
    with pytest.raises(RawEventContractError, match="require duplicate_of"):
        event(quality_status="duplicate")
    with pytest.raises(RawEventContractError, match="event_id must be a string"):
        event(event_id=None)
    with pytest.raises(RawEventContractError, match="at least one"):
        event(provenance={})
    with pytest.raises(RawEventContractError, match="valid UTF-8"):
        event(event_id="\ud800")
    scheduled = event(event_time=RECEIVED + timedelta(days=1))
    assert scheduled.event_time and scheduled.event_time > scheduled.publish_time  # type: ignore[operator]


def test_append_stores_exact_bytes_and_metadata(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    result = ledger.append(event())
    assert result.status == "stored"
    assert ledger.blobs.get(result.event.payload_hash) == PAYLOAD
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    row = json.loads(connection.execute("SELECT metadata_json FROM raw_events").fetchone()[0])
    connection.close()
    assert row["payload_hash"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert row["provenance"]["writer_id"] == "test"
    assert "payload_bytes" not in row
    assert stat.S_IMODE((tmp_path / "events.sqlite3").stat().st_mode) == 0o600


def test_ledger_can_share_an_existing_raw_first_blob_store(tmp_path: Path) -> None:
    existing_store = ImmutableRawStore(tmp_path / "existing-raw")
    ledger = RawEventLedger(tmp_path / "metadata", blob_store=existing_store)
    result = ledger.append(event())
    assert existing_store.get(result.event.payload_hash) == PAYLOAD
    assert not (tmp_path / "metadata" / "blobs").exists()


def test_same_event_is_idempotent_but_conflict_fails_closed(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    first = event()
    assert ledger.append(first).status == "stored"
    assert ledger.append(first).status == "idempotent"
    assert ledger.stats().total_events == 1

    changed_payload = b"changed"
    conflicting = replace(
        first,
        payload_bytes=changed_payload,
        payload_hash=hashlib.sha256(changed_payload).hexdigest(),
    )
    with pytest.raises(RawEventLedgerError, match="different evidence"):
        ledger.append(conflicting)


def test_same_event_is_idempotent_after_process_restart(tmp_path: Path) -> None:
    assert RawEventLedger(tmp_path).append(event()).status == "stored"
    assert RawEventLedger(tmp_path).append(event()).status == "idempotent"
    assert RawEventLedger(tmp_path).stats().total_events == 1


def test_health_and_stats_are_logically_read_only(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    before_connection = sqlite3.connect(tmp_path / "events.sqlite3")
    before = before_connection.execute(
        "SELECT event_id, input_sha256, record_sha256, metadata_json FROM raw_events"
    ).fetchall()
    before_connection.close()

    assert ledger.stats().total_events == 1
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
        required_symbols=("USDJPY",),
    )

    after_connection = sqlite3.connect(tmp_path / "events.sqlite3")
    after = after_connection.execute(
        "SELECT event_id, input_sha256, record_sha256, metadata_json FROM raw_events"
    ).fetchall()
    after_connection.close()
    assert health["status"] == "ok"
    assert after == before


def test_health_uses_one_consistent_snapshot_during_concurrent_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    original_stats = ledger._stats_from_connection
    annotation_written = False

    def stats_then_annotate(connection: sqlite3.Connection) -> object:
        nonlocal annotation_written
        snapshot_stats = original_stats(connection)
        if not annotation_written:
            annotation_written = True
            ledger.annotate_quarantine(
                "delivery-1",
                annotation_id="quality-concurrent",
                reason="late_quality_result",
                annotated_at=RECEIVED + timedelta(seconds=1),
                provenance={"validator": "concurrent-test"},
            )
        return snapshot_stats

    monkeypatch.setattr(ledger, "_stats_from_connection", stats_then_annotate)
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=2),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=0,
    )

    assert annotation_written is True
    assert health["status"] == "ok"
    assert health["quarantined_events"] == 0
    monkeypatch.undo()
    assert ledger.stats().quarantined_events == 1


def test_distinct_redelivery_is_retained_and_annotated_duplicate(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    duplicate = event(
        event_id="delivery-2",
        receive_time=RECEIVED + timedelta(seconds=2),
        ingest_time=RECEIVED + timedelta(seconds=2, milliseconds=10),
    )
    result = ledger.append(duplicate)
    assert result.event.quality_status == "duplicate"
    assert result.event.duplicate_of == "delivery-1"
    replay = ledger.append(duplicate)
    assert replay.status == "idempotent"
    assert replay.event.duplicate_of == "delivery-1"
    assert ledger.stats().duplicate_events == 1
    assert ledger.stats().total_events == 2


def test_source_event_identity_scope_prevents_cross_namespace_collision(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    other = event(
        event_id="delivery-2",
        receive_time=RECEIVED + timedelta(seconds=1),
        ingest_time=RECEIVED + timedelta(seconds=1, milliseconds=10),
        provenance={
            "endpoint_class": "fixture",
            "writer_id": "test",
            "source_event_id_scope": "fixture:calendar:event",
        },
    )
    assert ledger.append(other).event.duplicate_of is None


def test_changed_source_event_is_linked_as_revision(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    revised_payload = b'{"bid":"150.002","ask":"150.005"}'
    revision = event(
        event_id="delivery-2",
        receive_time=RECEIVED + timedelta(seconds=2),
        ingest_time=RECEIVED + timedelta(seconds=2, milliseconds=10),
        payload_bytes=revised_payload,
        payload_hash=hashlib.sha256(revised_payload).hexdigest(),
    )
    result = ledger.append(revision)
    assert result.event.duplicate_of is None
    assert result.event.supersedes_event_id == "delivery-1"


def test_revision_reversion_supersedes_current_head(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    revised_payload = b"v2"
    ledger.append(
        event(
            event_id="delivery-2",
            receive_time=RECEIVED + timedelta(seconds=1),
            ingest_time=RECEIVED + timedelta(seconds=1, milliseconds=10),
            payload_bytes=revised_payload,
            payload_hash=hashlib.sha256(revised_payload).hexdigest(),
        )
    )
    reverted = ledger.append(
        event(
            event_id="delivery-3",
            receive_time=RECEIVED + timedelta(seconds=2),
            ingest_time=RECEIVED + timedelta(seconds=2, milliseconds=10),
        )
    ).event
    assert reverted.duplicate_of is None
    assert reverted.supersedes_event_id == "delivery-2"


def test_multiple_ledger_instances_refresh_their_idempotency_index(tmp_path: Path) -> None:
    first = RawEventLedger(tmp_path)
    second = RawEventLedger(tmp_path)
    first.append(event())
    second.append(
        event(
            event_id="delivery-2",
            source_event_id="source-2",
            receive_time=RECEIVED + timedelta(seconds=1),
            ingest_time=RECEIVED + timedelta(seconds=1, milliseconds=10),
        )
    )
    result = first.append(
        event(
            event_id="delivery-3",
            source_event_id="source-3",
            receive_time=RECEIVED + timedelta(seconds=2),
            ingest_time=RECEIVED + timedelta(seconds=2, milliseconds=10),
        )
    )
    assert result.status == "stored"
    assert first.stats().total_events == 3


def test_quarantine_is_retained_and_visible_in_health(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    assert (
        ledger.annotate_quarantine(
            "delivery-1",
            annotation_id="quality-1",
            reason="crossed_book",
            annotated_at=RECEIVED + timedelta(seconds=1),
            provenance={"validator": "quote-schema-v1"},
        )
        == "stored"
    )
    assert (
        ledger.annotate_quarantine(
            "delivery-1",
            annotation_id="quality-1",
            reason="crossed_book",
            annotated_at=RECEIVED + timedelta(seconds=1),
            provenance={"validator": "quote-schema-v1"},
        )
        == "idempotent"
    )
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=5),
        stale_after_seconds=60,
        max_duplicate_ratio=1.0,
        max_quarantine_ratio=0.0,
    )
    assert health["status"] == "degraded"
    assert health["quarantined_events"] == 1
    assert "quarantine_ratio_exceeded" in health["reasons"]
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    annotation_times = connection.execute(
        "SELECT annotated_at, ledger_recorded_at, available_at FROM event_annotations"
    ).fetchone()
    connection.close()
    assert annotation_times[2] >= max(annotation_times[0], annotation_times[1])


def test_append_only_sqlite_rejects_update_and_delete(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE raw_events SET source = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM raw_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("""
            INSERT OR REPLACE INTO raw_events
            SELECT sequence, event_id, 'replaced', source_event_id,
                   source_event_id_scope, payload_hash,
                   quality_status, duplicate_of, supersedes_event_id, event_time,
                   ingest_time, ledger_recorded_at, provenance_present, input_sha256,
                   record_sha256, metadata_json
            FROM raw_events
            """)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("""
            INSERT OR REPLACE INTO raw_events
            SELECT sequence, 'replacement-id', source, source_event_id,
                   source_event_id_scope, payload_hash,
                   quality_status, duplicate_of, supersedes_event_id, event_time,
                   ingest_time, ledger_recorded_at, provenance_present, input_sha256,
                   record_sha256, metadata_json
            FROM raw_events
            """)
    connection.close()


def test_health_detects_metadata_record_tampering(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    connection.execute("DROP TRIGGER raw_events_no_update")
    connection.execute("UPDATE raw_events SET source = 'tampered'")
    connection.commit()
    connection.close()
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    assert health["status"] == "degraded"
    assert health["metadata_integrity_errors"] == 1


def test_health_detects_metadata_json_tampering(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    connection.execute("DROP TRIGGER raw_events_no_update")
    connection.execute("UPDATE raw_events SET metadata_json = '{}' ")
    connection.commit()
    connection.close()
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    assert health["status"] == "degraded"
    assert health["metadata_integrity_errors"] == 1


def test_health_detects_raw_input_digest_tampering(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    connection.execute("DROP TRIGGER raw_events_no_update")
    connection.execute("UPDATE raw_events SET input_sha256 = ?", ("0" * 64,))
    connection.commit()
    connection.close()

    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )

    assert health["status"] == "degraded"
    assert health["metadata_integrity_errors"] == 1


def test_health_detects_annotation_input_digest_tampering(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    ledger.annotate_quarantine(
        "delivery-1",
        annotation_id="quality-1",
        reason="crossed_book",
        annotated_at=RECEIVED + timedelta(seconds=1),
        provenance={"validator": "quote-schema-v1"},
    )
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    connection.execute("DROP TRIGGER event_annotations_no_update")
    connection.execute("UPDATE event_annotations SET input_sha256 = ?", ("0" * 64,))
    connection.commit()
    connection.close()

    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=2),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )

    assert health["status"] == "degraded"
    assert health["metadata_integrity_errors"] == 1


def test_health_fails_closed_for_uninitialized_database(tmp_path: Path) -> None:
    (tmp_path / "events.sqlite3").touch(mode=0o600)
    health = RawEventLedger(tmp_path).health_snapshot(
        now=RECEIVED,
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    assert health["status"] == "degraded"
    assert "metadata_stats_unavailable" in health["reasons"]


def test_dangling_annotation_and_caller_owned_links_are_rejected(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    with pytest.raises(RawEventLedgerError, match="unannotated"):
        ledger.append(event(duplicate_of="ghost", quality_status="duplicate"))
    with pytest.raises(RawEventLedgerError, match="unknown event_id"):
        ledger.annotate_quarantine(
            "ghost",
            annotation_id="quality-1",
            reason="bad",
            annotated_at=RECEIVED,
            provenance={"validator": "test"},
        )


def test_annotation_replace_is_rejected_without_recursive_triggers(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    ledger.annotate_quarantine(
        "delivery-1",
        annotation_id="quality-1",
        reason="bad",
        annotated_at=RECEIVED,
        provenance={"validator": "test"},
    )
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("""
            INSERT OR REPLACE INTO event_annotations
            SELECT sequence, 'replacement-id', event_id, quality_status, 'replaced',
                   annotated_at, ledger_recorded_at, available_at, provenance_json,
                   input_sha256, record_sha256
            FROM event_annotations
            """)
    connection.close()


def test_health_is_unknown_without_thresholds_and_fails_on_missing_blob(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    stored = ledger.append(event()).event
    assert ledger.health_snapshot(now=RECEIVED + timedelta(seconds=1))["status"] == "unknown"
    stored_path = stored.payload_hash
    blob_path = tmp_path / "blobs" / stored_path[:2] / stored_path[2:]
    blob_path.unlink()
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    assert health["status"] == "degraded"
    assert health["blob_integrity_errors"] == 1


def test_insecure_or_symlinked_storage_is_rejected(tmp_path: Path) -> None:
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(RawEventLedgerError, match="owner-only"):
        RawEventLedger(insecure).append(event())
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(RawEventLedgerError, match="symlink"):
        RawEventLedger(linked).append(event())
    ancestor_target = tmp_path / "ancestor-target"
    ancestor_target.mkdir()
    ancestor_target.chmod(0o700)
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(ancestor_target, target_is_directory=True)
    with pytest.raises(RawEventLedgerError, match="symlink ancestor"):
        RawEventLedger(ancestor_link / "ledger").append(event())
    hidden_existing = ancestor_target / "existing"
    hidden_existing.mkdir(mode=0o755)
    hidden_existing.chmod(0o755)
    with pytest.raises(RawEventLedgerError, match="symlink ancestor"):
        RawEventLedger(ancestor_link / "existing" / "ledger").append(event())


def test_health_returns_degraded_for_permission_drift(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event())
    (tmp_path / "events.sqlite3").chmod(0o644)
    health = ledger.health_snapshot(
        now=RECEIVED + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
    )
    assert health["status"] == "degraded"
    assert "storage_integrity_failed" in health["reasons"]


def test_health_rejects_stale_required_symbol_despite_fresh_other_symbol(
    tmp_path: Path,
) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(event(symbol="EURUSD"))
    fresh_time = RECEIVED + timedelta(days=1)
    ledger.append(
        event(
            event_id="delivery-2",
            source_event_id="source-2",
            symbol="USDJPY",
            receive_time=fresh_time,
            ingest_time=fresh_time,
        )
    )

    health = ledger.health_snapshot(
        now=fresh_time + timedelta(seconds=1),
        stale_after_seconds=60,
        max_duplicate_ratio=1,
        max_quarantine_ratio=1,
        required_symbols=("USD_JPY", "EUR_USD"),
    )

    assert health["status"] == "degraded"
    assert "required_symbol_stale" in health["reasons"]
    assert health["missing_required_symbols"] == []
    assert health["stale_required_symbols"] == ["EURUSD"]
    assert health["symbol_age_seconds"]["USDJPY"] == 1
    assert health["symbol_age_seconds"]["EURUSD"] > 60


def test_feature_flag_is_explicit_and_false_by_default() -> None:
    assert raw_ledger_enabled({}) is False
    assert raw_ledger_enabled({"FX_RAW_LEDGER_ENABLED": "false"}) is False
    assert raw_ledger_enabled({"FX_RAW_LEDGER_ENABLED": "true"}) is True


def test_disabled_shadow_writer_performs_no_write(tmp_path: Path) -> None:
    writer = RawLedgerShadowWriter(RawEventLedger(tmp_path), enabled=False)
    result = writer.capture(event())
    assert result.status == "disabled"
    assert list(tmp_path.iterdir()) == []
    assert writer.metrics()["attempted"] == 0


def test_shadow_failure_is_recorded_without_propagating(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir(mode=0o755)
    ledger_root.chmod(0o755)
    ledger = RawEventLedger(ledger_root)
    messages: list[str] = []
    writer = RawLedgerShadowWriter(
        ledger,
        enabled=True,
        failure_log=tmp_path / "failures.jsonl",
        error_logger=messages.append,
    )
    result = writer.capture(event(event_id="delivery-2"))
    assert result.status == "queued"
    assert writer.drain()
    assert writer.metrics()["failed"] == 1
    assert messages and "RawEventLedgerError" in messages[0]
    failure = json.loads((tmp_path / "failures.jsonl").read_text())
    assert "event_id" not in failure and len(failure["event_id_sha256"]) == 64


def test_shadow_error_callback_cannot_break_failure_isolation(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir(mode=0o755)
    ledger_root.chmod(0o755)
    ledger = RawEventLedger(ledger_root)

    def broken_logger(_detail: str) -> None:
        raise RuntimeError("logger unavailable")

    writer = RawLedgerShadowWriter(ledger, enabled=True, error_logger=broken_logger)
    assert writer.capture(event(event_id="delivery-2")).status == "queued"
    assert writer.drain()
    assert writer.metrics()["failed"] == 1


def test_shadow_worker_survives_base_exception_and_processes_following_event() -> None:
    class ExitOnceLedger:
        def __init__(self) -> None:
            self.calls = 0

        def append(self, _event: RawEvent) -> object:
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("simulated worker termination")
            return type("Result", (), {"status": "stored"})()

    ledger = ExitOnceLedger()
    writer = RawLedgerShadowWriter(ledger, enabled=True)  # type: ignore[arg-type]
    assert writer.capture(event()).status == "queued"
    assert writer.capture(event(event_id="delivery-2")).status == "queued"
    assert writer.drain()
    metrics = writer.metrics()
    assert ledger.calls == 2
    assert metrics["failed"] == 1
    assert metrics["stored"] == 1
    assert metrics["worker_alive"] is True
    assert metrics["queue_backlog"] == 0
    assert metrics["unfinished_tasks"] == 0


def test_shadow_capture_is_nonblocking_and_queue_overflow_is_explicit() -> None:
    release = Event()
    entered = Event()

    class BlockingLedger:
        def append(self, _event: RawEvent) -> object:
            entered.set()
            release.wait(timeout=2)
            return type("Result", (), {"status": "stored"})()

    writer = RawLedgerShadowWriter(BlockingLedger(), enabled=True, max_queue_size=1)  # type: ignore[arg-type]
    started = time.monotonic()
    assert writer.capture(event()).status == "queued"
    assert entered.wait(timeout=1)
    assert writer.capture(event(event_id="delivery-2")).status == "queued"
    assert writer.capture(event(event_id="delivery-3")).status == "failed"
    assert time.monotonic() - started < 1
    release.set()
    assert writer.drain()
    assert writer.metrics()["queue_overflow"] == 1
    assert writer.metrics()["raw_queue_overflow_ratio"] == pytest.approx(1 / 3)
    assert writer.metrics()["annotation_queue_overflow_ratio"] == 0


def test_shadow_queue_overflow_is_logged_off_the_capture_path(tmp_path: Path) -> None:
    release = Event()
    entered = Event()

    class BlockingLedger:
        def append(self, _event: RawEvent) -> object:
            entered.set()
            release.wait(timeout=2)
            return type("Result", (), {"status": "stored"})()

    failure_log = tmp_path / "failures.jsonl"
    writer = RawLedgerShadowWriter(
        BlockingLedger(),  # type: ignore[arg-type]
        enabled=True,
        failure_log=failure_log,
        max_queue_size=1,
    )
    assert writer.capture(event()).status == "queued"
    assert entered.wait(timeout=1)
    assert writer.capture(event(event_id="delivery-2")).status == "queued"
    started = time.monotonic()
    assert writer.capture(event(event_id="delivery-3")).status == "failed"
    assert time.monotonic() - started < 1
    release.set()
    assert writer.drain()

    failure = json.loads(failure_log.read_text())
    assert failure["error_type"] == "RawLedgerShadowQueueFull"
    assert "event_id" not in failure
    assert writer.metrics()["failure_reports_queued"] == 1


def test_shadow_failure_log_write_error_is_visible_in_metrics(tmp_path: Path) -> None:
    release = Event()
    entered = Event()

    class BlockingLedger:
        def append(self, _event: RawEvent) -> object:
            entered.set()
            release.wait(timeout=2)
            return type("Result", (), {"status": "stored"})()

    invalid_log = tmp_path / "failure-log-is-a-directory"
    invalid_log.mkdir()
    writer = RawLedgerShadowWriter(
        BlockingLedger(),  # type: ignore[arg-type]
        enabled=True,
        failure_log=invalid_log,
        max_queue_size=1,
    )
    assert writer.capture(event()).status == "queued"
    assert entered.wait(timeout=1)
    assert writer.capture(event(event_id="delivery-2")).status == "queued"
    assert writer.capture(event(event_id="delivery-3")).status == "failed"
    release.set()
    assert writer.drain()

    metrics = writer.metrics()
    assert metrics["failure_reports_queued"] == 1
    assert metrics["failure_log_write_errors"] == 1
    assert metrics["failure_report_errors"] == 1
