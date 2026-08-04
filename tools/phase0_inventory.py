#!/usr/bin/env python3
"""Build a secret-safe, read-only Phase 0 repository/runtime inventory.

The output is canonical JSON written to stdout.  The collector never prints
environment-variable values, git remote URLs, JSONL values, or SQLite row
contents.  It is intentionally usable through ``ssh ... python3 -`` so a
remote host can be observed without installing or changing repository files.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys

SCHEMA_VERSION = 2
CONTRACT = "fx-codex-phase0-inventory-v2"
SQLITE_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db"})
JSONL_ROOTS = ("logs", "collect")
SQLITE_ROOTS = ("logs", "collect", "runs")
RISK_NAME_RE = re.compile(
    r"(?:risk|policy|threshold|limit|stop|target|tp|sl|horizon|cost|label|promotion)",
    re.IGNORECASE,
)
SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\.|$)|id_(?:rsa|ed25519)|[^/]*\.(?:pem|key|p12|pfx))",
    re.IGNORECASE,
)
SENSITIVE_ARG_RE = re.compile(
    r"(?:token|secret|password|webhook|api[-_]?key|account[-_]?id)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|WEBHOOK|API_KEY|APIKEY|ACCOUNT_ID)[A-Za-z0-9_]*)=(?P<value>.*)$",
    re.IGNORECASE,
)
SENSITIVE_HEADER_RE = re.compile(
    r"^(?P<name>authorization|proxy-authorization|cookie|set-cookie)\s*:",
    re.IGNORECASE,
)
SENSITIVE_QUERY_RE = re.compile(
    r"(?P<prefix>[?&](?:token|secret|password|webhook|api[-_]?key|account[-_]?id)=)[^&\s]*",
    re.IGNORECASE,
)
TIMESTAMP_FIELDS = (
    "event_time",
    "provider_event_time",
    "prediction_time",
    "published_time",
    "available_time",
    "ingested_time",
    "received_at",
    "timestamp",
    "ts",
)
IGNORED_WALK_PARTS = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    }
)


class Phase0InventoryError(RuntimeError):
    """A required read-only inventory operation failed."""


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase0InventoryError(f"read-only command failed: {command[0]}") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise Phase0InventoryError(f"cannot hash inventory file: {path}") from error
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _file_identity(
    path: Path,
    *,
    root: Path,
    expose_path: bool = True,
) -> dict[str, object]:
    try:
        file_stat = path.lstat()
    except OSError as error:
        raise Phase0InventoryError(f"cannot stat inventory file: {path}") from error
    relative = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    sensitive_path = bool(SENSITIVE_PATH_RE.search(relative))
    identity: dict[str, object] = {
        "mode": oct(stat.S_IMODE(file_stat.st_mode)),
        "uid": file_stat.st_uid,
        "size_bytes": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "sensitive_path": sensitive_path,
    }
    if expose_path and not sensitive_path:
        identity["path"] = relative
    else:
        identity["path"] = "<sensitive-path>" if sensitive_path else "<path-hashed>"
        identity["path_sha256"] = _sha256_bytes(relative.encode("utf-8"))
    if stat.S_ISLNK(file_stat.st_mode):
        identity["kind"] = "symlink"
        if sensitive_path:
            identity["target_sha256_omitted"] = "sensitive_path"
        else:
            identity["target_sha256"] = _sha256_bytes(os.readlink(path).encode("utf-8"))
    elif stat.S_ISREG(file_stat.st_mode):
        identity["kind"] = "file"
        if sensitive_path:
            identity["sha256_omitted"] = "sensitive_path"
        else:
            identity["sha256"] = _sha256_file(path)
    elif stat.S_ISDIR(file_stat.st_mode):
        identity["kind"] = "directory"
    else:
        identity["kind"] = "other"
    return identity


def _git_inventory(root: Path) -> dict[str, object]:
    if not (root / ".git").exists():
        raise Phase0InventoryError(f"repository metadata is missing: {root}")
    head = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    branch = _run(["git", "branch", "--show-current"], cwd=root).stdout.strip()
    remote_names = sorted(
        value for value in _run(["git", "remote"], cwd=root).stdout.splitlines() if value
    )
    status_error: str | None = None
    try:
        status_text = _run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=root,
            timeout=30,
        ).stdout
    except Phase0InventoryError:
        status_text = ""
        status_error = "timeout_or_unavailable"
    entries: list[dict[str, object]] = []
    for raw_line in status_text.splitlines():
        if len(raw_line) < 4:
            continue
        code = raw_line[:2]
        raw_path = raw_line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        path = root / raw_path
        row: dict[str, object] = {
            "code": code,
            "path_sha256": _sha256_bytes(raw_path.encode("utf-8")),
            "sensitive_path": bool(SENSITIVE_PATH_RE.search(raw_path)),
        }
        if path.exists() or path.is_symlink():
            row["identity"] = _file_identity(path, root=root, expose_path=False)
        entries.append(row)
    diff: bytes | None = None
    if status_error is None:
        try:
            diff = _run(
                ["git", "diff", "--binary", "HEAD", "--", "."],
                cwd=root,
                timeout=30,
            ).stdout.encode("utf-8")
        except Phase0InventoryError:
            status_error = "diff_timeout_or_unavailable"
    recent_commits = [
        commit
        for commit in _run(["git", "log", "-10", "--format=%H"], cwd=root).stdout.splitlines()
        if commit
    ]
    worktrees: list[dict[str, object]] = []
    current: dict[str, object] = {}
    for line in _run(["git", "worktree", "list", "--porcelain"], cwd=root).stdout.splitlines() + [
        ""
    ]:
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"bare", "detached", "locked", "prunable"}:
            current[key] = True
        elif key == "worktree":
            current["worktree_sha256"] = _sha256_bytes(value.encode("utf-8"))
            current["is_repository_root"] = Path(value).resolve() == root
        elif key == "branch":
            branch_name = value.removeprefix("refs/heads/")
            current["branch_sha256"] = _sha256_bytes(branch_name.encode("utf-8"))
        else:
            current[key] = value
    counts = (
        {
            "tracked": sum(row["code"] != "??" for row in entries),
            "untracked": sum(row["code"] == "??" for row in entries),
            "deleted": sum("D" in str(row["code"]) for row in entries),
            "sensitive_paths": sum(bool(row["sensitive_path"]) for row in entries),
        }
        if status_error is None
        else None
    )
    return {
        "head": head,
        "branch_sha256": _sha256_bytes(branch.encode("utf-8")) if branch else None,
        "remote_name_sha256s": [_sha256_bytes(name.encode("utf-8")) for name in remote_names],
        "status_counts": counts,
        "status_entries": entries,
        "status_error": status_error,
        "recent_commits": recent_commits,
        "worktrees": worktrees,
        "tracked_diff_size_bytes": len(diff) if diff is not None else None,
        "tracked_diff_sha256": _sha256_bytes(diff) if diff is not None else None,
        "clean": not entries if status_error is None else None,
    }


def _runtime_environment_inventory(root: Path) -> dict[str, object]:
    # Preserve the virtual-environment entry point. Resolving the symlink before
    # ``python -m pip check`` would silently escape the venv on macOS.
    executable = Path(sys.executable).absolute()
    dependency_files = []
    for name in (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
    ):
        path = root / name
        if path.is_file():
            dependency_files.append(_file_identity(path, root=root))
    packages = sorted(
        (
            {
                "name": distribution.metadata["Name"] or distribution.name,
                "version": distribution.version,
            }
            for distribution in metadata.distributions()
        ),
        key=lambda row: (str(row["name"]).lower(), str(row["version"])),
    )
    pip_result: dict[str, object]
    try:
        pip_check = _run([str(executable), "-m", "pip", "check"], check=False, timeout=30)
    except Phase0InventoryError:
        pip_result = {
            "pip_check_ok": None,
            "pip_check_output_sha256": None,
            "pip_check_error": "timeout_or_unavailable",
        }
    else:
        pip_check_output = (pip_check.stdout + pip_check.stderr).encode("utf-8")
        pip_result = {
            "pip_check_ok": pip_check.returncode == 0,
            "pip_check_output_sha256": _sha256_bytes(pip_check_output),
            "pip_check_error": None,
        }
    result: dict[str, object] = {
        "python_executable": _file_identity(executable, root=root),
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "dependency_files": dependency_files,
        "installed_packages": packages,
        "installed_packages_sha256": _canonical_sha256(packages),
    }
    result.update(pip_result)
    return result


def _redact_arguments(arguments: Iterable[object]) -> list[str]:
    values = [str(value) for value in arguments]
    redacted: list[str] = []
    redact_next = False
    for value in values:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        assignment = SENSITIVE_ASSIGNMENT_RE.match(value)
        if assignment is not None:
            redacted.append(f'{assignment.group("name")}=<redacted>')
            continue
        header = SENSITIVE_HEADER_RE.match(value)
        if header is not None:
            redacted.append(f'{header.group("name")}: <redacted>')
            continue
        if value.startswith("--") and SENSITIVE_ARG_RE.search(value):
            if "=" in value:
                redacted.append(value.split("=", 1)[0] + "=<redacted>")
            else:
                redacted.append(value)
                redact_next = True
            continue
        sanitized = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<redacted>@", value)
        sanitized = SENSITIVE_QUERY_RE.sub(r"\g<prefix><redacted>", sanitized)
        sanitized = re.sub(
            r"(?i)(https?://[^/\s]+/api/webhooks/)[^\s]+",
            r"\1<redacted>",
            sanitized,
        )
        redacted.append(sanitized)
    return redacted


def _launchd_inventory(home: Path) -> list[dict[str, object]]:
    directory = home / "Library" / "LaunchAgents"
    rows: list[dict[str, object]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("com.fx-codex*.plist")):
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as error:
            rows.append({"path": str(path), "error": type(error).__name__})
            continue
        arguments = [str(value) for value in payload.get("ProgramArguments", ())]
        safe_view: dict[str, object] = {
            "label": payload.get("Label"),
            "path": str(path),
            "executable_basename": Path(arguments[0]).name if arguments else None,
            "argument_count": max(len(arguments) - 1, 0),
            "working_directory": payload.get("WorkingDirectory"),
            "start_interval": payload.get("StartInterval"),
            "start_calendar_interval": payload.get("StartCalendarInterval"),
            "run_at_load": payload.get("RunAtLoad"),
            "keep_alive": payload.get("KeepAlive"),
            "stdout": payload.get("StandardOutPath"),
            "stderr": payload.get("StandardErrorPath"),
            "environment_variable_names": sorted(
                str(name) for name in payload.get("EnvironmentVariables", {})
            ),
        }
        safe_view["safe_view_sha256"] = _canonical_sha256(safe_view)
        rows.append(safe_view)
    return rows


def _launchctl_inventory() -> list[dict[str, object]]:
    result = _run(["launchctl", "list"], check=False)
    if result.returncode != 0:
        return [{"error": "launchctl_unavailable"}]
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3 or "fx-codex" not in fields[2]:
            continue
        rows.append(
            {
                "pid": None if fields[0] == "-" else int(fields[0]),
                "last_exit_status": None if fields[1] == "-" else int(fields[1]),
                "label": fields[2],
            }
        )
    return sorted(rows, key=lambda row: str(row["label"]))


INTERPRETER_RE = re.compile(r"(?:^|/)python(?P<version>\d+(?:\.\d+)*)?$", re.IGNORECASE)
VERSIONED_PATH_RE = re.compile(r"/(?:Versions|python@)/?(?P<version>\d+\.\d+(?:\.\d+)*)/")


def _script_identity(token: str) -> dict[str, object]:
    """Identify one script by location *and* content.

    ``parent_path_sha256`` hashes the **parent path string**, not the
    directory's contents -- it answers "where does this live", never "is this
    directory unmodified".  ``file_sha256`` hashes the file's bytes and is what
    detects code edited in place inside an otherwise approved directory.  Both
    are required: the path digest alone accepts any tampering that preserves
    location.
    """

    path = Path(token)
    identity: dict[str, object] = {
        "parent_path_sha256": _sha256_bytes(str(path.parent).encode("utf-8")),
        "basename": path.name,
        "file_sha256": None,
        "file_sha256_unavailable": "not_read",
    }
    try:
        resolved = path.resolve(strict=True)
        identity["file_sha256"] = _sha256_file(resolved)
        identity["file_sha256_unavailable"] = None
    except (OSError, Phase0InventoryError):
        identity["file_sha256_unavailable"] = "unreadable"
    return identity


def _interpreter_identity(token: str) -> dict[str, object]:
    """Identify the interpreter by realpath and binary content.

    A version string cannot separate two different 3.12 builds (a Homebrew
    Python and a frozen-runtime Python report the same version), so the
    realpath digest and binary hash are what distinguish them.
    """

    path = Path(token)
    identity: dict[str, object] = {
        "version": None,
        "realpath_sha256": None,
        "binary_sha256": None,
        "binary_sha256_unavailable": "not_read",
    }
    resolved: Path | None = None
    try:
        resolved = path.resolve(strict=True)
        identity["realpath_sha256"] = _sha256_bytes(str(resolved).encode("utf-8"))
        identity["binary_sha256"] = _sha256_file(resolved)
        identity["binary_sha256_unavailable"] = None
    except (OSError, Phase0InventoryError):
        identity["binary_sha256_unavailable"] = "unreadable"

    # macOS framework builds expose the interpreter as ``.../MacOS/Python``,
    # whose basename carries no version.  Fall back to the versioned path
    # component (``.../Versions/3.12/...``) before giving up, so a real
    # deployment is not reported as unversioned.
    for candidate in (path.name, *((str(resolved),) if resolved else ()), token):
        match = INTERPRETER_RE.search(Path(str(candidate)).name)
        if match and match.group("version"):
            identity["version"] = match.group("version")
            break
        version_component = VERSIONED_PATH_RE.search(str(candidate))
        if version_component:
            identity["version"] = version_component.group("version")
            break
    return identity


def _entrypoint_provenance(raw_command: str, executable_token: str) -> dict[str, object]:
    """Identify the *entrypoint* scripts and interpreter, without exporting paths.

    **Scope of guarantee.**  This covers only the interpreter binary and the
    script tokens named on the command line.  It does **not** cover the modules
    those scripts import: ``sys.path`` entries, installed distributions,
    ``.pth`` files, ``PYTHONPATH``, and editable installs can all substitute
    code that never appears in ``ps`` output.  A matching entrypoint digest
    therefore proves the entrypoint is approved, not that the whole running
    program is.  Import-closure verification is a separate, unimplemented
    control; do not read a passing verdict as covering it.

    Lineage answers who started a process; it cannot answer whether the code is
    the approved build.  A dirty checkout started *under* launchd is mapped by
    ancestry yet is exactly the condition Gate 0 exists to catch.

    Wrappers matter: ``run_exclusive.py --name X -- <child>`` puts two scripts
    on one command line, and approving only one of them leaves the other
    unchecked.  Every script token is therefore identified, in order, and the
    caller must approve the whole sequence.

    A command this parser cannot decompose is reported as ``parse_status:
    "indeterminate"`` and must never be treated as approved.

    Only digests, a version string, and booleans are emitted; the collector's
    contract forbids exporting paths.
    """

    tokens = [token.strip("'\"") for token in raw_command.split()]
    scripts = [token for token in tokens[1:] if token.endswith((".py", ".sh"))]
    interpreter = _interpreter_identity(executable_token)

    if not scripts:
        parse_status = "indeterminate"
        reason: str | None = "no_script_token"
    elif interpreter["version"] is None and interpreter["binary_sha256"] is None:
        # Neither a parseable version nor a readable binary: nothing identifies
        # what actually executed, so this must not be approvable.
        parse_status = "indeterminate"
        reason = "unrecognised_interpreter"
    else:
        parse_status = "parsed"
        reason = None

    return {
        "parse_status": parse_status,
        "parse_reason": reason,
        "interpreter": interpreter,
        "script_count": len(scripts),
        "scripts": [_script_identity(token) for token in scripts],
    }


def _process_inventory(root: Path) -> list[dict[str, object]]:
    result = _run(["ps", "ax", "-o", "pid=,ppid=,lstart=,command="])
    patterns = (
        "fx-codex",
        "fx_briefing",
        "fx_tf_snapshot",
        "fx_quote_collector",
        "virtual_portfolio",
        "tv_discord_notify",
        "trader",
        "executor",
    )
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        if not any(pattern in line for pattern in patterns):
            continue
        if "phase0_inventory.py" in line or re.search(r"\bpython\S*\s+-\s+--root\s+", line):
            continue
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.{24})\s+(.*)$", line)
        if match is None:
            continue
        raw_command = match.group(4)
        executable_token = raw_command.lstrip().partition(" ")[0].strip("'\"")
        safe_view: dict[str, object] = {
            "pid": int(match.group(1)),
            "ppid": int(match.group(2)),
            "started": match.group(3).strip(),
            "executable_basename": Path(executable_token).name,
            "command_present": bool(raw_command),
            "argument_count_approximate": max(len(raw_command.split()) - 1, 0),
            "matched_process_kinds": sorted(
                pattern for pattern in patterns if pattern in raw_command
            ),
            "references_runtime_root": str(root) in raw_command,
            "entrypoint_provenance": _entrypoint_provenance(raw_command, executable_token),
        }
        safe_view["safe_view_sha256"] = _canonical_sha256(safe_view)
        rows.append(safe_view)
    return rows


def _observer_pid_chain(by_pid: Mapping[int, Mapping[str, object]]) -> frozenset[int]:
    """Return the pids of *this* inventory run's own ancestry.

    Only the collector process and the pids reachable by walking its ``ppid``
    links qualify.  Classification is never by executable name: excluding every
    ``ssh`` process would also excuse an unknown ssh session, a rogue listener,
    or an unexpected writer that happened to be named the same.  On
    trader-mini the six observed ``ssh`` processes are *outbound* clients that
    are not in this run's ancestry, so they stay ``unmapped``.

    A pid appears here only if it is also present in ``by_pid``; ancestry
    outside the collector's own filter is not synthesised into the result.
    """

    chain: set[int] = set()
    cursor: int | None = os.getpid()
    seen: set[int] = set()
    while isinstance(cursor, int) and cursor not in seen:
        seen.add(cursor)
        if cursor in by_pid:
            chain.add(cursor)
        row = by_pid.get(cursor)
        parent = row.get("ppid") if row else None
        if isinstance(parent, int):
            cursor = parent
            continue
        try:
            cursor = os.getppid() if cursor == os.getpid() else None
        except OSError:  # pragma: no cover - platform guard
            cursor = None
    return frozenset(chain)


def resolve_process_mapping(
    processes: Iterable[Mapping[str, object]],
    launchctl: Iterable[Mapping[str, object]],
    *,
    approved_provenance: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Decide which observed processes are accounted for by launchd.

    A process is ``mapped`` when its pid is a loaded launchd label's pid, or
    when walking ``ppid`` links reaches one.  Descendants matter because
    ``run_exclusive.py`` acquires a lock and then execs the real worker as a
    child, so the worker's pid never appears in ``launchctl list``.  Judging on
    direct pid equality alone reports that healthy child as unmapped; that
    false positive blocked Gate 0 on 2026-08-03 (pid 63257 under 63256).

    Pids are not stable: launchd assigns new ones every cycle.  Callers must
    treat the ``unmapped`` list as the finding and never pin an expectation to
    a literal pid value.

    Only pids present in ``processes`` are walked, so a chain leading to a
    process this collector filtered out (its own ssh/python invocation, for
    example) terminates without a match rather than borrowing a mapping.

    Lineage alone is **not** sufficient for Gate 0.  It answers who started a
    process, not what code that process runs, so a dirty checkout launched
    under an approved label is mapped yet unapproved -- the mirror image of the
    2026-08-03 false positive.  ``pass`` therefore additionally requires every
    mapped process to match the indivisible per-label tuple in
    ``approved_provenance``.  When it is omitted the provenance verdict is
    ``unverified`` and ``pass`` is ``None``: unknown, never true.

    Omitting it is the intended **observation mode** -- run first with no
    approved values to collect candidate provenance for every service while
    ``pass`` stays ``None``.  Observing a value is not approving it; approval
    is a separate human decision made against release manifests, SHAs, tree
    identity, and dirty state.
    """

    label_pids: dict[int, str] = {}
    for row in launchctl:
        label_pid = row.get("pid")
        label_name = row.get("label")
        if isinstance(label_pid, int) and isinstance(label_name, str):
            label_pids[label_pid] = label_name

    by_pid: dict[int, Mapping[str, object]] = {}
    for row in processes:
        pid = row.get("pid")
        if isinstance(pid, int):
            by_pid[pid] = row

    observer_pids = _observer_pid_chain(by_pid)

    mapped: list[dict[str, object]] = []
    unmapped: list[dict[str, object]] = []
    observers: list[dict[str, object]] = []
    for pid in sorted(by_pid):
        row = by_pid[pid]
        chain: list[int] = []
        cursor: int | None = pid
        label: str | None = None
        seen: set[int] = set()
        while isinstance(cursor, int) and cursor not in seen:
            seen.add(cursor)
            if cursor in label_pids:
                label = label_pids[cursor]
                break
            chain.append(cursor)
            parent = by_pid.get(cursor, {}).get("ppid")
            cursor = parent if isinstance(parent, int) else None

        entry: dict[str, object] = {
            "pid": pid,
            "ppid": row.get("ppid"),
            "executable_basename": row.get("executable_basename"),
        }
        if label is None and pid in observer_pids:
            observers.append({**entry, "basis": "inventory_process_ancestry"})
        elif label is None:
            unmapped.append(entry)
        else:
            mapped.append({**entry, "launchd_label": label, "ancestor_hops": len(chain)})

    provenance = _resolve_entrypoint_provenance(
        mapped,
        by_pid,
        approved_provenance=approved_provenance,
    )
    lineage_pass = not unmapped
    provenance_verdict = provenance["verdict"]
    if provenance_verdict == "unverified":
        overall: bool | None = None
    else:
        overall = lineage_pass and provenance_verdict == "approved"

    return {
        "rule": (
            "a process is mapped when its pid, or any pid reachable by walking "
            "ppid links through observed processes, is a loaded launchd label's "
            "pid; pid values are not stable across cycles"
        ),
        "provenance_rule": (
            "lineage establishes who started a process, not what code it runs; "
            "Gate 0 additionally requires every mapped process to run an "
            "approved entrypoint directory and interpreter version"
        ),
        "observer_rule": (
            "only this inventory run's own pid ancestry is classified as an "
            "observer; executable name is never a basis, so unknown ssh "
            "sessions, listeners and writers remain unmapped"
        ),
        "mapped": mapped,
        "observer_processes": observers,
        "unmapped": unmapped,
        "unmapped_processes": len(unmapped),
        "loaded_label_pids": len(label_pids),
        "lineage_pass": lineage_pass,
        "entrypoint_provenance": provenance,
        "pass": overall,
    }


def _resolve_entrypoint_provenance(
    mapped: Iterable[Mapping[str, object]],
    by_pid: Mapping[int, Mapping[str, object]],
    *,
    approved_provenance: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, object]:
    """Check that launchd-mapped processes run approved code.

    ``approved_provenance`` maps a launchd label to one **indivisible** tuple::

        {"com.fx-codex.quote-index": {
            "interpreter_binary_sha256": "...",
            "interpreter_realpath_sha256": "...",
            "scripts": [{"parent_path_sha256": "...", "file_sha256": "..."}, ...],
        }}

    Matching is per label and positional across ``scripts``.  A value approved
    for one service is therefore not accepted for another, and a wrapper's
    approved digest cannot stand in for its child.  Independent global sets
    would permit exactly that substitution.
    """

    if approved_provenance is None:
        return {
            "verdict": "unverified",
            "reason": "approved_provenance not supplied",
            "unapproved": [],
            "unapproved_processes": 0,
        }

    unapproved: list[dict[str, object]] = []
    for entry in mapped:
        pid = entry.get("pid")
        label = entry.get("launchd_label")
        row = by_pid.get(pid) if isinstance(pid, int) else None
        record = row.get("entrypoint_provenance") if row else None
        base: dict[str, object] = {"pid": pid, "launchd_label": label}

        if not isinstance(record, Mapping):
            unapproved.append({**base, "reason": "provenance_missing"})
            continue
        if record.get("parse_status") != "parsed":
            unapproved.append(
                {
                    **base,
                    "reason": "provenance_indeterminate",
                    "parse_reason": record.get("parse_reason"),
                }
            )
            continue
        if not isinstance(label, str) or label not in approved_provenance:
            unapproved.append({**base, "reason": "label_not_approved"})
            continue

        expected = approved_provenance[label]
        reasons: list[str] = []
        interpreter = record.get("interpreter")
        interpreter = interpreter if isinstance(interpreter, Mapping) else {}
        for field, key in (
            ("binary_sha256", "interpreter_binary_sha256"),
            ("realpath_sha256", "interpreter_realpath_sha256"),
        ):
            wanted = expected.get(key)
            if wanted is not None and interpreter.get(field) != wanted:
                reasons.append(f"{key}_mismatch")

        expected_scripts = expected.get("scripts")
        observed_scripts = record.get("scripts")
        if not isinstance(expected_scripts, (list, tuple)) or not isinstance(
            observed_scripts, (list, tuple)
        ):
            reasons.append("scripts_not_comparable")
        elif len(expected_scripts) != len(observed_scripts):
            reasons.append("script_count_mismatch")
        else:
            for index, (want, seen) in enumerate(zip(expected_scripts, observed_scripts)):
                if not isinstance(want, Mapping) or not isinstance(seen, Mapping):
                    reasons.append(f"script_{index}_not_comparable")
                    continue
                for key in ("parent_path_sha256", "file_sha256"):
                    wanted = want.get(key)
                    if wanted is not None and seen.get(key) != wanted:
                        reasons.append(f"script_{index}_{key}_mismatch")

        if reasons:
            unapproved.append({**base, "reason": ",".join(reasons)})

    return {
        "verdict": "approved" if not unapproved else "unapproved",
        "unapproved": unapproved,
        "unapproved_processes": len(unapproved),
    }


def _cron_inventory() -> dict[str, object]:
    result = _run(["crontab", "-l"], check=False)
    if result.returncode != 0:
        return {"available": False, "entry_count": 0, "sha256": None}
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {
        "available": True,
        "entry_count": len(lines),
        "sha256": _sha256_bytes(result.stdout.encode("utf-8")),
    }


def _docker_inventory() -> dict[str, object]:
    if shutil.which("docker") is None:
        return {"available": False, "containers": []}
    result = _run(
        ["docker", "ps", "--format", "{{json .}}"],
        check=False,
    )
    if result.returncode != 0:
        return {"available": False, "containers": []}
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(
            {
                "id": payload.get("ID"),
                "image": payload.get("Image"),
                "names": payload.get("Names"),
                "command_present": bool(payload.get("Command")),
                "status": payload.get("Status"),
            }
        )
    return {"available": True, "containers": rows}


def _sqlite_components(path: Path, *, root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for suffix in ("", "-wal", "-shm"):
        component = Path(str(path) + suffix)
        if not component.exists() and not component.is_symlink():
            continue
        try:
            rows.append(_file_identity(component, root=root))
        except Phase0InventoryError as error:
            rows.append(
                {
                    "path": str(component.relative_to(root)),
                    "error": type(error).__name__,
                }
            )
    return rows


def _sqlite_inventory(path: Path, *, root: Path) -> dict[str, object]:
    components_before = _sqlite_components(path, root=root)
    if not components_before:
        return {"path": str(path.relative_to(root)), "error": "missing"}
    result: dict[str, object] = dict(components_before[0])
    result["components"] = components_before
    durable_before = [
        component
        for component in components_before
        if not str(component.get("path", "")).endswith("-shm")
    ]
    result["durable_components_sha256"] = _canonical_sha256(durable_before)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("BEGIN")
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        schema_payload = json.dumps(
            objects,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tables: list[dict[str, object]] = []
        for object_type, name, _sql in objects:
            if object_type != "table":
                continue
            try:
                count = int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{str(name).replace(chr(34), chr(34) * 2)}"'
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                count = None
            tables.append({"name": name, "rows": count})
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        connection.close()
        components_after = _sqlite_components(path, root=root)
        durable_after = [
            component
            for component in components_after
            if not str(component.get("path", "")).endswith("-shm")
        ]
        durable_hashes_available = all(
            component.get("kind") == "file" and "sha256" in component
            for component in (*durable_before, *durable_after)
        )
        stable_components = durable_hashes_available and durable_before == durable_after
        result.update(
            {
                "integrity_check": integrity,
                "schema_sha256": _sha256_bytes(schema_payload),
                "objects": [
                    {"type": object_type, "name": name} for object_type, name, _sql in objects
                ],
                "tables": tables,
                "observation_consistency": (
                    "stable_component_set"
                    if stable_components
                    else (
                        "non_atomic_live_store"
                        if durable_hashes_available
                        else "component_hash_unavailable"
                    )
                ),
                "row_counts_reproducible_from_component_hashes": stable_components,
                "durable_components_after_sha256": _canonical_sha256(durable_after),
                "shm_observed": any(
                    str(component.get("path", "")).endswith("-shm")
                    for component in components_before
                ),
            }
        )
    except (OSError, sqlite3.Error) as error:
        result["error"] = type(error).__name__
    return result


def _jsonl_inventory(path: Path, *, root: Path) -> dict[str, object]:
    identity = _file_identity(path, root=root)
    digest = hashlib.sha256()
    rows = 0
    malformed = 0
    non_objects = 0
    naive_timestamps = 0
    future_availability = 0
    top_level_keys: set[str] = set()
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    reversals = 0
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                rows += 1
                try:
                    payload = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed += 1
                    continue
                if not isinstance(payload, Mapping):
                    non_objects += 1
                    continue
                top_level_keys.update(str(key) for key in payload)
                parsed_times: dict[str, datetime] = {}
                for name in TIMESTAMP_FIELDS:
                    value = payload.get(name)
                    if not isinstance(value, str):
                        continue
                    try:
                        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        naive_timestamps += 1
                        continue
                    parsed_times[name] = parsed.astimezone(UTC)
                primary = next(
                    (parsed_times[name] for name in TIMESTAMP_FIELDS if name in parsed_times),
                    None,
                )
                if primary is not None:
                    first_timestamp = (
                        primary if first_timestamp is None else min(first_timestamp, primary)
                    )
                    last_timestamp = (
                        primary if last_timestamp is None else max(last_timestamp, primary)
                    )
                    if previous_timestamp is not None and primary < previous_timestamp:
                        reversals += 1
                    previous_timestamp = primary
                event = parsed_times.get("event_time") or parsed_times.get("provider_event_time")
                available = parsed_times.get("available_time") or parsed_times.get("received_at")
                if event is not None and available is not None and event > available:
                    future_availability += 1
    except OSError as error:
        raise Phase0InventoryError(f"cannot read JSONL inventory file: {path}") from error
    identity.update(
        {
            "sha256": digest.hexdigest(),
            "rows": rows,
            "malformed_rows": malformed,
            "non_object_rows": non_objects,
            "naive_timestamp_fields": naive_timestamps,
            "timestamp_reversals": reversals,
            "event_after_availability_rows": future_availability,
            "first_primary_timestamp": (
                first_timestamp.isoformat() if first_timestamp is not None else None
            ),
            "last_primary_timestamp": (
                last_timestamp.isoformat() if last_timestamp is not None else None
            ),
            "top_level_keys": sorted(top_level_keys),
        }
    )
    return identity


def _walk_files(base: Path) -> Iterable[Path]:
    if not base.exists():
        return ()
    return (
        path
        for path in base.rglob("*")
        if path.is_file() and not any(part in IGNORED_WALK_PARTS for part in path.parts)
    )


def _data_store_inventory(root: Path) -> dict[str, object]:
    sqlite_paths: set[Path] = set()
    jsonl_paths: set[Path] = set()
    for relative in SQLITE_ROOTS:
        for path in _walk_files(root / relative):
            if path.suffix.lower() in SQLITE_SUFFIXES:
                sqlite_paths.add(path)
    for relative in JSONL_ROOTS:
        for path in _walk_files(root / relative):
            if path.suffix.lower() == ".jsonl":
                jsonl_paths.add(path)
    return {
        "sqlite": [_sqlite_inventory(path, root=root) for path in sorted(sqlite_paths)],
        "jsonl": [_jsonl_inventory(path, root=root) for path in sorted(jsonl_paths)],
    }


def _risk_config_inventory(root: Path) -> list[dict[str, object]]:
    candidates: set[Path] = set()
    for relative in ("config", "ops", "experiments"):
        for path in _walk_files(root / relative):
            rel = str(path.relative_to(root))
            if RISK_NAME_RE.search(rel) and path.suffix.lower() in {
                ".json",
                ".yaml",
                ".yml",
                ".toml",
            }:
                candidates.add(path)
    rows: list[dict[str, object]] = []
    for path in sorted(candidates):
        row = _file_identity(path, root=root)
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                row["json_valid"] = False
            else:
                row["json_valid"] = True
                row["top_level_keys"] = (
                    sorted(str(key) for key in payload) if isinstance(payload, Mapping) else []
                )
        rows.append(row)
    return rows


def _environment_file_inventory(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for path in sorted(root.glob(".env*")):
        if not path.is_file() and not path.is_symlink():
            continue
        row = _file_identity(path, root=root)
        key_names: set[str] = set()
        if path.is_symlink():
            row["key_names_unavailable"] = "symlink_not_followed"
        else:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        match = assignment.match(line)
                        if match is not None:
                            key_names.add(match.group(1))
            except OSError as error:
                row["key_names_unavailable"] = type(error).__name__
        row["key_names"] = sorted(key_names)
        rows.append(row)
    return rows


def build_inventory(root: Path, *, home: Path | None = None) -> dict[str, object]:
    resolved = root.expanduser().resolve(strict=True)
    host_home = (home or Path.home()).expanduser().resolve(strict=True)
    git = _git_inventory(resolved)
    launchd_plists = _launchd_inventory(host_home)
    launchctl = _launchctl_inventory()
    processes = _process_inventory(resolved)
    runtime = {
        "launchctl": launchctl,
        "processes": processes,
        "process_mapping": resolve_process_mapping(processes, launchctl),
        "cron": _cron_inventory(),
        "docker": _docker_inventory(),
    }
    payload: dict[str, object] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "hostname": os.uname().nodename,
        "repository_root": str(resolved),
        "repository_root_kind": "symlink" if root.expanduser().is_symlink() else "directory",
        "git": git,
        "runtime_environment": _runtime_environment_inventory(resolved),
        "launchd_plists": launchd_plists,
        "runtime": runtime,
        "data_stores": _data_store_inventory(resolved),
        "risk_configs": _risk_config_inventory(resolved),
        "environment_files": _environment_file_inventory(resolved),
        "safety": {
            "analysis_only_required": True,
            "runtime_order_surface_verified": False,
            "runtime_order_surface_basis": "requires separate structural tests and process audit",
            "secret_values_exported": False,
        },
    }
    payload["inventory_sha256"] = _canonical_sha256(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--home", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inventory = build_inventory(args.root, home=args.home)
    except (OSError, Phase0InventoryError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            inventory,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
