"""Prospective daily-report tests for the read-only data platform."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.raw_first import QuoteLog, ingest_payload
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.data_platform_daily_report import build_daily_report

STAMP = datetime.now(UTC) - timedelta(minutes=1)
DAY = STAMP.date()
MAX_AGE_FOR_TEST = 10


def _write_quote(collection_root: Path, instrument: str, payload: bytes) -> None:
    store = ImmutableRawStore(collection_root / "raw")
    ref = store.put(payload)
    quote = CollectedQuote(
        provider="tiingo",
        account_environment="datafeed",
        instrument=instrument,
        provider_event_time=STAMP,
        received_at=STAMP,
        bid=155.001,
        ask=155.004,
        bid_size=None,
        ask_size=None,
        tradable=True,
        sequence_id=None,
        connection_id="connection",
        writer_id="writer",
        revision_id=None,
        raw_payload_sha256=ref.sha256,
        source_endpoint_class="tiingo_fx_websocket",
        collection_mode="live_stream",
    )
    log_path = collection_root / "log" / "quotes.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(quote.to_dict(), sort_keys=True) + "\n")


def _write_journal_quote(
    collection_root: Path,
    instrument: str,
    payload: bytes,
    *,
    sequence: int,
    stamp: datetime = STAMP,
) -> None:
    log = QuoteLog(collection_root / "log", legacy_jsonl=False)

    def parser(raw: bytes) -> list[CollectedQuote]:
        return [
            CollectedQuote(
                provider="tiingo",
                account_environment="datafeed",
                instrument=instrument,
                provider_event_time=stamp + timedelta(seconds=sequence),
                received_at=stamp + timedelta(seconds=sequence),
                bid=155.001,
                ask=155.004,
                bid_size=None,
                ask_size=None,
                tradable=True,
                sequence_id=sequence,
                connection_id="connection",
                writer_id="writer",
                revision_id=None,
                raw_payload_sha256=hashlib.sha256(raw).hexdigest(),
                source_endpoint_class="tiingo_fx_websocket",
                collection_mode="live_stream",
            )
        ]

    ingest_payload(
        payload,
        parser=parser,
        log=log,
        now=lambda: stamp + timedelta(seconds=sequence),
    )


def _write_support(path: Path, field: str, value: bool, *, day: date = DAY) -> None:
    role, source_id = {
        "primary_up": ("primary_health", "tiingo_primary_health"),
        "secondary_up": ("independent_secondary", "independent_secondary_feed"),
        "replay_ok": ("deterministic_replay", "capture_journal_replay"),
    }[field]
    path.write_text(
        json.dumps(
            {
                "report_date": day.isoformat(),
                "observed_at": STAMP.isoformat(),
                "evidence_role": role,
                "source_id": source_id,
                field: value,
            }
        ),
        encoding="utf-8",
    )


def test_daily_report_qualifies_only_with_bound_same_day_evidence(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"):
        _write_quote(collection_root, instrument, hashlib.sha256(instrument.encode()).digest())

    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    replay = tmp_path / "replay.json"
    _write_support(primary, "primary_up", True)
    _write_support(secondary, "secondary_up", True)
    _write_support(replay, "replay_ok", True)

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=primary,
        secondary_evidence=secondary,
        replay_evidence=replay,
    )

    assert report["qualifying_day"] is True
    assert report["prospective_window_ok"] is True
    assert report["raw_hash_verified"] is True
    assert report["primary_up"] is True
    assert report["secondary_up"] is True
    assert report["replay_ok"] is True
    assert report["critical_incidents"] == 0
    assert report["quote_count"] == 4
    assert report["supporting_evidence_contract_ok"] is True
    assert report["supporting_evidence_distinct"] is True
    assert report["supporting_evidence"]["primary"]["sha256"]
    assert not (collection_root / "log" / "capture_journal").exists()


def test_daily_report_reads_canonical_journal_without_legacy_files(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for sequence, instrument in enumerate(
        ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"),
        start=1,
    ):
        _write_journal_quote(
            collection_root,
            instrument,
            instrument.encode(),
            sequence=sequence,
        )
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    replay = tmp_path / "replay.json"
    _write_support(primary, "primary_up", True)
    _write_support(secondary, "secondary_up", True)
    _write_support(replay, "replay_ok", True)

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=primary,
        secondary_evidence=secondary,
        replay_evidence=replay,
    )

    assert report["qualifying_day"] is True
    assert report["raw_hash_verified"] is True
    assert report["quote_count"] == 4
    assert report["raw_blob_count_verified"] == 4
    assert report["freshness_seconds"]["population_count"] == 4
    assert report["freshness_seconds"]["sample_count"] == 4
    assert not (collection_root / "log" / "quotes.jsonl").exists()


def test_invalid_canonical_journal_never_falls_back_to_legacy_logs(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for sequence, instrument in enumerate(
        ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"),
        start=1,
    ):
        _write_journal_quote(
            collection_root,
            instrument,
            f"journal-{instrument}".encode(),
            sequence=sequence,
        )
        _write_quote(
            collection_root,
            instrument,
            f"legacy-{instrument}".encode(),
        )
    shard = next((collection_root / "log" / "capture_journal").glob("capture-*.sqlite3"))
    with sqlite3.connect(shard) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            "UPDATE events SET raw_payload = ? WHERE sequence = 1",
            (b"tampered",),
        )

    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    replay = tmp_path / "replay.json"
    _write_support(primary, "primary_up", True)
    _write_support(secondary, "secondary_up", True)
    _write_support(replay, "replay_ok", True)
    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=primary,
        secondary_evidence=secondary,
        replay_evidence=replay,
    )

    assert report["qualifying_day"] is False
    assert report["raw_hash_verified"] is False
    assert report["quote_count"] == 0
    assert report["primary_pair_coverage_ok"] is False
    assert "capture_journal" in report["raw_verification_errors"][0]


def test_future_canonical_quote_is_nonqualifying(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    future = datetime.now(UTC) + timedelta(minutes=10)
    _write_journal_quote(
        collection_root,
        "USDJPY",
        b"future",
        sequence=1,
        stamp=future,
    )

    report = build_daily_report(
        collection_root=collection_root,
        day=future.date(),
        primary_evidence=None,
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["raw_hash_verified"] is False
    assert "future_quote_timestamp" in report["raw_verification_errors"]


def test_daily_report_is_nonqualifying_when_supporting_evidence_is_missing(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"):
        _write_quote(collection_root, instrument, instrument.encode())

    primary = tmp_path / "primary.json"
    _write_support(primary, "primary_up", True)
    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=primary,
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["qualifying_day"] is False
    assert report["primary_up"] is True
    assert report["secondary_up"] is False
    assert report["replay_ok"] is False
    assert {"secondary_up", "replay_ok"}.issubset(report["unmet_conditions"])


def test_daily_report_detects_missing_raw_blob(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    _write_quote(collection_root, "USDJPY", b"payload")
    quote_log = collection_root / "log" / "quotes.jsonl"
    row = json.loads(quote_log.read_text())
    row["raw_payload_sha256"] = "f" * 64
    quote_log.write_text(json.dumps(row) + "\n", encoding="utf-8")

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=None,
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["raw_hash_verified"] is False
    assert report["raw_verification_errors"]


def test_daily_report_refuses_historical_backfill_window(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    collection_root.mkdir()
    old_day = DAY - timedelta(days=MAX_AGE_FOR_TEST)

    report = build_daily_report(
        collection_root=collection_root,
        day=old_day,
        primary_evidence=None,
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["prospective_window_ok"] is False
    assert report["qualifying_day"] is False
    assert "prospective_window_ok" in report["unmet_conditions"]


def test_primary_quote_without_event_time_is_nonqualifying(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"):
        _write_quote(collection_root, instrument, instrument.encode())
    quote_log = collection_root / "log" / "quotes.jsonl"
    rows = [json.loads(line) for line in quote_log.read_text().splitlines()]
    rows[0]["provider_event_time"] = None
    rows[0]["received_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    quote_log.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    replay = tmp_path / "replay.json"
    _write_support(primary, "primary_up", True)
    _write_support(secondary, "secondary_up", True)
    _write_support(replay, "replay_ok", True)

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=primary,
        secondary_evidence=secondary,
        replay_evidence=replay,
    )

    assert report["qualifying_day"] is False
    assert report["raw_hash_verified"] is False
    assert report["primary_pair_coverage_ok"] is False
    assert report["freshness_seconds"]["population_count"] == 3
    assert "primary_event_timestamp_missing" in report["raw_verification_errors"]
    assert "future_quote_timestamp" in report["raw_verification_errors"]


def test_supporting_evidence_cannot_reuse_one_file_for_all_roles(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD"):
        _write_quote(collection_root, instrument, instrument.encode())
    evidence = tmp_path / "evidence.json"
    _write_support(evidence, "primary_up", True)

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        primary_evidence=evidence,
        secondary_evidence=evidence,
        replay_evidence=evidence,
    )

    assert report["qualifying_day"] is False
    assert report["supporting_evidence_contract_ok"] is False
    assert report["supporting_evidence_distinct"] is False
    assert "supporting_evidence_distinct" in report["unmet_conditions"]
