"""Compare active dashboard decision rows with the independent read API."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, UTC
import json
import math
from typing import Any


class DualReadError(ValueError):
    """Raised when either reader does not satisfy the comparison contract."""


def dashboard_rows(
    state: Mapping[str, object],
    *,
    symbol: str,
    timeframe: str,
) -> list[dict[str, object]]:
    """Extract newest-first rows from the active monolithic dashboard state."""
    evaluation = _mapping(state.get("evaluation"), "dashboard evaluation")
    recent = _mapping(
        evaluation.get("recent_decisions_by_timeframe"),
        "dashboard recent_decisions_by_timeframe",
    )
    raw_rows = recent.get(timeframe, [])
    if not isinstance(raw_rows, list):
        raise DualReadError(f"dashboard timeframe is not a list: {timeframe}")
    rows = [
        dict(row)
        for row in raw_rows
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol
    ]
    rows.sort(key=lambda row: _timestamp_key(row.get("ts")), reverse=True)
    return rows


def compare_dual_read(
    dashboard_state: Mapping[str, object],
    read_rows: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    symbol: str,
    timeframes: Sequence[str],
) -> dict[str, object]:
    """Compare compact signatures and preserve duplicate occurrence counts."""
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise DualReadError("symbol is required")
    reports: dict[str, object] = {}
    total_dashboard = 0
    total_read = 0
    total_missing_from_read = 0
    total_missing_from_dashboard = 0
    total_analysis_excluded = 0
    for timeframe in timeframes:
        active = dashboard_rows(
            dashboard_state,
            symbol=normalized_symbol,
            timeframe=timeframe,
        )
        independent = [dict(row) for row in read_rows.get(timeframe, ())]
        independent.sort(
            key=lambda row: _timestamp_key(row.get("event_time")),
            reverse=True,
        )
        expected_count = len(active)
        independent = independent[:expected_count]
        analysis_comparable = bool(independent) and all(
            row.get("analysis_direction_provenance") == "explicit" for row in independent
        )
        active_counter = Counter(
            _dashboard_signature(
                row,
                include_analysis_direction=analysis_comparable,
            )
            for row in active
        )
        read_counter = Counter(
            _read_signature(
                row,
                include_analysis_direction=analysis_comparable,
            )
            for row in independent
        )
        missing_from_read = active_counter - read_counter
        missing_from_dashboard = read_counter - active_counter
        reports[timeframe] = {
            "dashboard_rows": len(active),
            "read_rows": len(independent),
            "dashboard_duplicate_signatures": _duplicates(active_counter),
            "read_duplicate_signatures": _duplicates(read_counter),
            "analysis_direction_comparable": analysis_comparable,
            "analysis_direction_excluded": (0 if analysis_comparable else len(independent)),
            "missing_from_read": sum(missing_from_read.values()),
            "missing_from_dashboard": sum(missing_from_dashboard.values()),
            "missing_from_read_examples": _examples(missing_from_read),
            "missing_from_dashboard_examples": _examples(missing_from_dashboard),
            "verdict": (
                "parity_verified"
                if not missing_from_read and not missing_from_dashboard
                else "drift_detected"
            ),
        }
        total_dashboard += len(active)
        total_read += len(independent)
        total_missing_from_read += sum(missing_from_read.values())
        total_missing_from_dashboard += sum(missing_from_dashboard.values())
        total_analysis_excluded += 0 if analysis_comparable else len(independent)
    if total_dashboard == 0 or total_missing_from_read or total_missing_from_dashboard:
        verdict = "drift_detected"
    elif total_analysis_excluded:
        verdict = "parity_verified_with_exclusions"
    else:
        verdict = "parity_verified"
    return {
        "symbol": normalized_symbol,
        "timeframes": reports,
        "totals": {
            "dashboard_rows": total_dashboard,
            "read_rows": total_read,
            "missing_from_read": total_missing_from_read,
            "missing_from_dashboard": total_missing_from_dashboard,
            "analysis_direction_excluded": total_analysis_excluded,
        },
        "verdict": verdict,
    }


def _dashboard_signature(
    row: Mapping[str, object],
    *,
    include_analysis_direction: bool,
) -> str:
    blocked = row.get("blocked_gate")
    gate = blocked.get("gate") if isinstance(blocked, dict) else ""
    return _signature(
        event_time=row.get("ts"),
        symbol=row.get("symbol"),
        timeframe=row.get("timeframe"),
        direction=row.get("direction"),
        analysis_direction=(row.get("analysis_direction") if include_analysis_direction else None),
        analysis_score=row.get("analysis_score"),
        primary_gate=gate,
        pit_eligible=row.get("pit_eligible"),
    )


def _read_signature(
    row: Mapping[str, object],
    *,
    include_analysis_direction: bool,
) -> str:
    return _signature(
        event_time=row.get("event_time"),
        symbol=row.get("symbol"),
        timeframe=row.get("timeframe"),
        direction=row.get("direction"),
        analysis_direction=(row.get("analysis_direction") if include_analysis_direction else None),
        analysis_score=row.get("analysis_score"),
        primary_gate=row.get("primary_gate"),
        pit_eligible=row.get("pit_eligible"),
    )


def _signature(
    *,
    event_time: object,
    symbol: object,
    timeframe: object,
    direction: object,
    analysis_direction: object,
    analysis_score: object,
    primary_gate: object,
    pit_eligible: object,
) -> str:
    payload = {
        "event_time_us": _timestamp_key(event_time),
        "symbol": str(symbol or "").upper(),
        "timeframe": str(timeframe or ""),
        "direction": str(direction or ""),
        "analysis_direction": str(analysis_direction or ""),
        "analysis_score": _number(analysis_score),
        "primary_gate": str(primary_gate or ""),
        "pit_eligible": bool(pit_eligible),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _timestamp_key(value: object) -> int:
    if not isinstance(value, str) or not value.strip():
        raise DualReadError("decision timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DualReadError(f"invalid decision timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DualReadError("decision timestamp must be timezone-aware")
    delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        raise DualReadError("analysis score is not numeric")
    try:
        converted = float(value)
    except ValueError as error:
        raise DualReadError("analysis score is not numeric") from error
    if not math.isfinite(converted):
        raise DualReadError("analysis score must be finite")
    return round(converted, 12)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DualReadError(f"{label} is not an object")
    return value


def _duplicates(counter: Counter[str]) -> int:
    return sum(count - 1 for count in counter.values() if count > 1)


def _examples(counter: Counter[str], limit: int = 5) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    for signature, count in counter.most_common(limit):
        examples.append({"count": count, "signature": json.loads(signature)})
    return examples


__all__ = [
    "DualReadError",
    "compare_dual_read",
    "dashboard_rows",
]
