"""Small, immutable projections and keyset pagination for dashboard reads."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any

from .operational_store import OperationalStore

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_CURSOR_VERSION = 2
_CURSOR_HMAC_KEY = secrets.token_bytes(32)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_./-]{1,32}$")
_TIMEFRAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class ReadModelError(ValueError):
    """A page request or opaque cursor violates the read-model contract."""


@dataclass(frozen=True)
class PageCursor:
    kind: str
    max_rowid: int
    last_time_ns: int
    last_key: str
    filter_sha256: str
    snapshot_token: str


def decision_page(
    store: OperationalStore,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    pit_eligible: bool | None = None,
) -> dict[str, object]:
    """Return a stable newest-first page without the full raw decision payload."""

    page_size = _page_size(limit)
    normalized_symbol = _symbol(symbol)
    normalized_timeframe = _timeframe(timeframe)
    filters: dict[str, object] = {
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "pit_eligible": pit_eligible,
    }
    filter_hash = _filter_hash("decisions", filters)
    decoded = _decode_cursor(cursor, kind="decisions", filter_sha256=filter_hash)
    max_rowid = decoded.max_rowid if decoded is not None else _max_rowid(store, "audit_events")
    snapshot_token = (
        decoded.snapshot_token
        if decoded is not None
        else _snapshot_token(store, "decisions", max_rowid)
    )
    clauses = ["rowid <= ?"]
    values: list[object] = [max_rowid]
    if normalized_symbol is not None:
        clauses.append("symbol = ?")
        values.append(normalized_symbol)
    if normalized_timeframe is not None:
        clauses.append("timeframe = ?")
        values.append(normalized_timeframe)
    if pit_eligible is not None:
        clauses.append("pit_eligible = ?")
        values.append(int(pit_eligible))
    if decoded is not None:
        clauses.append("(event_time_ns < ? OR (event_time_ns = ? AND event_id < ?))")
        values.extend([decoded.last_time_ns, decoded.last_time_ns, decoded.last_key])
    values.append(page_size + 1)
    rows = store.connection.execute(
        f"""
        SELECT event_id, event_type, symbol, timeframe, event_time_ns,
               prediction_time_ns, available_time_ns, pit_eligible,
               pit_failure_reason, source, schema_version,
               COALESCE(
                   json_extract(payload_json, '$.decision.direction'),
                   json_extract(payload_json, '$.direction'),
                   ''
               ) AS direction,
               COALESCE(
                   json_extract(payload_json, '$.decision.analysis_direction'),
                   json_extract(payload_json, '$.analysis_direction')
               ) AS analysis_direction,
               CASE
                   WHEN json_type(
                       payload_json,
                       '$.decision.analysis_direction'
                   ) IS NOT NULL
                     OR json_type(payload_json, '$.analysis_direction') IS NOT NULL
                   THEN 'explicit'
                   ELSE 'unavailable_legacy'
               END AS analysis_direction_provenance,
               COALESCE(
                   json_extract(payload_json, '$.decision.analysis_score'),
                   json_extract(payload_json, '$.analysis_score'),
                   json_extract(payload_json, '$.decision.composite'),
                   json_extract(payload_json, '$.composite')
               ) AS analysis_score,
               json_extract(payload_json, '$.decision.composite') AS composite,
               json_extract(
                   payload_json,
                   '$.decision.direction_threshold'
               ) AS direction_threshold,
               COALESCE(
                   (
                       SELECT json_extract(gate.value, '$.gate')
                       FROM json_each(
                           payload_json,
                           '$.decision.gate_trace'
                       ) AS gate
                       WHERE json_extract(gate.value, '$.status') = 'blocked'
                       LIMIT 1
                   ),
                   (
                       SELECT json_extract(gate.value, '$.gate')
                       FROM json_each(payload_json, '$.gate_trace') AS gate
                       WHERE json_extract(gate.value, '$.status') = 'blocked'
                       LIMIT 1
                   ),
                   ''
               ) AS primary_gate,
               CASE
                   WHEN json_extract(payload_json, '$.decision.entry_bid') IS NOT NULL
                    AND json_extract(payload_json, '$.decision.entry_ask') IS NOT NULL
                   THEN 1 ELSE 0
               END AS quote_available,
               json_extract(
                   payload_json,
                   '$.decision.execution.net_expected_r'
               ) AS net_expected_r
        FROM audit_events
        WHERE {" AND ".join(clauses)}
        ORDER BY event_time_ns DESC, event_id DESC
        LIMIT ?
        """,
        values,
    ).fetchall()
    has_more = len(rows) > page_size
    visible = rows[:page_size]
    items: list[dict[str, object]] = [
        {
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "symbol": str(row["symbol"]),
            "timeframe": str(row["timeframe"]),
            "event_time": _iso_ns(int(row["event_time_ns"])),
            "prediction_time": _optional_iso_ns(row["prediction_time_ns"]),
            "available_time": _optional_iso_ns(row["available_time_ns"]),
            "pit_eligible": bool(row["pit_eligible"]),
            "pit_failure_reason": str(row["pit_failure_reason"]),
            "source": str(row["source"]),
            "schema_version": int(row["schema_version"]),
            "direction": str(row["direction"] or ""),
            "analysis_direction": (
                str(row["analysis_direction"]) if row["analysis_direction"] is not None else None
            ),
            "analysis_direction_provenance": str(row["analysis_direction_provenance"]),
            "analysis_score": _optional_float(row["analysis_score"]),
            "composite": _optional_float(row["composite"]),
            "direction_threshold": _optional_float(row["direction_threshold"]),
            "primary_gate": str(row["primary_gate"] or ""),
            "quote_available": bool(row["quote_available"]),
            "net_expected_r": _optional_float(row["net_expected_r"]),
        }
        for row in visible
    ]
    next_cursor = _next_cursor(
        kind="decisions",
        rows=visible,
        has_more=has_more,
        time_field="event_time_ns",
        key_field="event_id",
        max_rowid=max_rowid,
        filter_sha256=filter_hash,
        snapshot_token=snapshot_token,
    )
    return _page_payload(
        kind="decisions",
        items=items,
        limit=page_size,
        next_cursor=next_cursor,
        snapshot_token=snapshot_token,
        filters=filters,
    )


def price_page(
    store: OperationalStore,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, object]:
    """Return a stable newest-first page of compact point-in-time price rows."""

    page_size = _page_size(limit)
    normalized_symbol = _symbol(symbol)
    normalized_timeframe = _timeframe(timeframe)
    filters: dict[str, object] = {
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
    }
    filter_hash = _filter_hash("prices", filters)
    decoded = _decode_cursor(cursor, kind="prices", filter_sha256=filter_hash)
    max_rowid = decoded.max_rowid if decoded is not None else _max_rowid(store, "price_points")
    snapshot_token = (
        decoded.snapshot_token
        if decoded is not None
        else _snapshot_token(store, "prices", max_rowid)
    )
    clauses = ["rowid <= ?"]
    values: list[object] = [max_rowid]
    if normalized_symbol is not None:
        clauses.append("symbol = ?")
        values.append(normalized_symbol)
    if normalized_timeframe is not None:
        clauses.append("timeframe = ?")
        values.append(normalized_timeframe)
    if decoded is not None:
        clauses.append("""
            (event_time_ns < ?
             OR (event_time_ns = ? AND source_record_id < ?))
            """)
        values.extend([decoded.last_time_ns, decoded.last_time_ns, decoded.last_key])
    values.append(page_size + 1)
    rows = store.connection.execute(
        f"""
        SELECT source_record_id, symbol, timeframe, event_time_ns,
               available_time_ns, ingested_time_ns, source, revision_id,
               schema_version, ohlc_scope, open, high, low, close, bid, ask
        FROM price_points
        WHERE {" AND ".join(clauses)}
        ORDER BY event_time_ns DESC, source_record_id DESC
        LIMIT ?
        """,
        values,
    ).fetchall()
    has_more = len(rows) > page_size
    visible = rows[:page_size]
    items: list[dict[str, object]] = [
        {
            "source_record_id": str(row["source_record_id"]),
            "symbol": str(row["symbol"]),
            "timeframe": str(row["timeframe"]),
            "event_time": _iso_ns(int(row["event_time_ns"])),
            "available_time": _iso_ns(int(row["available_time_ns"])),
            "ingested_time": _iso_ns(int(row["ingested_time_ns"])),
            "source": str(row["source"]),
            "revision_id": str(row["revision_id"]),
            "schema_version": int(row["schema_version"]),
            "ohlc_scope": str(row["ohlc_scope"]),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": float(row["close"]),
            "bid": row["bid"],
            "ask": row["ask"],
        }
        for row in visible
    ]
    next_cursor = _next_cursor(
        kind="prices",
        rows=visible,
        has_more=has_more,
        time_field="event_time_ns",
        key_field="source_record_id",
        max_rowid=max_rowid,
        filter_sha256=filter_hash,
        snapshot_token=snapshot_token,
    )
    return _page_payload(
        kind="prices",
        items=items,
        limit=page_size,
        next_cursor=next_cursor,
        snapshot_token=snapshot_token,
        filters=filters,
    )


def read_model_metadata(store: OperationalStore) -> dict[str, object]:
    """Return cheap state needed by a display health/header view."""

    decisions = store.source_cursor("decisions")
    prices = store.source_cursor("prices")
    return {
        "schema_version": int(store.connection.execute("PRAGMA user_version").fetchone()[0]),
        "query_only": bool(store.connection.execute("PRAGMA query_only").fetchone()[0]),
        "rows": {
            "audit_events": _count(store, "audit_events"),
            "predictions": _count(store, "predictions"),
            "price_points": _count(store, "price_points"),
            "ingest_rejections": _count(store, "ingest_rejections"),
        },
        "latest": {
            "decision_event_time": _latest_time(store, "audit_events", "event_time_ns"),
            "price_event_time": _latest_time(store, "price_points", "event_time_ns"),
        },
        "sources": {
            "decisions": _cursor_metadata(decisions),
            "prices": _cursor_metadata(prices),
        },
    }


def canonical_response_bytes(payload: object) -> bytes:
    """Encode a deterministic JSON response suitable for strong ETags."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def response_etag(payload_bytes: bytes) -> str:
    """Return a quoted strong entity tag for exact response bytes."""

    return f'"sha256-{hashlib.sha256(payload_bytes).hexdigest()}"'


def _page_payload(
    *,
    kind: str,
    items: list[dict[str, object]],
    limit: int,
    next_cursor: str | None,
    snapshot_token: str,
    filters: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "snapshot_token": snapshot_token,
        "filters": filters,
        "page": {
            "limit": limit,
            "returned": len(items),
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
        "items": items,
    }


def _next_cursor(
    *,
    kind: str,
    rows: list[Any],
    has_more: bool,
    time_field: str,
    key_field: str,
    max_rowid: int,
    filter_sha256: str,
    snapshot_token: str,
) -> str | None:
    if not has_more or not rows:
        return None
    last = rows[-1]
    return _encode_cursor(
        PageCursor(
            kind=kind,
            max_rowid=max_rowid,
            last_time_ns=int(last[time_field]),
            last_key=str(last[key_field]),
            filter_sha256=filter_sha256,
            snapshot_token=snapshot_token,
        )
    )


def _encode_cursor(cursor: PageCursor) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "k": cursor.kind,
        "m": cursor.max_rowid,
        "t": cursor.last_time_ns,
        "i": cursor.last_key,
        "f": cursor.filter_sha256,
        "s": cursor.snapshot_token,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_CURSOR_HMAC_KEY, raw, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{encoded}.{signature}"


def _decode_cursor(
    value: str | None,
    *,
    kind: str,
    filter_sha256: str,
) -> PageCursor | None:
    if value is None:
        return None
    token = value.strip()
    try:
        encoded, signature = token.rsplit(".", 1)
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        expected = hmac.new(_CURSOR_HMAC_KEY, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ReadModelError("cursor signature mismatch")
        payload: Any = json.loads(raw)
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        if isinstance(error, ReadModelError):
            raise
        raise ReadModelError("invalid pagination cursor") from error
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise ReadModelError("unsupported pagination cursor")
    if payload.get("k") != kind:
        raise ReadModelError("pagination cursor kind mismatch")
    if payload.get("f") != filter_sha256:
        raise ReadModelError("pagination cursor filters changed")
    max_rowid = payload.get("m")
    last_time_ns = payload.get("t")
    last_key = str(payload.get("i") or "")
    snapshot_token = str(payload.get("s") or "")
    if (
        isinstance(max_rowid, bool)
        or not isinstance(max_rowid, int)
        or max_rowid < 0
        or isinstance(last_time_ns, bool)
        or not isinstance(last_time_ns, int)
        or not last_key
        or len(snapshot_token) != 64
    ):
        raise ReadModelError("pagination cursor fields are invalid")
    return PageCursor(
        kind=kind,
        max_rowid=max_rowid,
        last_time_ns=last_time_ns,
        last_key=last_key,
        filter_sha256=filter_sha256,
        snapshot_token=snapshot_token,
    )


def _snapshot_token(store: OperationalStore, source_name: str, max_rowid: int) -> str:
    cursor = store.source_cursor(source_name)
    if cursor is None:
        raise ReadModelError(f"source cursor is unavailable: {source_name}")
    chunks = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM source_chunks WHERE source_name = ?",
            (source_name,),
        ).fetchone()[0]
    )
    rejections = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM ingest_rejections WHERE source_name = ?",
            (source_name,),
        ).fetchone()[0]
    )
    payload = {
        "source_name": source_name,
        "byte_offset": cursor.byte_offset,
        "last_line_sha256": cursor.last_line_sha256,
        "chunks": chunks,
        "rejections": rejections,
        "max_rowid": max_rowid,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _filter_hash(kind: str, filters: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "filters": filters},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _page_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadModelError("limit must be an integer")
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise ReadModelError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _symbol(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ReadModelError("invalid symbol filter")
    return normalized


def _timeframe(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not _TIMEFRAME_PATTERN.fullmatch(normalized):
        raise ReadModelError("invalid timeframe filter")
    return normalized


def _max_rowid(store: OperationalStore, table: str) -> int:
    return int(
        store.connection.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
    )


def _iso_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    base = timestamp.strftime("%Y-%m-%dT%H:%M:%S")
    if nanoseconds:
        return f"{base}.{nanoseconds:09d}Z"
    return f"{base}Z"


def _optional_iso_ns(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadModelError("stored timestamp is not an integer")
    return _iso_ns(value)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _count(store: OperationalStore, table: str) -> int:
    return int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _latest_time(
    store: OperationalStore,
    table: str,
    field: str,
) -> str | None:
    value = store.connection.execute(f"SELECT MAX({field}) FROM {table}").fetchone()[0]
    return _optional_iso_ns(value)


def _cursor_metadata(cursor: Any) -> dict[str, object] | None:
    if cursor is None:
        return None
    return {
        "byte_offset": cursor.byte_offset,
        "source_size_at_sync": cursor.source_size_at_sync,
        "updated_at": _iso_ns(cursor.updated_at_ns),
        "last_line_sha256": cursor.last_line_sha256,
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PageCursor",
    "ReadModelError",
    "canonical_response_bytes",
    "decision_page",
    "price_page",
    "read_model_metadata",
    "response_etag",
]
