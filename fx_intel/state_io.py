"""原子的なJSON状態ファイル書込みの共通ヘルパー。

複数のtools/collectorが同じ「tmp→fsync→rename」パターンを個別実装していた。
途中クラッシュで壊れたJSONを残さないための単一の書込み契約をここへ集約する。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


@contextmanager
def advisory_file_lock(
    path: str | Path,
    *,
    exclusive: bool,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Hold a POSIX advisory lock for one multi-file state transaction."""

    if timeout_seconds is not None and timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if timeout_seconds is None:
            fcntl.flock(handle.fileno(), operation)
        else:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"timed out waiting for state lock: {target}")
                    time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: str | Path, payload: object) -> None:
    """tmp→fsync→renameでJSONを原子的に書く(途中クラッシュで壊れた行を残さない)。

    同一ディレクトリ内の一時ファイルへ書いてからrenameするため、readerは
    常に完全なJSON、または旧内容のどちらかだけを観測する。renameに失敗した
    場合は一時ファイルを残さない。
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=destination.parent, suffix=".tmp", delete=False, encoding="utf-8"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, destination)
    except BaseException:
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json_create_only(path: str | Path, payload: object) -> None:
    """Atomically publish JSON only when *path* has never existed.

    The completed, fsynced temporary inode is hard-linked into place. ``link``
    is atomic and fails with ``FileExistsError`` instead of replacing prior
    evidence, closing the check-then-rename race in run-report writers.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=destination.parent, suffix=".tmp", delete=False, encoding="utf-8"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(handle.name, destination)
        os.unlink(handle.name)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise
