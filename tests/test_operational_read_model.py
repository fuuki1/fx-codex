from __future__ import annotations

import base64
from datetime import datetime, timedelta, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fx_intel import operational_read_api as read_api
from fx_intel import operational_read_model as read_model
from fx_intel import operational_store as store

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 4))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.4")


def _audit(identifier: str, *, minutes: int, payload_size: int = 0) -> store.AuditEventRecord:
    stamp = NOW + timedelta(minutes=minutes)
    return store.AuditEventRecord(
        event_id=identifier,
        event_type="chart_decision",
        symbol="USDJPY",
        timeframe="15m",
        event_time=stamp,
        prediction_time=stamp,
        available_time=stamp - timedelta(seconds=1),
        source="test",
        payload={
            "decision": {
                "direction": "neutral",
                "analysis_direction": "long",
            },
            "large": "x" * payload_size,
        },
        pit_eligible=True,
        pit_failure_reason="",
        ingested_time=stamp,
    )


def _price(identifier: str, *, minutes: int) -> store.PricePointRecord:
    stamp = NOW + timedelta(minutes=minutes)
    return store.PricePointRecord(
        source_record_id=identifier,
        symbol="USDJPY",
        timeframe="15m",
        event_time=stamp,
        available_time=stamp,
        ingested_time=stamp + timedelta(seconds=1),
        source="test",
        revision_id="initial",
        close=150.0 + minutes / 100,
        ohlc_scope="completed_bar",
        payload={"identifier": identifier},
    )


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "operational.sqlite3"
    empty_hash = hashlib.sha256(b"").hexdigest()
    with store.open_operational_writer(database, writer_id="fixture") as opened:
        for index, minutes in enumerate((0, 1, 1, 2, 3)):
            opened.record_audit_event(
                _audit(
                    f"decision-{index}",
                    minutes=minutes,
                    payload_size=10_000 if index == 4 else 0,
                )
            )
            opened.record_price_point(_price(f"price-{index}", minutes=index))
        for source_name, source_kind in (
            ("decisions", "decisions"),
            ("prices", "prices"),
        ):
            source_path = tmp_path / f"{source_name}.jsonl"
            source_path.write_bytes(b"")
            source_stat = source_path.stat()
            opened.bootstrap_source_cursor(
                source_name=source_name,
                source_kind=source_kind,
                source_path=str(source_path),
                device_id=source_stat.st_dev,
                inode=source_stat.st_ino,
                byte_offset=0,
                last_line_start_offset=0,
                last_line_sha256=empty_hash,
                source_mtime_ns=source_stat.st_mtime_ns,
                source_ctime_ns=source_stat.st_ctime_ns,
                source_sha256=empty_hash,
                row_count=0,
                captured_at=NOW,
            )
        opened.checkpoint("TRUNCATE")
    return database


def test_decision_keyset_pagination_has_no_gaps_or_duplicates(tmp_path: Path) -> None:
    database = _database(tmp_path)
    identifiers: list[str] = []
    cursor = None
    snapshot = None
    with store.open_operational_reader(database) as opened:
        while True:
            page = read_model.decision_page(opened, limit=2, cursor=cursor)
            if snapshot is None:
                snapshot = page["snapshot_token"]
            assert page["snapshot_token"] == snapshot
            identifiers.extend(item["event_id"] for item in page["items"])
            cursor = page["page"]["next_cursor"]
            if cursor is None:
                break

    assert identifiers == [
        "decision-4",
        "decision-3",
        "decision-2",
        "decision-1",
        "decision-0",
    ]
    assert len(identifiers) == len(set(identifiers))


def test_cursor_snapshot_excludes_late_insert_between_pages(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with store.open_operational_reader(database) as opened:
        first = read_model.decision_page(opened, limit=2)
    with store.open_operational_writer(database, writer_id="late-insert") as opened:
        opened.record_audit_event(_audit("late-row", minutes=1))
    identifiers = [item["event_id"] for item in first["items"]]
    cursor = first["page"]["next_cursor"]
    with store.open_operational_reader(database) as opened:
        while cursor is not None:
            page = read_model.decision_page(opened, limit=2, cursor=cursor)
            identifiers.extend(item["event_id"] for item in page["items"])
            cursor = page["page"]["next_cursor"]

    assert "late-row" not in identifiers
    assert len(identifiers) == 5
    with store.open_operational_reader(database) as opened:
        fresh = read_model.decision_page(opened, limit=10)
    assert "late-row" in [item["event_id"] for item in fresh["items"]]


def test_cursor_rejects_tampering_and_changed_filters(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with store.open_operational_reader(database) as opened:
        first = read_model.decision_page(opened, limit=2, symbol="USDJPY")
        cursor = first["page"]["next_cursor"]
        assert cursor is not None
        with pytest.raises(read_model.ReadModelError, match="filters changed"):
            read_model.decision_page(
                opened,
                limit=2,
                cursor=cursor,
                symbol="EURUSD",
            )
        with pytest.raises(read_model.ReadModelError):
            replacement = "0" if cursor[-1] != "0" else "1"
            read_model.decision_page(
                opened,
                limit=2,
                cursor=f"{cursor[:-1]}{replacement}",
                symbol="USDJPY",
            )


def test_cursor_rejects_forged_snapshot_even_with_public_sha256(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with store.open_operational_reader(database) as opened:
        first = read_model.decision_page(opened, limit=2)
        cursor = first["page"]["next_cursor"]
        assert cursor is not None
        encoded, _signature = cursor.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        payload["m"] += 10_000
        forged_raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        forged_encoded = base64.urlsafe_b64encode(forged_raw).decode().rstrip("=")
        forged = f"{forged_encoded}.{hashlib.sha256(forged_raw).hexdigest()}"

        with pytest.raises(read_model.ReadModelError, match="signature"):
            read_model.decision_page(opened, limit=2, cursor=forged)


def test_reader_remains_read_only_after_query_only_is_disabled(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with store.open_operational_reader(database) as opened:
        opened.connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(store.sqlite3.OperationalError, match="readonly"):
            opened.connection.execute("CREATE TABLE forbidden_write(value INTEGER)")


def test_compact_pages_omit_large_raw_payload_and_price_page_is_stable(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with store.open_operational_reader(database) as opened:
        decisions = read_model.decision_page(opened, limit=1)
        prices = read_model.price_page(opened, limit=3)
        metadata = read_model.read_model_metadata(opened)

    encoded = read_model.canonical_response_bytes(decisions)
    assert len(encoded) < 2_000
    assert b'"large"' not in encoded
    assert decisions["items"][0]["direction"] == "neutral"
    assert [item["source_record_id"] for item in prices["items"]] == [
        "price-4",
        "price-3",
        "price-2",
    ]
    assert metadata["query_only"] is True
    assert metadata["rows"]["audit_events"] == 5


def test_decision_page_exposes_compact_abstention_evidence_from_current_shape(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    stamp = NOW + timedelta(minutes=10)
    with store.open_operational_writer(database, writer_id="current-shape") as opened:
        opened.record_audit_event(
            store.AuditEventRecord(
                event_id="current-shape",
                event_type="chart_decision",
                symbol="USDJPY",
                timeframe="4h",
                event_time=stamp,
                prediction_time=None,
                available_time=None,
                source="test",
                payload={
                    "decision": {
                        "direction": "neutral",
                        "composite": 0.285,
                        "direction_threshold": 0.15,
                        "entry_bid": None,
                        "entry_ask": None,
                        "execution": {"net_expected_r": -0.06},
                        "gate_trace": [{"gate": "expectancy_guard", "status": "blocked"}],
                        "shadow_predictions": [{"direction": "long", "score": 0.285}],
                    },
                    "large": "x" * 10_000,
                },
                pit_eligible=False,
                pit_failure_reason="pit_flag_not_true",
                ingested_time=stamp,
            )
        )

    with store.open_operational_reader(database) as opened:
        page = read_model.decision_page(opened, limit=1)

    item = page["items"][0]
    assert item["event_id"] == "current-shape"
    assert item["direction"] == "neutral"
    assert item["analysis_direction"] is None
    assert item["analysis_direction_provenance"] == "unavailable_legacy"
    assert item["analysis_score"] == pytest.approx(0.285)
    assert item["composite"] == pytest.approx(0.285)
    assert item["direction_threshold"] == pytest.approx(0.15)
    assert item["primary_gate"] == "expectancy_guard"
    assert item["quote_available"] is False
    assert item["net_expected_r"] == pytest.approx(-0.06)
    encoded = read_model.canonical_response_bytes(page)
    assert b'"large"' not in encoded
    assert len(encoded) < 2_000


def test_decision_page_exposes_only_explicit_analysis_direction_and_blocked_gate(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    stamp = NOW + timedelta(minutes=11)
    with store.open_operational_writer(database, writer_id="explicit-analysis") as opened:
        opened.record_audit_event(
            store.AuditEventRecord(
                event_id="explicit-analysis",
                event_type="chart_decision",
                symbol="USDJPY",
                timeframe="1d",
                event_time=stamp,
                prediction_time=None,
                available_time=None,
                source="test",
                payload={
                    "decision": {
                        "direction": "long",
                        "analysis_direction": "long",
                        "analysis_score": 0.3,
                        "gate_trace": [
                            {"gate": "liquidity", "status": "observed"},
                            {"gate": "expectancy_guard", "status": "blocked"},
                        ],
                    }
                },
                pit_eligible=False,
                pit_failure_reason="pit_flag_not_true",
                ingested_time=stamp,
            )
        )

    with store.open_operational_reader(database) as opened:
        item = read_model.decision_page(opened, limit=1)["items"][0]

    assert item["analysis_direction"] == "long"
    assert item["analysis_direction_provenance"] == "explicit"
    assert item["primary_gate"] == "expectancy_guard"


def test_response_etag_is_strong_and_byte_exact() -> None:
    first = read_model.canonical_response_bytes({"value": 1})
    same = read_model.canonical_response_bytes({"value": 1})
    changed = read_model.canonical_response_bytes({"value": 2})

    assert read_model.response_etag(first) == read_model.response_etag(same)
    assert read_model.response_etag(first) != read_model.response_etag(changed)
    assert read_model.response_etag(first).startswith('"sha256-')


def test_http_api_returns_etag_304_and_validates_query(tmp_path: Path) -> None:
    database = _database(tmp_path)
    server = read_api.OperationalReadHTTPServer(
        ("127.0.0.1", 0),
        read_api.make_handler(database),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/v1/decisions?limit=2", timeout=2) as response:
            assert response.status == 200
            etag = response.headers["ETag"]
            snapshot = response.headers["X-Read-Model-Snapshot"]
            assert response.headers["Connection"] == "close"
            payload = json.loads(response.read())
        assert payload["page"]["returned"] == 2
        assert payload["snapshot_token"] == snapshot
        request = Request(
            f"{base}/v1/decisions?limit=2",
            headers={"If-None-Match": f"W/{etag}"},
        )
        with pytest.raises(HTTPError) as unchanged:
            urlopen(request, timeout=2)
        assert unchanged.value.code == 304
        assert unchanged.value.headers["ETag"] == etag
        with pytest.raises(HTTPError) as invalid:
            urlopen(f"{base}/v1/prices?unknown=true", timeout=2)
        assert invalid.value.code == 400
        error = json.loads(invalid.value.read())
        assert error["status"] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_api_returns_503_when_raw_source_advances_before_sync(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with (tmp_path / "decisions.jsonl").open("ab") as handle:
        handle.write(b'{"event_type":"unexpected_suffix"}\n')
    server = read_api.OperationalReadHTTPServer(
        ("127.0.0.1", 0),
        read_api.make_handler(database),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route in ("/healthz", "/v1/decisions?limit=2"):
            with pytest.raises(HTTPError) as stale:
                urlopen(f"{base}{route}", timeout=2)
            assert stale.value.code == 503
            error = json.loads(stale.value.read())
            assert "advanced beyond read model" in error["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_api_holds_raw_snapshot_until_response_is_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    source_path = tmp_path / "decisions.jsonl"
    serialization_started = threading.Event()
    release_response = threading.Event()
    writer_attempting = threading.Event()
    writer_done = threading.Event()
    original_canonical_response_bytes = read_api.canonical_response_bytes

    def paused_canonical_response_bytes(payload: object) -> bytes:
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "decisions"
            and not serialization_started.is_set()
        ):
            serialization_started.set()
            if not release_response.wait(timeout=2):
                raise TimeoutError("test did not release response serialization")
        return original_canonical_response_bytes(payload)

    monkeypatch.setattr(
        read_api,
        "canonical_response_bytes",
        paused_canonical_response_bytes,
    )
    server = read_api.OperationalReadHTTPServer(
        ("127.0.0.1", 0),
        read_api.make_handler(database),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    response_result: dict[str, object] = {}

    def request_snapshot() -> None:
        try:
            with urlopen(f"{base}/v1/decisions?limit=2", timeout=3) as response:
                response_result["status"] = response.status
                response_result["payload"] = json.loads(response.read())
        except BaseException as error:
            response_result["error"] = error

    def append_raw_source() -> None:
        with source_path.open("a+b", buffering=0) as handle:
            writer_attempting.set()
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(b'{"event_type":"raced_suffix"}\n')
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        writer_done.set()

    request_thread = threading.Thread(target=request_snapshot, daemon=True)
    writer_thread = threading.Thread(target=append_raw_source, daemon=True)
    try:
        request_thread.start()
        assert serialization_started.wait(timeout=2)
        writer_thread.start()
        assert writer_attempting.wait(timeout=2)
        assert not writer_done.wait(timeout=0.2)

        release_response.set()
        request_thread.join(timeout=2)
        writer_thread.join(timeout=2)
        assert not request_thread.is_alive()
        assert not writer_thread.is_alive()
        assert "error" not in response_result
        assert response_result["status"] == 200
        payload = response_result["payload"]
        assert isinstance(payload, dict)
        assert payload["page"]["returned"] == 2
        assert writer_done.is_set()

        with pytest.raises(HTTPError) as stale:
            urlopen(f"{base}/v1/decisions?limit=2", timeout=2)
        assert stale.value.code == 503
    finally:
        release_response.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        request_thread.join(timeout=2)
        writer_thread.join(timeout=2)
