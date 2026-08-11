"""Hardened parser for the private read-only collector environment file."""

from __future__ import annotations

from collections.abc import Collection
import hashlib
import os
from pathlib import Path
import stat

MAX_COLLECTOR_ENV_BYTES = 64 * 1024


class CollectorEnvironmentError(RuntimeError):
    """Raised when collector environment evidence is unsafe or malformed."""


def _reject_symlink_ancestors(path: Path) -> None:
    candidate = path.absolute()
    for current in (candidate, *candidate.parents):
        try:
            if current.is_symlink():
                raise CollectorEnvironmentError(
                    f"collector env path contains symlink ancestor: {current}"
                )
        except OSError as error:
            raise CollectorEnvironmentError(
                f"cannot inspect collector env path: {current}"
            ) from error
    for parent in candidate.parents:
        try:
            parent_stat = parent.lstat()
        except OSError as error:
            raise CollectorEnvironmentError(
                f"cannot inspect collector env parent: {parent}"
            ) from error
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid not in (0, os.getuid())
            or stat.S_IMODE(parent_stat.st_mode) & 0o022
        ):
            raise CollectorEnvironmentError(
                f"collector env parent must be trusted and non-writable: {parent}"
            )


def _stable_private_bytes(path: Path) -> bytes:
    _reject_symlink_ancestors(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CollectorEnvironmentError(f"cannot open collector env file: {path}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_COLLECTOR_ENV_BYTES
        ):
            raise CollectorEnvironmentError(
                "collector env file must be user-owned mode-600 regular file"
            )
        chunks: list[bytes] = []
        remaining = MAX_COLLECTOR_ENV_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) > MAX_COLLECTOR_ENV_BYTES or identity_before != identity_after:
            raise CollectorEnvironmentError("collector env file changed while reading")
        return payload
    finally:
        os.close(descriptor)


def load_private_collector_env_evidence(
    path: Path,
    *,
    allowed_keys: Collection[str],
) -> tuple[dict[str, str], str]:
    """Parse one stable private env file and return the exact-byte SHA-256."""

    try:
        payload = _stable_private_bytes(path)
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise CollectorEnvironmentError("collector env file must be UTF-8") from error
    allowed = frozenset(allowed_keys)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CollectorEnvironmentError(
                f"invalid collector env line {line_number}: expected KEY=VALUE"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if key not in allowed:
            raise CollectorEnvironmentError(
                f"unsupported key in collector env file at line {line_number}: {key}"
            )
        if key in values:
            raise CollectorEnvironmentError(f"duplicate key in collector env file: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values, hashlib.sha256(payload).hexdigest()


def load_private_collector_env(
    path: Path,
    *,
    allowed_keys: Collection[str],
) -> dict[str, str]:
    """Read and parse one stable private env file without evaluating shell syntax."""

    values, _sha256 = load_private_collector_env_evidence(path, allowed_keys=allowed_keys)
    return values
