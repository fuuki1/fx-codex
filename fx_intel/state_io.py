"""原子的なJSON状態ファイル書込みの共通ヘルパー。

複数のtools/collectorが同じ「tmp→fsync→rename」パターンを個別実装していた。
途中クラッシュで壊れたJSONを残さないための単一の書込み契約をここへ集約する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


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
