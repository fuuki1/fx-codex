"""Disposable, disk-backed quote identities; the append-only log stays authoritative."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path


class DiskIdentitySet:
    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="fx-quote-identities-")
        self._connection = sqlite3.connect(Path(self._directory.name) / "identities.sqlite3")
        # This disposable index is rebuilt from the verified log on every start.
        # These settings never apply to canonical or persistent quote databases.
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA cache_size=-8192")
        self._connection.execute("CREATE TABLE identities (key TEXT PRIMARY KEY) WITHOUT ROWID")
        self._pending = 0

    @staticmethod
    def _key(value: tuple[str, str, str, int | None]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def add(self, value: tuple[str, str, str, int | None]) -> None:
        self._connection.execute("INSERT OR IGNORE INTO identities VALUES (?)", (self._key(value),))
        self._pending += 1
        if self._pending >= 10000:
            self._connection.commit()
            self._pending = 0

    def __contains__(self, value: tuple[str, str, str, int | None]) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM identities WHERE key=?", (self._key(value),)
            ).fetchone()
            is not None
        )

    def close(self) -> None:
        self._connection.close()
        self._directory.cleanup()
