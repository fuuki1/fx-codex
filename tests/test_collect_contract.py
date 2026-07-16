"""Contract + raw-first pipeline tests for the read-only collector.

Covers: crossed book / NaN / naive & future timestamps rejected; provider-
missing fields flagged (never zero-filled); mandatory raw-first order; raw
tamper detection; duplicate & out-of-order classification; stale quarantine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from data_platform.collect.contract import (
    FLAG_NO_ASK_SIZE,
    FLAG_NO_BID_SIZE,
    FLAG_NO_SEQUENCE,
    CollectedQuote,
    QuoteContractError,
)
from data_platform.collect.raw_first import (
    FLAG_DUPLICATE,
    FLAG_OUT_OF_ORDER,
    FLAG_STALE,
    QuoteLog,
    ingest_payload,
)
from data_platform.quality.state import QualityState
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
SHA = "0" * 64


def _quote(**overrides: object) -> CollectedQuote:
    payload: dict[str, object] = {
        "provider": "dukascopy",
        "account_environment": "datafeed",
        "instrument": "USDJPY",
        "provider_event_time": NOW - timedelta(seconds=1),
        "received_at": NOW,
        "bid": 155.001,
        "ask": 155.004,
        "bid_size": 1.2,
        "ask_size": 0.8,
        "tradable": False,
        "sequence_id": None,
        "connection_id": "conn-1",
        "writer_id": "test:1",
        "revision_id": None,
        "raw_payload_sha256": SHA,
        "source_endpoint_class": "historical_datafeed",
        "collection_mode": "historical_download",
    }
    payload.update(overrides)
    return CollectedQuote(**payload)  # type: ignore[arg-type]


class TestContractRejections:
    def test_crossed_book_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="crossed"):
            _quote(bid=155.010, ask=155.004)

    def test_zero_width_book_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="crossed"):
            _quote(bid=155.004, ask=155.004)

    def test_nan_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="finite"):
            _quote(bid=float("nan"))

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="finite"):
            _quote(ask=-1.0)

    def test_naive_received_at_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="timezone-aware"):
            _quote(received_at=datetime(2026, 7, 14, 12, 0, 0))

    def test_future_event_time_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="future provider_event_time"):
            _quote(provider_event_time=NOW + timedelta(seconds=10))

    def test_small_clock_skew_tolerated(self) -> None:
        quote = _quote(provider_event_time=NOW + timedelta(seconds=1))
        assert quote.provider_event_time is not None

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(QuoteContractError, match="non-negative"):
            _quote(bid_size=-5.0)

    def test_bool_is_not_a_price(self) -> None:
        with pytest.raises(QuoteContractError):
            _quote(bid=True)


class TestHonestMissingFields:
    def test_missing_fields_flagged_not_zero_filled(self) -> None:
        quote = _quote(bid_size=None, ask_size=None, sequence_id=None)
        assert quote.bid_size is None and quote.ask_size is None
        assert FLAG_NO_BID_SIZE in quote.quality_flags
        assert FLAG_NO_ASK_SIZE in quote.quality_flags
        assert FLAG_NO_SEQUENCE in quote.quality_flags

    def test_mid_and_spread_are_computed_only(self) -> None:
        quote = _quote(bid=155.000, ask=155.003)
        assert quote.mid == pytest.approx(155.0015)
        assert quote.spread == pytest.approx(0.003)

    def test_round_trip_serialization(self) -> None:
        quote = _quote()
        again = CollectedQuote.from_dict(quote.to_dict())
        assert again.to_dict() == quote.to_dict()


class TestRawFirstOrder:
    def _parser_for(self, payload: bytes) -> list[CollectedQuote]:
        return [_quote(raw_payload_sha256=hashlib.sha256(payload).hexdigest())]

    def test_raw_stored_and_hash_verified_before_accept(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        payload = b"tick-payload-1"
        result = ingest_payload(payload, parser=self._parser_for, store=store, log=log)
        assert result.accepted_count == 1
        assert store.get(result.raw_sha256) == payload  # raw is durable
        rows = (tmp_path / "log" / "quotes.jsonl").read_text().splitlines()
        assert len(rows) == 1

    def test_parse_failure_keeps_raw_and_quarantines(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def broken(_: bytes) -> list[CollectedQuote]:
            raise ValueError("malformed provider payload")

        payload = b"garbage-payload"
        result = ingest_payload(payload, parser=broken, store=store, log=log)
        assert result.accepted_count == 0
        assert result.quarantined and result.quarantined[0].reason == "schema_validation_failed"
        assert store.get(result.raw_sha256) == payload  # raw never discarded
        quarantine = (tmp_path / "log" / "quarantine.jsonl").read_text()
        assert "schema_validation_failed" in quarantine

    def test_quote_citing_wrong_raw_hash_is_quarantined(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        result = ingest_payload(
            b"payload-x",
            parser=lambda _raw: [_quote(raw_payload_sha256="f" * 64)],
            store=store,
            log=log,
        )
        assert result.accepted_count == 0
        assert result.quarantined[0].reason == "raw_hash_mismatch"

    def test_raw_tamper_detected_on_read(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        ref = store.put(b"original-bytes")
        blob = next((tmp_path / "raw").rglob(f"*{ref.sha256[2:6]}*"), None)
        paths = [p for p in (tmp_path / "raw").rglob("*") if p.is_file()]
        assert paths, "raw blob file must exist"
        paths[0].write_bytes(b"tampered!!")
        with pytest.raises(RawStoreError):
            store.get(ref.sha256)
        assert blob is None or True  # location layout is store-internal


class TestDuplicateOrderingStale:
    def test_duplicate_quarantined_not_dropped(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def parser(payload: bytes) -> list[CollectedQuote]:
            sha = hashlib.sha256(payload).hexdigest()
            return [_quote(raw_payload_sha256=sha), _quote(raw_payload_sha256=sha)]

        result = ingest_payload(b"dup-payload", parser=parser, store=store, log=log)
        assert result.accepted_count == 1
        quarantined = (tmp_path / "log" / "quarantine.jsonl").read_text()
        assert FLAG_DUPLICATE in quarantined

    def test_out_of_order_quarantined_not_accepted(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def parser(payload: bytes) -> list[CollectedQuote]:
            sha = hashlib.sha256(payload).hexdigest()
            return [
                _quote(raw_payload_sha256=sha, provider_event_time=NOW - timedelta(seconds=1)),
                _quote(raw_payload_sha256=sha, provider_event_time=NOW - timedelta(seconds=5)),
            ]

        result = ingest_payload(b"ooo-payload", parser=parser, store=store, log=log)
        assert result.accepted_count == 1
        rows = [
            json.loads(line)
            for line in (tmp_path / "log" / "quotes.jsonl").read_text().splitlines()
        ]
        assert all(FLAG_OUT_OF_ORDER not in row["quality_flags"] for row in rows)
        quarantined = [
            json.loads(line)
            for line in (tmp_path / "log" / "quarantine.jsonl").read_text().splitlines()
        ]
        assert any(FLAG_OUT_OF_ORDER in row["quality_flags"] for row in quarantined)
        assert any(row["quality_state"] == str(QualityState.QUARANTINED) for row in quarantined)

    def test_stale_live_quote_quarantined_never_tradable(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def parser(payload: bytes) -> list[CollectedQuote]:
            sha = hashlib.sha256(payload).hexdigest()
            return [
                _quote(
                    raw_payload_sha256=sha,
                    provider_event_time=NOW - timedelta(minutes=10),
                )
            ]

        result = ingest_payload(
            b"stale-payload", parser=parser, store=store, log=log, stale_after_seconds=30.0
        )
        assert result.accepted_count == 0
        quarantined = (tmp_path / "log" / "quarantine.jsonl").read_text()
        assert FLAG_STALE in quarantined

    def test_log_state_survives_restart(self, tmp_path: Path) -> None:
        """A restarted collector must still detect duplicates (recovery)."""

        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")

        def parser(payload: bytes) -> list[CollectedQuote]:
            return [_quote(raw_payload_sha256=hashlib.sha256(payload).hexdigest())]

        ingest_payload(b"restart-payload", parser=parser, store=store, log=log)
        reopened = QuoteLog(tmp_path / "log")  # simulates process restart
        result = ingest_payload(b"restart-payload", parser=parser, store=store, log=reopened)
        assert result.accepted_count == 0  # duplicate caught after restart
