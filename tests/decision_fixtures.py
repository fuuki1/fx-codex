"""Test-only helpers for writing fully committed decision transactions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, UTC
from pathlib import Path

from fx_intel import decision_commit, decision_log, journal


def write_committed_decision_events(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    mode: str,
) -> None:
    """Append exact full events plus their durable compact proof and commit."""

    normalized_rows = [dict(row) for row in rows]
    decision_ids = [str(row.get("decision_id") or "") for row in normalized_rows]
    transaction_id = decision_commit.transaction_id_for(decision_ids)
    full_batch = decision_log.append_decision_events(
        path,
        normalized_rows,
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        decision_commit.compact_path_for_mode(path, mode),
        normalized_rows,
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        path,
        decision_ids=decision_ids,
        full_batch_sha256=str(full_batch["batch_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode=mode,
        committed_at=datetime.now(UTC),
    )


def write_committed_compact_rows(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    mode: str,
) -> None:
    normalized_rows = [dict(row) for row in rows]
    decision_ids = [str(row.get("decision_id") or "") for row in normalized_rows]
    transaction_id = decision_commit.transaction_id_for(decision_ids)
    full_events = [
        {
            "schema": 1,
            "event_type": decision_log.EVENT_TYPE,
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe", "fusion"),
            "horizon_hours": row.get("horizon_hours", 24.0),
            "source": "test_fixture",
            "decision": {
                "direction": row.get("direction"),
                "conviction": row.get("conviction"),
                "close": row.get("close"),
                "stop": row.get("stop"),
                "target1": row.get("target1"),
                "target2": row.get("target2"),
            },
            **{
                key: row.get(key)
                for key in (
                    "ts",
                    "prediction_time",
                    "source_cutoff",
                    "max_feature_available_time",
                    "pit_eligible",
                    "pit_contract",
                    "decision_id",
                    "mode",
                    "producer",
                    "producer_version",
                    "input_context_id",
                    "source_record_ids",
                )
            },
        }
        for row in normalized_rows
    ]
    full_path = decision_commit.commit_path_for(path)
    full_batch = decision_log.append_decision_events(
        full_path,
        full_events,
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        path,
        normalized_rows,
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=decision_ids,
        full_batch_sha256=str(full_batch["batch_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        mode=mode,
        committed_at=datetime.now(UTC),
    )
