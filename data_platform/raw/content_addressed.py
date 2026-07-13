"""Content addressing for raw payloads.

The address of a blob *is* the SHA-256 of its bytes. Two ingests of identical
bytes land at the same address (idempotent), and any change of content changes
the address, so a raw blob can never be edited in place under the same name.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def content_address(data: bytes) -> str:
    """Return the SHA-256 hex digest that addresses ``data``."""

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("content_address requires bytes")
    return hashlib.sha256(data).hexdigest()


def address_path(root: Path, digest: str) -> Path:
    """Map a digest to a sharded path ``root/ab/cdef...`` to avoid huge dirs.

    Fails closed on a malformed digest so a caller cannot escape ``root`` with a
    crafted address (e.g. one containing path separators).
    """

    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError("digest must be a 64-char hex SHA-256")
    lowered = digest.lower()
    return root / lowered[:2] / lowered[2:]
