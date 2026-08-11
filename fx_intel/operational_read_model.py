"""Small, immutable projections and keyset pagination for dashboard reads."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
from typing import Any, BinaryIO

from .operational_store import OperationalStore

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_CHART_PRICE_POINTS = 2_100
MAX_CHART_DECISIONS = 500
CHART_STALE_AFTER = timedelta(minutes=15)
_CURSOR_VERSION = 2
_CURSOR_HMAC_KEY = secrets.token_bytes(32)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_./-]{1,32}$")
_TIMEFRAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class ReadModelError(ValueError):
    """A page request or opaque cursor violates the read-model contract."""


class ReadModelStaleError(RuntimeError):
    """Raw evidence has moved beyond the last verified operational snapshot."""


@contextmanager
def current_source_snapshot(store: OperationalStore) -> Iterator[None]:
    """Hold every raw source stable while a read-model response is constructed."""

    cursor_columns = {
        str(row["name"])
        for row in store.connection.execute("PRAGMA table_info(source_cursors)").fetchall()
    }
    if "source_ctime_ns" not in cursor_columns:
        # Schema 4 predates the immutable raw-source envelope. Preserve its
        # historical read behavior; schema 5+ must pass the strict checks below.
        yield
        return
    rows = store.connection.execute("""
        SELECT source_name, source_path, device_id, inode, byte_offset,
               last_line_start_offset, last_line_sha256,
               source_size_at_sync, source_mtime_ns, source_ctime_ns
        FROM source_cursors
        ORDER BY source_name
        """).fetchall()
    names = {str(row["source_name"]) for row in rows}
    if not {"decisions", "prices"}.issubset(names):
        raise ReadModelStaleError("required source cursors are not bootstrapped")
    ordered_rows = sorted(
        rows,
        key=lambda row: str(Path(str(row["source_path"])).resolve()),
    )
    resolved_paths = [Path(str(row["source_path"])).resolve() for row in ordered_rows]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ReadModelStaleError("multiple source cursors reference the same raw source")

    locked_handles: list[tuple[Any, BinaryIO]] = []
    try:
        for row, path in zip(ordered_rows, resolved_paths, strict=True):
            try:
                opened_handle = path.open("rb")
            except OSError as error:
                raise ReadModelStaleError(
                    f"raw source is unavailable: {row['source_name']}"
                ) from error
            try:
                fcntl.flock(opened_handle.fileno(), fcntl.LOCK_SH)
            except BaseException:
                opened_handle.close()
                raise
            locked_handles.append((row, opened_handle))

        for row, locked_handle in locked_handles:
            observed = os.fstat(locked_handle.fileno())
            expected_identity = (int(row["device_id"]), int(row["inode"]))
            if (observed.st_dev, observed.st_ino) != expected_identity:
                raise ReadModelStaleError(f"raw source identity changed: {row['source_name']}")
            if observed.st_size != int(row["source_size_at_sync"]) or observed.st_mtime_ns != int(
                row["source_mtime_ns"]
            ):
                raise ReadModelStaleError(
                    f"raw source advanced beyond read model: {row['source_name']}"
                )
            if observed.st_ctime_ns != int(row["source_ctime_ns"]):
                raise ReadModelStaleError(
                    f"raw source metadata changed beyond read model: {row['source_name']}"
                )
            byte_offset = int(row["byte_offset"])
            if byte_offset:
                line_start = int(row["last_line_start_offset"])
                if line_start < 0 or line_start >= byte_offset:
                    raise ReadModelStaleError(
                        f"raw source boundary is invalid: {row['source_name']}"
                    )
                locked_handle.seek(line_start)
                line = locked_handle.read(byte_offset - line_start)
                if not line.endswith(b"\n") or hashlib.sha256(line).hexdigest() != str(
                    row["last_line_sha256"]
                ):
                    raise ReadModelStaleError(f"raw source boundary changed: {row['source_name']}")
            after = os.fstat(locked_handle.fileno())
            if (
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_dev,
                observed.st_ino,
            ) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            ):
                raise ReadModelStaleError(f"raw source changed during read: {row['source_name']}")

        yield
    finally:
        for _row, release_handle in reversed(locked_handles):
            try:
                fcntl.flock(release_handle.fileno(), fcntl.LOCK_UN)
            finally:
                release_handle.close()


def require_current_source_snapshot(store: OperationalStore) -> None:
    """Fail closed unless every raw source still equals its verified cursor state."""

    with current_source_snapshot(store):
        pass


@dataclass(frozen=True)
class PageCursor:
    kind: str
    max_rowid: int
    last_time_ns: int
    last_key: str
    filter_sha256: str
    snapshot_token: str


def chart_snapshot(
    store: OperationalStore,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    as_of: datetime,
    price_limit: int = MAX_CHART_PRICE_POINTS,
    decision_limit: int = MAX_CHART_DECISIONS,
) -> dict[str, object]:
    """Return one bounded PIT-consistent price and prediction chart snapshot."""

    normalized_symbol = _symbol(symbol)
    normalized_timeframe = _timeframe(timeframe)
    if normalized_symbol is None or normalized_timeframe is None:
        raise ReadModelError("chart symbol and timeframe are required")
    start_ns = _aware_ns(start, "start")
    end_ns = _aware_ns(end, "end")
    as_of_ns = _aware_ns(as_of, "as_of")
    if end_ns < start_ns:
        raise ReadModelError("chart end must not precede start")
    if end_ns > as_of_ns:
        raise ReadModelError("chart end must not exceed as_of")
    if end_ns - start_ns > int(timedelta(days=7).total_seconds() * 1_000_000_000):
        raise ReadModelError("chart range must not exceed 7 days")
    _bounded_limit(price_limit, MAX_CHART_PRICE_POINTS, "price_limit")
    _bounded_limit(decision_limit, MAX_CHART_DECISIONS, "decision_limit")

    connection = store.connection
    connection.execute("BEGIN")
    try:
        max_price_rowid = _max_rowid(store, "price_points")
        max_prediction_rowid = _max_rowid(store, "predictions")
        max_outcome_rowid = _max_rowid(store, "outcomes")
        max_audit_rowid = _max_rowid(store, "audit_events")
        price_rows = connection.execute(
            """
            SELECT source_record_id, event_time_ns, available_time_ns,
                   source, schema_version, ohlc_scope,
                   open, high, low, close, bid, ask
            FROM price_points
            WHERE rowid <= ?
              AND symbol = ?
              AND timeframe = ?
              AND event_time_ns BETWEEN ? AND ?
              AND available_time_ns <= ?
            ORDER BY event_time_ns, source_record_id
            LIMIT ?
            """,
            (
                max_price_rowid,
                normalized_symbol,
                normalized_timeframe,
                start_ns,
                end_ns,
                as_of_ns,
                price_limit + 1,
            ),
        ).fetchall()
        if len(price_rows) > price_limit:
            raise ReadModelError("chart price window exceeds the bounded point limit")

        decisions_total = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM predictions AS p
                WHERE p.rowid <= ?
                  AND p.symbol = ?
                  AND p.timeframe = ?
                  AND p.prediction_time_ns BETWEEN ? AND ?
                  AND p.available_time_ns <= ?
                """,
                (
                    max_prediction_rowid,
                    normalized_symbol,
                    normalized_timeframe,
                    start_ns,
                    end_ns,
                    as_of_ns,
                ),
            ).fetchone()[0]
        )
        # Keep evenly spaced decisions instead of failing the whole chart. The
        # stride is applied inside SQLite so the expensive json_extract
        # projection below only runs for rows that survive sampling.
        decision_stride = max(1, -(-decisions_total // decision_limit))
        prediction_rows = connection.execute(
            """
            WITH window_rows AS (
                SELECT p.rowid AS rid,
                       ROW_NUMBER() OVER (
                           ORDER BY p.prediction_time_ns, p.prediction_id
                       ) AS rn
                FROM predictions AS p
                WHERE p.rowid <= ?
                  AND p.symbol = ?
                  AND p.timeframe = ?
                  AND p.prediction_time_ns BETWEEN ? AND ?
                  AND p.available_time_ns <= ?
            ),
            sampled AS (
                SELECT rid
                FROM window_rows
                WHERE (rn - 1) % ? = 0
                   OR rn = (SELECT MAX(rn) FROM window_rows)
            )
            SELECT p.prediction_id, p.decision_id, p.prediction_time_ns,
                   p.available_time_ns, p.due_time_ns,
                   CASE
                       WHEN o.status IS NULL THEN 'pending'
                       ELSE o.status
                   END AS status,
                   p.source,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.direction'),
                       json_extract(p.payload_json, '$.direction'),
                       ''
                   ) AS direction,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.analysis_direction'),
                       json_extract(p.payload_json, '$.analysis_direction')
                   ) AS analysis_direction,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.analysis_score'),
                       json_extract(p.payload_json, '$.analysis_score'),
                       json_extract(p.payload_json, '$.decision.composite'),
                       json_extract(p.payload_json, '$.composite')
                   ) AS analysis_score,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.analysis_conviction'),
                       json_extract(p.payload_json, '$.analysis_conviction')
                   ) AS analysis_conviction,
                   COALESCE(
                       (
                           SELECT json_extract(shadow.value, '$.direction')
                           FROM json_each(
                               p.payload_json,
                               '$.decision.shadow_predictions'
                           ) AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       ),
                       (
                           SELECT json_extract(shadow.value, '$.direction')
                           FROM json_each(p.payload_json, '$.shadow_predictions') AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       )
                   ) AS moderate_shadow_direction,
                   COALESCE(
                       (
                           SELECT json_extract(shadow.value, '$.score')
                           FROM json_each(
                               p.payload_json,
                               '$.decision.shadow_predictions'
                           ) AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       ),
                       (
                           SELECT json_extract(shadow.value, '$.score')
                           FROM json_each(p.payload_json, '$.shadow_predictions') AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       )
                   ) AS moderate_shadow_score,
                   COALESCE(
                       (
                           SELECT json_extract(shadow.value, '$.decision_threshold')
                           FROM json_each(
                               p.payload_json,
                               '$.decision.shadow_predictions'
                           ) AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       ),
                       (
                           SELECT json_extract(shadow.value, '$.decision_threshold')
                           FROM json_each(p.payload_json, '$.shadow_predictions') AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       )
                   ) AS moderate_shadow_threshold,
                   COALESCE(
                       (
                           SELECT json_extract(shadow.value, '$.policy_id')
                           FROM json_each(
                               p.payload_json,
                               '$.decision.shadow_predictions'
                           ) AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       ),
                       (
                           SELECT json_extract(shadow.value, '$.policy_id')
                           FROM json_each(p.payload_json, '$.shadow_predictions') AS shadow
                           WHERE json_extract(shadow.value, '$.producer')
                               = 'moderate_aggressive_policy'
                           LIMIT 1
                       )
                   ) AS moderate_shadow_policy_id,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.close'),
                       json_extract(p.payload_json, '$.close'),
                       json_extract(p.payload_json, '$.decision.shadow_predictions[0].close'),
                       json_extract(p.payload_json, '$.shadow_predictions[0].close')
                   ) AS prediction_close,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.stop'),
                       json_extract(p.payload_json, '$.stop'),
                       json_extract(p.payload_json, '$.decision.shadow_predictions[0].stop'),
                       json_extract(p.payload_json, '$.shadow_predictions[0].stop')
                   ) AS stop,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.target1'),
                       json_extract(p.payload_json, '$.target1'),
                       json_extract(p.payload_json, '$.decision.shadow_predictions[0].target1'),
                       json_extract(p.payload_json, '$.shadow_predictions[0].target1')
                   ) AS target1,
                   COALESCE(
                       json_extract(p.payload_json, '$.decision.target2'),
                       json_extract(p.payload_json, '$.target2'),
                       json_extract(p.payload_json, '$.decision.shadow_predictions[0].target2'),
                       json_extract(p.payload_json, '$.shadow_predictions[0].target2')
                   ) AS target2,
                   COALESCE(
                       json_extract(
                           p.payload_json,
                           '$.decision.shadow_predictions[0].stage_at_prediction'
                       ),
                       json_extract(
                           p.payload_json,
                           '$.shadow_predictions[0].stage_at_prediction'
                       ),
                       'shadow'
                   ) AS stage_at_prediction,
                   COALESCE(
                       (
                           SELECT json_extract(gate.value, '$.gate')
                           FROM json_each(
                               p.payload_json,
                               '$.decision.gate_trace'
                           ) AS gate
                           WHERE json_extract(gate.value, '$.status') = 'blocked'
                           LIMIT 1
                       ),
                       (
                           SELECT json_extract(gate.value, '$.gate')
                           FROM json_each(p.payload_json, '$.gate_trace') AS gate
                           WHERE json_extract(gate.value, '$.status') = 'blocked'
                           LIMIT 1
                       ),
                       ''
                   ) AS primary_gate,
                   o.status AS outcome_status,
                   o.realized_net_r,
                   o.label_version
            FROM predictions AS p
            JOIN sampled ON sampled.rid = p.rowid
            LEFT JOIN outcomes AS o
              ON o.rowid = (
                  SELECT latest.rowid
                  FROM outcomes AS latest
                  WHERE latest.prediction_id = p.prediction_id
                    AND latest.rowid <= ?
                    AND latest.scored_time_ns <= ?
                  ORDER BY latest.scored_time_ns DESC, latest.label_version DESC
                  LIMIT 1
              )
            ORDER BY p.prediction_time_ns, p.prediction_id
            LIMIT ?
            """,
            (
                max_prediction_rowid,
                normalized_symbol,
                normalized_timeframe,
                start_ns,
                end_ns,
                as_of_ns,
                decision_stride,
                max_outcome_rowid,
                as_of_ns,
                decision_limit,
            ),
        ).fetchall()

        pit_excluded = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM audit_events
                WHERE rowid <= ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND event_time_ns BETWEEN ? AND ?
                  AND ingested_time_ns <= ?
                  AND pit_eligible = 0
                """,
                (
                    max_audit_rowid,
                    normalized_symbol,
                    normalized_timeframe,
                    start_ns,
                    end_ns,
                    as_of_ns,
                ),
            ).fetchone()[0]
        )
        prices_token = _snapshot_token(store, "prices", max_price_rowid)
        decisions_token = _snapshot_token(store, "decisions", max_prediction_rowid)
    finally:
        connection.execute("ROLLBACK")

    prices = [
        {
            "source_record_id": str(row["source_record_id"]),
            "event_time": _iso_ns(int(row["event_time_ns"])),
            "available_time": _iso_ns(int(row["available_time_ns"])),
            "source": str(row["source"]),
            "schema_version": int(row["schema_version"]),
            "ohlc_scope": str(row["ohlc_scope"]),
            "open": _optional_float(row["open"]),
            "high": _optional_float(row["high"]),
            "low": _optional_float(row["low"]),
            "close": float(row["close"]),
            "bid": _optional_float(row["bid"]),
            "ask": _optional_float(row["ask"]),
        }
        for row in price_rows
    ]
    decisions = [
        {
            "prediction_id": str(row["prediction_id"]),
            "decision_id": str(row["decision_id"]),
            "prediction_time": _iso_ns(int(row["prediction_time_ns"])),
            "available_time": _iso_ns(int(row["available_time_ns"])),
            "due_time": _iso_ns(int(row["due_time_ns"])),
            "status": str(row["status"]),
            "source": str(row["source"]),
            "direction": str(row["direction"] or ""),
            "analysis_direction": (
                str(row["analysis_direction"]) if row["analysis_direction"] is not None else None
            ),
            "analysis_score": _optional_float(row["analysis_score"]),
            "analysis_conviction": _optional_float(row["analysis_conviction"]),
            "moderate_shadow_direction": (
                str(row["moderate_shadow_direction"])
                if row["moderate_shadow_direction"] is not None
                else None
            ),
            "moderate_shadow_score": _optional_float(row["moderate_shadow_score"]),
            "moderate_shadow_threshold": _optional_float(row["moderate_shadow_threshold"]),
            "moderate_shadow_policy_id": (
                str(row["moderate_shadow_policy_id"])
                if row["moderate_shadow_policy_id"] is not None
                else None
            ),
            "prediction_close": _optional_float(row["prediction_close"]),
            "stop": _optional_float(row["stop"]),
            "target1": _optional_float(row["target1"]),
            "target2": _optional_float(row["target2"]),
            "stage_at_prediction": str(row["stage_at_prediction"] or "shadow"),
            "primary_gate": str(row["primary_gate"] or ""),
            "pit_eligible": True,
            "outcome_status": (
                str(row["outcome_status"]) if row["outcome_status"] is not None else "pending"
            ),
            "realized_net_r": _optional_float(row["realized_net_r"]),
            "label_version": (
                str(row["label_version"]) if row["label_version"] is not None else None
            ),
        }
        for row in prediction_rows
    ]
    scope_counts: dict[str, int] = {}
    bid_ask_rows = 0
    completed_rows = 0
    for row in prices:
        scope = str(row["ohlc_scope"])
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        if row["bid"] is not None and row["ask"] is not None:
            bid_ask_rows += 1
        if (
            scope == "completed_bid_ask_bar"
            and row["open"] is not None
            and row["high"] is not None
            and row["low"] is not None
        ):
            completed_rows += 1
    latest_available = max(
        (int(row["available_time_ns"]) for row in price_rows),
        default=None,
    )
    stale = latest_available is None or as_of_ns - latest_available > int(
        CHART_STALE_AFTER.total_seconds() * 1_000_000_000
    )
    snapshot_token = hashlib.sha256(
        f"{prices_token}:{decisions_token}:{normalized_symbol}:{normalized_timeframe}:"
        f"{max_outcome_rowid}:{max_audit_rowid}:{start_ns}:{end_ns}:{as_of_ns}".encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "chart",
        "generated_at": _iso_ns(as_of_ns),
        "as_of": _iso_ns(as_of_ns),
        "snapshot_token": snapshot_token,
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "window": {"start": _iso_ns(start_ns), "end": _iso_ns(end_ns)},
        "price_mode": (
            "completed_candles" if prices and completed_rows == len(prices) else "snapshot_line"
        ),
        "quality": {
            "status": "stale" if stale else "ok",
            "latest_available_time": (
                _iso_ns(latest_available) if latest_available is not None else None
            ),
            "ohlc_scope_counts": scope_counts,
            "bid_ask_coverage": bid_ask_rows / len(prices) if prices else 0.0,
            "pit_excluded": pit_excluded,
            "decisions_total": decisions_total,
            "decisions_returned": len(decisions),
            "decision_stride": decision_stride,
            "decisions_sampled": decision_stride > 1,
        },
        "prices": prices,
        "decisions": decisions,
    }


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


def _bounded_limit(value: int, maximum: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadModelError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ReadModelError(f"{name} must be between 1 and {maximum}")


def _aware_ns(value: datetime, name: str) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReadModelError(f"{name} must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


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
    "CHART_STALE_AFTER",
    "DEFAULT_PAGE_SIZE",
    "MAX_CHART_DECISIONS",
    "MAX_CHART_PRICE_POINTS",
    "MAX_PAGE_SIZE",
    "PageCursor",
    "ReadModelError",
    "ReadModelStaleError",
    "canonical_response_bytes",
    "chart_snapshot",
    "current_source_snapshot",
    "decision_page",
    "price_page",
    "read_model_metadata",
    "require_current_source_snapshot",
    "response_etag",
]
