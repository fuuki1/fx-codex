"""Type-safe coercion helpers for untyped mapping payloads.

Journals, learning profiles and notice profiles are persisted as JSON and read
back as ``Mapping[str, object]``.  Passing an ``object`` straight into ``int()``
or ``float()`` is rejected by mypy (``int``/``float`` have no overload for
``object``) and is *also* genuinely unsafe: ``int("abc")`` raises, ``int(None)``
raises, and ``int(True)`` silently coerces a bool.

These helpers narrow the runtime type explicitly before constructing the number,
so both the type checker and the runtime agree on the contract.  They are the
single place other modules should go through when decoding numeric fields from
untrusted/untyped payloads, replacing the near-identical ``_int_value`` /
``_stat_int`` / ``_int_from_mapping`` variants that previously lived in each
module.

Design choices (kept deliberate and documented so callers can rely on them):

* ``bool`` is **not** treated as a number.  ``True``/``False`` decode to the
  supplied default, never to ``1``/``0`` — a boolean in a numeric slot is a data
  bug, not an integer.
* Strings are parsed leniently (``" 42 "`` -> ``42``) so hand-edited JSON keeps
  working, but non-numeric strings fall back to the default instead of raising.
* Non-finite floats (``nan``/``inf``) decode to ``None`` in the ``*_or_none``
  variants, matching the existing ``_float`` guards in the codebase.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

__all__ = [
    "to_int",
    "to_int_or_none",
    "to_float",
    "to_float_or_none",
    "int_field",
    "float_field",
    "float_field_or_none",
]


def to_int_or_none(value: object) -> int | None:
    """Coerce an untyped value to ``int``, or ``None`` when not representable.

    ``bool`` is rejected on purpose (see module docstring).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            # Accept "42.0"-style strings that int() alone would reject.
            try:
                parsed = float(text)
            except ValueError:
                return None
            return int(parsed) if math.isfinite(parsed) else None
    return None


def to_int(value: object, default: int = 0) -> int:
    """Coerce an untyped value to ``int``, falling back to ``default``."""
    result = to_int_or_none(value)
    return default if result is None else result


def to_float_or_none(value: object) -> float | None:
    """Coerce an untyped value to a finite ``float``, or ``None``.

    ``bool`` is rejected; ``nan``/``inf`` decode to ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            return None
        return numeric if math.isfinite(numeric) else None
    return None


def to_float(value: object, default: float = 0.0) -> float:
    """Coerce an untyped value to a finite ``float``, falling back to ``default``."""
    result = to_float_or_none(value)
    return default if result is None else result


def int_field(mapping: Mapping[str, object], key: str, default: int = 0) -> int:
    """Read ``key`` from an untyped mapping as ``int`` with a default."""
    return to_int(mapping.get(key, default), default)


def float_field(mapping: Mapping[str, object], key: str, default: float = 0.0) -> float:
    """Read ``key`` from an untyped mapping as ``float`` with a default."""
    return to_float(mapping.get(key, default), default)


def float_field_or_none(mapping: Mapping[str, object], key: str) -> float | None:
    """Read ``key`` from an untyped mapping as ``float`` or ``None``."""
    return to_float_or_none(mapping.get(key))
