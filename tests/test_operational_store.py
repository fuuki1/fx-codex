from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path
import sqlite3

import pytest

from fx_intel import operational_store as store

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.3")


def _prediction(identifier: str = "prediction-1") -> store.PredictionRecord:
    return store.PredictionRecord(
        prediction_id=identifier,
        decision_id="decision-1",
        symbol="USDJPY",
        timeframe="15m",
        prediction_time=NOW,
        available_time=NOW - timedelta(seconds=1),
        due_time=NOW + timedelta(minutes=15),
        source="fx_briefing",
        payload={"direction": "neutral", "confidence": 0.5},
        ingested_time=NOW,
    )


def _audit_event(identifier: str = "decision-1") -> store.AuditEventRecord:
    return store.AuditEventRecord(
        event_id=identifier,
        event_type="chart_decision",
        symbol="USDJPY",
        timeframe="15m",
        event_time=NOW,
        prediction_time=NOW,
        available_time=NOW - timedelta(seconds=1),
        source="fx_briefing",
        payload={"direction": "neutral"},
        pit_eligible=True,
        pit_failure_reason="",
        ingested_time=NOW,
    )


def _price(identifier: str = "price-1", *, minutes: int = 15) -> store.PricePointRecord:
    stamp = NOW + timedelta(minutes=minutes)
    return store.PricePointRecord(
        source_record_id=identifier,
        symbol="USDJPY",
        timeframe="15m",
        event_time=stamp,
        available_time=stamp,
        ingested_time=stamp + timedelta(seconds=1),
        source="oanda",
        revision_id="initial",
        open=150.0,
        high=150.2,
        low=149.9,
        close=150.1,
        bid=150.09,
        ask=150.11,
        ohlc_scope="completed_bar",
        payload={"quote_source": "oanda", "complete": True},
    )


def test_wal_version_gate_accepts_only_fixed_branches() -> None:
    assert store.sqlite_wal_runtime_is_safe((3, 44, 6))
    assert store.sqlite_wal_runtime_is_safe((3, 50, 7))
    assert store.sqlite_wal_runtime_is_safe((3, 51, 3))
    assert store.sqlite_wal_runtime_is_safe((3, 53, 3))
    assert not store.sqlite_wal_runtime_is_safe((3, 45, 3))
    assert not store.sqlite_wal_runtime_is_safe((3, 50, 6))
    assert not store.sqlite_wal_runtime_is_safe((3, 51, 2))


def test_validate_sqlite_runtime_rejects_known_unsafe_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 51, 0))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.51.0")

    with pytest.raises(store.UnsafeSQLiteRuntime, match="not approved"):
        store.validate_sqlite_runtime()


def test_initializes_strict_wal_store_with_full_sync(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"

    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        audit = opened.audit()
        table_sql = opened.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'predictions'"
        ).fetchone()[0]

    assert audit.schema_version == 4
    assert audit.journal_mode == "wal"
    assert audit.synchronous == 2  # SQLITE_SYNC_FULL
    assert audit.integrity_check == "ok"
    assert audit.foreign_key_violations == 0
    assert audit.source_manifest_violations == 0
    assert "STRICT" in table_sql
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == store.APPLICATION_ID


def test_writer_migrates_schema_v1_candidate_to_v4(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE audit_events")
        connection.execute("PRAGMA user_version=1")

    with store.open_operational_writer(path, writer_id="migration-writer") as opened:
        audit = opened.audit()

    assert audit.schema_version == 4
    assert audit.audit_events == 0


def test_writer_migrates_schema_v2_candidate_to_v4(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer"):
        pass
    with sqlite3.connect(path) as connection:
        for trigger in (
            "source_cursors_no_delete",
            "source_cursors_forward_only",
            "source_cursors_chunk_required",
            "source_chunks_contiguous",
            "source_chunks_no_update",
            "source_chunks_no_delete",
            "ingest_rejections_no_update",
            "ingest_rejections_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in ("source_cursors", "source_chunks", "ingest_rejections"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=2")

    with store.open_operational_writer(path, writer_id="migration-writer") as opened:
        audit = opened.audit()

    assert audit.schema_version == 4
    assert audit.source_cursors == 0
    assert audit.source_chunks == 0
    assert audit.ingest_rejections == 0


def test_writer_migrates_schema_v3_candidate_to_v4_page_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer"):
        pass
    with sqlite3.connect(path) as connection:
        for index in (
            "audit_events_page_idx",
            "price_points_page_idx",
            "price_points_symbol_page_idx",
        ):
            connection.execute(f"DROP INDEX {index}")
        connection.execute("PRAGMA user_version=3")

    with store.open_operational_writer(path, writer_id="migration-writer") as opened:
        indexes = {str(row[0]) for row in opened.connection.execute("""
                SELECT name FROM sqlite_schema
                WHERE type = 'index' AND name LIKE '%page_idx'
                """).fetchall()}
        audit = opened.audit()

    assert audit.schema_version == 4
    assert {
        "audit_events_page_idx",
        "price_points_page_idx",
        "price_points_symbol_page_idx",
    } <= indexes


def test_source_cursor_cannot_advance_without_matching_chunk(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    empty_hash = store.hashlib.sha256(b"").hexdigest()
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.bootstrap_source_cursor(
            source_name="decisions",
            source_kind="decisions",
            source_path=str(tmp_path / "decisions.jsonl"),
            device_id=1,
            inode=2,
            byte_offset=0,
            last_line_start_offset=0,
            last_line_sha256=empty_hash,
            source_mtime_ns=0,
            source_sha256=empty_hash,
            row_count=0,
            captured_at=NOW,
        )
        with pytest.raises(sqlite3.IntegrityError, match="matching chunk"):
            opened.connection.execute(
                """
                UPDATE source_cursors
                SET byte_offset = 1,
                    last_line_start_offset = 0,
                    last_line_sha256 = ?
                WHERE source_name = 'decisions'
                """,
                (empty_hash,),
            )


def test_noop_writer_open_does_not_mutate_database_or_create_wal(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="initialize") as opened:
        opened.checkpoint("TRUNCATE")
    before = store.file_sha256(path)

    with store.open_operational_writer(path, writer_id="no-op"):
        pass

    assert store.file_sha256(path) == before
    assert not Path(f"{path}-wal").exists()


def test_audit_events_keep_legacy_rows_out_of_prediction_table(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    legacy = store.AuditEventRecord(
        event_id="legacy-1",
        event_type="chart_decision",
        symbol="USDJPY",
        timeframe="15m",
        event_time=NOW,
        source="legacy-jsonl",
        payload={"direction": "neutral"},
        pit_eligible=False,
        pit_failure_reason="missing_pit_contract",
        ingested_time=NOW,
    )
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        assert opened.record_audit_event(legacy)
        assert not opened.record_audit_event(legacy)
        audit = opened.audit()

    assert audit.audit_events == 1
    assert audit.predictions == 0


def test_bulk_insert_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    conflicting = store.AuditEventRecord(
        **{
            **_audit_event().__dict__,
            "payload": {"direction": "long"},
        }
    )
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.record_audit_event(_audit_event())
        with pytest.raises(store.ImmutableRecordConflict):
            opened.record_audit_events([_audit_event("decision-2"), conflicting])
        count = opened.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    assert count == 1


def test_prediction_retry_is_idempotent_but_content_change_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        assert opened.record_prediction(_prediction())
        assert not opened.record_prediction(_prediction())
        changed = store.PredictionRecord(
            **{
                **_prediction().__dict__,
                "payload": {"direction": "long", "confidence": 0.9},
            }
        )
        with pytest.raises(store.ImmutableRecordConflict):
            opened.record_prediction(changed)


def test_prediction_retry_does_not_change_first_ingestion_identity(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    first = _prediction()
    retried = store.PredictionRecord(
        **{
            **first.__dict__,
            "ingested_time": NOW + timedelta(minutes=1),
        }
    )
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        assert opened.record_prediction(first)
        assert not opened.record_prediction(retried)
        stored_ingestion = opened.connection.execute(
            "SELECT ingested_time_ns FROM predictions WHERE prediction_id = 'prediction-1'"
        ).fetchone()[0]

    assert stored_ingestion == store._utc_ns(NOW, "expected")


def test_prediction_rejects_future_feature_availability(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    invalid = store.PredictionRecord(
        **{
            **_prediction().__dict__,
            "available_time": NOW + timedelta(seconds=1),
        }
    )

    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        with pytest.raises(store.InvalidOperationalRecord, match="available_time"):
            opened.record_prediction(invalid)


def test_due_query_and_price_window_are_bounded_and_sorted(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.record_prediction(_prediction())
        opened.record_prediction(
            store.PredictionRecord(
                **{
                    **_prediction("prediction-2").__dict__,
                    "due_time": NOW + timedelta(hours=4),
                }
            )
        )
        opened.record_price_point(_price("later", minutes=20))
        opened.record_price_point(_price("earlier", minutes=15))

        due = opened.due_predictions(as_of=NOW + timedelta(minutes=30))
        prices = opened.price_window(
            symbol="USDJPY",
            timeframe="15m",
            start=NOW,
            end=NOW + timedelta(minutes=30),
        )

    assert [row["prediction_id"] for row in due] == ["prediction-1"]
    assert [row["source_record_id"] for row in prices] == ["earlier", "later"]
    assert prices[0]["payload"] == {"complete": True, "quote_source": "oanda"}


def test_price_natural_key_and_quote_quality_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        assert opened.record_price_point(_price())
        assert not opened.record_price_point(_price())
        duplicate_slot = store.PricePointRecord(
            **{
                **_price("another-source-id").__dict__,
                "close": 150.15,
                "payload": {"different": True},
            }
        )
        with pytest.raises(store.ImmutableRecordConflict, match="natural key"):
            opened.record_price_point(duplicate_slot)
        crossed = store.PricePointRecord(
            **{
                **_price("crossed", minutes=20).__dict__,
                "bid": 150.2,
                "ask": 150.1,
            }
        )
        with pytest.raises(store.InvalidOperationalRecord, match="crossed"):
            opened.record_price_point(crossed)


def test_outcome_and_prediction_status_commit_atomically(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    outcome = store.OutcomeRecord(
        prediction_id="prediction-1",
        label_version="net-r-v2",
        holding_end_time=NOW + timedelta(minutes=15),
        scored_time=NOW + timedelta(minutes=16),
        status="scored",
        realized_net_r=-0.25,
        source_checkpoint="prices:2026-07-27T00:16:00Z",
        payload={"cost_model": "spread-slippage-v1"},
    )
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.record_prediction(_prediction())
        assert opened.record_outcome(outcome)
        assert not opened.record_outcome(outcome)
        status = opened.connection.execute(
            "SELECT status FROM predictions WHERE prediction_id = 'prediction-1'"
        ).fetchone()[0]

    assert status == "scored"


def test_outcome_rejects_unknown_prediction(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    outcome = store.OutcomeRecord(
        prediction_id="missing",
        label_version="net-r-v2",
        holding_end_time=NOW,
        scored_time=NOW,
        status="evaluation_unavailable",
        source_checkpoint="prices:none",
        payload={"reason": "missing_cost"},
    )
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        with pytest.raises(store.InvalidOperationalRecord, match="unknown prediction"):
            opened.record_outcome(outcome)


def test_database_triggers_block_history_rewrite_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.record_audit_event(_audit_event())
        opened.record_prediction(_prediction())
        opened.record_price_point(_price())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            opened.connection.execute(
                "UPDATE audit_events SET symbol = 'EURUSD' WHERE event_id = 'decision-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable prediction"):
            opened.connection.execute(
                "UPDATE predictions SET symbol = 'EURUSD' WHERE prediction_id = 'prediction-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            opened.connection.execute("DELETE FROM price_points WHERE source_record_id = 'price-1'")


def test_checkpoint_cannot_move_backward(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.advance_checkpoint(
            job_name="outcome-scorer",
            high_watermark=NOW,
            high_watermark_key="b",
            source_manifest_sha256="a" * 64,
        )
        with pytest.raises(store.InvalidOperationalRecord, match="backward"):
            opened.advance_checkpoint(
                job_name="outcome-scorer",
                high_watermark=NOW - timedelta(seconds=1),
                high_watermark_key="a",
                source_manifest_sha256="b" * 64,
            )


def test_checkpoint_requires_manifest_sha256(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        with pytest.raises(store.InvalidOperationalRecord, match="SHA-256"):
            opened.advance_checkpoint(
                job_name="outcome-scorer",
                high_watermark=NOW,
                high_watermark_key="a",
                source_manifest_sha256="not-a-hash",
            )


def test_second_writer_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    with store.open_operational_writer(path, writer_id="first"):
        with pytest.raises(store.WriterBusy):
            with store.open_operational_writer(path, writer_id="second"):
                raise AssertionError("unreachable")


def test_reader_is_query_only_and_backup_is_consistent(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    backup = tmp_path / "backup" / "snapshot.sqlite3"
    with store.open_operational_writer(path, writer_id="test-writer") as opened:
        opened.record_prediction(_prediction())

    with store.open_operational_reader(path) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.connection.execute("INSERT INTO store_metadata VALUES ('forbidden', 'x', 0)")
        result = store.online_backup(reader, backup)

    assert result == backup.resolve()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert len(store.file_sha256(backup)) == 64
