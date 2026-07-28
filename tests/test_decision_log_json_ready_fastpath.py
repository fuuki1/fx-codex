"""`_json_ready` の plain-JSON fast path が変換結果を変えないことの回帰テスト。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json

from fx_intel import decision_log


@dataclass(frozen=True)
class _Sample:
    name: str
    value: float


def test_plain_json_is_returned_unchanged() -> None:
    payload = {
        "symbol": "USDJPY",
        "close": 150.25,
        "count": 3,
        "ok": True,
        "missing": None,
        "flags": ["a", "b"],
        "nested": {"k": [1, 2, {"deep": "v"}]},
    }

    assert decision_log._json_ready(payload) == payload


def test_dataclass_and_datetime_still_convert() -> None:
    stamp = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    converted = decision_log._json_ready(
        {"at": stamp, "sample": _Sample(name="n", value=1.5), "items": (1, 2)}
    )

    assert converted["at"] == stamp.isoformat()
    assert converted["sample"] == {"name": "n", "value": 1.5}
    # Tuples must still become lists so the result is JSON serializable.
    assert converted["items"] == [1, 2]
    json.dumps(converted, allow_nan=False)


def test_non_string_keys_are_coerced_not_fast_pathed() -> None:
    converted = decision_log._json_ready({1: "a", "b": 2})

    assert converted == {"1": "a", "b": 2}
    json.dumps(converted, allow_nan=False)


def test_non_finite_floats_are_still_folded_to_null() -> None:
    """NaN/Infinity are not JSON compliant and must not take the fast path."""

    assert not decision_log._is_plain_json(float("inf"))
    assert not decision_log._is_plain_json(float("nan"))
    assert not decision_log._is_plain_json({"r": float("-inf")})

    converted = decision_log._json_ready({"r": float("inf"), "ok": 1.5})

    assert converted == {"r": None, "ok": 1.5}
    json.dumps(converted, allow_nan=False)


def test_bool_subclass_takes_the_conversion_path() -> None:
    class Weird(int):
        pass

    # An int subclass is not JSON-native for our purposes; it must not be
    # returned as-is just because isinstance(value, int) would be True.
    assert not decision_log._is_plain_json(Weird(3))


def test_nested_non_plain_value_disqualifies_the_container() -> None:
    stamp = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    assert not decision_log._is_plain_json({"a": {"b": [stamp]}})
    converted = decision_log._json_ready({"a": {"b": [stamp]}})
    assert converted == {"a": {"b": [stamp.isoformat()]}}


def test_scoring_does_not_mutate_its_inputs() -> None:
    """The fast path returns shared containers, so callers must not mutate."""

    price_rows = [
        {
            "ts": "2026-07-28T00:00:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 150.0,
            "data_quality_flags": ["ok"],
        }
    ]
    events = [
        {
            "schema": decision_log.SCHEMA_VERSION,
            "decision_id": "d1",
            "ts": "2026-07-28T00:00:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "decision": {"direction": "long", "close": 150.0},
        }
    ]
    snapshot = json.dumps([price_rows, events], sort_keys=True)

    decision_log.score_decision_events(iter(events), price_entries=price_rows)

    assert json.dumps([price_rows, events], sort_keys=True) == snapshot
