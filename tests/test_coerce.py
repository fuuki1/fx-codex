"""Contract tests for fx_intel.coerce.

These lock in the intentional edge-case behavior that the notice/learning/trade
decoders now rely on: bool is not a number, non-finite floats are dropped, and
numeric strings are parsed leniently.
"""

from __future__ import annotations

import math

import pytest

from fx_intel.coerce import (
    float_field,
    float_field_or_none,
    int_field,
    to_float,
    to_float_or_none,
    to_int,
    to_int_or_none,
)


class TestToInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (5, 5),
            (-3, -3),
            (3.9, 3),  # truncates toward zero like int()
            (-3.9, -3),
            ("42", 42),
            ("  42  ", 42),
            ("42.0", 42),  # numeric string that bare int() would reject
            ("-7", -7),
        ],
    )
    def test_representable(self, value: object, expected: int) -> None:
        assert to_int(value) == expected
        assert to_int_or_none(value) == expected

    @pytest.mark.parametrize("value", [None, "abc", "", "  ", object(), [1], {}])
    def test_not_representable_uses_default(self, value: object) -> None:
        assert to_int(value) == 0
        assert to_int(value, 9) == 9
        assert to_int_or_none(value) is None

    def test_bool_is_not_an_int(self) -> None:
        # A bool in a numeric slot is a data bug, not 0/1.
        assert to_int_or_none(True) is None
        assert to_int_or_none(False) is None
        assert to_int(True, 5) == 5

    def test_non_finite_float_is_dropped(self) -> None:
        assert to_int_or_none(float("nan")) is None
        assert to_int_or_none(float("inf")) is None
        assert to_int(float("nan"), 3) == 3


class TestToFloat:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1, 1.0),
            (1.5, 1.5),
            ("1.5", 1.5),
            ("  -2.25 ", -2.25),
            (0, 0.0),
        ],
    )
    def test_representable(self, value: object, expected: float) -> None:
        assert to_float(value) == expected
        assert to_float_or_none(value) == expected

    @pytest.mark.parametrize("value", [None, "abc", "", object(), [1.0]])
    def test_not_representable_uses_default(self, value: object) -> None:
        assert to_float(value) == 0.0
        assert to_float(value, 1.0) == 1.0
        assert to_float_or_none(value) is None

    def test_bool_is_not_a_float(self) -> None:
        assert to_float_or_none(True) is None
        assert to_float(True, 2.0) == 2.0

    def test_non_finite_is_dropped(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
            assert to_float_or_none(bad) is None
            assert to_float(bad, 7.0) == 7.0


class TestFieldHelpers:
    def test_int_field_reads_key_with_default(self) -> None:
        mapping: dict[str, object] = {"total": "12", "bad": None}
        assert int_field(mapping, "total") == 12
        assert int_field(mapping, "bad") == 0
        assert int_field(mapping, "missing", 4) == 4

    def test_float_field_reads_key_with_default(self) -> None:
        mapping: dict[str, object] = {"factor": "0.5", "bad": "x"}
        assert float_field(mapping, "factor") == 0.5
        assert float_field(mapping, "bad", 1.0) == 1.0
        assert float_field(mapping, "missing", 1.0) == 1.0

    def test_float_field_or_none(self) -> None:
        mapping: dict[str, object] = {"val": 2.0, "bad": "x"}
        assert float_field_or_none(mapping, "val") == 2.0
        assert float_field_or_none(mapping, "bad") is None
        assert float_field_or_none(mapping, "missing") is None

    def test_finite_guard_holds_end_to_end(self) -> None:
        mapping: dict[str, object] = {"x": math.inf}
        assert float_field(mapping, "x", 0.0) == 0.0
        assert float_field_or_none(mapping, "x") is None
