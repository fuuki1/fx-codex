#!/usr/bin/env python3
"""Crash-inject canonical capture-journal transaction and rotation boundaries."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.capture_journal import (  # noqa: E402
    CaptureJournal,
    CaptureJournalError,
)
from data_platform.collect.contract import CollectedQuote  # noqa: E402
from data_platform.collect.raw_first import QuoteLog, ingest_payload  # noqa: E402
from data_platform.runtime import bid_ask_bridge  # noqa: E402

PROBE_TIME = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
_EXPECTED_CHILD_EXITS = {
    "after_raw_commit": 91,
    "during_terminal_transaction": 92,
    "after_terminal_commit": 93,
    "after_utc_rotation": 94,
    "after_capacity_rejection": 95,
    "during_utc_rotation": 96,
    "materializer_before_atomic_commit": 98,
}


def _quote(raw: bytes, *, when: datetime, sequence: int) -> CollectedQuote:
    return CollectedQuote(
        provider="oanda",
        account_environment="practice",
        instrument="USDJPY",
        provider_event_time=when,
        received_at=when,
        bid=155.001 + sequence / 100_000,
        ask=155.004 + sequence / 100_000,
        bid_size=1_000_000,
        ask_size=1_000_000,
        tradable=False,
        sequence_id=sequence,
        connection_id="crash-probe",
        writer_id="crash-probe",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(raw).hexdigest(),
        source_endpoint_class="replay_fixture",
        collection_mode="replay",
    )


def _child(stage: str, root: Path) -> None:
    if stage == "after_raw_commit":
        CaptureJournal(root).begin(b"raw-before-crash", occurred_at=PROBE_TIME)
    elif stage == "during_terminal_transaction":
        journal = CaptureJournal(root)
        capture = journal.begin(b"terminal-rollback", occurred_at=PROBE_TIME)
        shard = journal.shard_paths()[0]
        connection = sqlite3.connect(shard, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO quote_rows (
                capture_id, row_index, disposition, payload_json,
                payload_sha256, identity_sha256, provider, instrument, event_time
            ) VALUES (?, 0, 'quarantined', '{}', ?, NULL, NULL, NULL, NULL)
            """,
            (capture.capture_id, hashlib.sha256(b"uncommitted").hexdigest()),
        )
        # Deliberately skip commit/close. SQLite must roll back this transaction.
    elif stage == "after_terminal_commit":
        log = QuoteLog(root.parent, legacy_jsonl=False)
        payload = b"committed-before-crash"
        ingest_payload(
            payload,
            parser=lambda raw: [_quote(raw, when=PROBE_TIME, sequence=1)],
            log=log,
            now=lambda: PROBE_TIME,
        )
    elif stage == "after_utc_rotation":
        log = QuoteLog(root.parent, legacy_jsonl=False)
        for offset in range(2):
            when = PROBE_TIME + timedelta(days=offset)
            payload = f"rotation-{offset}".encode()

            def parser(
                raw: bytes,
                *,
                stamp: datetime = when,
                sequence: int = offset,
            ) -> list[CollectedQuote]:
                return [_quote(raw, when=stamp, sequence=sequence + 1)]

            def clock(*, stamp: datetime = when) -> datetime:
                return stamp

            ingest_payload(
                payload,
                parser=parser,
                log=log,
                now=clock,
            )
        log.journal.checkpoint()
    elif stage == "during_utc_rotation":
        log = QuoteLog(root.parent, legacy_jsonl=False)
        first_payload = b"committed-before-rotation"
        ingest_payload(
            first_payload,
            parser=lambda raw: [_quote(raw, when=PROBE_TIME, sequence=1)],
            log=log,
            now=lambda: PROBE_TIME,
        )

        class CrashAfterCheckpoint(CaptureJournal):
            def _initialize_shard(self, path: Path, shard_date: str) -> None:
                del path, shard_date
                os._exit(_EXPECTED_CHILD_EXITS["during_utc_rotation"])

        interrupted = CrashAfterCheckpoint(root)
        interrupted.begin(
            b"must-not-exist-after-rotation-crash",
            occurred_at=PROBE_TIME + timedelta(days=1),
        )
    elif stage == "after_capacity_rejection":
        log = QuoteLog(root.parent, legacy_jsonl=False)
        payload = b"committed-before-capacity-rejection"
        ingest_payload(
            payload,
            parser=lambda raw: [_quote(raw, when=PROBE_TIME, sequence=1)],
            log=log,
            now=lambda: PROBE_TIME,
        )
        constrained = CaptureJournal(root, max_shard_bytes=1)
        try:
            constrained.begin(
                b"must-not-become-durable",
                occurred_at=PROBE_TIME + timedelta(seconds=1),
            )
        except CaptureJournalError:
            pass
        else:
            os._exit(97)
    elif stage == "materializer_before_atomic_commit":

        def crash_before_commit(hook_stage: str) -> None:
            if hook_stage == "after_snapshot_inserts":
                os._exit(_EXPECTED_CHILD_EXITS["materializer_before_atomic_commit"])

        setattr(bid_ask_bridge, "_transaction_test_hook", crash_before_commit)
        bid_ask_bridge.materialize_increment(
            ingest_log_dir=root / "log",
            output_path=root / "prices.sqlite3",
            instruments=["USDJPY"],
            as_of=PROBE_TIME + timedelta(minutes=1, seconds=31),
        )
    else:
        raise ValueError(f"unknown child stage: {stage}")
    os._exit(_EXPECTED_CHILD_EXITS[stage])


def _run_child(stage: str, journal_root: Path) -> int:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-stage",
            stage,
            "--child-journal-root",
            str(journal_root),
        ],
        check=False,
    )
    return result.returncode


def run_probe(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}

    raw_root = root / "after_raw_commit" / "capture_journal"
    raw_exit = _run_child("after_raw_commit", raw_root)
    raw_journal = CaptureJournal(raw_root)
    raw_visible = raw_journal.verified_quote_count()
    raw_recovered = raw_journal.recover_incomplete(occurred_at=PROBE_TIME + timedelta(seconds=1))
    raw_journal.verify()
    results["after_raw_commit"] = {
        "child_exit": raw_exit,
        "published_quotes_before_recovery": raw_visible,
        "recovered_unavailable_count": len(raw_recovered),
        "passed": (
            raw_exit == _EXPECTED_CHILD_EXITS["after_raw_commit"]
            and raw_visible == 0
            and len(raw_recovered) == 1
        ),
    }

    rollback_root = root / "during_terminal_transaction" / "capture_journal"
    rollback_exit = _run_child("during_terminal_transaction", rollback_root)
    rollback_journal = CaptureJournal(rollback_root)
    rollback_shard = rollback_journal.shard_paths()[0]
    with sqlite3.connect(rollback_shard) as connection:
        rolled_back_rows = int(connection.execute("SELECT COUNT(*) FROM quote_rows").fetchone()[0])
    rollback_visible = rollback_journal.verified_quote_count()
    rollback_recovered = rollback_journal.recover_incomplete(
        occurred_at=PROBE_TIME + timedelta(seconds=1)
    )
    rollback_journal.verify()
    results["during_terminal_transaction"] = {
        "child_exit": rollback_exit,
        "uncommitted_rows_after_restart": rolled_back_rows,
        "published_quotes_before_recovery": rollback_visible,
        "recovered_unavailable_count": len(rollback_recovered),
        "passed": (
            rollback_exit == _EXPECTED_CHILD_EXITS["during_terminal_transaction"]
            and rolled_back_rows == 0
            and rollback_visible == 0
            and len(rollback_recovered) == 1
        ),
    }

    committed_root = root / "after_terminal_commit" / "capture_journal"
    committed_exit = _run_child("after_terminal_commit", committed_root)
    committed_journal = CaptureJournal(committed_root)
    committed_count = committed_journal.verified_quote_count()
    results["after_terminal_commit"] = {
        "child_exit": committed_exit,
        "verified_quotes_after_restart": committed_count,
        "passed": (
            committed_exit == _EXPECTED_CHILD_EXITS["after_terminal_commit"]
            and committed_count == 1
        ),
    }

    rotation_root = root / "after_utc_rotation" / "capture_journal"
    rotation_exit = _run_child("after_utc_rotation", rotation_root)
    rotation_journal = CaptureJournal(rotation_root)
    rotation_count = rotation_journal.verified_quote_count()
    identity = rotation_journal.identity()
    results["after_utc_rotation"] = {
        "child_exit": rotation_exit,
        "verified_quotes_after_restart": rotation_count,
        "shard_count": identity.shard_count,
        "head_sequence": identity.head_sequence,
        "passed": (
            rotation_exit == _EXPECTED_CHILD_EXITS["after_utc_rotation"]
            and rotation_count == 2
            and identity.shard_count == 2
        ),
    }

    during_rotation_root = root / "during_utc_rotation" / "capture_journal"
    during_rotation_exit = _run_child("during_utc_rotation", during_rotation_root)
    interrupted_rotation = CaptureJournal(during_rotation_root)
    before_resume_count = interrupted_rotation.verified_quote_count()
    before_resume_shards = len(interrupted_rotation.shard_paths())
    resumed_log = QuoteLog(during_rotation_root.parent, legacy_jsonl=False)
    resumed_time = PROBE_TIME + timedelta(days=1)
    resumed_payload = b"committed-after-rotation-resume"
    ingest_payload(
        resumed_payload,
        parser=lambda raw: [_quote(raw, when=resumed_time, sequence=2)],
        log=resumed_log,
        now=lambda: resumed_time,
    )
    after_resume_count = resumed_log.journal.verified_quote_count()
    after_resume_shards = len(resumed_log.journal.shard_paths())
    results["during_utc_rotation"] = {
        "child_exit": during_rotation_exit,
        "verified_quotes_before_resume": before_resume_count,
        "shard_count_before_resume": before_resume_shards,
        "verified_quotes_after_resume": after_resume_count,
        "shard_count_after_resume": after_resume_shards,
        "passed": (
            during_rotation_exit == _EXPECTED_CHILD_EXITS["during_utc_rotation"]
            and before_resume_count == 1
            and before_resume_shards == 1
            and after_resume_count == 2
            and after_resume_shards == 2
        ),
    }

    capacity_root = root / "after_capacity_rejection" / "capture_journal"
    capacity_exit = _run_child("after_capacity_rejection", capacity_root)
    capacity_journal = CaptureJournal(capacity_root)
    capacity_count = capacity_journal.verified_quote_count()
    capacity_entries = capacity_journal.entries()
    results["after_capacity_rejection"] = {
        "child_exit": capacity_exit,
        "verified_quotes_after_restart": capacity_count,
        "durable_entries_after_restart": len(capacity_entries),
        "passed": (
            capacity_exit == _EXPECTED_CHILD_EXITS["after_capacity_rejection"]
            and capacity_count == 1
            and len(capacity_entries) == 2
        ),
    }

    materializer_root = root / "materializer_before_atomic_commit"
    materializer_log = QuoteLog(materializer_root / "log", legacy_jsonl=False)
    for sequence, seconds in enumerate((5, 50), start=1):
        when = PROBE_TIME + timedelta(seconds=seconds)
        payload = f"materializer-{sequence}".encode()

        def materializer_parser(
            raw: bytes,
            *,
            stamp: datetime = when,
            number: int = sequence,
        ) -> list[CollectedQuote]:
            return [
                replace(
                    _quote(raw, when=stamp, sequence=number),
                    source_endpoint_class="streaming_pricing",
                    collection_mode="live_stream",
                )
            ]

        def materializer_clock(*, stamp: datetime = when) -> datetime:
            return stamp

        ingest_payload(
            payload,
            parser=materializer_parser,
            log=materializer_log,
            now=materializer_clock,
        )
    materializer_exit = _run_child("materializer_before_atomic_commit", materializer_root)
    materializer_output = materializer_root / "prices.sqlite3"
    with sqlite3.connect(materializer_output) as connection:
        rows_before_resume = int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
        state_before_resume = int(
            connection.execute("SELECT COUNT(*) FROM bridge_state").fetchone()[0]
        )
    resumed = bid_ask_bridge.materialize_increment(
        ingest_log_dir=materializer_root / "log",
        output_path=materializer_output,
        instruments=["USDJPY"],
        as_of=PROBE_TIME + timedelta(minutes=1, seconds=31),
    )
    with sqlite3.connect(materializer_output) as connection:
        rows_after_resume = int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
        state_after_resume = int(
            connection.execute("SELECT COUNT(*) FROM bridge_state").fetchone()[0]
        )
    results["materializer_before_atomic_commit"] = {
        "child_exit": materializer_exit,
        "rows_durable_before_resume": rows_before_resume,
        "checkpoint_rows_before_resume": state_before_resume,
        "rows_after_resume": rows_after_resume,
        "rows_appended_on_resume": resumed.appended_rows,
        "checkpoint_rows_after_resume": state_after_resume,
        "passed": (
            materializer_exit == _EXPECTED_CHILD_EXITS["materializer_before_atomic_commit"]
            and rows_before_resume == 0
            and state_before_resume == 0
            and rows_after_resume == 4
            and resumed.appended_rows == 4
            and state_after_resume == 1
        ),
    }

    return {
        "schema_version": 1,
        "evidence_class": "synthetic_process_crash_probe",
        "generated_at": datetime.now(UTC).isoformat(),
        "stages": results,
        "passed": all(bool(stage["passed"]) for stage in results.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child-stage", choices=tuple(_EXPECTED_CHILD_EXITS))
    parser.add_argument("--child-journal-root", type=Path)
    args = parser.parse_args(argv)

    if args.child_stage is not None:
        if args.child_journal_root is None:
            parser.error("--child-journal-root is required with --child-stage")
        _child(args.child_stage, args.child_journal_root)
        return 2
    if args.child_journal_root is not None:
        parser.error("--child-journal-root requires --child-stage")
    if args.report is not None and args.report.exists():
        parser.error(f"report is create-only and already exists: {args.report}")

    if args.output_root is None:
        with tempfile.TemporaryDirectory(prefix="fx-capture-journal-crash-") as temporary:
            report = run_probe(Path(temporary))
    else:
        report = run_probe(args.output_root)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            args.report,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o440,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
