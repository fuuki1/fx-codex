"""Prospective daily-report tests for the read-only data platform."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path

from data_platform.collect.contract import CollectedQuote
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.data_platform_daily_report import build_daily_report

DAY = date(2026, 7, 14)
STAMP = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _write_quote(collection_root: Path, instrument: str, payload: bytes) -> None:
    store = ImmutableRawStore(collection_root / "raw")
    ref = store.put(payload)
    quote = CollectedQuote(
        provider="oanda",
        account_environment="live",
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
        source_endpoint_class="streaming_pricing",
        collection_mode="live_stream",
    )
    log_path = collection_root / "log" / "quotes.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(quote.to_dict(), sort_keys=True) + "\n")


def _write_support(path: Path, field: str, value: bool) -> None:
    path.write_text(
        json.dumps({"report_date": DAY.isoformat(), field: value}),
        encoding="utf-8",
    )


def test_daily_report_qualifies_only_with_same_day_evidence(tmp_path: Path) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD"):
        _write_quote(collection_root, instrument, hashlib.sha256(instrument.encode()).digest())

    secondary = tmp_path / "secondary.json"
    replay = tmp_path / "replay.json"
    _write_support(secondary, "secondary_up", True)
    _write_support(replay, "replay_ok", True)

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        secondary_evidence=secondary,
        replay_evidence=replay,
    )

    assert report["qualifying_day"] is True
    assert report["raw_hash_verified"] is True
    assert report["primary_up"] is True
    assert report["secondary_up"] is True
    assert report["replay_ok"] is True
    assert report["critical_incidents"] == 0
    assert report["quote_count"] == 3


def test_daily_report_is_nonqualifying_when_supporting_evidence_is_missing(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "collect"
    for instrument in ("USDJPY", "EURUSD", "GBPUSD"):
        _write_quote(collection_root, instrument, instrument.encode())

    report = build_daily_report(
        collection_root=collection_root,
        day=DAY,
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["qualifying_day"] is False
    assert report["primary_up"] is True
    assert report["secondary_up"] is False
    assert report["replay_ok"] is False
    assert set(report["unmet_conditions"]) == {"secondary_up", "replay_ok"}


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
        secondary_evidence=None,
        replay_evidence=None,
    )

    assert report["raw_hash_verified"] is False
    assert report["raw_verification_errors"]
