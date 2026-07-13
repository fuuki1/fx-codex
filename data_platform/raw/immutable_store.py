"""Append-only, content-addressed raw blob store.

Raw bytes are written exactly once at their content address and never modified.
Re-putting identical bytes is a no-op (idempotent retry); putting *different*
bytes that somehow collide on an address would be rejected, but more usefully,
attempting to overwrite an existing address with different content is impossible
because the address is derived from the content itself.

Single-writer discipline is enforced with an exclusive create (``O_EXCL``) plus
an ``fsync`` of the file and its directory, so a crash mid-write cannot leave a
half-written blob that later reads as valid. This is the platform's guarantee
that *"raw data is never overwritten"*.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from data_platform.raw.content_addressed import address_path, content_address


class RawStoreError(RuntimeError):
    """Raised when the immutable store's invariants would be violated."""


@dataclass(frozen=True)
class RawBlobRef:
    """A durable pointer to stored bytes: its address and on-disk location."""

    sha256: str
    path: Path
    size: int


class ImmutableRawStore:
    """Content-addressed store where a blob is written once and never mutated."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, data: bytes) -> RawBlobRef:
        """Store ``data`` idempotently and return its reference.

        If the address already exists, its bytes are verified to match and the
        call is a no-op; a mismatch means on-disk corruption and fails closed.
        """

        if not isinstance(data, (bytes, bytearray)):
            raise RawStoreError("put requires bytes")
        payload = bytes(data)
        digest = content_address(payload)
        destination = address_path(self.root, digest)

        if destination.exists():
            existing = destination.read_bytes()
            if content_address(existing) != digest:
                raise RawStoreError(
                    f"stored blob at {digest} is corrupt; on-disk content no longer matches"
                )
            return RawBlobRef(sha256=digest, path=destination, size=len(existing))

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, fsync, then atomically link
        # into place with O_EXCL so two writers cannot both win.
        fd, tmp_name = tempfile.mkstemp(dir=str(destination.parent), prefix=".ingest-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # link (not replace): fails if another writer already created it.
                os.link(tmp_path, destination)
            except FileExistsError:
                # Lost the race; verify the winner's bytes match and accept them.
                existing = destination.read_bytes()
                if content_address(existing) != digest:
                    raise RawStoreError(
                        f"concurrent write to {digest} produced mismatching content"
                    ) from None
        finally:
            tmp_path.unlink(missing_ok=True)

        self._fsync_dir(destination.parent)
        return RawBlobRef(sha256=digest, path=destination, size=len(payload))

    def get(self, sha256: str) -> bytes:
        """Return the stored bytes for ``sha256``, verifying integrity.

        Raises rather than returning partial/corrupt data — a read that cannot be
        proven intact is an error, not a best-effort value.
        """

        path = address_path(self.root, sha256)
        if not path.exists():
            raise RawStoreError(f"no raw blob stored at {sha256}")
        data = path.read_bytes()
        if content_address(data) != sha256.lower():
            raise RawStoreError(f"stored blob at {sha256} failed integrity check on read")
        return data

    def exists(self, sha256: str) -> bool:
        return address_path(self.root, sha256).exists()

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        # Directory fsync makes the new link durable; skipped where unsupported.
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
