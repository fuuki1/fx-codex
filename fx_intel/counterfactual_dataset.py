"""Immutable counterfactual dataset artifacts.

A reconstructed population is only useful as evidence if it can be pointed at
later and shown to be the same population. This module freezes one reconstruction
run into a directory that cannot be edited in place: every file is written
read-only under ``O_EXCL`` into a staging directory and published by a single
atomic rename, and the run id embeds the dataset hash.

The layout mirrors ``tools/virtual_portfolio_learning.py`` so the two kinds of run
can be audited the same way:

    runs/counterfactual_dataset/<run_id>/
        manifest.json          run identity, window, policies, git binding
        dataset.jsonl          one row per decision, all three actions valued
        dataset_manifest.json  per-row hashes and the unavailable-reason ledger
        label_schema.json      what each label means and how it was produced
        completion.json        sha256 of every file in the run

Nothing here is measured evidence and nothing here is promotable: the rows are
reconstructions against a ``tradable=false`` historical tape, so every artifact
carries that provenance forward.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .counterfactual_labels import COUNTERFACTUAL_CONTRACT, LABEL_SOURCE

DATASET_CONTRACT = "virtual-decision-counterfactual-dataset-v1"
LABEL_SCHEMA_CONTRACT = "virtual-decision-counterfactual-label-schema-v1"
COMPLETION_CONTRACT = "virtual-decision-counterfactual-run-completion-v1"

_ARTIFACT_MODE = 0o444


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _git_binding(root: Path) -> tuple[str, bool, str | None]:
    """Record the source commit, whether the tree was dirty, and what differed.

    A dataset produced from a dirty tree cannot be rebuilt from its commit
    alone. ``working_tree_diff_sha256`` pins the exact difference so a later
    reader can tell whether two runs came from identical producing code.
    """

    def _run(*args: str) -> str:
        try:
            return subprocess.run(
                args, cwd=root, capture_output=True, text=True, timeout=10, check=False
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = _run("git", "rev-parse", "HEAD")
    status = _run("git", "status", "--porcelain")
    dirty = bool(status)
    if not dirty:
        return commit, False, None
    # A dirty tree makes the commit alone insufficient to rebuild the producing
    # code, so pin the difference too. `git diff HEAD` covers tracked edits;
    # untracked files never appear there, so hash their content separately and
    # fold both into one digest. The result identifies the working tree, not just
    # the commit it was based on.
    tracked = _run("git", "diff", "HEAD")
    untracked_names = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
    untracked: list[tuple[str, str]] = []
    for name in sorted(untracked_names):
        target = root / name
        if target.is_file():
            try:
                untracked.append((name, hashlib.sha256(target.read_bytes()).hexdigest()))
            except OSError:
                untracked.append((name, "unreadable"))
        elif target.is_dir():
            # Directories appear as a single porcelain entry; hash their contents.
            for child in sorted(p for p in target.rglob("*") if p.is_file()):
                try:
                    digest = hashlib.sha256(child.read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
                untracked.append((str(child.relative_to(root)), digest))
    diff_sha = _sha256({"tracked_diff": tracked, "untracked": untracked})
    return commit, True, diff_sha


def build_label_schema() -> dict[str, object]:
    """Describe every label so a later reader need not infer its meaning."""

    return {
        "contract": LABEL_SCHEMA_CONTRACT,
        "label_source": LABEL_SOURCE,
        "measured": False,
        "actions": ["long", "short", "abstain"],
        "labels": {
            "long_net_r": "reconstructed net R had LONG been taken, after modeled cost",
            "short_net_r": "reconstructed net R had SHORT been taken, after modeled cost",
            "abstain_net_r": "identically 0.0; taking no position realises nothing",
            "long_cost_r": "modeled cost in R charged to the LONG reconstruction",
            "short_cost_r": "modeled cost in R charged to the SHORT reconstruction",
            "long_mfe_r": "maximum favorable excursion in R for LONG, before its exit",
            "long_mae_r": "maximum adverse excursion in R for LONG, before its exit",
            "short_mfe_r": "maximum favorable excursion in R for SHORT, before its exit",
            "short_mae_r": "maximum adverse excursion in R for SHORT, before its exit",
            "long_close_reason": "stop_loss | target1 | time_exit",
            "short_close_reason": "stop_loss | target1 | time_exit",
        },
        "gross_is_derivable": "gross_r = net_r + cost_r for each action",
        "entry_rule": (
            "first tick at or after decision_time and within quote_max_age_seconds; "
            "the event-time window is the live contract and is not widened"
        ),
        "arrival_rule": (
            "the entry tick must have been ingested within "
            "replay_quote_policy.max_ingest_delay_seconds"
        ),
        "opposite_action_rule": (
            "stop and target are mirrored about the decision close so both actions "
            "are evaluated at the same risk distance"
        ),
        "fail_closed": (
            "a decision whose label cannot be established is excluded and its "
            "counterfactual_unavailable_reason is recorded, never defaulted to zero"
        ),
        "not_measured": (
            "these rows are reconstructions against a tradable=false historical "
            "download; they may not be presented as executed fills"
        ),
    }


def build_dataset_manifest(
    rows: Sequence[Mapping[str, Any]],
    *,
    unavailable_reasons: Mapping[str, int],
    considered_count: int,
    quote_policy_sha256: str,
    cost_policy_sha256: str,
    tape_source: Mapping[str, object],
    as_of: datetime,
    repo_root: Path,
) -> dict[str, object]:
    """Summarise the population, its provenance, and what it could not cover."""

    moment = as_of.astimezone(UTC)
    ordered = sorted(rows, key=lambda r: (str(r.get("t")), str(r.get("decision_id"))))
    row_hashes = [_sha256(row) for row in ordered]
    times = [str(row["t"]) for row in ordered if row.get("t")]
    symbols = sorted({str(row.get("symbol")) for row in ordered})
    timeframes = sorted({str(row.get("timeframe")) for row in ordered})
    commit, dirty, diff_sha = _git_binding(repo_root)
    excluded = sum(int(v) for v in unavailable_reasons.values())
    manifest: dict[str, object] = {
        "contract": DATASET_CONTRACT,
        "label_contract": COUNTERFACTUAL_CONTRACT,
        "label_source": LABEL_SOURCE,
        "measured": False,
        "promotion_eligible": False,
        "execution_target": "shadow_decision_only",
        "broker_connected": False,
        "tradable": False,
        "generated_at": moment.isoformat(),
        "row_count": len(ordered),
        "considered_count": considered_count,
        "excluded_count": excluded,
        "coverage": (len(ordered) / considered_count) if considered_count else 0.0,
        "window_start": times[0] if times else None,
        "window_end": times[-1] if times else None,
        "symbols": symbols,
        "timeframes": timeframes,
        "dataset_sha256": _sha256(row_hashes),
        "row_hashes_sha256": _sha256(row_hashes),
        "replay_quote_policy_sha256": quote_policy_sha256,
        "replay_cost_policy_sha256": cost_policy_sha256,
        "tape_source": dict(tape_source),
        "counterfactual_unavailable_reasons": dict(unavailable_reasons),
        "git_commit": commit,
        "dirty_worktree": dirty,
        "working_tree_diff_sha256": diff_sha,
        "reproducible_from_commit_alone": (not dirty),
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def _write_readonly(path: Path, payload: bytes) -> str:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _ARTIFACT_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(payload).hexdigest()


def write_counterfactual_run(
    run_root: str | Path,
    *,
    rows: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    label_schema: Mapping[str, Any] | None = None,
) -> Path:
    """Publish one immutable counterfactual dataset run.

    Raises ``FileExistsError`` if the run id already exists: a reconstruction is
    never silently replaced, so a rerun that produced different rows is visible
    as a different run rather than as an edit to an old one.
    """

    ordered = sorted(rows, key=lambda r: (str(r.get("t")), str(r.get("decision_id"))))
    generated = datetime.fromisoformat(str(manifest["generated_at"]))
    dataset_hash = str(manifest["dataset_sha256"])
    run_id = f"{generated.strftime('%Y%m%dT%H%M%SZ')}-{dataset_hash[:12]}"
    root = Path(run_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable counterfactual run already exists: {run_dir}")

    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.staging-", dir=root))
    schema = dict(label_schema or build_label_schema())
    dataset_bytes = ("".join(_canonical(row) + "\n" for row in ordered)).encode()
    file_hashes = {
        "dataset.jsonl": _write_readonly(staging / "dataset.jsonl", dataset_bytes),
        "manifest.json": _write_readonly(
            staging / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        ),
        "label_schema.json": _write_readonly(
            staging / "label_schema.json",
            (json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        ),
    }
    completion = {
        "contract": COMPLETION_CONTRACT,
        "run_id": run_id,
        "row_count": len(ordered),
        "files": file_hashes,
    }
    _write_readonly(
        staging / "completion.json",
        (json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )
    os.rename(staging, run_dir)
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return run_dir


def load_counterfactual_run(run_dir: str | Path) -> dict[str, object]:
    """Read a published run back and verify it against its own hashes."""

    path = Path(run_dir).resolve()
    manifest = json.loads((path / "manifest.json").read_text())
    completion = json.loads((path / "completion.json").read_text())
    rows = [json.loads(line) for line in (path / "dataset.jsonl").read_text().splitlines() if line]
    dataset_bytes = (path / "dataset.jsonl").read_bytes()
    actual = hashlib.sha256(dataset_bytes).hexdigest()
    expected = str(completion["files"]["dataset.jsonl"])
    if actual != expected:
        raise ValueError(f"dataset.jsonl hash mismatch in {path}")
    recomputed = _sha256([_sha256(row) for row in rows])
    if recomputed != str(manifest["dataset_sha256"]):
        raise ValueError(f"dataset_sha256 does not reconcile in {path}")
    return {"manifest": manifest, "completion": completion, "rows": rows}


__all__ = [
    "COMPLETION_CONTRACT",
    "DATASET_CONTRACT",
    "LABEL_SCHEMA_CONTRACT",
    "build_dataset_manifest",
    "build_label_schema",
    "load_counterfactual_run",
    "write_counterfactual_run",
]
