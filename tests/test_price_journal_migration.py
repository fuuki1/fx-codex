from __future__ import annotations

from datetime import UTC, datetime
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from fx_intel.append_only import exclusive_sidecar_lock

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "prepare_v2_price_journal.py"


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("prepare_v2_price_journal", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_legacy_journal_is_archived_with_hash_manifest(migration, tmp_path) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    original = b'{"ts":"2026-07-10T00:00:00+00:00","close":150.0}\n'
    source.write_bytes(original)

    result = migration.migrate_journal(tmp_path, now=datetime(2026, 7, 12, 0, 0, tzinfo=UTC))

    archive = Path(result["archive_path"])
    manifest = Path(str(archive) + ".manifest.json")
    assert result["applied"] is True
    assert result["migration_state"] == "complete"
    assert not source.exists()
    assert archive.read_bytes() == original
    assert json.loads(manifest.read_text())["source_sha256"] == hashlib.sha256(original).hexdigest()


def test_corrupt_or_tampered_v2_journal_is_never_migrated(migration, tmp_path) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    source.write_text('{"schema_version":2,"content_hash":"' + "0" * 64 + '"}\n')

    with pytest.raises(ValueError, match="content_hash mismatch"):
        migration.migrate_journal(tmp_path)
    assert source.exists()


def test_migration_rejects_active_legacy_inode_writer(migration, tmp_path) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    source.write_text('{"legacy":1}\n', encoding="utf-8")

    with source.open("a+", encoding="utf-8") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX)
        with pytest.raises(migration.MigrationLockError, match="active price writer"):
            migration.migrate_journal(tmp_path)

    assert source.exists()
    assert not (tmp_path / "logs" / "legacy").exists()


def test_migration_rejects_active_sidecar_writer(migration, tmp_path) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    source.write_text('{"legacy":1}\n', encoding="utf-8")

    with exclusive_sidecar_lock(source):
        with pytest.raises(migration.MigrationLockError, match="sidecar lock"):
            migration.migrate_journal(tmp_path)


def test_generic_decision_journal_is_archived_with_same_safety_contract(
    migration, tmp_path
) -> None:
    relative = Path("logs/briefing_decisions.jsonl")
    source = tmp_path / relative
    source.parent.mkdir()
    original = b'{"schema":1,"ts":"2026-07-10T00:00:00+00:00"}\n'
    source.write_bytes(original)

    result = migration.migrate_journal(
        tmp_path,
        relative_path=relative,
        now=datetime(2026, 7, 12, 0, 0, tzinfo=UTC),
    )

    archive = Path(result["archive_path"])
    assert result["applied"] is True
    assert not source.exists()
    assert archive.name.startswith("briefing_decisions.pre-v2.")
    assert archive.read_bytes() == original


def test_generic_migration_rejects_path_escape(migration, tmp_path) -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        migration.migrate_journal(tmp_path, relative_path=Path("../secret.jsonl"))


def test_migration_rejects_symlink_without_changing_link_or_target(migration, tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    target = logs / "actual.jsonl"
    original = b'{"legacy":1}\n'
    target.write_bytes(original)
    source = logs / "briefing_tf_prices.jsonl"
    source.symlink_to(target.name)

    with pytest.raises(ValueError, match="must not be a symlink"):
        migration.migrate_journal(tmp_path)

    assert source.is_symlink()
    assert source.readlink() == Path(target.name)
    assert target.read_bytes() == original
    assert not (logs / "legacy").exists()


def test_migration_rejects_symlink_parent_without_changing_target(migration, tmp_path) -> None:
    actual_logs = tmp_path / "actual-logs"
    actual_logs.mkdir()
    target = actual_logs / "briefing_tf_prices.jsonl"
    original = b'{"legacy":1}\n'
    target.write_bytes(original)
    (tmp_path / "logs").symlink_to(actual_logs.name, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parents"):
        migration.migrate_journal(tmp_path)

    assert (tmp_path / "logs").is_symlink()
    assert target.read_bytes() == original
    assert not (actual_logs / "legacy").exists()


def test_migration_rejects_symlink_archive_directory_without_retiring_source(
    migration, tmp_path
) -> None:
    logs = tmp_path / "logs"
    external = tmp_path / "external-archive"
    logs.mkdir()
    external.mkdir()
    source = logs / "briefing_tf_prices.jsonl"
    original = b'{"legacy":1}\n'
    source.write_bytes(original)
    (logs / "legacy").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parents"):
        migration.migrate_journal(tmp_path)

    assert source.read_bytes() == original
    assert list(external.iterdir()) == []


def test_manifest_prepare_failure_keeps_active_source(migration, tmp_path, monkeypatch) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    original = b'{"legacy":1}\n'
    source.write_bytes(original)

    def fail_manifest(*_args, **_kwargs):
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(migration, "_atomic_json", fail_manifest)
    with pytest.raises(OSError, match="simulated manifest failure"):
        migration.migrate_journal(tmp_path)

    assert source.read_bytes() == original


def test_manifest_completion_failure_restores_active_source(
    migration, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "logs" / "briefing_tf_prices.jsonl"
    source.parent.mkdir()
    original = b'{"legacy":1}\n'
    source.write_bytes(original)
    real_atomic_json = migration._atomic_json
    calls = 0

    def fail_completion(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated completion failure")
        real_atomic_json(path, payload)

    monkeypatch.setattr(migration, "_atomic_json", fail_completion)
    with pytest.raises(OSError, match="simulated completion failure"):
        migration.migrate_journal(tmp_path)

    assert source.read_bytes() == original
    manifest = next((tmp_path / "logs" / "legacy").glob("*.manifest.json"))
    assert json.loads(manifest.read_text())["migration_state"] == "prepared"
