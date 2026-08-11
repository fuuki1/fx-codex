from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import fx_intel.virtual_portfolio_checkpoint as checkpoint_module
from fx_intel.virtual_portfolio import open_virtual_portfolio_writer
from fx_intel.virtual_portfolio_checkpoint import (
    VirtualPortfolioCheckpointError,
    checkpoint_file_sha256,
    create_v2_checkpoint,
    verify_v2_checkpoint,
)
from tests.test_virtual_portfolio import _initialized
from tests.test_virtual_portfolio_v2 import _valuation


def test_checkpoint_is_create_only_and_verifies_chain_prefix(tmp_path, monkeypatch) -> None:
    database, _ = _initialized(tmp_path)
    checkpoint = tmp_path / "external" / "checkpoint.json"
    monkeypatch.setattr(checkpoint_module.socket, "gethostname", lambda: "mac-mini")
    created = create_v2_checkpoint(
        database,
        checkpoint,
    )
    expected_digest = checkpoint_file_sha256(checkpoint)
    same_host = verify_v2_checkpoint(
        database,
        checkpoint,
        expected_checkpoint_file_sha256=expected_digest,
    )
    monkeypatch.setattr(checkpoint_module.socket, "gethostname", lambda: "macbook")
    external = verify_v2_checkpoint(
        database,
        checkpoint,
        expected_checkpoint_file_sha256=expected_digest,
    )

    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert created["event_chain"]["event_count"] == 1
    assert same_host["passed"] is True
    assert same_host["prefix_preserved"] is True
    assert same_host["different_host_copy_observed"] is False
    assert same_host["independent_custody_verified"] is False
    assert external["passed"] is True
    assert external["different_host_copy_observed"] is True
    assert external["out_of_band_digest_match"] is True
    assert external["independent_custody_verified"] is False
    with pytest.raises(VirtualPortfolioCheckpointError, match="already exists"):
        create_v2_checkpoint(database, checkpoint)


def test_checkpoint_rejects_truncated_or_replaced_chain(tmp_path) -> None:
    database, _ = _initialized(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    create_v2_checkpoint(database, checkpoint)
    expected_digest = checkpoint_file_sha256(checkpoint)
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement, _ = _initialized(replacement_root)
    with open_virtual_portfolio_writer(replacement, writer_id="test") as store:
        store.connection.execute("DROP TRIGGER portfolio_event_log_no_update")
        store.connection.execute(
            "UPDATE portfolio_event_log SET event_sha256 = ?",
            ("f" * 64,),
        )
        store.connection.commit()

    verification = verify_v2_checkpoint(
        replacement,
        checkpoint,
        expected_checkpoint_file_sha256=expected_digest,
    )

    assert verification["passed"] is False
    assert verification["prefix_preserved"] is False
    assert verification["v2_audit_passed"] is False


def test_checkpoint_accepts_audited_append_only_extension(tmp_path) -> None:
    database, policy = _initialized(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    created = create_v2_checkpoint(database, checkpoint)
    expected_digest = checkpoint_file_sha256(checkpoint)
    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(
            _valuation(policy, (), valuation_id="post-checkpoint-valuation")
        )

    verification = verify_v2_checkpoint(
        database,
        checkpoint,
        expected_checkpoint_file_sha256=expected_digest,
    )

    assert verification["passed"] is True
    assert verification["prefix_preserved"] is True
    assert verification["current_event_count"] > created["event_chain"]["event_count"]


def test_checkpoint_hash_detects_modified_external_copy(tmp_path) -> None:
    database, _ = _initialized(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    create_v2_checkpoint(database, checkpoint)
    original_digest = checkpoint_file_sha256(checkpoint)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["source"]["host"] = "tampered"
    identity = {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
    payload["checkpoint_sha256"] = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    checkpoint.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    verification = verify_v2_checkpoint(
        database,
        checkpoint,
        expected_checkpoint_file_sha256=original_digest,
    )

    assert verification["passed"] is False
    assert verification["checkpoint_hash_valid"] is True
    assert verification["out_of_band_digest_match"] is False


def test_checkpoint_cli_fails_closed_when_output_exists(tmp_path) -> None:
    database, _ = _initialized(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    script = Path(__file__).resolve().parents[1] / "tools" / "virtual_portfolio_checkpoint.py"
    command = [
        sys.executable,
        str(script),
        "create",
        "--database",
        str(database),
        "--output",
        str(checkpoint),
    ]
    created = subprocess.run(command, capture_output=True, check=False, text=True)
    duplicate = subprocess.run(command, capture_output=True, check=False, text=True)

    assert created.returncode == 0
    assert json.loads(created.stdout)["ok"] is True
    assert duplicate.returncode == 1
    assert json.loads(duplicate.stdout)["status"] == "failed_closed"


def test_checkpoint_cli_fails_closed_without_database(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "virtual_portfolio_checkpoint.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "verify",
            "--database",
            str(tmp_path / "missing.sqlite3"),
            "--checkpoint",
            str(tmp_path / "missing.json"),
            "--expected-checkpoint-file-sha256",
            "0" * 64,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "failed_closed"
    assert "Traceback" not in result.stderr
