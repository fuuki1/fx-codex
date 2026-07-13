from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import implementation_manifest


def test_manifest_includes_tests_and_binds_paths_to_content(monkeypatch, tmp_path: Path) -> None:
    files = {
        "fx_backtester/core.py": b"core\n",
        "tests/test_core.py": b"test\n",
        "root_tool.py": b"root\n",
        "requirements.lock": b"dep==1\n",
        "reports/result.md": b"excluded\n",
    }
    for relative_path, payload in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def fake_git_output(root: Path, *arguments: str) -> bytes:
        assert root == tmp_path.resolve()
        if arguments[0] == "ls-files":
            return b"\0".join(path.encode() for path in files) + b"\0"
        if arguments == ("rev-parse", "HEAD"):
            return b"a" * 40 + b"\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(implementation_manifest, "_git_output", fake_git_output)
    manifest = implementation_manifest.build_manifest(tmp_path)

    included = [entry["path"] for entry in manifest["files"]]
    assert included == [
        "fx_backtester/core.py",
        "requirements.lock",
        "root_tool.py",
        "tests/test_core.py",
    ]
    assert manifest["file_count"] == 4
    assert "reports/result.md" not in included

    original_hash = manifest["tree_sha256"]
    manifest["files"][0]["path"] = "fx_backtester/renamed.py"
    changed_hash = hashlib.sha256(
        implementation_manifest._canonical_bytes(manifest["files"])
    ).hexdigest()
    assert changed_hash != original_hash


def test_write_manifest_is_deterministic(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "tree_sha256": "a" * 64,
        "manifest_payload_sha256": "b" * 64,
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    implementation_manifest.write_manifest(first, manifest)
    implementation_manifest.write_manifest(second, manifest)

    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == manifest
