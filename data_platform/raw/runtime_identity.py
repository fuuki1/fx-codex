"""Deterministic identity for code loaded by the read-only quote collector."""

from __future__ import annotations

import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

COLLECTOR_ENVIRONMENT_CONTRACT = "raw-ledger-collector-environment-v1"
COLLECTOR_ENVIRONMENT_LOCK_CONTRACT = "raw-ledger-collector-environment-lock-v1"
COLLECTOR_ENVIRONMENT_LOCK_PATH = "ops/raw_ledger_collector_environment.lock.json"
COLLECTOR_DISTRIBUTIONS = (
    "certifi",
    "charset-normalizer",
    "idna",
    "requests",
    "urllib3",
)
COLLECTOR_IMPORT_MODULES = (
    ("certifi", "certifi"),
    ("charset-normalizer", "charset_normalizer"),
    ("idna", "idna"),
    ("requests", "requests"),
    ("urllib3", "urllib3"),
)

CRITICAL_COLLECTOR_PATHS = (
    "tools/fx_quote_collector.py",
    "tools/raw_ledger_flag_probe.py",
    "tools/run_exclusive.py",
    "scripts/run_quote_collector.sh",
    "data_platform/collect/oanda.py",
    "data_platform/collect/oanda_raw_shadow.py",
    "data_platform/collect/collector_env.py",
    "data_platform/collect/raw_first.py",
    "data_platform/collect/contract.py",
    "data_platform/collect/reconnect.py",
    "data_platform/contracts/raw_event.py",
    "data_platform/quality/state.py",
    "data_platform/raw/content_addressed.py",
    "data_platform/raw/event_ledger.py",
    "data_platform/raw/immutable_store.py",
    "data_platform/raw/runtime_identity.py",
    "data_platform/raw/shadow.py",
    COLLECTOR_ENVIRONMENT_LOCK_PATH,
)


class RuntimeIdentityError(RuntimeError):
    """Raised when the collector runtime cannot be bound to its reviewed lock."""


def _stable_trusted_file_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    # APFS/File Provider may materialize an otherwise immutable file on its first
    # read and update ctime without changing its identity or bytes. Reopen once
    # after an unstable observation, then require a completely stable read. A
    # continuously changing file still fails closed, and the resulting bytes are
    # subsequently compared with the reviewed environment lock.
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise RuntimeIdentityError(f"cannot open trusted runtime file: {path}") from error
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in (0, os.getuid())
                or stat.S_IMODE(before.st_mode) & 0o022
                or (max_bytes is not None and before.st_size > max_bytes)
            ):
                raise RuntimeIdentityError(f"unsafe trusted runtime file: {path}")
            chunks: list[bytes] = []
            remaining = None if max_bytes is None else max_bytes + 1
            while remaining is None or remaining > 0:
                size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
                chunk = os.read(descriptor, size)
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            stable = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if max_bytes is not None and len(payload) > max_bytes:
                raise RuntimeIdentityError(f"unsafe trusted runtime file: {path}")
            if stable:
                return payload
        finally:
            os.close(descriptor)
        if attempt == 0:
            continue
    raise RuntimeIdentityError(f"trusted runtime file changed while hashing: {path}")


def _require_no_symlink_ancestors(path: Path, *, root: Path) -> None:
    candidate = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(root))
    if not candidate.is_relative_to(boundary):
        raise RuntimeIdentityError(f"runtime file escapes environment root: {path}")
    current = candidate
    while True:
        try:
            if current.is_symlink():
                raise RuntimeIdentityError(f"runtime file has a symlink ancestor: {current}")
        except OSError as error:
            raise RuntimeIdentityError(f"cannot inspect runtime path: {current}") from error
        if current == boundary:
            return
        current = current.parent


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def python_runtime_isolated() -> bool:
    """Require no site initialization, environment injection, or bytecode cache reads."""

    return bool(
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.no_user_site
        and sys.flags.ignore_environment
        and sys.flags.dont_write_bytecode
        and sys.pycache_prefix == "/dev/null"
    )


def _distribution_identity(name: str, *, environment_root: Path) -> dict[str, str | int]:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as error:
        raise RuntimeIdentityError(f"required collector distribution is missing: {name}") from error
    files = distribution.files
    if files is None:
        raise RuntimeIdentityError(
            f"collector distribution has no installed file inventory: {name}"
        )

    environment_root = environment_root.resolve()
    digest = hashlib.sha256()
    file_count = 0
    for relative in sorted(
        (Path(str(item)) for item in files),
        key=lambda item: item.as_posix(),
    ):
        if relative.suffix in {".pyc", ".pyo"} or "__pycache__" in relative.parts:
            continue
        path = Path(str(distribution.locate_file(relative)))
        try:
            _require_no_symlink_ancestors(path, root=environment_root)
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise RuntimeIdentityError(
                f"cannot inspect collector distribution file: {name}/{relative.as_posix()}"
            ) from error
        if not resolved.is_relative_to(environment_root):
            raise RuntimeIdentityError(
                f"unsafe collector distribution file: {name}/{relative.as_posix()}"
            )
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            hashlib.sha256(_stable_trusted_file_bytes(resolved)).hexdigest().encode("ascii")
        )
        digest.update(b"\n")
        file_count += 1
    if file_count == 0:
        raise RuntimeIdentityError(f"collector distribution has no hashable files: {name}")
    return {
        "name": name,
        "version": distribution.version,
        "file_count": file_count,
        "files_sha256": digest.hexdigest(),
    }


def _loaded_transport_modules(environment_root: Path) -> list[dict[str, str]]:
    environment_root = environment_root.resolve()
    base_root = Path(sys.base_prefix).resolve()
    original_path = list(sys.path)
    restricted_path: list[str] = []
    for entry in original_path:
        if not entry:
            continue
        resolved = Path(entry).resolve()
        is_venv_site = (
            resolved.is_relative_to(environment_root) and resolved.name == "site-packages"
        )
        is_base_library = (
            resolved.is_relative_to(base_root) and "site-packages" not in resolved.parts
        )
        if is_venv_site or is_base_library:
            restricted_path.append(entry)
    if not any(Path(entry).resolve().is_relative_to(environment_root) for entry in restricted_path):
        raise RuntimeIdentityError("approved collector site-packages is not on sys.path")

    existing = {
        module_name: sys.modules.get(module_name) for _, module_name in COLLECTOR_IMPORT_MODULES
    }
    for module_name, module in existing.items():
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None or not Path(module_file).resolve().is_relative_to(environment_root):
            raise RuntimeIdentityError(
                f"collector module was preloaded outside approved venv: {module_name}"
            )

    modules: list[dict[str, str]] = []
    try:
        sys.path[:] = restricted_path
        for distribution_name, module_name in COLLECTOR_IMPORT_MODULES:
            module = importlib.import_module(module_name)
            module_file = getattr(module, "__file__", None)
            if module_file is None:
                raise RuntimeIdentityError(f"collector module has no file origin: {module_name}")
            origin = Path(module_file)
            _require_no_symlink_ancestors(origin, root=environment_root)
            resolved = origin.resolve(strict=True)
            modules.append(
                {
                    "distribution": distribution_name,
                    "module": module_name,
                    "origin": resolved.relative_to(environment_root).as_posix(),
                    "origin_sha256": hashlib.sha256(
                        _stable_trusted_file_bytes(resolved)
                    ).hexdigest(),
                }
            )
    finally:
        sys.path[:] = original_path
    return modules


def collector_environment_snapshot(environment_root: Path | None = None) -> dict[str, Any]:
    """Inventory immutable installed files used by the lazy requests transport."""

    root = (environment_root or Path(sys.prefix)).resolve()
    return {
        "contract": COLLECTOR_ENVIRONMENT_CONTRACT,
        "distributions": [
            _distribution_identity(name, environment_root=root) for name in COLLECTOR_DISTRIBUTIONS
        ],
        "loaded_modules": _loaded_transport_modules(root),
    }


def verified_collector_environment_identity(
    lock_path: Path,
    *,
    environment_root: Path | None = None,
) -> tuple[str, str]:
    """Require the installed transport closure to equal the reviewed lock exactly."""

    try:
        lock_bytes = _stable_trusted_file_bytes(lock_path, max_bytes=1024 * 1024)
        lock_payload = json.loads(lock_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeIdentityError("cannot read collector environment lock") from error
    environment = collector_environment_snapshot(environment_root)
    expected: dict[str, Any] = {
        "contract": COLLECTOR_ENVIRONMENT_LOCK_CONTRACT,
        "environment": environment,
    }
    if lock_payload != expected:
        raise RuntimeIdentityError("installed collector environment does not match reviewed lock")
    return (
        hashlib.sha256(_canonical_json(environment)).hexdigest(),
        hashlib.sha256(lock_bytes).hexdigest(),
    )


def critical_collector_code_sha256(repo_root: Path) -> str:
    """Hash stable current on-disk bytes in the collector trust boundary.

    This is an on-disk deployment observation, not proof of the Python bytecode
    already loaded into a running process. Production evidence therefore also
    requires code timestamps no later than process start and a sealed release.
    """

    digest = hashlib.sha256()
    for relative in CRITICAL_COLLECTOR_PATHS:
        path = repo_root / relative
        digest.update(relative.encode())
        digest.update(hashlib.sha256(_stable_trusted_file_bytes(path)).hexdigest().encode("ascii"))
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """Hash one stable, non-symlink regular executable or evidence file."""

    return hashlib.sha256(_stable_trusted_file_bytes(path)).hexdigest()
