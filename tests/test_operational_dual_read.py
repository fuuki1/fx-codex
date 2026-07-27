from __future__ import annotations

from fx_intel import operational_dual_read as dual


def _dashboard_row(
    *,
    timestamp: str = "2026-07-27T04:15:06.025246+00:00",
    direction: str = "neutral",
    gate: str = "expectancy_guard",
) -> dict[str, object]:
    return {
        "ts": timestamp,
        "symbol": "USDJPY",
        "timeframe": "4h",
        "direction": direction,
        "analysis_direction": "long",
        "analysis_score": 0.285,
        "blocked_gate": {"gate": gate},
        "pit_eligible": False,
    }


def _read_row(
    *,
    timestamp: str = "2026-07-27T04:15:06.025246000Z",
    direction: str = "neutral",
    gate: str = "expectancy_guard",
) -> dict[str, object]:
    return {
        "event_time": timestamp,
        "symbol": "USDJPY",
        "timeframe": "4h",
        "direction": direction,
        "analysis_direction": "long",
        "analysis_score": 0.285,
        "primary_gate": gate,
        "pit_eligible": False,
    }


def _state(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "evaluation": {
            "recent_decisions_by_timeframe": {
                "4h": rows,
            }
        }
    }


def test_dual_read_normalizes_utc_format_and_verifies_parity() -> None:
    report = dual.compare_dual_read(
        _state([_dashboard_row()]),
        {"4h": [_read_row()]},
        symbol="USDJPY",
        timeframes=["4h"],
    )

    assert report["verdict"] == "parity_verified"
    assert report["totals"]["dashboard_rows"] == 1


def test_dual_read_preserves_duplicate_signature_counts() -> None:
    report = dual.compare_dual_read(
        _state([_dashboard_row(), _dashboard_row()]),
        {"4h": [_read_row()]},
        symbol="USDJPY",
        timeframes=["4h"],
    )

    timeframe = report["timeframes"]["4h"]
    assert report["verdict"] == "drift_detected"
    assert timeframe["dashboard_duplicate_signatures"] == 1
    assert timeframe["missing_from_read"] == 1


def test_dual_read_reports_gate_drift() -> None:
    report = dual.compare_dual_read(
        _state([_dashboard_row()]),
        {"4h": [_read_row(gate="below_production_threshold")]},
        symbol="USDJPY",
        timeframes=["4h"],
    )

    timeframe = report["timeframes"]["4h"]
    assert timeframe["verdict"] == "drift_detected"
    assert timeframe["missing_from_read"] == 1
    assert timeframe["missing_from_dashboard"] == 1


def test_dashboard_rows_filters_symbol_and_sorts_newest_first() -> None:
    eur = _dashboard_row()
    eur["symbol"] = "EURUSD"
    older = _dashboard_row(timestamp="2026-07-27T04:10:00Z")
    newest = _dashboard_row(timestamp="2026-07-27T04:20:00Z")

    rows = dual.dashboard_rows(
        _state([older, eur, newest]),
        symbol="USDJPY",
        timeframe="4h",
    )

    assert [row["ts"] for row in rows] == [
        "2026-07-27T04:20:00Z",
        "2026-07-27T04:10:00Z",
    ]
