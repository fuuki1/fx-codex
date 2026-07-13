"""Preserve an unverifiable legacy price journal before v2 hash enforcement."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.append_only import SidecarLockError, exclusive_sidecar_lock  # noqa: E402

DEFAULT_RELATIVE_PATH = Path("logs/briefing_tf_prices.jsonl")


class MigrationLockError(RuntimeError):
    """Raised when the price writer cannot be proven stopped."""


def inspect_journal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("price journal must be a regular file")
    rows = 0
    legacy_rows = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed JSONL at line {line_number}") from error
            if not isinstance(row, dict):
                raise ValueError(f"non-object JSONL row at line {line_number}")
            rows += 1
            supplied = row.get("content_hash")
            schema = row.get("schema_version", row.get("schema"))
            if schema != 2 or not isinstance(supplied, str):
                legacy_rows += 1
                continue
            if supplied != _row_hash(row):
                raise ValueError(f"content_hash mismatch at line {line_number}")
    return {
        "path": str(path),
        "rows": rows,
        "legacy_rows": legacy_rows,
        "sha256": _file_hash(path),
        "bytes": path.stat().st_size,
        "migration_required": legacy_rows > 0,
    }


def migrate_journal(
    root: Path,
    *,
    relative_path: Path = DEFAULT_RELATIVE_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    source = _source_path(root, relative_path)
    try:
        with exclusive_sidecar_lock(source, blocking=False):
            with _open_regular_journal(source) as source_handle:
                try:
                    fcntl.flock(source_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise MigrationLockError("active price writer holds the journal") from error
                try:
                    return _migrate_journal_locked(root, source, now=now)
                finally:
                    fcntl.flock(source_handle.fileno(), fcntl.LOCK_UN)
    except SidecarLockError as error:
        raise MigrationLockError("active price writer holds the sidecar lock") from error


def _migrate_journal_locked(
    root: Path,
    source: Path,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    """Migrate while both the stable sidecar and legacy inode locks are held."""

    report = inspect_journal(source)
    if not report["migration_required"]:
        return {**report, "applied": False, "reason": "already_v2"}

    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = _validated_archive_directory(root, create=True)
    archive = archive_dir / f"{source.stem}.pre-v2.{stamp}{source.suffix}"
    manifest = archive.with_suffix(archive.suffix + ".manifest.json")
    if _lexists(archive) or _lexists(manifest):
        raise FileExistsError(f"migration archive already exists: {archive}")

    # Copy the locked bytes into a new inode before unlinking the active path.
    # A legacy writer that opened the old inode before this migration but was
    # waiting on flock can therefore never mutate the immutable archive/manifest.
    archive_tmp = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    archive_tmp_created = False
    try:
        _validated_archive_directory(root, create=False)
        with source.open("rb") as source_copy, archive_tmp.open("xb") as archive_handle:
            archive_tmp_created = True
            shutil.copyfileobj(source_copy, archive_handle)
            archive_handle.flush()
            os.fsync(archive_handle.fileno())
        _validated_archive_directory(root, create=False)
        os.replace(archive_tmp, archive)
    finally:
        if archive_tmp_created and _lexists(archive_tmp):
            archive_tmp.unlink()
    _fsync_directory(archive_dir)
    payload = {
        "schema_version": 1,
        "migration": "append_only_journal_to_fresh_v2",
        "migrated_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "source_path": str(source),
        "archive_path": str(archive),
        "source_sha256": report["sha256"],
        "source_bytes": report["bytes"],
        "source_rows": report["rows"],
        "legacy_rows": report["legacy_rows"],
        "active_journal_created": False,
        "migration_state": "prepared",
        "note": "The next verified writer creates a new v2 active journal.",
    }
    # The archive manifest must be durable before the active source is retired.
    # If any later filesystem operation fails, restore the active source from
    # the already-fsynced archive so an interrupted migration remains reversible.
    _validated_archive_directory(root, create=False)
    _atomic_json(manifest, payload)
    try:
        _validated_archive_directory(root, create=False)
        source.unlink()
        _fsync_directory(source.parent)
        completed_payload = {**payload, "migration_state": "complete"}
        _atomic_json(manifest, completed_payload)
    except OSError:
        _restore_source_from_archive(archive, source)
        raise
    return {**report, **completed_payload, "applied": True}


def _row_hash(row: dict[str, Any]) -> str:
    payload = {key: value for key, value in row.items() if key != "content_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("journal path must be repository-relative")
    resolved_root = root.resolve(strict=True)
    source = resolved_root / relative_path
    if source.parent != resolved_root / "logs":
        raise ValueError("journal path must be a direct child of logs/")
    parent = resolved_root
    for component in relative_path.parts[:-1]:
        parent /= component
        try:
            metadata = os.lstat(parent)
        except FileNotFoundError as error:
            raise ValueError("journal parent must be an existing regular directory") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("journal path must not contain symlink parents")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("journal parent must be an existing regular directory")
    try:
        metadata = os.lstat(source)
    except FileNotFoundError as error:
        raise ValueError("price journal must be an existing regular file") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("price journal must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("price journal must be a regular file")
    return source


def _validated_archive_directory(root: Path, *, create: bool) -> Path:
    """Return the in-repository archive directory without following a symlink."""

    resolved_root = root.resolve(strict=True)
    logs_dir = resolved_root / "logs"
    try:
        logs_metadata = os.lstat(logs_dir)
    except FileNotFoundError as error:
        raise ValueError("journal archive parent must be an existing regular directory") from error
    if stat.S_ISLNK(logs_metadata.st_mode) or not stat.S_ISDIR(logs_metadata.st_mode):
        raise ValueError("journal archive path must not contain symlink parents")

    archive_dir = logs_dir / "legacy"
    try:
        archive_metadata = os.lstat(archive_dir)
    except FileNotFoundError:
        if not create:
            raise ValueError("journal archive directory disappeared during migration") from None
        try:
            archive_dir.mkdir(mode=0o700)
        except FileExistsError:
            # A concurrent replacement must go through the same lstat checks.
            pass
        archive_metadata = os.lstat(archive_dir)
    if stat.S_ISLNK(archive_metadata.st_mode) or not stat.S_ISDIR(archive_metadata.st_mode):
        raise ValueError("journal archive path must not contain symlink parents")
    try:
        resolved_archive = archive_dir.resolve(strict=True)
        resolved_archive.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise ValueError("journal archive directory must remain inside the repository") from error
    if resolved_archive != archive_dir:
        raise ValueError("journal archive path must not contain symlink parents")
    return archive_dir


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _open_regular_journal(path: Path):
    """Open the already-validated lexical path without following a replacement link."""

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        lexical = os.lstat(path)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, lexical):
            raise ValueError("price journal changed during migration preflight")
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    return handle


def _atomic_json(path: Path, payload: object) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _restore_source_from_archive(archive: Path, source: Path) -> None:
    """Restore a retired active source without modifying the durable archive."""

    if source.exists():
        return
    restore = source.with_name(f".{source.name}.restore-{os.getpid()}")
    try:
        shutil.copyfile(archive, restore)
        with restore.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(restore, source)
        _fsync_directory(source.parent)
    finally:
        if restore.exists():
            restore.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    parser.add_argument(
        "--relative-path",
        type=Path,
        default=DEFAULT_RELATIVE_PATH,
        help="repository-relative journal path (must be directly under logs/)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    source = _source_path(root, args.relative_path)
    if args.apply:
        if not args.confirm_writers_stopped:
            parser.error("--apply requires --confirm-writers-stopped")
        result = migrate_journal(root, relative_path=args.relative_path)
    else:
        result = {**inspect_journal(source), "applied": False, "reason": "dry_run"}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
