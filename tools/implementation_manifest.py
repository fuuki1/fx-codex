#!/usr/bin/env python3
"""Create a deterministic manifest for the reviewable implementation tree."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

SCHEMA_VERSION = 1
IMPLEMENTATION_PREFIXES = (
    "fx_backtester/",
    "fx_intel/",
    "ops/",
    "scripts/",
    "tests/",
    "tools/",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _git_output(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _is_implementation_path(relative_path: str) -> bool:
    if relative_path.startswith(IMPLEMENTATION_PREFIXES):
        return True
    if "/" in relative_path:
        return False
    return relative_path.endswith(".py") or relative_path.startswith("requirements")


def implementation_paths(root: Path) -> list[str]:
    """Return tracked and non-ignored untracked implementation paths."""

    raw = _git_output(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths = [entry.decode("utf-8") for entry in raw.split(b"\0") if entry]
    return sorted(path for path in paths if _is_implementation_path(path))


def _entry(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    if path.is_symlink():
        payload = os.readlink(path).encode("utf-8")
        kind = "symlink"
    elif path.is_file():
        payload = path.read_bytes()
        kind = "file"
    else:
        raise ValueError(f"manifest path is not a file: {relative_path}")
    return {
        "path": relative_path,
        "kind": kind,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def build_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    entries = [_entry(root, path) for path in implementation_paths(root)]
    tree_hash = hashlib.sha256(_canonical_bytes(entries)).hexdigest()
    head = _git_output(root, "rev-parse", "HEAD").decode("ascii").strip()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "git_head": head,
        "scope": {
            "directory_prefixes": list(IMPLEMENTATION_PREFIXES),
            "root_globs": ["*.py", "requirements*"],
            "source": "git tracked plus non-ignored untracked files",
        },
        "file_count": len(entries),
        "tree_sha256": tree_hash,
        "files": entries,
    }
    # A file cannot contain its own file hash.  This binds the canonical payload
    # before this field is added; callers separately hash the retained JSON file.
    manifest["manifest_payload_sha256"] = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_manifest(args.root)
    if args.output is not None:
        output = args.output
        if not output.is_absolute():
            output = args.root / output
        write_manifest(output, manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
