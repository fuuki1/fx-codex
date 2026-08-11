from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
from typing import Any, cast

from tools import phase0_inventory


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_inventory_is_hash_bound_and_does_not_export_secret_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "Phase Zero")
    _git(root, "config", "user.email", "phase0@example.invalid")
    secret = "secret-fixture"
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", f"baseline {secret}")
    (root / "README.md").write_text("dirty\n", encoding="utf-8")
    (root / ".env.local").write_text(f"API_TOKEN={secret}\n", encoding="utf-8")

    logs = root / "logs"
    logs.mkdir()
    now = datetime(2026, 8, 2, tzinfo=UTC)
    rows = [
        {"event_time": now.isoformat(), "available_time": now.isoformat(), "value": 1},
        {
            "event_time": (now - timedelta(minutes=1)).isoformat(),
            "available_time": now.isoformat(),
            "value": 2,
        },
    ]
    (logs / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + "not-json\n",
        encoding="utf-8",
    )
    database = logs / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence VALUES('row-1', 'private-row-value')")
    connection.commit()
    connection.close()
    config = root / "config"
    config.mkdir()
    (config / "risk_policy.json").write_text(
        json.dumps({"max_loss": 1, "nested": {"private": "not-exported"}}),
        encoding="utf-8",
    )

    plist_path = home / "Library" / "LaunchAgents" / "com.fx-codex.test.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.fx-codex.test",
                "ProgramArguments": [
                    "python",
                    "worker.py",
                    "--auth",
                    secret,
                    f"postgres://user:{secret}@localhost/db",
                ],
                "EnvironmentVariables": {"FX_API_TOKEN": secret},
            },
            handle,
        )

    monkeypatch.setattr(phase0_inventory, "_launchctl_inventory", lambda: [])
    monkeypatch.setattr(phase0_inventory, "_process_inventory", lambda _root, **_kwargs: ([], []))
    monkeypatch.setattr(
        phase0_inventory,
        "_cron_inventory",
        lambda: {"available": False, "entry_count": 0, "sha256": None},
    )
    monkeypatch.setattr(
        phase0_inventory,
        "_docker_inventory",
        lambda: {"available": False, "containers": []},
    )
    monkeypatch.setattr(
        phase0_inventory,
        "_runtime_environment_inventory",
        lambda _root: {"pip_check_ok": True},
    )

    inventory = cast(dict[str, Any], phase0_inventory.build_inventory(root, home=home))
    encoded = json.dumps(inventory, sort_keys=True)

    assert inventory["contract"] == "fx-codex-phase0-inventory-v2"
    assert inventory["git"]["status_counts"] == {
        "tracked": 1,
        "untracked": 4,
        "deleted": 0,
        "sensitive_paths": 1,
    }
    assert len(str(inventory["inventory_sha256"])) == 64
    assert secret not in encoded
    assert "private-row-value" not in encoded
    assert "not-exported" not in encoded
    assert "FX_API_TOKEN" in encoded
    assert inventory["environment_files"][0]["key_names"] == ["API_TOKEN"]
    assert inventory["environment_files"][0]["sha256_omitted"] == "sensitive_path"
    assert "program_arguments" not in encoded
    assert len(str(inventory["launchd_plists"][0]["safe_view_sha256"])) == 64
    jsonl = inventory["data_stores"]["jsonl"][0]
    assert jsonl["rows"] == 3
    assert jsonl["malformed_rows"] == 1
    assert jsonl["timestamp_reversals"] == 1
    sqlite = inventory["data_stores"]["sqlite"][0]
    assert sqlite["integrity_check"] == "ok"
    assert sqlite["tables"] == [{"name": "evidence", "rows": 1}]


def test_runtime_inventory_preserves_virtual_environment_entrypoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    executable = root / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(sys.executable))
    commands: list[list[str]] = []
    timeouts: list[float] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        commands.append(command)
        timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, "No broken requirements found.\n", "")

    monkeypatch.setattr(phase0_inventory.sys, "executable", str(executable))
    monkeypatch.setattr(phase0_inventory.metadata, "distributions", lambda: [])
    monkeypatch.setattr(phase0_inventory, "_run", fake_run)

    inventory = cast(dict[str, Any], phase0_inventory._runtime_environment_inventory(root))

    assert inventory["python_executable"]["path"] == ".venv/bin/python"
    assert commands == [[str(executable), "-m", "pip", "check"]]
    assert timeouts == [30]


def test_argument_redaction_covers_environment_headers_urls_and_flag_values() -> None:
    values = [
        "FX_API_TOKEN=environment-secret",
        "Authorization: Bearer header-secret",
        "https://example.invalid/path?api_key=query-secret&mode=read",
        "https://discord.com/api/webhooks/123/webhook-secret",
        "--password",
        "flag-secret",
    ]

    redacted = phase0_inventory._redact_arguments(values)
    encoded = json.dumps(redacted)

    assert redacted[0] == "FX_API_TOKEN=<redacted>"
    assert redacted[1].endswith(": <redacted>")
    assert "api_key=<redacted>&mode=read" in redacted[2]
    assert redacted[3].endswith("/api/webhooks/<redacted>")
    assert redacted[-2:] == ["--password", "<redacted>"]
    for secret in (
        "environment-secret",
        "header-secret",
        "query-secret",
        "webhook-secret",
        "flag-secret",
    ):
        assert secret not in encoded


def test_process_inventory_never_exports_unstructured_command_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "proc-fixture"
    command = (
        " 12  1 Sat Aug  2 09:00:00 2026 "
        f"/usr/bin/curl fx-codex -H 'Authorization: Bearer {secret}' "
        f"--api-token='{secret} with spaces'"
    )

    monkeypatch.setattr(
        phase0_inventory,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, command + "\n", ""),
    )

    inventory, _excluded = phase0_inventory._process_inventory(tmp_path)
    encoded = json.dumps(inventory)

    assert len(inventory) == 1
    assert inventory[0]["executable_basename"] == "curl"
    assert inventory[0]["command_present"] is True
    assert len(str(inventory[0]["safe_view_sha256"])) == 64
    assert "command" not in inventory[0]
    assert secret not in encoded


def test_sqlite_inventory_binds_wal_component_to_row_counts(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "live.sqlite3"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
    writer.execute("INSERT INTO evidence VALUES(1)")
    writer.commit()

    inventory = cast(dict[str, Any], phase0_inventory._sqlite_inventory(path, root=root))
    writer.close()

    component_paths = [row["path"] for row in inventory["components"]]
    assert "live.sqlite3-wal" in component_paths
    assert inventory["tables"] == [{"name": "evidence", "rows": 1}]
    assert inventory["observation_consistency"] == "stable_component_set"
    assert inventory["row_counts_reproducible_from_component_hashes"] is True


def test_sqlite_inventory_never_marks_missing_component_hash_as_reproducible(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    component = {
        "path": "state.sqlite3",
        "kind": "file",
        "error": "Phase0InventoryError",
    }
    monkeypatch.setattr(
        phase0_inventory,
        "_sqlite_components",
        lambda *_args, **_kwargs: [dict(component)],
    )

    inventory = phase0_inventory._sqlite_inventory(path, root=tmp_path)

    assert inventory["observation_consistency"] == "component_hash_unavailable"
    assert inventory["row_counts_reproducible_from_component_hashes"] is False


def _digest(text: str) -> str:
    return phase0_inventory._sha256_bytes(text.encode("utf-8"))


def _h(text: str) -> str:
    """A syntactically valid sha256 fixture value."""
    return _digest(text)


def _script(parent: str, file_digest: str, basename: str = "worker.py") -> dict[str, object]:
    return {
        "parent_path_sha256": _digest(parent),
        "basename": basename,
        "file_sha256": file_digest,
        "file_sha256_unavailable": None,
    }


def _provenance(
    scripts: list[dict[str, object]],
    *,
    interpreter_binary: str = _h("interp-a"),
    interpreter_realpath: str = "/runtimes/approved/bin/python",
    status: str = "parsed",
) -> dict[str, object]:
    return {
        "parse_status": status,
        "parse_reason": None if status == "parsed" else "unrecognised_interpreter",
        "interpreter": {
            "version": "3.12",
            "realpath_sha256": _digest(interpreter_realpath),
            "binary_sha256": interpreter_binary,
            "binary_sha256_unavailable": None,
        },
        "script_count": len(scripts),
        "scripts": scripts,
    }


def _process(pid: int, ppid: int, provenance: dict[str, object] | None = None) -> dict[str, object]:
    row: dict[str, object] = {"pid": pid, "ppid": ppid, "executable_basename": "Python"}
    if provenance is not None:
        row["entrypoint_provenance"] = provenance
    return row


def _label(pid: int | None, label: str) -> dict[str, object]:
    return {"pid": pid, "label": label, "last_exit_status": 0}


def _approval(scripts: list[dict[str, object]], interpreter_binary: str = _h("interp-a")):
    return {
        "interpreter_binary_sha256": interpreter_binary,
        "interpreter_realpath_sha256": _digest("/runtimes/approved/bin/python"),
        "scripts": [
            {"parent_path_sha256": s["parent_path_sha256"], "file_sha256": s["file_sha256"]}
            for s in scripts
        ],
    }


QUOTE_SCRIPTS = [
    _script("/srv/approved/tools", _h("wrapper"), "run_exclusive.py"),
    _script("/srv/approved/tools", _h("child"), "quote_tape_index.py"),
]
DASH_SCRIPTS = [_script("/srv/approved/dash", _h("dash"), "server.py")]


def test_run_exclusive_child_is_mapped_through_its_launchd_parent() -> None:
    """Regression: the false positive that blocked Gate 0 on 2026-08-03."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(63256, 1, _provenance(QUOTE_SCRIPTS))],
        [_label(63256, "com.fx-codex.quote-index")],
        approved_provenance={"com.fx-codex.quote-index": _approval(QUOTE_SCRIPTS)},
    )

    assert mapping["unmapped_processes"] == 0
    assert mapping["pass"] is True


def test_wrapper_and_child_are_both_verified() -> None:
    """A tampered child must fail even when the wrapper is approved."""

    tampered = [
        QUOTE_SCRIPTS[0],
        _script("/srv/approved/tools", _h("child-tampered"), "quote_tape_index.py"),
    ]
    mapping = phase0_inventory.resolve_process_mapping(
        [_process(63256, 1, _provenance(tampered))],
        [_label(63256, "com.fx-codex.quote-index")],
        approved_provenance={"com.fx-codex.quote-index": _approval(QUOTE_SCRIPTS)},
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert reason["reason"] == "script_1_file_sha256_mismatch"


def test_same_directory_content_change_is_detected() -> None:
    """Editing a file in place inside an approved directory must fail."""

    edited = [_script("/srv/approved/dash", _h("edited"), "server.py")]
    assert edited[0]["parent_path_sha256"] == DASH_SCRIPTS[0]["parent_path_sha256"]

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(11764, 1, _provenance(edited))],
        [_label(11764, "com.fx-codex.dashboard")],
        approved_provenance={"com.fx-codex.dashboard": _approval(DASH_SCRIPTS)},
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert reason["reason"] == "script_0_file_sha256_mismatch"


def test_cross_service_substitution_is_rejected() -> None:
    """A digest approved for one label must not satisfy another."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(11764, 1, _provenance(DASH_SCRIPTS))],
        [_label(11764, "com.fx-codex.dashboard")],
        approved_provenance={
            "com.fx-codex.dashboard": _approval(QUOTE_SCRIPTS),
            "com.fx-codex.quote-index": _approval(DASH_SCRIPTS),
        },
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert "script_count_mismatch" in reason["reason"]


def test_same_version_different_interpreter_is_rejected() -> None:
    """Two 3.12 builds differ by binary identity, not by version string."""

    other = _provenance(DASH_SCRIPTS, interpreter_binary=_h("interp-b"))
    mapping = phase0_inventory.resolve_process_mapping(
        [_process(11764, 1, other)],
        [_label(11764, "com.fx-codex.dashboard")],
        approved_provenance={"com.fx-codex.dashboard": _approval(DASH_SCRIPTS)},
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert reason["reason"] == "interpreter_binary_sha256_mismatch"


def test_indeterminate_command_does_not_pass() -> None:
    """An unparsable command must fail, never be waved through."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(500, 1, _provenance([], status="indeterminate"))],
        [_label(500, "com.fx-codex.dashboard")],
        approved_provenance={"com.fx-codex.dashboard": _approval(DASH_SCRIPTS)},
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert reason["reason"] == "provenance_indeterminate"


def test_unlisted_label_is_rejected() -> None:
    """A label absent from the approval map cannot pass."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(900, 1, _provenance(DASH_SCRIPTS))],
        [_label(900, "com.fx-codex.newcomer")],
        approved_provenance={"com.fx-codex.dashboard": _approval(DASH_SCRIPTS)},
    )

    assert mapping["pass"] is False
    reason = cast(list[Any], cast(dict[str, Any], mapping["entrypoint_provenance"])["unapproved"])[
        0
    ]
    assert reason["reason"] == "label_not_approved"


def test_observation_mode_yields_none_not_true() -> None:
    """No approved values: collect provenance, judge nothing."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(63256, 1, _provenance(QUOTE_SCRIPTS))],
        [_label(63256, "com.fx-codex.quote-index")],
    )

    assert mapping["lineage_pass"] is True
    assert mapping["pass"] is None
    assert cast(dict[str, Any], mapping["entrypoint_provenance"])["verdict"] == "unverified"


def test_manually_started_process_is_unmapped() -> None:
    mapping = phase0_inventory.resolve_process_mapping(
        [
            _process(55195, 63592, _provenance(DASH_SCRIPTS)),
            _process(11764, 1, _provenance(DASH_SCRIPTS)),
        ],
        [_label(11764, "com.fx-codex.dashboard")],
        process_graph={55195: 63592, 63592: 1, 11764: 1},
        approved_provenance={"com.fx-codex.dashboard": _approval(DASH_SCRIPTS)},
    )

    assert mapping["unmapped_processes"] == 1
    assert mapping["pass"] is False
    assert cast(list[Any], mapping["unmapped"])[0]["pid"] == 55195


def test_ppid_cycle_terminates() -> None:
    mapping = phase0_inventory.resolve_process_mapping(
        [_process(10, 11), _process(11, 10)],
        [_label(99, "com.fx-codex.briefing")],
    )

    assert mapping["unmapped_processes"] == 2


def test_mapping_does_not_pin_literal_pid_values() -> None:
    first = phase0_inventory.resolve_process_mapping(
        [_process(63256, 1, _provenance(QUOTE_SCRIPTS))],
        [_label(63256, "com.fx-codex.quote-index")],
        approved_provenance={"com.fx-codex.quote-index": _approval(QUOTE_SCRIPTS)},
    )
    rotated = phase0_inventory.resolve_process_mapping(
        [_process(79718, 1, _provenance(QUOTE_SCRIPTS))],
        [_label(79718, "com.fx-codex.quote-index")],
        approved_provenance={"com.fx-codex.quote-index": _approval(QUOTE_SCRIPTS)},
    )

    assert first["pass"] == rotated["pass"] is True


def test_script_identity_separates_path_from_content(tmp_path: Path) -> None:
    """parent_path_sha256 is a path-string hash; file_sha256 is content."""

    script = tmp_path / "worker.py"
    script.write_text("original\n", encoding="utf-8")
    before = phase0_inventory._script_identity(str(script))
    script.write_text("modified\n", encoding="utf-8")
    after = phase0_inventory._script_identity(str(script))

    assert before["parent_path_sha256"] == after["parent_path_sha256"]
    assert before["file_sha256"] != after["file_sha256"]
    assert before["parent_path_sha256"] == _digest(str(tmp_path))


def test_unreadable_script_is_flagged_not_silently_null(tmp_path: Path) -> None:
    identity = phase0_inventory._script_identity(str(tmp_path / "missing.py"))

    assert identity["file_sha256"] is None
    assert identity["file_sha256_unavailable"] == "unreadable"


def test_entrypoint_provenance_records_wrapper_and_child_in_order(tmp_path: Path) -> None:
    wrapper = tmp_path / "run_exclusive.py"
    child = tmp_path / "quote_tape_index.py"
    wrapper.write_text("w\n", encoding="utf-8")
    child.write_text("c\n", encoding="utf-8")
    command = f"/usr/bin/python3.12 {wrapper} --name fx-quote-index -- {child} --db x"

    record = phase0_inventory._entrypoint_provenance(command, "/usr/bin/python3.12")

    assert record["parse_status"] == "parsed"
    assert record["script_count"] == 2
    scripts = cast(list[Any], record["scripts"])
    assert scripts[0]["basename"] == "run_exclusive.py"
    assert scripts[1]["basename"] == "quote_tape_index.py"
    assert scripts[0]["file_sha256"] != scripts[1]["file_sha256"]
    assert str(tmp_path) not in json.dumps(record)


def test_command_without_script_token_is_indeterminate() -> None:
    record = phase0_inventory._entrypoint_provenance(
        "/usr/bin/curl https://example.invalid", "/usr/bin/curl"
    )

    assert record["parse_status"] == "indeterminate"
    assert record["parse_reason"] == "no_script_token"


def test_macos_framework_interpreter_version_is_recovered_from_path(tmp_path: Path) -> None:
    """Real deployment shape: basename is ``Python`` with no version digits.

    Observed on trader-mini 2026-08-04; parsing only the basename reported
    every service as indeterminate.
    """

    binary = tmp_path / "Versions" / "3.12" / "Resources" / "Python.app" / "MacOS" / "Python"
    binary.parent.mkdir(parents=True)
    binary.write_text("interpreter\n", encoding="utf-8")

    identity = phase0_inventory._interpreter_identity(str(binary))

    assert identity["version"] == "3.12"
    assert identity["binary_sha256"] is not None


def test_framework_interpreter_command_parses(tmp_path: Path) -> None:
    binary = tmp_path / "Versions" / "3.12" / "MacOS" / "Python"
    binary.parent.mkdir(parents=True)
    binary.write_text("i\n", encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text("s\n", encoding="utf-8")

    record = phase0_inventory._entrypoint_provenance(f"{binary} {script} --port 8788", str(binary))

    assert record["parse_status"] == "parsed"
    assert cast(dict[str, Any], record["interpreter"])["version"] == "3.12"


def test_observer_classification_is_limited_to_this_runs_ancestry(monkeypatch) -> None:
    """Only the collector's own lineage is an observer; ssh alone is not."""

    monkeypatch.setattr(phase0_inventory.os, "getpid", lambda: 900)
    processes = [
        {"pid": 900, "ppid": 800, "executable_basename": "Python"},
        {"pid": 800, "ppid": 1, "executable_basename": "sshd-session"},
        {"pid": 49499, "ppid": 41598, "executable_basename": "ssh"},
    ]

    mapping = phase0_inventory.resolve_process_mapping(
        processes, [], process_graph={900: 800, 800: 1, 49499: 41598, 41598: 1}
    )

    observers = {row["pid"] for row in cast(list[Any], mapping["observer_processes"])}
    unmapped = {row["pid"] for row in cast(list[Any], mapping["unmapped"])}

    assert observers == {900, 800}
    assert unmapped == {49499}
    assert mapping["unmapped_processes"] == 1


def test_unknown_ssh_listener_and_writer_are_not_excused(monkeypatch) -> None:
    """Name-based exclusion would hide exactly these; ancestry must not."""

    monkeypatch.setattr(phase0_inventory.os, "getpid", lambda: 900)
    processes = [
        {"pid": 900, "ppid": 1, "executable_basename": "Python"},
        {"pid": 111, "ppid": 1, "executable_basename": "ssh"},
        {"pid": 222, "ppid": 1, "executable_basename": "Python"},
    ]

    mapping = phase0_inventory.resolve_process_mapping(
        processes, [], process_graph={900: 1, 111: 1, 222: 1}
    )

    assert {row["pid"] for row in cast(list[Any], mapping["unmapped"])} == {111, 222}
    assert mapping["lineage_pass"] is False


def test_observer_basis_is_recorded() -> None:
    mapping = phase0_inventory.resolve_process_mapping(
        [{"pid": phase0_inventory.os.getpid(), "ppid": 1, "executable_basename": "Python"}],
        [],
    )

    observers = cast(list[Any], mapping["observer_processes"])
    assert observers[0]["basis"] == "inventory_process_ancestry"


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _full_approval() -> dict[str, object]:
    return {
        "interpreter_binary_sha256": _SHA_A,
        "interpreter_realpath_sha256": _SHA_B,
        "scripts": [{"parent_path_sha256": _SHA_A, "file_sha256": _SHA_B}],
    }


def _policy_verdict(approval: object) -> dict[str, Any]:
    mapping = phase0_inventory.resolve_process_mapping(
        [_process(1, 1, _provenance(DASH_SCRIPTS))],
        [_label(1, "com.fx-codex.dashboard")],
        approved_provenance={"com.fx-codex.dashboard": cast(Any, approval)},
    )
    return cast(dict[str, Any], mapping["entrypoint_provenance"])


def test_partial_approval_is_policy_invalid_not_wildcard() -> None:
    """An omitted field must fail the policy, never match anything."""

    for dropped in ("interpreter_binary_sha256", "interpreter_realpath_sha256", "scripts"):
        approval = _full_approval()
        del approval[dropped]
        verdict = _policy_verdict(approval)

        assert verdict["verdict"] == "approval_policy_invalid", dropped
        assert any(e["field"] == dropped for e in verdict["policy_errors"]), dropped


def test_none_and_empty_approval_values_are_policy_invalid() -> None:
    for bad in (None, "", "not-a-sha", 123):
        approval = _full_approval()
        approval["interpreter_binary_sha256"] = cast(Any, bad)
        verdict = _policy_verdict(approval)

        assert verdict["verdict"] == "approval_policy_invalid", bad


def test_partial_script_entry_is_policy_invalid() -> None:
    approval = _full_approval()
    approval["scripts"] = [{"parent_path_sha256": _SHA_A}]
    verdict = _policy_verdict(approval)

    assert verdict["verdict"] == "approval_policy_invalid"
    assert verdict["policy_errors"][0]["field"] == "scripts[0].file_sha256"


def test_empty_scripts_list_is_policy_invalid() -> None:
    approval = _full_approval()
    approval["scripts"] = []

    assert _policy_verdict(approval)["verdict"] == "approval_policy_invalid"


def test_invalid_policy_never_yields_pass_true() -> None:
    """A malformed policy must not approve, even with matching processes."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(1, 1, _provenance(DASH_SCRIPTS))],
        [_label(1, "com.fx-codex.dashboard")],
        approved_provenance={"com.fx-codex.dashboard": cast(Any, {"scripts": []})},
    )

    assert mapping["pass"] is False


def test_ancestry_through_filtered_parent_still_maps() -> None:
    """A launchd chain passing through an out-of-scope ancestor must map."""

    graph = {700: 600, 600: 500, 500: 1}
    mapping = phase0_inventory.resolve_process_mapping(
        [_process(700, 600)],
        [_label(500, "com.fx-codex.briefing")],
        process_graph=graph,
    )

    assert mapping["unmapped_processes"] == 0
    assert cast(list[Any], mapping["mapped"])[0]["launchd_label"] == "com.fx-codex.briefing"


def test_broken_ancestry_is_indeterminate_not_unmapped() -> None:
    """A vanished parent yields an unknown verdict, not a clean failure."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(700, 600)],
        [_label(500, "com.fx-codex.briefing")],
        process_graph={},
    )

    assert mapping["unmapped_processes"] == 0
    assert mapping["indeterminate_processes"] == 1
    assert cast(list[Any], mapping["indeterminate"])[0]["reason"] == "ancestry_incomplete"
    assert mapping["lineage_pass"] is False


def test_chain_terminating_at_pid_one_is_unmapped_not_indeterminate() -> None:
    """Reaching init without a label is a real finding, not missing data."""

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(700, 1)],
        [_label(500, "com.fx-codex.briefing")],
        process_graph={700: 1},
    )

    assert mapping["unmapped_processes"] == 1
    assert mapping["indeterminate_processes"] == 0


def test_relative_script_token_is_indeterminate() -> None:
    """Without the target's cwd a relative path cannot be identified."""

    record = phase0_inventory._entrypoint_provenance(
        "/usr/bin/python3.12 tools/server.py --port 8788",
        "/usr/bin/python3.12",
    )

    assert record["parse_status"] == "indeterminate"
    assert record["parse_reason"] == "relative_script_token_without_cwd"


def test_chain_reaching_init_via_pid_zero_is_unmapped_not_indeterminate() -> None:
    """``ps`` reports pid 1's parent as 0; that is a complete chain.

    Observed on trader-mini 2026-08-04: 49499 -> 41598 -> 41597 -> 406 -> 1,
    where ``graph[1] == 0``.  Treating pid 0 as a missing parent misreported
    all six ssh clients as ancestry_incomplete.
    """

    mapping = phase0_inventory.resolve_process_mapping(
        [_process(49499, 41598)],
        [_label(11764, "com.fx-codex.dashboard")],
        process_graph={49499: 41598, 41598: 41597, 41597: 406, 406: 1, 1: 0},
    )

    assert mapping["indeterminate_processes"] == 0
    assert mapping["unmapped_processes"] == 1
    assert cast(list[Any], mapping["unmapped"])[0]["pid"] == 49499


def _ps_line(pid: int, ppid: int, command: str) -> str:
    return f" {pid} {ppid} Sat Aug  2 09:00:00 2026 {command}"


def _fake_ps(monkeypatch, lines: list[str]) -> None:
    monkeypatch.setattr(
        phase0_inventory,
        "_run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "\n".join(lines) + "\n", ""),
    )


def test_ssh_to_host_named_trader_is_out_of_scope(tmp_path: Path, monkeypatch) -> None:
    """``"trader"`` matches the *hostname*, not any fx-codex code.

    This substring pulled six ordinary outbound ssh clients into the
    2026-08-04 audit and made them look like unmapped fx-codex processes.
    """

    _fake_ps(monkeypatch, [_ps_line(49499, 41598, "ssh trader-mini")])

    rows, excluded = phase0_inventory._process_inventory(tmp_path)

    assert rows == []
    assert [row["pid"] for row in excluded] == [49499]
    assert excluded[0]["reason"] == "no_scope_evidence"


def test_ssh_holding_a_monitored_listener_stays_in_scope(tmp_path: Path, monkeypatch) -> None:
    """An ssh process that is a listener is evidence, not noise."""

    _fake_ps(monkeypatch, [_ps_line(600, 1, "ssh -L 8788:localhost:8788 trader-mini")])

    rows, _excluded = phase0_inventory._process_inventory(tmp_path, listener_pids=frozenset({600}))

    assert [row["pid"] for row in rows] == [600]
    assert "monitored_listener" in cast(list[Any], rows[0]["scope_evidence"])


def test_ssh_holding_a_monitored_writer_stays_in_scope(tmp_path: Path, monkeypatch) -> None:
    _fake_ps(monkeypatch, [_ps_line(601, 1, "ssh trader-mini")])

    rows, _excluded = phase0_inventory._process_inventory(tmp_path, writer_pids=frozenset({601}))

    assert "monitored_writer" in cast(list[Any], rows[0]["scope_evidence"])


def test_launchd_descendant_is_in_scope_without_any_keyword(tmp_path: Path, monkeypatch) -> None:
    """The label is stronger evidence than any command-line string."""

    _fake_ps(monkeypatch, [_ps_line(802, 801, "/bin/sleep 60")])

    rows, _excluded = phase0_inventory._process_inventory(
        tmp_path,
        launchd_pids=frozenset({800}),
        graph={802: 801, 801: 800, 800: 1},
    )

    assert [row["pid"] for row in rows] == [802]
    assert "launchd_descendant" in cast(list[Any], rows[0]["scope_evidence"])


def test_fx_entrypoint_is_in_scope(tmp_path: Path, monkeypatch) -> None:
    _fake_ps(
        monkeypatch, [_ps_line(900, 1, "/usr/bin/python3.12 /srv/x/tools/quote_tape_index.py")]
    )

    rows, _excluded = phase0_inventory._process_inventory(tmp_path)

    assert "fx_entrypoint" in cast(list[Any], rows[0]["scope_evidence"])


def test_out_of_scope_is_reported_separately_from_unmapped() -> None:
    """out_of_scope must not inflate the unmapped finding."""

    mapping = phase0_inventory.resolve_process_mapping(
        [],
        [],
        out_of_scope=[{"pid": 49499, "executable_basename": "ssh", "reason": "no_scope_evidence"}],
    )

    assert mapping["out_of_scope_processes"] == 1
    assert mapping["unmapped_processes"] == 0
    assert mapping["indeterminate_processes"] == 0
    assert mapping["lineage_pass"] is True


def test_unrelated_system_listener_is_not_scope_evidence(monkeypatch) -> None:
    """Only monitored ports count; ARDAgent:3283 and postgres:5432 do not.

    Observed on trader-mini 2026-08-04: treating any listening socket as
    evidence pulled both unrelated system services into the audit.
    """

    listing = "\n".join(
        [
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME",
            "ARDAgent 422 fuuki 10u IPv6 0x1 0t0 TCP *:3283 (LISTEN)",
            "postgres 747 fuuki 7u IPv6 0x2 0t0 TCP [::1]:5432 (LISTEN)",
            "Python 11764 fuuki 3u IPv4 0x3 0t0 TCP 100.118.242.40:8788 (LISTEN)",
        ]
    )
    monkeypatch.setattr(
        phase0_inventory,
        "_run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, listing + "\n", ""),
    )

    pids = phase0_inventory._listener_pids()

    assert pids == frozenset({11764})
