"""Immutable content-addressed raw store: idempotency, no-overwrite, integrity."""

from __future__ import annotations

from pathlib import Path

import pytest

from data_platform.raw.content_addressed import address_path, content_address
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError


class TestContentAddressed:
    def test_address_is_sha256(self) -> None:
        assert content_address(b"hello") == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_address_path_rejects_malformed_digest(self) -> None:
        with pytest.raises(ValueError):
            address_path(Path("/tmp"), "../escape")


class TestImmutableRawStore:
    def test_put_then_get_roundtrip(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        ref = store.put(b"raw-bytes")
        assert store.get(ref.sha256) == b"raw-bytes"
        assert ref.size == len(b"raw-bytes")

    def test_put_is_idempotent(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        first = store.put(b"same")
        second = store.put(b"same")
        assert first.sha256 == second.sha256
        assert first.path == second.path

    def test_different_content_lands_at_different_address(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        a = store.put(b"one")
        b = store.put(b"two")
        assert a.sha256 != b.sha256

    def test_overwrite_is_impossible_content_defines_address(self, tmp_path: Path) -> None:
        # There is no API to overwrite; editing bytes changes the address, so the
        # original blob is untouched and both coexist.
        store = ImmutableRawStore(tmp_path)
        original = store.put(b"v1")
        edited = store.put(b"v2")
        assert store.get(original.sha256) == b"v1"
        assert store.get(edited.sha256) == b"v2"

    def test_corrupted_blob_fails_closed_on_read(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        ref = store.put(b"intact")
        ref.path.write_bytes(b"tampered")  # simulate on-disk corruption
        with pytest.raises(RawStoreError, match="integrity check"):
            store.get(ref.sha256)

    def test_get_missing_fails(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        with pytest.raises(RawStoreError, match="no raw blob"):
            store.get("f" * 64)

    def test_put_requires_bytes(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path)
        with pytest.raises(RawStoreError):
            store.put("a string")  # type: ignore[arg-type]
