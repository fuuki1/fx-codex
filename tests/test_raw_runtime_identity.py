"""Installed transport closure identity for raw-ledger activation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from data_platform.raw.runtime_identity import (
    COLLECTOR_DISTRIBUTIONS,
    COLLECTOR_ENVIRONMENT_LOCK_PATH,
    RuntimeIdentityError,
    collector_environment_snapshot,
    verified_collector_environment_identity,
)
from data_platform.raw import runtime_identity

REPO_ROOT = Path(__file__).resolve().parents[1]


def _stat_view(source: os.stat_result, *, ctime_ns: int) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=source.st_mode,
        st_uid=source.st_uid,
        st_size=source.st_size,
        st_dev=source.st_dev,
        st_ino=source.st_ino,
        st_mtime_ns=source.st_mtime_ns,
        st_ctime_ns=ctime_ns,
    )


def test_trusted_file_read_retries_one_materialization_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted.txt"
    trusted.write_bytes(b"reviewed")
    observed = trusted.stat()
    views = iter(
        (
            _stat_view(observed, ctime_ns=1),
            _stat_view(observed, ctime_ns=2),
            _stat_view(observed, ctime_ns=2),
            _stat_view(observed, ctime_ns=2),
        )
    )
    monkeypatch.setattr(runtime_identity.os, "fstat", lambda _descriptor: next(views))

    assert runtime_identity._stable_trusted_file_bytes(trusted) == b"reviewed"


def test_trusted_file_read_rejects_continuous_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted.txt"
    trusted.write_bytes(b"reviewed")
    observed = trusted.stat()
    views = iter(_stat_view(observed, ctime_ns=value) for value in (1, 2, 3, 4))
    monkeypatch.setattr(runtime_identity.os, "fstat", lambda _descriptor: next(views))

    with pytest.raises(RuntimeIdentityError, match="changed while hashing"):
        runtime_identity._stable_trusted_file_bytes(trusted)


def test_reviewed_collector_environment_lock_matches_installed_files() -> None:
    snapshot = collector_environment_snapshot()
    environment_sha256, lock_sha256 = verified_collector_environment_identity(
        REPO_ROOT / COLLECTOR_ENVIRONMENT_LOCK_PATH
    )

    assert tuple(item["name"] for item in snapshot["distributions"]) == COLLECTOR_DISTRIBUTIONS
    assert len(environment_sha256) == 64
    assert len(lock_sha256) == 64


def test_collector_environment_lock_rejects_tampering(tmp_path: Path) -> None:
    source = REPO_ROOT / COLLECTOR_ENVIRONMENT_LOCK_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["environment"]["distributions"][0]["version"] = "tampered"
    lock = tmp_path / "collector-environment.lock.json"
    lock.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeIdentityError, match="does not match"):
        verified_collector_environment_identity(lock)


def test_isolated_wrapper_ignores_pythonpath_and_loads_reviewed_requests(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "shadow-imported"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('sitecustomize')\n",
        encoding="utf-8",
    )
    shadow_requests = shadow / "requests"
    shadow_requests.mkdir()
    (shadow_requests / "__init__.py").write_text(
        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('requests')\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "collector.env"
    env_file.write_text(
        "FX_OANDA_API_TOKEN=masked-token\n"
        "FX_OANDA_ACCOUNT_ID=masked-account\n"
        "FX_OANDA_ENV=live\n"
        "FX_RAW_LEDGER_ENABLED=true\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    environment = {
        **os.environ,
        "FX_CODEX_COLLECTOR_ENV_FILE": str(env_file),
        "FX_CODEX_COLLECTOR_PYTHON": str(REPO_ROOT / ".venv" / "bin" / "python"),
        "PYTHONHOME": str(shadow),
        "PYTHONPATH": str(shadow),
    }

    result = subprocess.run(
        [
            "/bin/sh",
            str(REPO_ROOT / "scripts" / "run_quote_collector.sh"),
            "--output-root",
            str(tmp_path / "collect"),
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert "masked-token" not in result.stdout
    assert "masked-account" not in result.stdout
